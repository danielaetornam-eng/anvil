"""``anvil completions`` -- install and preview shell completions."""

from __future__ import annotations

import argparse

from anvil.core import completion, term
from anvil.core.app import Context, register_command
from anvil.core.types import output_path

HELP = "install shell completions for bash, zsh and fish"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "completions", aliases=list(aliases), help=HELP,
        description="Generate shell completions from the live parser tree.\n\n"
                    "The script is a by-product of the same add_argument calls "
                    "that build --help, so it cannot fall out of date.",
    )
    parser.add_examples(
        "anvil completions show bash",
        "anvil completions install fish",
        "anvil completions path zsh",
        "anvil completions check",
    )

    subs = parser.add_subparsers(dest="completions_command", metavar="SUBCOMMAND")
    subs.required = True

    show = subs.add_parser("show", aliases=["generate"], help="print the script",
                           description="Write the completion script to stdout.")
    show.add_argument("shell", choices=completion.SHELLS)
    show.set_defaults(func=_show)

    install = subs.add_parser("install", help="write the script to disk",
                              description="Install the completion script.")
    install.add_argument("shell", choices=completion.SHELLS)
    install.add_argument("--dest", type=output_path, default=None, metavar="PATH",
                         help="override the install location")
    install.set_defaults(func=_install)

    path = subs.add_parser("path", help="print the default install location")
    path.add_argument("shell", choices=completion.SHELLS, nargs="?",
                      default="bash")
    path.set_defaults(func=_path)

    check = subs.add_parser("check", help="report completion health",
                            description="Check whether completions are "
                                        "installed and which shell is detected.")
    check.set_defaults(func=_check)
    return parser


@register_command("completions")
def _show(ctx: Context, args: argparse.Namespace) -> int:
    ctx.out(completion.render(args.shell, ctx.prog).rstrip())
    return 0


def _install(ctx: Context, args: argparse.Namespace) -> int:
    if ctx.dry_run:
        target = args.dest or completion.completion_paths(args.shell, ctx.prog)
        ctx.out(ctx.renderer.scalar({"shell": args.shell, "path": str(target),
                                     "written": False}))
        ctx.err(term.A.hint(f"would write {target}"))
        return 0
    probe, target = completion.install(args.shell, ctx.prog, dest=args.dest)
    ctx.out(ctx.renderer.scalar({"shell": args.shell, "path": str(target),
                                 "helper": str(probe), "written": True}))
    ctx.err(term.A.ok(f"installed to {target}"))
    ctx.err(term.A.hint(completion._shell_rc_hint(args.shell, target)))
    return 0


def _path(ctx: Context, args: argparse.Namespace) -> int:
    ctx.out(str(completion.completion_paths(args.shell, ctx.prog)))
    return 0


def _check(ctx: Context, args: argparse.Namespace) -> int:
    detected = completion.shell_of()
    rows = []
    for shell in completion.SHELLS:
        path = completion.completion_paths(shell, ctx.prog)
        executable = completion.which(shell)
        rows.append({
            "shell": shell,
            "detected": shell == detected,
            "installed": path.exists(),
            "path": str(path),
            "executable": bool(executable),
        })
    ctx.tabular()
    table = term.Table(
        [term.Column("shell", "shell"),
         term.Column("detected", "detected", align="center"),
         term.Column("installed", "installed", align="center"),
         term.Column("executable", "in $PATH", align="center"),
         term.Column("path", "path")],
    )
    for row in rows:
        table.add_row([row["shell"],
                       _mark(row["detected"]), _mark(row["installed"]),
                       _mark(row["executable"]), row["path"]])
    ctx.render(table)
    if not any(row["installed"] for row in rows):
        ctx.err(term.A.hint("nothing installed; try 'anvil completions install bash'"))
    return 0


def _mark(value: bool) -> str:
    return term.A.ok("yes") if value else term.A.hint("no")

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _check
