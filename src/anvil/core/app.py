"""The application: parser assembly, dispatch, and the single exit path.

The structure worth copying from a real CLI:

* **Global flags live on the root parser, and subparsers inherit them.** They
  are parsed once, before dispatch, so ``anvil --quiet run ...`` and
  ``anvil run --quiet ...`` behave identically. This is what makes flag
  position irrelevant, which users expect even though POSIX says otherwise.
* **Dispatch is data, not a giant if/elif.** Each command module exposes a
  ``register(subparsers)`` function; the registry maps a name to a
  ``handler(ctx, args) -> int``.
* **Nothing calls ``sys.exit()``.** Handlers return an exit code or raise, and
  :func:`anvil.core.errors.run_cli` owns every transition to a process status.
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import actions as _actions
from . import completion, formatters, term
from . import config as _config
from . import io as _io
from . import logging_ as _logging
from . import types as _types
from .errors import (
    AnvilError,
    ExitCode,
    ExitRequested,
    UsageError,
    run_cli,
)

__all__ = [
    "Command",
    "Context",
    "Handler",
    "Registry",
    "build_parser",
    "main",
    "register_command",
]

PROG = "anvil"
VERSION = "1.0.0"

Handler = Callable[["Context", argparse.Namespace], Any]


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


@dataclass
class Context:
    """Everything a command handler is allowed to depend on.

    Passed explicitly rather than read from globals so that a command can be
    exercised in a test without a process, a terminal, or a real filesystem.
    """

    prog: str = PROG
    version: str = VERSION
    parser: argparse.ArgumentParser | None = None
    namespace: argparse.Namespace | None = None
    config: _config.Config = field(default_factory=_config.Config)
    renderer: _io.Renderer = field(default_factory=_io.Renderer)
    log: Any = None
    stdout: Any = sys.stdout
    stderr: Any = sys.stderr
    color: bool = True
    verbosity: int = 0
    dry_run: bool = False
    assume_yes: bool = False
    trace: bool = False
    invoked_as: list[str] = field(default_factory=list)
    #: Everything after the first bare ``--``, split off before parsing.
    passthrough: list[str] = field(default_factory=list)
    timings: list[tuple[str, float]] = field(default_factory=list)
    _started: float = field(default_factory=time.perf_counter)

    # -- output helpers ----------------------------------------------------- #

    @property
    def output_format(self) -> str:
        return self.renderer.format

    def out(self, text: str = "") -> None:
        """Write data to stdout."""
        self.stdout.write(text if text.endswith("\n") or not text else text + "\n")
        _flush(self.stdout)

    def emit(self, text: str = "") -> None:
        self.out(text)

    def err(self, text: str = "") -> None:
        """Write a diagnostic to stderr."""
        self.stderr.write(text if text.endswith("\n") or not text else text + "\n")
        _flush(self.stderr)

    def info(self, text: str) -> None:
        if self.verbosity >= 1:
            self.err(term.A.hint(f"{PROG}: {text}"))

    def debug(self, text: str) -> None:
        if self.log is not None:
            self.log.debug(text)

    def warn(self, text: str) -> None:
        if self.verbosity >= -1:
            self.err(term.A.warn(f"warning: {text}"))

    def progress(self, total: int | None = None, label: str = "") -> term.Progress:
        """A progress reporter that stays silent unless it is safe to draw."""
        interactive = self.color and self.stderr.isatty() and self.verbosity >= 0
        return term.Progress(total, label=label, stream=self.stderr,
                             enabled=interactive)

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Ask a yes/no question, honouring ``--yes`` and a non-TTY stdin."""
        if self.dry_run:
            return True
        return _actions.confirm(f"{PROG}: {prompt}", default=default,
                                assume_yes=self.assume_yes)

    def option(self, name: str, default: Any = None) -> Any:
        """Read a namespace value by name, tolerating an absent attribute."""
        ns = self.namespace
        return getattr(ns, name, default) if ns is not None else default

    def table(self, columns: Sequence[Any] | None = None, **kwargs: Any) -> term.Table:
        return term.Table(list(columns or []), **kwargs)

    def tabular(self, default: str = "table") -> str:
        """Resolve ``--output auto`` to *default* and return the format.

        ``auto`` means "the richest format this stream can show", which for a
        pipe would collapse a table into whitespace-only lines and throw the
        headers away. Commands whose entire purpose is tabular output call this
        so that ``anvil explain | less`` still shows a table instead of a
        column of bare values.
        """
        if self.renderer.requested == "auto":
            self.renderer.format = default
        return self.renderer.format

    def render(self, table: term.Table | str, target: Any = None) -> None:
        text = table if isinstance(table, str) else self.renderer.table(table)
        if target is None:
            self.out(text)
        else:
            _io.write_output(target, text + "\n")

    def finish(self, code: int = 0) -> int:
        """Record the elapsed time and hand back an exit code."""
        self.timings.append(("total", time.perf_counter() - self._started))
        if self.option("profile"):
            self.report_profile()
        return code


