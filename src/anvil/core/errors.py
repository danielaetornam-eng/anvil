"""Errors, exit codes, suggestions, and a parser that raises instead of exiting.

The central idea here is that ``argparse`` calling ``sys.exit()`` from inside
``parse_args`` is convenient for scripts and hostile to everything else: it
truncates the stack trace, makes the CLI untestable without subprocesses, and
forces a second error-handling path for command failures.

So :class:`AnvilArgumentParser` is configured with ``exit_on_error=False`` and
overrides ``error()``/``exit()`` to *raise*. A single ``try`` block in
``main()`` then owns the entire exit-path: it can render a suggestion, choose
an exit code from a taxonomy, and suppress the traceback for expected failures
while still showing one for genuine bugs.
"""

from __future__ import annotations

import argparse
import difflib
import errno
import sys
import weakref
from collections.abc import Iterable, Sequence
from enum import IntEnum
from typing import IO, Any, NoReturn

from . import term

__all__ = [
    "AnvilError",
    "ConfigError",
    "ExitCode",
    "ExitRequested",
    "InputError",
    "NotFoundError",
    "OutputError",
    "ParseFailure",
    "PermissionError_",
    "StateError",
    "TimeoutError_",
    "UsageError",
    "best_match",
    "did_you_mean",
    "run_cli",
    "similar",
]


class ExitCode(IntEnum):
    """A ``sysexits.h``-shaped exit-code contract.

    Exit codes are part of a CLI's public API: a shell script branches on them.
    Collapsing everything to ``1`` destroys information a caller needs, and
    using ``2`` for everything wastes the one code POSIX reserves for *usage*
    errors -- the one a human can actually fix.
    """

    OK = 0
    """Everything worked."""

    FAILURE = 1
    """Generic, unclassified failure."""

    USAGE = 2
    """Bad command line. Reserved for argparse; never reused."""

    DATA = 65
    """Malformed input data (invalid JSON, bad CSV, unparsable document)."""

    NO_INPUT = 66
    """An input file could not be opened."""

    NO_USER = 67
    """The addressed user does not exist."""

    NO_HOST = 68
    """The host does not respond."""

    UNAVAILABLE = 69
    """A service or required resource is unavailable."""

    SOFTWARE = 70
    """An internal bug. Never used for user error."""

    OS_FILE = 71
    """An operating-system level failure."""

    OS_FILE2 = 72
    """A critical OS error (out of memory, disk full)."""

    TEMPFAIL = 75
    """A temporary condition; retrying may succeed."""

    PROTOCOL = 76
    """A remote error in a pipeline."""

    NOPERM = 77
    """Permission denied."""

    CONFIG = 78
    """A configuration error."""

    PROTOCOL2 = 79
    """An unknown protocol or a plugin that failed to load."""

    IOERR = 74
    """An I/O error while writing the result."""

    INTERRUPTED = 130
    """SIGINT (128 + 2), the conventional Ctrl-C status."""

    TERMINATED = 143
    """SIGTERM (128 + 15)."""


class AnvilError(Exception):
    """Base class for every failure this CLI raises on purpose.

    Carries an exit code and an optional ``hint``. A failure the user caused
    gets exit 65-78 and a hint; a bug gets :attr:`ExitCode.SOFTWARE` and a
    traceback.
    """

    exit_code: ExitCode = ExitCode.FAILURE
    #: Short "try this instead" guidance rendered under the message.
    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None,
                 exit_code: ExitCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint
        if exit_code is not None:
            self.exit_code = exit_code

    def render(self) -> str:
        """The full stderr block for this error."""
        body = f"{term.A.err('error')}: {self.message}"
        if self.hint:
            body += f"\n{term.A.hint('hint')}: {self.hint}"
        return body


class UsageError(AnvilError):
    """The command line itself is wrong."""

    exit_code = ExitCode.USAGE


class InputError(AnvilError):
    """Input exists but cannot be parsed or makes no sense."""

    exit_code = ExitCode.DATA


class NotFoundError(AnvilError):
    """A required thing does not exist."""

    exit_code = ExitCode.NO_INPUT


class PermissionError_(AnvilError):
    """Access was refused by the filesystem or the OS."""

    exit_code = ExitCode.NOPERM


class OutputError(AnvilError):
    """Writing the result failed."""

    exit_code = ExitCode.IOERR


