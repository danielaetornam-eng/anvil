"""``anvil prompt`` -- interactive selection with a non-interactive escape.

Argparse patterns: ``nargs='+'`` with ``choices`` for a menu, ``action='append'``
for defaults, and the ``--yes``/``--non-interactive`` pair that every prompt
needs so CI never hangs waiting for a human.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, UsageError
from anvil.core.types import output_path

HELP = "ask the user a question, safely in scripts too"

KINDS = ("select", "multiselect", "confirm", "text", "password", "number",
         "choice")


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "prompt", aliases=["ask"], help=HELP,
        description="Ask the user for input.\n\n"
                    "Every prompt has a non-interactive escape. Without one, a "
                    "CI job that reaches a prompt hangs until it is killed.",
    )
    parser.add_examples(
        "anvil prompt 'Continue?' --confirm --default no",
        "anvil prompt 'Pick one' --choice red,green,blue",
        "anvil prompt 'Pick many' --choice a,b,c --multiselect --default a",
        'NAME=$(anvil prompt "Your name" --default world)',
    )

    group = parser.add_argument_group("Question")
    group.add_argument("--confirm", action="store_true", default=False,
                       help="ask a yes/no question")
    group.add_argument("--multiselect", action="store_true", default=False,
                       help="allow more than one answer")
    group.add_argument("--choice", type=_choices, default=None, metavar="A,B,C",
                       help="present a menu of options")
    group.add_argument("--choice-file", default=None, metavar="PATH",
                       help="read one option per line from a file")
    group.add_argument("--label", action="append", dest="labels", default=[],
                       metavar="NAME=TEXT", help="a named value, printed as a map")
    group.add_argument("--default", default=None, metavar="TEXT",
                       help="value used when the user just presses enter")
    group.add_argument("--number", action="store_true", default=False,
                       help="require a number")
    group.add_argument("--min", type=float, default=None, metavar="N",
                       help="lowest acceptable value with --number")
    group.add_argument("--max", type=float, default=None, metavar="N",
                       help="highest acceptable value with --number")
    group.add_argument("--timeout", type=_seconds, default=None,
                       metavar="SEC", help="give up after this many seconds")

    safety = parser.add_argument_group("Non-interactive behaviour")
    safety.add_argument("--non-interactive", action="store_true", default=False,
                        help="never prompt; use --default or fail")
    safety.add_argument("--yes", "-y", action="store_true", default=False,
                        help="accept the default without asking")
    safety.add_argument("--abort", action="store_true", default=False,
                        help="treat EOF as a failure")
    safety.add_argument("--error-on-eof", action="store_true", default=False,
                        help="exit non-zero when stdin ends unexpectedly")
    safety.add_argument("--output", "-o", type=output_path, default=None,
                        metavar="FILE", help="write the answer to a file too")

    parser.add_argument("question", metavar="QUESTION")
    parser.set_defaults(func=_prompt)
    return parser


def _choices(value: str) -> list[str]:
    return [c.strip() for c in value.split(",") if c.strip()]


def _seconds(value: str) -> float:
    from anvil.core.types import duration

    return duration(value)


@register_command("prompt")
def _prompt(ctx: Context, args: argparse.Namespace) -> int:
    options = list(args.choice or [])
    if args.choice_file:
        try:
            with open(args.choice_file, encoding="utf-8") as handle:
                options += [line.strip() for line in handle if line.strip()]
        except OSError as exc:
            from anvil.core.errors import translate_os_error

            raise translate_os_error(exc, context=args.choice_file) from None
    if args.multiselect and not options:
        raise UsageError("--multiselect needs --choice or --choice-file")

    interactive = _is_interactive(args)
    if not interactive:
        # A piped stdin is still readable: the piped text is the answer.
        if not args.non_interactive and not args.yes and _has_piped_input():
            answer = _ask_line(args)
            _report(ctx, args, answer)
            return 0
        return _non_interactive(ctx, args, options)

    if args.confirm:
        answer: object = ctx.confirm(args.question, default=bool(args.default))
    elif options:
        answer = _select(args, options)
    elif args.number:
        answer = _ask_number(args)
    else:
        answer = _ask_line(args, secret=not sys.stdin.isatty() and False)

    if args.multiselect and isinstance(answer, list) and not answer:
        answer = []
    _report(ctx, args, answer)
    return 0


def _is_interactive(args: argparse.Namespace) -> bool:
    """Whether to actually ask a question.

    A pipe is *not* treated as non-interactive: ``echo world | anvil prompt
    'Name?' --default x`` is a perfectly ordinary way to script this, and the
    piped text is the answer. Only an explicit flag forces the default path.
    """
    if args.non_interactive or args.yes:
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _non_interactive(ctx: Context, args: argparse.Namespace,
                     options: list[str]) -> int:
    if args.default is None:
        raise InputError(
            "not a terminal and no --default given",
            hint="add --default VALUE, or --non-interactive to fail loudly",
        )
    answer: object = args.default
    if args.confirm:
        answer = str(args.default).strip().lower() in ("1", "true", "yes", "y")
    elif options and str(answer) not in options:
        raise InputError(
            f"--default {answer!r} is not one of {', '.join(options)}",
            hint=f"choose from: {', '.join(options)}",
        )
    if args.number and answer is not None:
        answer = _check_range(args, float(answer))
    _report(ctx, args, answer)
    return 0


def _has_piped_input() -> bool:
    """True when stdin has data waiting (a pipe or redirect, not a terminal)."""
    try:
        return not sys.stdin.isatty() and not sys.stdin.closed
    except Exception:
        return False


def _ask_line(args: argparse.Namespace, *, secret: bool = False) -> str:
    if secret:
        return getpass.getpass(f"{args.question}: ")
    print(f"{args.question}: ", end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        # EOF straight away means nobody answered. Returning "" silently would
        # hand a script an empty value it then treats as a real answer.
        if args.error_on_eof:
            raise InputError("stdin ended before an answer arrived")
        if args.default is None:
            raise InputError(
                "no answer available: stdin is empty and no --default was given",
                hint="pass --default VALUE, or --error-on-eof to be stricter",
            )
        return args.default
    value = line.rstrip("\n")
    return value if value.strip() else (args.default if args.default is not None
                                        else value)


def _ask_number(args: argparse.Namespace) -> float:
    while True:
        raw = _ask_line(args)
        try:
            value = float(raw)
        except ValueError:
            print("  a number is required", file=sys.stderr)
            continue
        return _check_range(args, value)


def _check_range(args: argparse.Namespace, value: float) -> float:
    if args.min is not None and value < args.min:
        raise InputError(f"{value} is below the minimum of {args.min}")
    if args.max is not None and value > args.max:
        raise InputError(f"{value} is above the maximum of {args.max}")
    return value


def _select(args: argparse.Namespace, options: list[str]) -> object:
    default = [d.strip() for d in (args.default or "").split(",") if d.strip()]
    if args.multiselect:
        print("\n  pick one or more (comma separated):", file=sys.stderr)
    else:
        print("", file=sys.stderr)
    for index, option in enumerate(options, 1):
        mark = "*" if option in default else " "
        print(f" {mark} {index}) {option}", file=sys.stderr)
    print("", file=sys.stderr)
    while True:
        raw = _ask_line(args).strip()
        if not raw:
            return default if args.multiselect else (default[0] if default else None)
        chosen = _parse_selection(raw, options, args.multiselect)
        if chosen is not None:
            return chosen


def _parse_selection(raw: str, options: list[str],
                     multiple: bool) -> object | None:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    picked: list[str] = []
    for part in parts:
        if part in options:
            picked.append(part)
            continue
        if part.isdigit() and 1 <= int(part) <= len(options):
            picked.append(options[int(part) - 1])
            continue
        print(f"  {part!r} is not one of the options", file=sys.stderr)
        return None
    if not picked:
        print("  pick at least one", file=sys.stderr)
        return None
    return picked if multiple else picked[0]


def _report(ctx: Context, args: argparse.Namespace, answer: object) -> None:
    """Print the answer as data.

    Deliberately *not* exported into the environment: a child process cannot
    modify its parent's shell, so ``export``-style tricks silently do nothing.
    Capture stdout instead (``ANSWER=$(anvil prompt 'Name' --default x)``).
    """
    payload: object = answer
    if args.labels:
        if not isinstance(answer, list):
            answer = [answer]
        payload = dict(zip(args.labels, answer, strict=False))
    ctx.out(ctx.renderer.scalar(payload))
    if args.output:
        from anvil.core.io import write_output

        write_output(args.output, (ctx.renderer.scalar(payload)) + "\n")

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _prompt