def _flush(stream: Any) -> None:
    try:
        stream.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass
class Command:
    """One registered command."""

    name: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    parser: argparse.ArgumentParser | None = None

    def __call__(self, ctx: Context, args: argparse.Namespace) -> Any:
        return self.handler(ctx, args)


class Registry:
    """An ordered command table.

    Order matters because it determines ``--help`` listing order, which should
    follow how often people reach for a command, not the alphabet.
    """

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._order: list[str] = []

    def register(self, name: str, handler: Handler, *,
                 aliases: Sequence[str] = (), tags: Sequence[str] = ()) -> Command:
        if name in self._commands:
            raise ValueError(f"command {name!r} is already registered")
        command = Command(name, handler, tuple(aliases), tuple(tags))
        self._commands[name] = command
        self._order.append(name)
        return command

    def get(self, name: str | None) -> Command | None:
        return self._commands.get(name) if name else None

    def names(self) -> list[str]:
        return list(self._order)

    def items(self) -> list[Command]:
        return [self._commands[name] for name in self._order]

    def __contains__(self, name: object) -> bool:
        return str(name) in self._commands

    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self):
        return iter(self.items())


REGISTRY = Registry()
"""The process-wide command table, populated by :mod:`anvil.commands`."""


def register_command(name: str, *, aliases: Sequence[str] = (),
                     tags: Sequence[str] = ()) -> Callable[[Handler], Handler]:
    """Decorator form of :meth:`Registry.register`.

    The wrapper records the decorated function's name so it can be introspected
    by ``anvil docs`` without an extra registry entry.
    """

    def decorate(handler: Handler) -> Handler:
        handler.__anvil_command__ = name  # type: ignore[attr-defined]
        handler.__anvil_aliases__ = tuple(aliases)  # type: ignore[attr-defined]
        handler.__anvil_tags__ = tuple(tags)  # type: ignore[attr-defined]
        REGISTRY.register(name, handler, aliases=aliases, tags=tags)
        return handler

    return decorate


# --------------------------------------------------------------------------- #
# Parser assembly
# --------------------------------------------------------------------------- #


def _global_arguments(parser: argparse.ArgumentParser) -> None:
    """Flags accepted both before and after the command name."""
    _logging.add_logging_arguments(parser)

    config = parser.add_argument_group("Configuration")
    config.add_argument(
        "--config", metavar="PATH", default=None,
        help="read this config file instead of searching for one",
        anvil_env="ANVIL_CONFIG",
    )
    config.add_argument(
        "--set", action="append", dest="set_", metavar="KEY=VALUE", default=None,
        help="override one config key; repeatable",
    )

    group = parser.add_argument_group("Output")
    group.add_argument(
        "-o", "--output", type=_types.output_format, default="auto",
        metavar="FORMAT", help="output format", anvil_env="ANVIL_OUTPUT",
    )
    group.add_argument(
        "--no-color", action="store_true", default=False,
        help="disable colour and unicode decorations",
    )
    group.add_argument(
        "--table-style", type=_types.table_style, default="unicode",
        help="box-drawing style for tables",
    )
    group.add_argument(
        "--yes", "-y", action="store_true", default=False,
        help="assume yes for every confirmation",
    )
    group.add_argument(
        "--dry-run", action="store_true", default=False,
        help="report what would happen without doing it",
    )


def _parse_preamble(argv: Sequence[str]) -> dict[str, Any]:
    """Extract ``--config``, ``--set`` and friends before the real parse.

    argparse cannot do this itself: the config file decides some defaults, and
    defaults are resolved during parsing. A tiny private parser reading the same
    tokens with ``parse_known_args`` is the standard, honest way to bootstrap.
    """
    boot = argparse.ArgumentParser(add_help=False, prog=PROG)
    boot.add_argument("--config", metavar="PATH", default=None)
    boot.add_argument("--set", action="append", dest="set_", metavar="KEY=VALUE",
                      default=None)
    boot.add_argument("--no-color", action="store_true", default=False)
    boot.add_argument("--profile", action="store_true", default=False)
    boot.add_argument("--trace", action="store_true", default=False)
    known, _unknown = boot.parse_known_args(list(argv))
    return vars(known)