class StateError(AnvilError):
    """The operation is valid but the current state forbids it."""

    exit_code = ExitCode.FAILURE


class ConfigError(AnvilError):
    """Configuration is missing, unreadable, or internally inconsistent."""

    exit_code = ExitCode.CONFIG


class TimeoutError_(AnvilError):
    """A deadline elapsed."""

    exit_code = ExitCode.TEMPFAIL


class ExitRequested(Exception):
    """Raised in place of ``SystemExit`` for intentional termination.

    ``--help`` and ``--version`` both want to print and stop with status 0.
    Raising keeps them inside the same ``try`` block as everything else.
    """

    def __init__(self, status: int = 0, message: str | None = None) -> None:
        super().__init__(message or f"exit {status}")
        self.status = status
        self.message = message


class ParseFailure(AnvilError):
    """Raised when parsing fails, carrying the offending parser.

    Holds the parser so the handler can print usage and highlight the correct
    subcommand's help rather than the root's.
    """

    exit_code = ExitCode.USAGE

    def __init__(self, message: str, parser: argparse.ArgumentParser | None = None,
                 **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.parser = parser


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


def _damerau_ratio(left: str, right: str) -> float:
    """Similarity that counts an adjacent transposition as *one* edit.

    ``difflib.SequenceMatcher`` scores ``"teh"`` against ``"text"`` at 0.29,
    because the two share only the leading ``t`` -- yet transposing two
    adjacent letters is *the* most common typo there is. A Damerau-Levenshtein
    distance treats it as a single edit, which is what a user means.
    """
    if not left or not right:
        return 0.0
    # Restrict to short strings: the full O(n*m) table is wasted on long tokens
    # that are never going to be a typo of each other.
    if len(left) > 32 or len(right) > 32:
        return 0.0
    rows, cols = len(left), len(right)
    previous_previous: list[int] | None = None
    previous = list(range(cols + 1))
    for i in range(1, rows + 1):
        current = [i] + [0] * cols
        for j in range(1, cols + 1):
            cost = 0 if left[i - 1] == right[j - 1] else 1
            current[j] = min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + cost,   # substitution
            )
            if (i > 1 and j > 1
                    and left[i - 1] == right[j - 2]
                    and left[i - 2] == right[j - 1]):
                # Adjacent transposition: one edit, not two.
                current[j] = min(current[j],
                                 (previous_previous[j - 2] if previous_previous else i - 2))
        previous_previous, previous = previous, current
    return 1.0 - previous[cols] / max(rows, cols)


def _similarity(left: str, right: str) -> float:
    """Best of the character-overlap ratio and the transposition-aware one."""
    overlap = difflib.SequenceMatcher(None, left, right).ratio()
    return max(overlap, _damerau_ratio(left, right))


def similar(word: str, options: Iterable[str], *, limit: int = 3,
            cutoff: float = 0.6) -> list[str]:
    """Rank *options* by similarity to *word*.

    Matching runs on lowercased, ``-``/``_``-normalised text so that
    ``--dryrun`` suggests ``--dry-run`` and ``colour`` suggests ``--color``.
    A prefix bonus matters as much as the ratio: when a user has typed the
    first few characters of a flag, the intended candidate nearly always
    *starts* with them, even though its overall edit distance is not minimal.
    """
    pool = [o for o in options if o]
    if not pool or not word:
        return []
    needle = word.lower().replace("_", "-").lstrip("-")
    folded = {o.lower().replace("_", "-"): o for o in pool}
    typed = needle[:3]

    def score(key: str) -> tuple[float, int, str]:
        ratio = _similarity(needle, key)
        bonus = 0.3 if typed and key.startswith(typed) else 0.0
        return (-(ratio + bonus), len(key), key)

    out: list[str] = []
    for key in sorted(folded, key=score):
        if _similarity(needle, key) >= cutoff or (typed and key.startswith(typed)):
            out.append(folded[key])
        if len(out) >= limit:
            break
    return out


def best_match(word: str, options: Iterable[str]) -> str | None:
    """The single best suggestion, or ``None``."""
    matches = similar(word, options, limit=1)
    return matches[0] if matches else None


