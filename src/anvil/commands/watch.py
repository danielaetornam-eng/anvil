"""``anvil watch`` -- rerun a command when files change."""

from __future__ import annotations

import argparse
import fnmatch
import os
import pathlib
import subprocess
import time

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, UsageError
from anvil.core.types import csv_list, duration, non_negative_int, positive_int

HELP = "rerun a command whenever files change"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "watch", aliases=["rerun"], help=HELP,
        description="Watch a directory tree and rerun a command on change.\n\n"
                    "Debounced, so an editor that writes a file three times "
                    "triggers one run, not three.",
    )
    parser.add_examples(
        "anvil watch --exec 'pytest -q'",
        "anvil watch --exec 'npm run build' --exclude 'node_modules,dist' .",
        "anvil watch --exec 'make' --on-change --once",
    )

    watch = parser.add_argument_group("Watching")
    watch.add_argument("--interval", type=duration, default="500ms", metavar="DUR",
                       help="how often to poll")
    watch.add_argument("--debounce", type=duration, default="200ms", metavar="DUR",
                       help="wait for writes to settle before running")
    watch.add_argument("--paths", type=csv_list, default=None, metavar="DIR[,DIR]",
                       help="directories to watch (default: .)")
    watch.add_argument("--include", action="comma_list", default=[],
                       metavar="GLOB[,GLOB]", help="only react to these names")
    watch.add_argument("--exclude", action="comma_list",
                       default=["*.pyc", "__pycache__/*", ".git/*", "*.swp",
                                "*~", ".DS_Store"],
                       metavar="GLOB[,GLOB]", help="ignore these names")
    watch.add_argument("--follow-subdirs", action="store_true", default=True,
                       help="descend (default)")
    watch.add_argument("--no-follow-subdirs", dest="follow_subdirs",
                       action="store_false", help="only the top level")

    run = parser.add_argument_group("Running")
    run.add_argument("--exec", dest="command", required=True, metavar="CMD",
                     help="the command to rerun")
    run.add_argument("--on-change", action="store_true", default=False,
                     help="run once at the start, then on every change")
    run.add_argument("--once", action="store_true", default=False,
                     help="run once and exit")
    run.add_argument("--max-runs", type=non_negative_int, default=None,
                     metavar="N", help="stop after this many runs")
    run.add_argument("--clear", action="store_true", default=False,
                     help="clear the screen before each run")
    run.add_argument("--shell", default=None, metavar="PATH",
                     help="shell used to interpret the command")
    run.add_argument("--timeout", type=duration, default=None, metavar="DUR",
                     help="abort a run that takes too long")
    run.add_argument("--stop-on-error", action="store_true", default=False,
                     help="exit as soon as a run fails")
    run.add_argument("--success-pause", type=duration, default=None,
                     metavar="DUR", help="sleep after a successful run")
    run.add_argument("--limit", type=positive_int, default=0, metavar="N",
                     help="ignore changes for N seconds after a run")
    parser.set_defaults(func=_watch)
    return parser


@register_command("watch")
def _watch(ctx: Context, args: argparse.Namespace) -> int:
    import shlex

    roots = [pathlib.Path(p).expanduser().resolve()
             for p in (args.paths or ["."])]
    for root in roots:
        if not root.is_dir():
            raise InputError(f"{root} is not a directory",
                             hint="create it, or point --paths somewhere else")
    command = shlex.split(args.command)
    if not command:
        raise UsageError("--exec is empty")

    if ctx.dry_run:
        ctx.tabular("json")
        ctx.out(ctx.renderer.scalar({"watching": [str(r) for r in roots],
                                     "command": args.command}))
        ctx.err(term.A.hint(f"would run: {args.command}"))
        return 0

    runs = 0
    if args.once or args.on_change:
        code = _invoke(ctx, args, command)
        runs += 1
        if args.once:
            return code
        if args.stop_on_error and code != 0:
            return code

    snapshot = _scan(roots, args)
    ctx.err(term.A.hint(
        f"watching {', '.join(str(r) for r in roots)}; "
        f"run '{args.command}' on change (ctrl-c to stop)"))

    try:
        while True:
            time.sleep(args.interval)
            current = _scan(roots, args)
            changed = _changed(snapshot, current)
            if not changed:
                continue
            snapshot = current
            if args.debounce:
                time.sleep(args.debounce)
                snapshot = _scan(roots, args)
            if args.clear and ctx.color:
                ctx.err("\x1b[2J\x1b[H", )
            for name, _kind in sorted(changed):
                ctx.err(term.A.hint(f"changed: {name}"))
            code = _invoke(ctx, args, command)
            runs += 1
            if args.max_runs is not None and runs >= args.max_runs:
                ctx.err(term.A.ok(f"stopping after {runs} run(s)"))
                return code
            if args.stop_on_error and code != 0:
                ctx.err(term.A.err("stopping on error"))
                return code
            if args.success_pause:
                time.sleep(args.success_pause)
            snapshot = _scan(roots, args)
    except KeyboardInterrupt:
        ctx.err(term.A.warn("stopped"))
    return 0


def _scan(roots, args: argparse.Namespace) -> dict[str, tuple[float, int]]:
    """A cheap fingerprint of the tree: ``path -> (mtime, size)``."""
    out: dict[str, tuple[float, int]] = {}
    for root in roots:
        if not args.follow_subdirs:
            for entry in root.iterdir():
                if entry.is_file() and _wanted(entry.name, args):
                    _record(out, entry)
            continue
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if _wanted(d, args))
            for name in sorted(files):
                if _wanted(name, args):
                    _record(out, pathlib.Path(base) / name)
    return out


def _record(out: dict, path: pathlib.Path) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    out[str(path)] = (stat.st_mtime, stat.st_size)


def _wanted(name: str, args: argparse.Namespace) -> bool:
    if any(fnmatch.fnmatch(name, pattern) for pattern in args.exclude):
        return False
    if args.include:
        return any(fnmatch.fnmatch(name, pattern) for pattern in args.include)
    return True


def _changed(before: dict, after: dict) -> dict:
    changed: dict[str, tuple[float, int]] = {}
    for path, fingerprint in after.items():
        if before.get(path) != fingerprint:
            changed[path] = fingerprint
    for path in before:
        if path not in after:
            changed[path] = (0.0, 0)
    return changed


def _invoke(ctx: Context, args: argparse.Namespace, command: list[str]) -> int:
    import shlex

    line = command if args.shell is None else [args.shell, "-c", shlex.join(command)]
    ctx.err(term.A.hint(f"$ {args.command}"))
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            line, shell=args.shell is not None, timeout=args.timeout
        )
    except subprocess.TimeoutExpired:
        ctx.err(term.A.err(f"timed out after {args.timeout}"))
        return 75
    except OSError as exc:
        from anvil.core.errors import translate_os_error

        raise translate_os_error(exc, context=command[0]) from None
    elapsed = time.perf_counter() - started
    paint = term.A.ok if completed.returncode == 0 else term.A.err
    ctx.err(paint(f"exit {completed.returncode}") + term.A.hint(
        f"  {term.human_duration(elapsed)}"))
    return completed.returncode

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _watch