def build_parser(
    prog: str = PROG,
    *,
    registry: Registry | None = None,
    description: str | None = None,
    version: str = VERSION,
) -> argparse.ArgumentParser:
    """Construct the whole parser tree.

    Called by ``main`` *and* by completion generation, which is why it must be
    side-effect free: no reads, no writes, no environment mutation.
    """
    registry = registry or REGISTRY
    from .. import commands as _commands

    # Commands register themselves into REGISTRY at import time, so the modules
    # must be imported *before* the registry is read. Getting this order wrong
    # yields a CLI whose --help lists no commands and whose every command is
    # "unknown" -- with no error anywhere.
    _commands.load()

    parser = _errors_parser(
        prog=prog,
        description=description or _commands.DESCRIPTION,
        epilog=_commands.EPILOG,
        formatter_class=formatters.AnvilHelpFormatter,
    )
    parser.add_examples(*_commands.EXAMPLES)
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"{prog} {version} (python {sys.version.split()[0]}, "
                f"argparse from the stdlib)",
        help="show the version and exit",
    )
    _global_arguments(parser)

    subparsers = parser.add_subparsers(
        dest="command", metavar="COMMAND", title="Commands",
        description="Every command documents itself with --help.",
    )
    subparsers.required = True
    for command in registry.items():
        command.parser = _commands.register(subparsers, command, prog=prog)
    _commands.decorate(parser)
    return parser


