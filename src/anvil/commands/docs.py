"""``anvil docs`` -- the parser tree as data.

This is the command that justifies building every parser programmatically: the
JSON it emits is the same structure ``--help`` renders, so documentation,
completions, and tests can never drift from the implementation.
"""

from __future__ import annotations

import argparse
import json

from anvil.core import term
from anvil.core.app import Context, build_parser, register_command
from anvil.core.errors import NotFoundError
from anvil.core.formatters import MarkdownHelpFormatter
from anvil.core.introspect import ParserNode, to_dict
from anvil.core.types import non_negative_int

HELP = "dump the parser tree as JSON, Markdown, or a man-page stub"

FORMATS = ("json", "markdown", "completion", "bash", "zsh", "fish", "tree",
           "usage", "options")


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "docs", aliases=list(aliases), help=HELP,
        description="Machine-readable documentation generated from the live "
                    "parser tree.\n\n"
                    "Because it is derived from the same objects that render "
                    "`--help`, it cannot go stale.",
    )
    parser.add_examples(
        "anvil docs --format json",
        "anvil docs --format json --only run --output json",
        "anvil docs --format markdown > docs.md",
        "anvil docs --format tree",
    )

    group = parser.add_argument_group("Selection")
    group.add_argument("--only", type=_command_path, default=None, metavar="CMD",
                       help="restrict output to one command, e.g. 'json query'")
    group.add_argument("--depth", type=non_negative_int, default=None,
                       metavar="N", help="how deep to descend")
    group.add_argument("--include-defaults", action="store_true", default=False,
                       help="omit nothing (defaults are always included)")
    group.add_argument("--no-defaults", dest="include_defaults",
                       action="store_false", default=True,
                       help="leave default values out of the dump")

    group2 = parser.add_argument_group("Output")
    group2.add_argument("--format", "-f", choices=FORMATS, default="json",
                        metavar="FORMAT", help="what to emit")
    group2.add_argument("--only-options", action="store_true", default=False,
                        help="list just the option names")
    parser.set_defaults(func=_docs)
    return parser


def _command_path(value: str) -> list[str]:
    return [p for p in value.split() if p]


@register_command("docs")
def _docs(ctx: Context, args: argparse.Namespace) -> int:
    root = build_parser(prog=ctx.prog)
    node = ParserNode(root)

    if args.only:
        target = node.find(*args.only)
        if target is None:
            raise NotFoundError(
                f"no such command: {' '.join(args.only)}",
                hint="try 'anvil --help' for the list",
            )
        node = target

    if args.only_options:
        ctx.out("\n".join(node.option_flags()))
        return 0

    fmt = args.format
    if fmt == "usage":
        ctx.out(node.parser.format_usage().rstrip())
        return 0
    if fmt in ("bash", "zsh", "fish"):
        from anvil.core import completion

        ctx.out(completion.render(fmt, ctx.prog).rstrip())
        return 0
    if fmt == "completion":
        from anvil.core import completion

        for shell in completion.SHELLS:
            ctx.out(f"# {shell}")
            ctx.out(completion.render(shell, ctx.prog).rstrip())
        return 0
    if fmt == "markdown":
        ctx.out(_markdown(node))
        return 0
    if fmt == "tree":
        ctx.out(_tree(node))
        return 0

    depth = args.depth if args.depth is not None else 6
    payload = to_dict(node.parser, max_depth=depth,
                      include_defaults=args.include_defaults)
    ctx.out(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    return 0


def _tree(node: ParserNode) -> str:
    root = term.TreeNode(node.parser.prog)
    for child in sorted(node.children.values(), key=lambda n: n.name):
        root.add(child.name)
    _fill(root, node)
    return term.Tree().render(root)


def _fill(parent: term.TreeNode, node: ParserNode) -> None:
    for child in sorted(node.children.values(), key=lambda n: n.name):
        branch = parent.add(child.name)
        for action in child.parser._actions:
            if action.help is argparse.SUPPRESS:
                continue
            flags = ", ".join(action.option_strings) or action.dest
            branch.add(f"{flags}  {action.help or ''}".rstrip())
        _fill(branch, child)


def _markdown(node: ParserNode) -> str:
    lines = [f"# `{node.parser.prog}`", ""]
    if node.parser.description:
        lines += [node.parser.description, ""]
    lines += ["## Commands", ""]
    for child in sorted(node.children.values(), key=lambda n: n.name):
        aliases = ""
        lines.append(f"- `{child.name}`{' (' + child.help + ')' if child.help else ''}{aliases}")
    lines.append("")
    lines += _markdown_parser(node)
    for child in sorted(node.children.values(), key=lambda n: n.name):
        lines += _markdown_parser(child)
    return "\n".join(lines).rstrip() + "\n"


def _markdown_parser(node: ParserNode) -> list[str]:
    lines = [f"## `{node.parser.prog}`", ""]
    if node.parser.description:
        lines += [node.parser.description, ""]
    formatter = MarkdownHelpFormatter(node.parser.prog)
    for group in node.parser._action_groups:
        visible = [a for a in group._group_actions
                   if a.help is not argparse.SUPPRESS]
        if not visible:
            continue
        lines += [f"### {group.title}", ""]
        for action in visible:
            formatter.add_argument(action)
        lines.append(formatter.format_help().rstrip("\n"))
        lines.append("")
    return lines

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _docs
