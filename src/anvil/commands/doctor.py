"""``anvil doctor`` -- diagnose the environment and the CLI itself."""

from __future__ import annotations

import argparse
import os
import platform
import sys

from anvil.core import term
from anvil.core.app import VERSION, Context, build_parser, register_command
from anvil.core.errors import InputError
from anvil.core.types import non_negative_int

HELP = "check the environment for problems"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "doctor", aliases=list(aliases), help=HELP,
        description="Report anything that could make anvil misbehave.\n\n"
                    "Checks the Python version, terminal capabilities, "
                    "configuration, shell completions, and the parser tree's "
                    "internal consistency.",
    )
    parser.add_examples(
        "anvil doctor",
        "anvil doctor --output json",
        "anvil doctor --only terminal,config",
    )
    group = parser.add_argument_group("Selection")
    group.add_argument("--only", type=_split, default=None, metavar="A,B",
                       help="run only these sections")
    group.add_argument("--skip", type=_split, default=None, metavar="A,B",
                       help="skip these sections")
    group.add_argument("--timeout", type=non_negative_int, default=5,
                       metavar="N", help="seconds before a check is abandoned")
    group.add_argument("--tag", action="key_value", dest="tags", default={},
                       metavar="NAME=TEXT",
                       help="attach an extra field to every finding; repeatable")
    parser.set_defaults(func=_doctor)
    return parser


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


@register_command("doctor")
def _doctor(ctx: Context, args: argparse.Namespace) -> int:
    sections = {
        "python": _python,
        "terminal": _terminal,
        "config": _config,
        "parser": _parser,
        "completions": _completions,
        "commands": _commands,
    }
    unknown = [n for n in (args.only or []) if n not in sections]
    if unknown:
        raise InputError(
            f"unknown section(s): {', '.join(unknown)}",
            hint=f"choose from {', '.join(sorted(sections))}",
        )
    wanted = [n for n in sections
              if (not args.only or n in args.only)
              and (not args.skip or n not in args.skip)]
    if not wanted:
        raise InputError("every selected section was skipped",
                         hint=f"check --skip against {', '.join(sorted(sections))}")

    findings: list[dict] = []
    for name in wanted:
        findings.extend(sections[name](ctx))
    # ``--tag host=ci`` becomes the key "host" for every finding, with no
    # repeated key literal -- the auto_name action's whole purpose.
    if args.tags:
        for finding in findings:
            finding.update(args.tags)
    if args.only:
        ctx.out(ctx.renderer.scalar(findings))
        return 0

    order = {"fail": 0, "warn": 1, "ok": 2}
    findings.sort(key=lambda f: order.get(f["status"], 3))
    ctx.tabular()
    table = term.Table(
        [term.Column("status", ""), term.Column("section", "section"),
         term.Column("check", "check"), term.Column("detail", "detail")],
    )
    for finding in findings:
        table.add_row([_paint(finding["status"]), finding["section"],
                       finding["check"], finding["detail"]])
    ctx.render(table)

    bad = sum(1 for f in findings if f["status"] == "fail")
    warn = sum(1 for f in findings if f["status"] == "warn")
    if bad:
        ctx.err(term.A.err(f"{bad} problem(s), {warn} warning(s)"))
        return 1
    if warn:
        ctx.err(term.A.warn(f"{warn} warning(s)"))
    return 0


def _paint(status: str) -> str:
    if status == "ok":
        return term.A.ok("ok")
    if status == "warn":
        return term.A.warn("warn")
    return term.A.err("fail")


def _finding(status: str, section: str, check: str, detail: str) -> dict:
    return {"status": status, "section": section, "check": check, "detail": detail}


def _python(ctx: Context) -> list[dict]:
    import argparse
    import inspect

    out = []
    version = platform.python_version()
    ok = sys.version_info >= (3, 11)
    out.append(_finding(
        "ok" if ok else "fail", "python", "version",
        f"{version} on {platform.python_implementation()}",
    ))
    if not ok:
        out.append(_finding("fail", "python", "minimum",
                            "anvil needs 3.11 or newer for tomllib"))
    out.append(_finding("ok", "python", "executable", sys.executable))
    if getattr(sys, "frozen", False):
        out.append(_finding("warn", "python", "frozen",
                            "running from a frozen bundle; limits type() lookups"))
    signature = inspect.signature(argparse.ArgumentParser.__init__)
    extras = [p for p in signature.parameters
              if p in ("suggest_on_error", "color")]
    out.append(_finding(
        "ok", "python", "argparse",
        f"stdlib argparse; 3.14 extras present: {extras or 'none'}",
    ))
    return out