def _errors_parser(**kwargs: Any) -> Any:
    from .errors import AnvilArgumentParser

    return AnvilArgumentParser(**kwargs)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def _bootstrap(argv: Sequence[str] | None = None,
               holder: dict[str, Any] | None = None) -> tuple[
                   argparse.ArgumentParser, Context, argparse.Namespace]:
    """Parse, resolve configuration, and build the :class:`Context`.

    *holder* is published into as soon as the parser exists -- before parsing --
    so that a parse failure can still print usage and a suggestion. Without
    that, every "did you mean" for a typo would be lost.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    boot = _parse_preamble(argv)

    term.enable_windows_vt()
    color = not boot.get("no_color") and not os.environ.get("NO_COLOR")
    term.set_color(bool(color))

    loaded = _config.Config.load(path=boot.get("config"),
                                 overrides=boot.get("set_"))
    resolved = loaded.resolve()

    parser = build_parser()
    if holder is not None:
        holder["parser"] = parser
    namespace = parser.parse_args(argv)

    verbosity = int(namespace.verbosity or 0)
    if resolved.get("verbosity") is not None and "--verbose" not in argv:
        verbosity = int(resolved["verbosity"])
    log = _logging.setup_logging(
        verbosity=verbosity,
        level=getattr(namespace, "log_level", None),
        log_format=getattr(namespace, "log_format", "auto") or "auto",
        log_file=getattr(namespace, "log_file", None),
        color=color,
    )

    ctx = Context(
        prog=parser.prog,
        parser=parser,
        namespace=namespace,
        config=loaded,
        renderer=_io.Renderer(
            namespace.output or "auto",
            color=color,
            table_style=namespace.table_style,
        ),
        log=log,
        color=bool(color),
        verbosity=verbosity,
        dry_run=bool(namespace.dry_run),
        assume_yes=bool(namespace.yes),
        trace=bool(namespace.trace or boot.get("trace")),
        invoked_as=[namespace.command] if namespace.command else [],
    )

    # ``nargs='+'`` greedily consumes everything, including tokens after a
    # bare ``--``, so a trailing REMAINDER positional after it never receives
    # anything. Splitting argv once, up front, is the only reliable way to
    # honour ``--`` and it is what docker, git and kubectl all do.
    namespace.passthrough = _split_passthrough(argv)
    ctx.passthrough = namespace.passthrough

    # Environment fallbacks and string-default conversion are post-parse
    # because both need the full set of declared types and the resolved config.
    _actions.apply_env_fallbacks(parser, namespace)
    _actions.convert_string_defaults(parser, namespace)
    _actions.apply_env_fallbacks(parser, namespace)

    if ctx.trace:
        _enable_tracebacks()
    return parser, ctx, namespace


def _split_passthrough(argv: Sequence[str]) -> list[str]:
    """Return everything after the first bare ``--`` in *argv*."""
    try:
        index = list(argv).index("--")
    except ValueError:
        return []
    return list(argv)[index + 1:]


def _enable_tracebacks() -> None:
    """Restore the default excepthook so bugs are readable with --trace."""
    sys.excepthook = sys.__excepthook__


def _dispatch(ctx: Context, namespace: argparse.Namespace) -> int:
    """Invoke the handler the parser selected.

    ``set_defaults(func=...)`` wins over the registry: a command whose parser
    is a *parent* of subcommands (``anvil config list`` vs ``anvil config get``)
    has one registry entry but many leaf handlers, and argparse copies the
    leaf's defaults into the shared namespace for exactly this reason. Falling
    back to the registry only matters for a command that forgot to set ``func``.
    """
    handler = getattr(namespace, "func", None)
    if handler is None:
        command = REGISTRY.get(namespace.command)
        if command is None:
            raise UsageError(
                f"unknown command {namespace.command!r}",
                hint=f"run '{ctx.prog} --help' to list the available commands",
            )
        handler = command.handler
    result = handler(ctx, namespace)
    return 0 if result is None else int(result)


def _install_signal_handlers() -> None:
    """Turn SIGINT/SIGTERM into exceptions so cleanup can run.

    Without this, a ``--output`` file can be left half-written by a Ctrl-C.
    The handler raises, unwinds the command's ``finally`` blocks, and
    :func:`run_cli` maps the exception to 130 -- the conventional status.
    """

    def handler(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        raise KeyboardInterrupt(f"{name} received")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            # Not the main thread, or the platform lacks the signal.
            pass


def main(argv: Sequence[str] | None = None, *,
         traceback_on_error: bool = False) -> int:
    """The console-script entry point. Returns the process exit code."""
    _install_signal_handlers()
    state: dict[str, Any] = {"profiler": None, "parser": None}

    def entry(_ignored: Sequence[str] | None = None) -> int:
        # ``run_cli`` passes argv through for callers that want it; this entry
        # point already captured it, so it takes the argument and ignores it.
        parser, ctx, namespace = _bootstrap(argv, holder=state)
        state["parser"] = parser
        if getattr(namespace, "profile", False):
            state["profiler"] = cProfile.Profile()
            state["profiler"].enable()
        try:
            return _dispatch(ctx, namespace)
        finally:
            profiler = state["profiler"]
            if profiler is not None:
                profiler.disable()
                report_timings(profiler)

    try:
        code = run_cli(entry, argv, parser=lambda: state.get("parser"),
                       traceback_on_error=traceback_on_error)
    finally:
        _flush(sys.stdout)
        _flush(sys.stderr)
    return code


# --------------------------------------------------------------------------- #
# Profiling
# --------------------------------------------------------------------------- #


def report_timings(profiler: cProfile.Profile, top: int = 18,
                   stream: Any = None) -> None:
    """Print a cumulative-time profile table to stderr."""
    out = stream if stream is not None else sys.stderr
    stats = pstats.Stats(profiler, stream=out).strip_dirs().sort_stats("cumulative")
    out.write(term.A.head("\nprofile (cumulative time)\n"))
    stats.print_stats(top)


def startup_profile(stream: Any = None) -> cProfile.Profile:
    """Start profiling *imports*, for the performance section of ``anvil doctor``."""
    profiler = cProfile.Profile()
    profiler.enable()
    return profiler


def stop_profile(profiler: cProfile.Profile, stream: Any = None) -> None:
    profiler.disable()
    report_timings(profiler, stream=stream)


def exit_with(code: int) -> None:
    """Raise :class:`ExitRequested` so ``main`` owns the status transition."""
    raise ExitRequested(code)


def require(condition: bool, message: str, *, hint: str | None = None) -> None:
    """Assert a precondition as a user-facing error, not an ``AssertionError``."""
    if not condition:
        raise AnvilError(message, hint=hint, exit_code=ExitCode.USAGE)


def command_names() -> list[str]:
    from . import commands as _commands

    _commands.load()
    return REGISTRY.names()


def completion_script(shell: str, prog: str = PROG) -> str:
    return completion.render(shell, prog, module="anvil.completion_probe")


def usage() -> str:
    return build_parser().format_usage()


def describe() -> str:
    from ..commands import DESCRIPTION

    return DESCRIPTION


def show_version(prog: str = PROG, version: str = VERSION) -> str:
    return (f"{prog} {version}\n"
            f"  python   {sys.version.split()[0]}\n"
            f"  platform {sys.platform}\n"
            f"  commands {len(REGISTRY)}\n")
