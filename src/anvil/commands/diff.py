"""``anvil diff`` -- compare files, directories, and text."""

from __future__ import annotations

import argparse
import difflib
import filecmp
import fnmatch
import os
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, NotFoundError, UsageError
from anvil.core.io import read_text
from anvil.core.types import non_negative_int

HELP = "diff files, directories, or streams"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "diff", aliases=list(aliases), help=HELP,
        description="Compare two things.\n\n"
                    "Detects automatically whether you gave two files, two "
                    "directories, or two streams, and picks the right algorithm.",
    )
    parser.add_examples(
        "anvil diff old.json new.json",
        "anvil diff --unified=0 a.txt b.txt",
        "anvil diff --brief --recursive --exclude '*.lock' dirA dirB",
        "git diff | anvil diff --stdin",
    )

    group = parser.add_argument_group("Comparison")
    group.add_argument("--unified", "-U", type=non_negative_int, default=3,
                       metavar="N", help="context lines (0 disables context)")
    group.add_argument("--context", "-C", type=non_negative_int, default=None,
                       metavar="N", help="show the whole file as context")
    group.add_argument("--ignore-case", "-i", action="store_true", default=False,
                       help="compare case-insensitively")
    group.add_argument("--ignore-space-change", action="store_true", default=False,
                       help="ignore whitespace-only changes")
    group.add_argument("--ignore-blank-lines", action="store_true", default=False,
                       help="skip blank lines entirely")
    group.add_argument("--ignore-all-space", action="store_true", default=False,
                       help="ignore all whitespace")
    group.add_argument("--words", action="store_true", default=False,
                       help="diff by word instead of by line")
    group.add_argument("--characters", "-C-chars", dest="characters",
                       action="store_true", default=False,
                       help="diff by character")
    group.add_argument("--minimal", action="store_true", default=False,
                       help="use difflib's quadratic minimal algorithm")
    group.add_argument("--speed-large-files", action="store_true", default=False,
                       help="forget identities, just compare content")

    report = parser.add_argument_group("Reporting")
    report.add_argument("--brief", "-q", action="store_true", default=False,
                        help="report only which files differ")
    report.add_argument("--recursive", "-r", action="store_true", default=False,
                        help="descend into directories")
    report.add_argument("--exclude", action="comma_list", default=[],
                        metavar="GLOB[,GLOB]", help="names to skip")
    report.add_argument("--include", action="comma_list", default=[],
                        metavar="GLOB[,GLOB]", help="names to keep")
    report.add_argument("--stat", action="store_true", default=False,
                        help="print a summary instead of a patch")
    report.add_argument("--jobs", "-j", type=non_negative_int, default=None,
                        metavar="N", help="parallel workers")
    report.add_argument("--left", metavar="LABEL", default="left",
                        help="label for the first input")
    report.add_argument("--right", metavar="LABEL", default="right",
                        help="label for the second input")
    report.add_argument("--color", choices=("auto", "always", "never"),
                        default="auto", help="colour the diff")

    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="two files, two directories, or - for stdin")
    parser.set_defaults(func=_diff)
    return parser


@register_command("diff")
def _diff(ctx: Context, args: argparse.Namespace) -> int:
    if len(args.paths) != 2:
        raise UsageError(
            f"diff needs exactly two paths, got {len(args.paths)}",
            hint="use '-' to read one side from stdin",
        )
    left, right = args.paths
    colour = ctx.color if args.color == "auto" else args.color == "always"

    if pathlib.Path(left).is_dir() or pathlib.Path(right).is_dir():
        return _diff_trees(ctx, args, left, right, colour=colour)
    return _diff_text(ctx, args, left, right, colour=colour)


def _diff_text(ctx: Context, args: argparse.Namespace, left: str, right: str,
               *, colour: bool) -> int:
    a = _read(left)
    b = _read(right)
    normalize = lambda text: text  # noqa: E731 - named for the branches below
    if args.ignore_case:
        normalize = lambda t: t.lower()  # noqa: E731
    elif args.ignore_all_space:
        normalize = lambda t: "".join(t.split())  # noqa: E731
    elif args.ignore_space_change:
        normalize = lambda t: " ".join(t.split())  # noqa: E731
    if args.ignore_blank_lines:
        a = "\n".join(line for line in a.splitlines() if line.strip())
        b = "\n".join(line for line in b.splitlines() if line.strip())

    lines = _unified(a, b, args, normalize)
    if args.brief:
        if lines:
            ctx.out(f"Files {left} and {right} differ")
            return 1
        return 0
    for line in lines:
        ctx.out(_paint(line, colour))
    return 1 if lines else 0


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return read_text(path)
    except (NotFoundError, InputError) as exc:
        raise InputError(exc.message, hint=exc.hint or "check the path") from None


