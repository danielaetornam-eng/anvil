"""``anvil text`` -- line-oriented text transforms."""

from __future__ import annotations

import argparse
import re
import textwrap

from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, UsageError
from anvil.core.io import read_text
from anvil.core.types import interval, non_negative_int, regex_string

HELP = "case, pad, wrap, number and filter text lines"

CASES = ("upper", "lower", "title", "sentence", "camel", "snake", "kebab",
         "constant", "alternating", "reverse")
FILTERS = ("empty", "numbered", "longer-than", "matching", "non-matching")


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "text", aliases=list(aliases), help=HELP,
        description="Line-oriented text transforms.\n\n"
                    "Reads stdin by default, so it pipes: "
                    "`anvil table x.json | anvil text --case title`.",
    )
    parser.add_examples(
        "anvil text --case kebab < names.txt",
        "cat data.json | anvil text --wrap 72 --number",
        "anvil text --strip --keep-non-empty --pad 4",
        "anvil text --match '^#' --invert notes.md",
    )

    shape = parser.add_argument_group("Shape")
    shape.add_argument("--case", choices=CASES, default=None, metavar="CASE",
                       help=f"transform case: {', '.join(CASES)}")
    shape.add_argument("--wrap", type=non_negative_int, default=None, metavar="N",
                       help="wrap lines to N columns")
    shape.add_argument("--pad", type=non_negative_int, default=None, metavar="N",
                       help="right-pad every line to N characters")
    shape.add_argument("--indent", type=non_negative_int, default=None,
                       metavar="N", help="prefix every line with N spaces")
    shape.add_argument("--truncate", type=non_negative_int, default=None,
                       metavar="N", help="cut every line to N characters")
    shape.add_argument("--prefix", default=None, metavar="TEXT",
                       help="prepend this to every line")
    shape.add_argument("--suffix", default=None, metavar="TEXT",
                       help="append this to every line")
    shape.add_argument("--replace", action="comma_list", default=[],
                       metavar="FROM,TO", help="replace every occurrence")

    clean = parser.add_argument_group("Cleaning")
    clean.add_argument("--strip", action="store_true", default=False,
                       help="strip leading and trailing whitespace")
    clean.add_argument("--squeeze", action="store_true", default=False,
                       help="collapse runs of whitespace")
    clean.add_argument("--keep-non-empty", action="store_true", default=False,
                       help="drop blank lines")
    clean.add_argument("--trim", type=non_negative_int, default=None, metavar="N",
                       help="keep only the first N lines")
    clean.add_argument("--reverse", action="store_true", default=False,
                       help="reverse the order of the lines")
    clean.add_argument("--unique", action="store_true", default=False,
                       help="drop duplicate lines, keeping the first")
    clean.add_argument("--sort", action="store_true", default=False,
                       help="sort the lines")
    clean.add_argument("--reverse-sort", action="store_true", default=False,
                       help="sort the lines descending")

    select = parser.add_argument_group("Selection")
    select.add_argument("--match", type=regex_string, default=None, metavar="RE",
                        help="keep only lines matching RE")
    select.add_argument("--invert", action="store_true", default=False,
                        help="invert the match")
    select.add_argument("--lines", type=interval(1, None), default=None,
                        metavar="SPEC", help="keep only these line numbers")
    select.add_argument("--exclude-lines", type=interval(1, None), default=None,
                        metavar="SPEC", help="drop these line numbers")
    select.add_argument("--head", type=non_negative_int, default=None, metavar="N",
                        help="keep only the first N lines")
    select.add_argument("--tail", type=non_negative_int, default=None, metavar="N",
                        help="keep only the last N lines")

    select.add_argument("--number", action="store_true", default=False,
                        help="prefix lines with right-aligned numbers")
    select.add_argument("--separator", default=None, metavar="TEXT",
                        help="join every line into one, separated by TEXT")

    parser.add_argument("file", nargs="?", default="-", metavar="FILE")
    parser.set_defaults(func=_text)
    return parser


@register_command("text")
def _text(ctx: Context, args: argparse.Namespace) -> int:
    if args.head is not None and args.tail is not None:
        raise UsageError("--head and --tail cannot both be given")
    raw = read_text(args.file)
    lines = raw.splitlines()

    if args.match is not None:
        lines = [line for line in lines if bool(args.match.search(line)) != args.invert]
    if args.lines is not None:
        wanted = set(args.lines)
        lines = [line for n, line in enumerate(lines, 1) if n in wanted]
    if args.exclude_lines is not None:
        unwanted = set(args.exclude_lines)
        lines = [line for n, line in enumerate(lines, 1) if n not in unwanted]

    if args.strip:
        lines = [line.strip() for line in lines]
    if args.squeeze:
        lines = [re.sub(r"\s+", " ", line).strip() for line in lines]
    if args.keep_non_empty:
        lines = [line for line in lines if line.strip()]

    for pair in args.replace or []:
        if "," not in pair:
            raise InputError(f"--replace expects FROM,TO, got {pair!r}")
        find, _, replacement = pair.partition(",")
        lines = [line.replace(find, replacement) for line in lines]

    if args.case:
        lines = [_case(args.case, line) for line in lines]
    if args.truncate is not None:
        lines = [line[: args.truncate] for line in lines]
    if args.pad is not None:
        lines = [line.ljust(args.pad) for line in lines]
    if args.indent is not None:
        lines = [" " * args.indent + line for line in lines]
    if args.prefix is not None:
        lines = [args.prefix + line for line in lines]
    if args.suffix is not None:
        lines = [line + args.suffix for line in lines]

    if args.unique:
        seen: set[str] = set()
        lines = [line for line in lines if not (line in seen or seen.add(line))]
    if args.sort:
        lines = sorted(lines)
    if args.reverse_sort:
        lines = sorted(lines, reverse=True)
    if args.reverse:
        lines = list(reversed(lines))

    if args.wrap:
        wrapped: list[str] = []
        for line in lines:
            wrapped += textwrap.wrap(line, args.wrap, break_long_words=False,
                                     break_on_hyphens=False,
                                     replace_whitespace=False) or [""]
        lines = wrapped

    if args.tail is not None:
        lines = lines[-args.tail:] if args.tail else []
    if args.trim is not None:
        lines = lines[: args.trim]

    if args.number:
        width = len(str(len(lines)))
        lines = [f"{n:>{width}}  {line}" for n, line in enumerate(lines, 1)]

    ctx.out(args.separator.join(lines) if args.separator else "\n".join(lines))
    return 0


def _case(style: str, line: str) -> str:
    if style == "upper":
        return line.upper()
    if style == "lower":
        return line.lower()
    if style == "title":
        return line.title()
    if style == "sentence":
        stripped = line.strip()
        return (stripped[:1].upper() + stripped[1:].lower()) if stripped else line
    if style == "snake":
        return re.sub(r"[-\s]+", "_", line).strip("_").lower()
    if style == "kebab":
        return re.sub(r"[_\s]+", "-", line).strip("-").lower()
    if style == "constant":
        return re.sub(r"[-\s]+", "_", line).strip("_").upper()
    words = re.split(r"[-\s_]+", line)
    if style == "camel":
        return words[0].lower() + "".join(w.capitalize() for w in words[1:]) if words else line
    if style == "alternating":
        return "".join(c.upper() if i % 2 == 0 else c.lower()
                       for i, c in enumerate(line))
    if style == "reverse":
        return line[::-1]
    return line

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _text