def _terminal(ctx: Context) -> list[dict]:
    out = []
    columns, _rows = term.terminal_size()
    out.append(_finding("ok", "terminal", "width", f"{columns} columns"))
    encoding = sys.stdout.encoding or "unknown"
    utf8 = "utf" in encoding.lower()
    out.append(_finding(
        "ok" if utf8 else "warn", "terminal", "encoding",
        f"stdout={encoding}; unicode box drawing needs utf-8",
    ))
    out.append(_finding(
        "ok", "terminal", "colour",
        "enabled" if term.supports_color(sys.stdout)
        else "disabled (not a tty, or NO_COLOR is set)",
    ))
    for name, value in (("NO_COLOR", os.environ.get("NO_COLOR")),
                        ("TERM", os.environ.get("TERM")),
                        ("FORCE_COLOR", os.environ.get("FORCE_COLOR"))):
        if value:
            out.append(_finding("ok", "terminal", name, value))
    if os.name == "nt":
        out.append(_finding("ok", "terminal", "vt",
                            "Windows: ANSI enabled at start-up when supported"))
    return out


def _config(ctx: Context) -> list[dict]:
    out = []
    stack = ctx.config
    if stack.file_path:
        out.append(_finding("ok", "config", "file", str(stack.file_path)))
    else:
        out.append(_finding("warn", "config", "file",
                            "no config file found (anvil config init to create one)"))
    try:
        data = stack.resolve()
        out.append(_finding("ok", "config", "keys", f"{len(data)} resolved"))
        from anvil.core.config import flatten

        secrets = [k for k in flatten(data) if _is_secret(k)]
        if secrets:
            out.append(_finding("warn", "config", "secrets",
                                f"{len(secrets)} key(s) look like credentials; "
                                "they are redacted in 'config list'"))
    except Exception as exc:
        out.append(_finding("fail", "config", "load", str(exc)))
    return out


def _is_secret(key: str) -> bool:
    from anvil.core.config import looks_secret

    return looks_secret(key.rsplit(".", 1)[-1])


def _parser(ctx: Context) -> list[dict]:
    """Check the parser tree for the mistakes that are easy to make."""
    import argparse

    out = []
    try:
        parser = build_parser(prog=ctx.prog)
    except Exception as exc:
        return [_finding("fail", "parser", "build", str(exc))]
    out.append(_finding("ok", "parser", "build", f"{len(parser._actions)} actions"))

    dupes: dict[str, int] = {}
    for action in parser._actions:
        for flag in action.option_strings:
            dupes[flag] = dupes.get(flag, 0) + 1
    repeated = sorted(f for f, n in dupes.items() if n > 1)
    out.append(_finding(
        "fail" if repeated else "ok", "parser", "duplicate flags",
        ", ".join(repeated) if repeated else "none",
    ))

    undocumented = sorted(
        action.dest
        for action in parser._actions
        if action.option_strings
        and action.help is None
        and action.help is not argparse.SUPPRESS
        and not isinstance(action, argparse._HelpAction)
    )
    out.append(_finding(
        "ok" if not undocumented else "warn", "parser", "undocumented",
        ", ".join(undocumented) if undocumented else "every option has help",
    ))
    return out


def _completions(ctx: Context) -> list[dict]:
    from anvil.core import completion

    out = []
    for shell in completion.SHELLS:
        path = completion.completion_paths(shell, ctx.prog)
        found = completion.which(shell)
        out.append(_finding(
            "ok" if path.exists() else "warn", "completions", shell,
            f"installed at {path}" if path.exists() else f"not installed ({path})",
        ))
        if not found:
            out.append(_finding("warn", "completions", f"{shell} binary",
                                "not on PATH; completion cannot be exercised"))
    return out


def _commands(ctx: Context) -> list[dict]:
    from anvil.commands import MODULES, load
    from anvil.core.app import REGISTRY

    load()
    out = [_finding("ok", "commands", "registered", f"{len(REGISTRY)} commands")]
    missing = [name for name, module in MODULES.items() if name not in REGISTRY]
    out.append(_finding(
        "fail" if missing else "ok", "commands", "coverage",
        f"modules without a registered command: {missing}" if missing
        else "every module registers a command",
    ))
    orphans = [c.name for c in REGISTRY if c.name not in MODULES]
    out.append(_finding(
        "warn" if orphans else "ok", "commands", "registry",
        f"commands without a module: {orphans}" if orphans else "registry matches",
    ))
    out.append(_finding("ok", "commands", "version", f"anvil {VERSION}"))
    return out

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _doctor