def did_you_mean(word: str, options: Iterable[str], *, limit: int = 3) -> str | None:
    """A ready-to-print ``Did you mean ...?`` clause, or ``None``."""
    matches = similar(word, options, limit=limit)
    if not matches:
        return None
    if len(matches) == 1:
        return f"did you mean {term.A.info(matches[0])}?"
    listed = ", ".join(term.A.info(m) for m in matches)
    return f"did you mean one of {listed}?"


def _format_option_list(options: Iterable[str], limit: int = 18) -> str:
    options = list(options)
    if len(options) <= limit:
        return " ".join(sorted(options))
    head = " ".join(sorted(options)[:limit])
    return f"{head} ... (+{len(options) - limit} more)"


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #


#: ``anvil_*`` keyword arguments that are metadata for the help renderer
#: rather than things ``argparse`` knows how to build an action with.
ACTION_METADATA = (
    "anvil_env", "anvil_since", "anvil_deprecated", "anvil_replacement",
)


def _pop_action_metadata(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: kwargs.pop(k) for k in ACTION_METADATA if k in kwargs}


def _apply_action_metadata(action: argparse.Action,
                           metadata: dict[str, Any]) -> argparse.Action:
    for key, value in metadata.items():
        setattr(action, key, value)
    return action


class _AnvilArgumentGroup(argparse._ArgumentGroup):
    """An argument group that also accepts ``anvil_*`` metadata.

    Two argparse details force this class to exist:

    1. ``_ArgumentGroup.add_argument`` calls ``_ActionsContainer.add_argument``
       directly, so it never delegates to the container parser -- an override
       on the parser alone would do nothing for every *grouped* option.
    2. Every ``_ActionsContainer`` builds its **own** ``_registries`` dict in
       ``__init__``, containing only the stock actions. So ``action="store"``
       resolves inside a group but a registered custom action does not, and the
       failure is "unknown action 'comma_list'" in a place that has nothing to
       do with the group.
    """

    def add_argument(self, *args: Any, **kwargs: Any):
        metadata = _pop_action_metadata(kwargs)
        kwargs = _resolve_custom_action(self, kwargs)
        return _apply_action_metadata(
            super().add_argument(*args, **kwargs), metadata
        )


class _AnvilMutuallyExclusiveGroup(argparse._MutuallyExclusiveGroup):
    """Same two treatments for mutually exclusive groups."""

    def add_argument(self, *args: Any, **kwargs: Any):
        metadata = _pop_action_metadata(kwargs)
        kwargs = _resolve_custom_action(self, kwargs)
        return _apply_action_metadata(
            super().add_argument(*args, **kwargs), metadata
        )


def _resolve_custom_action(group: argparse._ActionsContainer,
                           kwargs: dict[str, Any]) -> dict[str, Any]:
    """Look a named action up in the *container parser's* registry.

    An unknown name is left as the original string so argparse still raises its
    own ``unknown action`` error, which names the class that is missing.
    """
    action = kwargs.get("action")
    container = getattr(group, "_container", None)
    if isinstance(action, str) and container is not None:
        resolved = container._registry_get("action", action)
        if resolved is not None:
            kwargs = dict(kwargs)
            kwargs["action"] = resolved
    return kwargs


#: Flags that must be accepted both before and after the command name.
GLOBAL_FLAGS = (
    "-o", "--output",
    "--no-color",
    "--table-style",
    "-y", "--yes",
    "--dry-run",
    "-v", "--verbose", "-q", "--quiet",
    "--log-level", "--log-format", "--log-file",
    "--show-env", "--trace", "--profile",
    "--config", "--set",
)