def _unified(a: str, b: str, args: argparse.Namespace, normalize) -> list[str]:
    """Produce a unified diff, honouring the granularity flags."""
    if args.characters:
        a_units, b_units = list(a), list(b)
    elif args.words:
        a_units, b_units = a.split(), b.split()
    else:
        a_units, b_units = a.splitlines(keepends=True), b.splitlines(keepends=True)

    # --context=N means "show N lines around a change" with a marker line,
    # which is what difflib's n=N plus the group_headers hook produces.
    context = args.context if args.context is not None else args.unified
    diff = difflib.unified_diff(
        [normalize(unit) for unit in a_units],
        [normalize(unit) for unit in b_units],
        fromfile=args.left,
        tofile=args.right,
        n=context,
        lineterm="",
    )
    return [line[:-1] if line.endswith("\n") else line for line in diff]


def _paint(line: str, colour: bool) -> str:
    if not colour:
        return line
    if line.startswith("+++") or line.startswith("---"):
        return term.A.bold(line)
    if line.startswith("@@"):
        return term.A.cyan(line)
    if line.startswith("+"):
        return term.A.ok(line)
    if line.startswith("-"):
        return term.A.err(line)
    return line


# --------------------------------------------------------------------------- #
# directory trees
# --------------------------------------------------------------------------- #


def _diff_trees(ctx: Context, args: argparse.Namespace, left: str, right: str,
                *, colour: bool) -> int:
    if not args.recursive:
        raise UsageError(
            f"{left} and {right} look like directories",
            hint="add --recursive",
        )
    left_root = pathlib.Path(left)
    right_root = pathlib.Path(right)
    if not left_root.is_dir() or not right_root.is_dir():
        raise InputError("one side is a file and the other a directory",
                         hint="diff them directly instead")
    names = _collect(args, left_root, right_root)
    workers = args.jobs or min(32, (os.cpu_count() or 1) + 4)

    def compare(name: str) -> tuple[str, str]:
        a, b = left_root / name, right_root / name
        if a.is_file() and b.is_file():
            equal = filecmp.cmp(a, b, shallow=not args.speed_large_files)
            return (name, "same" if equal else "differs")
        return (name, "only-left" if not b.exists() else "only-right")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(compare, names))

    differing = [n for n, state in results if state == "differs"]
    only_left = [n for n, state in results if state == "only-left"]
    only_right = [n for n, state in results if state == "only-right"]

    table = term.Table([term.Column("state", "state"), term.Column("path", "path")])
    for name, state in sorted(results, key=lambda r: (r[1], r[0])):
        if state == "same" and not args.stat:
            continue
        style = {"differs": "err", "only-left": "warn", "only-right": "warn",
                 "same": "gray"}.get(state, "")
        table.add_row([term.A.paint(style, state) if ctx.color else state, name])

    if args.brief:
        for name in differing:
            ctx.out(f"Files {left}/{name} and {right}/{name} differ")
        for name in only_left:
            ctx.out(f"Only in {left}: {name}")
        for name in only_right:
            ctx.out(f"Only in {right}: {name}")
    else:
        ctx.render(table)

    if args.stat:
        ctx.err(term.A.hint(
            f"{len(names)} compared, {len(differing)} differ, "
            f"{len(only_left)} only-left, {len(only_right)} only-right"))
    return 1 if (differing or only_left or only_right) else 0


def _collect(args: argparse.Namespace, left: pathlib.Path,
             right: pathlib.Path) -> list[str]:
    names: set[str] = set()
    for root in (left, right):
        for base, dirs, files in os.walk(root):
            dirs.sort()
            for name in files:
                rel = os.path.relpath(os.path.join(base, name), root)
                if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
                       for p in args.exclude):
                    continue
                if args.include and not any(
                    fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
                    for p in args.include
                ):
                    continue
                names.add(rel)
    return sorted(names)

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _diff
