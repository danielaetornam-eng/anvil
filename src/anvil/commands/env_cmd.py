"""``anvil env`` -- inspect, compare, and validate environment variables."""

from __future__ import annotations

import argparse
import os

from anvil.core import term
from anvil.core.app import Context
from anvil.core.errors import InputError, NotFoundError
from anvil.core.io import read_text
from anvil.core.types import env_var_name, existing_path

HELP = "inspect, compare and validate environment variables"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "env", aliases=list(aliases), help=HELP,
        description="Work with environment variables.\n\n"
                    "`--required` makes the exit code depend on presence, which "
                    "is how you gate a deploy on a secret being present without "
                    "printing it.",
    )
    parser.add_examples(
        "anvil env list --prefix ANVIL_",
        "anvil env get HOME",
        "anvil env required API_KEY DATABASE_URL",
        "anvil env compare .env --example .env.example",
    )

    subs = parser.add_subparsers(dest="env_command", metavar="SUBCOMMAND")
    subs.required = True
    _list(subs)
    _get(subs)
    _required(subs)
    _compare(subs)
    _where(subs)
    return parser


def _list(subs) -> None:
    p = subs.add_parser("list", aliases=["ls"], help="list variables",
                        description="List environment variables, optionally "
                                    "filtered and redacted.")
    p.add_argument("--prefix", default=None, metavar="TEXT",
                   help="only names starting with this")
    p.add_argument("--pattern", default=None, metavar="RE",
                   help="only names matching this regular expression")
    p.add_argument("--values", action="store_true", default=False,
                   help="print values (redacted by default)")
    p.add_argument("--no-redact", action="store_true", default=False,
                   help="print values verbatim (unsafe for shared logs)")
    p.add_argument("--sort", action="store_true", default=False,
                   help="sort by name")
    p.add_argument("--reverse", action="store_true", default=False,
                   help="reverse the order")
    p.set_defaults(func=_do_list)


def _get(subs) -> None:
    p = subs.add_parser("get", help="print one variable",
                        description="Print the value of one variable.")
    p.add_argument("--default", default=None, metavar="TEXT",
                   help="printed when unset")
    p.add_argument("--show", action="store_true", default=False,
                   help="print the value even if it looks secret")
    p.add_argument("name", type=env_var_name, metavar="NAME")
    p.set_defaults(func=_do_get)


def _required(subs) -> None:
    p = subs.add_parser("required", help="check that variables are set",
                        description="Exit non-zero if any named variable is "
                                    "unset or empty.")
    p.add_argument("--allow-empty", action="store_true", default=False,
                   help="treat an empty value as present")
    p.add_argument("names", nargs="+", type=env_var_name, metavar="NAME")
    p.set_defaults(func=_do_required)


def _compare(subs) -> None:
    p = subs.add_parser("compare", aliases=["diff"], help="compare with a file",
                        description="Compare current variables against a "
                                    "KEY=VALUE file.")
    p.add_argument("--example", type=existing_path, default=None, metavar="PATH",
                   help="the file of expected keys")
    p.add_argument("--values", action="store_true", default=False,
                   help="also report value differences")
    p.add_argument("file", type=existing_path, metavar="FILE",
                   help="a KEY=VALUE file to compare against the environment")
    p.set_defaults(func=_do_compare)


def _where(subs) -> None:
    p = subs.add_parser("where", help="show which variables come from where",
                        description="Report the source of each variable: the "
                                    "shell, the anvil config, or a dotenv file.")
    p.add_argument("--names", type=_split, default=None, metavar="A,B",
                   help="only these variables")
    p.add_argument("--config", action="store_true", default=False,
                   help="also show config-supplied values")
    p.set_defaults(func=_do_where)


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #


def _do_list(ctx: Context, args: argparse.Namespace) -> int:
    from anvil.core.config import looks_secret

    items = list(os.environ.items())
    if args.prefix:
        items = [(k, v) for k, v in items if k.startswith(args.prefix)]
    if args.pattern:
        import re

        compiled = re.compile(args.pattern)
        items = [(k, v) for k, v in items if compiled.search(k)]
    if args.sort:
        items.sort(reverse=args.reverse)

    rows = []
    for name, value in items:
        if args.values and not (args.no_redact or not looks_secret(name)):
            shown = f"<{len(value)} chars>"
        elif args.values:
            shown = value
        else:
            shown = ""
        rows.append({"name": name, "value": shown})
    if not rows:
        ctx.err(term.A.hint("no matching variables"))
        return 0
    ctx.render(ctx.table().add_dicts(rows))
    return 0


def _do_get(ctx: Context, args: argparse.Namespace) -> int:
    from anvil.core.config import looks_secret

    value = os.environ.get(args.name)
    if value is None:
        if args.default is None:
            raise NotFoundError(f"{args.name} is not set",
                                hint=f"export {args.name}=... first")
        ctx.out(args.default)
        return 0
    if looks_secret(args.name) and not args.show:
        ctx.out(f"<{len(value)} chars>")
        return 0
    ctx.out(value)
    return 0


def _do_required(ctx: Context, args: argparse.Namespace) -> int:
    missing = []
    for name in args.names:
        value = os.environ.get(name)
        if value is None or (value == "" and not args.allow_empty):
            missing.append(name)
    if missing:
        raise InputError(
            f"required variable(s) not set: {', '.join(missing)}",
            hint="set them, or unset --allow-empty for zero-length values",
        )
    ctx.out(ctx.renderer.scalar({"ok": True, "names": args.names}))
    return 0


def _parse_env_file(text: str, source: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            raise InputError(f"{source}:{number}: expected KEY=VALUE, got {line!r}")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _do_compare(ctx: Context, args: argparse.Namespace) -> int:
    from anvil.core.config import looks_secret

    expected = _parse_env_file(read_text(args.example), str(args.example)) \
        if args.example else {}
    actual = _parse_env_file(read_text(args.file), str(args.file))

    rows = []
    for name in sorted(set(expected) | set(actual)):
        in_expected = name in expected
        in_actual = name in actual
        status = "ok" if in_expected == in_actual else (
            "extra" if in_actual else "missing"
        )
        row = {"name": name, "status": status,
               "in_file": "yes" if in_actual else "no",
               "in_example": "yes" if in_expected else "no"}
        if args.values and in_actual and in_expected:
            row["values_match"] = (
                "yes" if actual[name] == expected[name] else "no"
            )
            if row["values_match"] == "no" and not looks_secret(name):
                row["file_value"] = actual[name]
                row["example_value"] = expected[name]
        rows.append(row)

    ctx.tabular()
    table = term.Table([term.Column("name", "name"), term.Column("status", "status"),
                        term.Column("in_file", "in file"),
                        term.Column("in_example", "in example")])
    for row in rows:
        table.add_row([row["name"], row["status"], row["in_file"], row["in_example"]])
    ctx.render(table)

    problems = [r for r in rows if r["status"] != "ok"]
    if problems:
        ctx.err(term.A.warn(f"{len(problems)} key(s) differ"))
        return 1
    return 0


def _do_where(ctx: Context, args: argparse.Namespace) -> int:
    rows = []
    names = args.names or sorted(os.environ)
    for name in names:
        rows.append({
            "name": name,
            "shell": "set" if name in os.environ else "unset",
            "config": ctx.config.get(name.lower().replace("-", "_")),
        })
    ctx.render(ctx.table().add_dicts(rows))
    return 0

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _do_list