def _global_option_specs(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """Describe each global option as a re-buildable ``add_argument`` spec."""
    specs: list[dict[str, Any]] = []
    for option in parser._actions:
        if not any(o in GLOBAL_FLAGS for o in option.option_strings):
            continue
        if option.help is argparse.SUPPRESS:
            continue
        kwargs: dict[str, Any] = {
            "help": argparse.SUPPRESS,
            "dest": option.dest,
            # ``Action`` instances do not keep the action class they were built
            # from -- ``action=`` is consumed by add_argument and never stored.
            # Reading it back as an attribute yields None, which silently
            # degrades every flag into a value-taking ``store`` option. The
            # class itself is just ``type(option)``.
            "action": type(option),
            # SUPPRESS is essential, not cosmetic. ``_SubParsersAction`` parses
            # the child's argv into a *fresh* namespace and then copies every
            # attribute over the parent's -- including the child's defaults.
            # A copy with a real default therefore silently resets whatever
            # the root parser already decided, so `anvil --dry-run run x`
            # would run x for real. With SUPPRESS the child contributes the
            # attribute only when the flag is actually given.
            "default": argparse.SUPPRESS,
        }
        if option.nargs is not None and option.nargs != 0:
            # ``nargs=0`` is set by the flag action classes themselves, and
            # forwarding it trips _StoreAction's "nargs must be != 0" check.
            kwargs["nargs"] = option.nargs
        if option.type is not None:
            kwargs["type"] = option.type
        if option.choices is not None:
            kwargs["choices"] = option.choices
        if option.const is not None and option.nargs == "?":
            kwargs["const"] = option.const
        if option.metavar is not None:
            kwargs["metavar"] = option.metavar
        specs.append({"flags": list(option.option_strings), "kwargs": kwargs})
    return specs


def _build_global_parents(prog: str,
                          specs: list[dict[str, Any]]) -> AnvilArgumentParser:
    """Build a *fresh* parent parser holding hidden copies of the global options."""
    shared = AnvilArgumentParser(prog=prog, add_help=False)
    for spec in specs:
        shared.add_argument(*spec["flags"], **spec["kwargs"])
    return shared


def _inject_global_parents(action: argparse._SubParsersAction,
                           specs: list[dict[str, Any]]) -> None:
    """Give every child parser its own hidden copy of the global options.

    ``parents=`` is the supported way to share arguments, but it shares the
    *same action objects*, and ``conflict_handler='resolve'`` mutates them
    in place -- ``_handle_conflict_resolve`` calls
    ``action.option_strings.remove(option_string)`` on the action it displaces.
    Sharing one parent across all subcommands therefore lets the first command
    that redefines a global flag silently strip the option string from every
    other command's copy, turning it into an unrecognised positional.

    So a new parent is built per ``add_parser`` call. It costs a handful of
    ``add_argument`` calls per subcommand and is the only safe arrangement.
    """
    original = action.add_parser

    def add_parser(name: str, **kwargs: Any):
        parents = list(kwargs.pop("parents", None) or [])
        parents.append(_build_global_parents(kwargs.get("prog", name), specs))
        kwargs["parents"] = parents
        child = original(name, **kwargs)
        child._global_specs = specs
        return child

    action.add_parser = add_parser  # type: ignore[method-assign]


class AnvilArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that raises, colours, suggests, and self-documents.

    Five changes to stock behaviour, each deliberate:

    1. ``error()`` raises :class:`ParseFailure` instead of calling ``exit(2)``.
    2. ``exit()`` raises :class:`ExitRequested`, so ``--help`` is not a
       ``SystemExit`` that unwinds the stack.
    3. ``formatter_class`` defaults to :class:`AnvilHelpFormatter`, which means
       every subparser inherits it -- argparse does *not* propagate the parent's
       formatter to ``add_parser`` children, so relying on inheritance silently
       gives you stock help for every command.
    4. ``-h``/``--help`` is replaced with :class:`ShowHelpAction` so an examples
       block can be printed without polluting ``format_help()``.
    5. 3.14's native suggestion and colour machinery is switched off so the
       messages look identical on every supported version.

    ``exit_on_error=False`` is what makes ``ArgumentError`` reachable at all.
    """

    #: Examples printed by ``--help``; set via :meth:`add_examples`.
    examples: tuple[str, ...] = ()

    def __init__(self, *args: Any, add_help: bool = True, **kwargs: Any) -> None:
        from . import actions as _actions
        from .formatters import AnvilHelpFormatter, ShowHelpAction

        kwargs.setdefault("formatter_class", AnvilHelpFormatter)
        # ``resolve`` rather than ``error``: every subparser inherits the
        # global options, so a command that wants the same flag name for a
        # narrower meaning must be able to win instead of aborting the build
        # with "conflicting option string". Genuine accidental duplicates are
        # surfaced by ``anvil doctor``, which reports them as a warning.
        kwargs.setdefault("conflict_handler", "resolve")
        super().__init__(*args, add_help=False, exit_on_error=False, **kwargs)
        _actions.register_all(self)
        _PARSERS.add(self)
        self.examples = ()
        self.show_suggestion = True
        self.suggestion: str | None = None
        self._colors = term.supports_color(sys.stderr)
        if add_help:
            prefix = "-" if "-" in self.prefix_chars else self.prefix_chars[0]
            self.add_argument(
                prefix + "h",
                prefix * 2 + "help",
                action=ShowHelpAction,
                default=argparse.SUPPRESS,
                help="show this help and exit",
            )
        # 3.14 ships its own suggestion and colour machinery; both are turned
        # off so that messages look identical on every supported version.
        if hasattr(self, "suggest_on_error"):
            self.suggest_on_error = False
        if hasattr(self, "color"):
            self.color = False

    def add_examples(self, *examples: str) -> AnvilArgumentParser:
        """Attach worked examples to ``--help``; returns ``self`` for chaining."""
        self.examples = tuple(examples)
        return self

    def add_argument(self, *args: Any, **kwargs: Any):
        """Strip ``anvil_*`` metadata, then defer to argparse.

        ``add_argument`` forwards every unknown keyword straight to
        ``Action.__init__``, which rejects anything it does not recognise. That
        makes it impossible to attach your own metadata to an option without
        intercepting the call -- which is what lets the help renderer print
        ``$ANVIL_TOKEN`` and ``since 1.2`` without every command having to
        hand-write that into its own ``help=`` string.
        """
        metadata = _pop_action_metadata(kwargs)
        return _apply_action_metadata(super().add_argument(*args, **kwargs), metadata)

    def _parse_known_args(self, arg_strings, namespace, *args, **kwargs):
        """Tag parse failures with *this* parser.

        ``ArgumentError`` does not know which parser raised it, so an
        "unrecognized arguments" error from ``anvil table`` would otherwise be
        answered with suggestions drawn from the *root* parser's flags -- and
        ``--colums`` would get no hint at all, because ``--columns`` belongs to
        the subparser. One attribute on the exception is enough to fix it.

        The signature forwards ``*args``/``**kwargs`` because 3.14 added an
        ``intermixed`` parameter here.
        """
        try:
            return super()._parse_known_args(arg_strings, namespace, *args, **kwargs)
        except argparse.ArgumentError as exc:
            exc.anvil_parser = self
            raise

    def add_argument_group(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        group = _AnvilArgumentGroup(self, *args, **kwargs)
        self._action_groups.append(group)
        return group

    def add_mutually_exclusive_group(self, *, required: bool = False):  # type: ignore[override]
        group = _AnvilMutuallyExclusiveGroup(self, required=required)
        self._mutually_exclusive_groups.append(group)
        return group

    #: Global option specs, propagated so nested subparsers inherit them too.
    _global_specs: list[dict[str, Any]] = []

    def add_subparsers(self, **kwargs: Any):  # type: ignore[override]
        """``add_subparsers`` that defaults ``parser_class`` to this class.

        Two defaults are injected, and both are necessary rather than tidy:

        * ``parser_class`` -- otherwise ``add_parser`` builds a stock
          ``ArgumentParser`` and every subcommand silently loses the formatter,
          the custom help action, the suggestion machinery, and the registered
          custom actions.
        * ``parents`` -- a hidden copy of the global options, so that
          ``anvil json format --output json`` works as well as
          ``anvil --output json json format``. Users expect flag position to be
          irrelevant; POSIX says otherwise, and users win. The copies carry
          ``help=SUPPRESS`` so the global flags are documented exactly once, on
          the root.

        The specs are passed down to the child parser, which is what makes the
        globals available at *every* depth rather than only one.
        """
        kwargs.setdefault("parser_class", type(self))
        action = super().add_subparsers(**kwargs)
        specs = self._global_specs or _global_option_specs(self)
        if specs:
            self._global_specs = specs
            _inject_global_parents(action, specs)
        return action

    # -- suggestions -------------------------------------------------------- #

    def attach_suggestion(self, message: str | None) -> None:
        """Compute and stash a "did you mean" clause for the current error."""
        if not message or not self.show_suggestion:
            return
        self.suggestion = self._suggest(message)

    def _suggest(self, message: str) -> str | None:
            if "invalid choice" in message:
                # argparse formats this as: invalid choice: 'x' (choose from ...)
                quoted = _first_quoted(message)
                choices = _choices_from(message)
                if quoted and choices:
                    return did_you_mean(quoted, choices)
                return None
            if "unrecognized arguments" in message:
                extras = message.split(":", 1)[-1]
                words = [w for w in extras.split() if w.startswith("-")]
                here = self._option_strings()
                for word in words:
                    guess = did_you_mean(word, here)
                    if guess:
                        return f"{guess} ({_format_option_list(here)})"
                    # The offending flag may belong to a *sub*command: argparse
                    # reports leftovers at the root, so the root's own options are
                    # the wrong place to look. Search every parser we have built
                    # and name the command that owns the match.
                    owner = _owner_elsewhere(self, word)
                    if owner:
                        command, flag = owner
                        return (f"did you mean {term.A.info(flag)}? "
                                f"it belongs to {term.A.info(command)}")
                return None
            if "expected one argument" in message or "expected at least" in message:
                return f"run {term.A.info(f'{self.prog} --help')} to see the usage"
            return None

    def _option_strings(self) -> set[str]:
        found: set[str] = set()
        for action in self._actions:
            found.update(action.option_strings)
        return found

    # -- terminals ---------------------------------------------------------- #

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        self.attach_suggestion(message)
        raise ParseFailure(message, parser=self)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:  # type: ignore[override]
        raise ExitRequested(status, message)


#: Every live :class:`AnvilArgumentParser`, held weakly so the registry does
#: not keep parsers alive in tests or long-lived embeddings. A plain list would
#: leak a full parser tree per ``build_parser()`` call.
_PARSERS: weakref.WeakSet[Any] = weakref.WeakSet()


def _owner_elsewhere(source: argparse.ArgumentParser,
                     word: str) -> tuple[str, str] | None:
    """Find a subcommand that owns a flag close to *word*.

    ``anvil table --colums x`` reports the leftover at the *root* parser, so the
    root's own option list can never suggest ``--columns``. This walks every
    parser the CLI has built and reports both the flag and the command that
    owns it, which turns a dead end into a pointer.
    """
    for parser in list(_PARSERS):
        if parser is source:
            continue
        options = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        matches = similar(word, options, limit=1)
        if matches:
            return parser.prog, matches[0]
    return None


def _first_quoted(message: str) -> str | None:
    start = message.find("'")
    end = message.find("'", start + 1)
    if start >= 0 and end > start:
        return message[start + 1: end]
    return None


def _choices_from(message: str) -> list[str]:
    marker = "choose from "
    index = message.find(marker)
    if index < 0:
        return []
    tail = message[index + len(marker):].strip()
    if tail.endswith(")"):
        tail = tail[:-1]
    return [c.strip().strip("'\"") for c in tail.split(",") if c.strip()]


# --------------------------------------------------------------------------- #
# OS error translation
# --------------------------------------------------------------------------- #

_OS_ERRORS: dict[int, tuple[type[AnvilError], ExitCode, str]] = {
    errno.ENOENT: (NotFoundError, ExitCode.NO_INPUT, "check the path"),
    errno.EACCES: (PermissionError_, ExitCode.NOPERM, "check the permissions"),
    errno.EPERM: (PermissionError_, ExitCode.NOPERM, "check the permissions"),
    errno.EISDIR: (InputError, ExitCode.DATA, "that is a directory, not a file"),
    errno.ENOTDIR: (InputError, ExitCode.DATA, "a path component is not a directory"),
    errno.ENOSPC: (OutputError, ExitCode.OS_FILE2, "the disk is full"),
    errno.EMFILE: (OutputError, ExitCode.OS_FILE, "too many open files"),
    errno.ENAMETOOLONG: (UsageError, ExitCode.USAGE, "the path is too long"),
    errno.ECONNREFUSED: (AnvilError, ExitCode.UNAVAILABLE, "nothing is listening there"),
    errno.EHOSTUNREACH: (AnvilError, ExitCode.NO_HOST, "no route to that host"),
    errno.ENETUNREACH: (AnvilError, ExitCode.NO_HOST, "the network is unreachable"),
}


def translate_os_error(exc: OSError, *, context: str = "") -> AnvilError:
    """Turn an ``OSError`` into a typed, user-facing :class:`AnvilError`.

    Raw ``[Errno 2] No such file or directory: 'x'`` tells a user nothing
    actionable. This maps the errno to an exit code and a hint, and mentions
    the operation that failed.
    """
    errno_code = exc.errno
    cls, code, hint = _OS_ERRORS.get(
        errno_code, (AnvilError, ExitCode.FAILURE, "")
    )
    message = exc.strerror or str(exc)
    prefix = f"{context}: " if context else ""
    return cls(
        f"{prefix}{message}",
        hint=hint or None,
        exit_code=code,
    )


# --------------------------------------------------------------------------- #
# The single exit path
# --------------------------------------------------------------------------- #


def run_cli(
    handler: Any,
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    parser: Any = None,
    traceback_on_error: bool = False,
) -> int:
    """Invoke *handler* and own every exit path.

    This is the only place in the program that turns an exception into an exit
    status. Commands therefore never call ``sys.exit()`` and never print their
    own "error:" line; they just raise.

    ``parser`` lets the handler's parse failures print the *relevant* usage
    line -- the subcommand's, not the root's. It may be a parser or a callable
    returning one; the callable form matters because the parse happens *inside*
    the handler, after the argument has already been evaluated.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    resolve = parser if callable(parser) else (lambda: parser)
    try:
        result = handler(list(argv) if argv is not None else None)
    except ExitRequested as exc:
        if exc.message:
            out.write(exc.message if exc.message.endswith("\n") else exc.message + "\n")
        return int(exc.status)
    except ParseFailure as exc:
        _render_parse_error(err, exc.message, exc.parser or resolve())
        return int(ExitCode.USAGE)
    except argparse.ArgumentError as exc:
        # Reached because the parsers run with exit_on_error=False, which turns
        # a bad argument into an exception rather than a sys.exit(2). Without
        # this handler every typo would surface as an "internal error" with a
        # traceback -- exactly the failure mode exit_on_error=False was meant
        # to remove.
        target = getattr(exc, "anvil_parser", None) or resolve()
        if target is not None:
            _attach_suggestion(target, exc.message)
        _render_parse_error(err, _argument_error_text(exc), target)
        return int(ExitCode.USAGE)
    except KeyboardInterrupt:
        err.write("\n" + term.A.warn("interrupted") + "\n")
        return int(ExitCode.INTERRUPTED)
    except BrokenPipeError:
        # Downstream closed (``| head``). Silence the interpreter's shutdown
        # noise by pointing fd 1 at /dev/null -- the documented remedy.
        _silence_broken_pipe()
        return int(ExitCode.FAILURE)
    except AnvilError as exc:
        err.write(exc.render() + "\n")
        return int(exc.exit_code)
    except OSError as exc:
        translated = translate_os_error(exc)
        err.write(translated.render() + "\n")
        return int(translated.exit_code)
    except Exception:
        if traceback_on_error:
            raise
        import traceback

        err.write(term.A.err("internal error") + "\n")
        err.write(term.A.hint("this is a bug; rerun with --trace for the full traceback\n"))
        err.write(
            "".join(traceback.format_exception_only(*sys.exc_info()[:2])).rstrip() + "\n"
        )
        return int(ExitCode.SOFTWARE)
    return int(result or 0)


def _argument_error_text(exc: argparse.ArgumentError) -> str:
    """Phrase an ``ArgumentError`` the way argparse's own ``error()`` would."""
    name = exc.argument_name
    message = exc.message or ""
    if not message:
        return "invalid arguments"
    if name and not message.startswith(name):
        return f"argument {name}: {message}"
    return message


def _attach_suggestion(parser: argparse.ArgumentParser, message: str) -> None:
    attach = getattr(parser, "attach_suggestion", None)
    if callable(attach):
        attach(message)


def _render_parse_error(err: IO[str], message: str,
                        parser: argparse.ArgumentParser | None) -> None:
    """Usage line, blank line, then a coloured error block."""
    if parser is None:
        err.write(f"{term.A.err(term.A.bold('error'))}: {message}\n")
        return
    for line in parser.format_usage().strip().splitlines():
        err.write(term.A.hint(line.rstrip()) + "\n")
    err.write("\n")
    err.write(f"{term.A.err(term.A.bold('error'))}: {message}\n")
    suggestion = getattr(parser, "suggestion", None)
    if suggestion:
        err.write(f"{term.A.warn('hint')}: {suggestion}\n")
    err.write(term.A.hint(f"run '{parser.prog} --help' for usage\n"))


def _silence_broken_pipe() -> None:
    """Redirect fd 1 to the null device so interpreter shutdown stays quiet."""
    try:
        import os

        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except Exception:
        pass
