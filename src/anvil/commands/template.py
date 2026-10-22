"""``anvil template`` -- render ``${name}`` templates with strict mode."""

from __future__ import annotations

import argparse
import os
import re

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, UsageError
from anvil.core.io import read_text
from anvil.core.types import any_path, existing_path, output_path

HELP = "render ${name} templates from flags, files, or the environment"

DELIMITERS = ("dollar", "brace", "at", "percent")


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "template", aliases=["tpl"], help=HELP,
        description="Substitute variables into text.\n\n"
                    "Resolves from, in order: --var, --file, the environment, "
                    "then defaults. Unresolved names are an error in --strict "
                    "mode, which is the difference between a template engine "
                    "you can trust in CI and one that silently emits blanks.",
    )
    parser.add_examples(
        "anvil template --var name=ada --var city=london 'Hello ${name}'",
        "anvil template --file values.env --strict template.txt",
        "anvil template -f t.tmpl -o out.txt --var x=1",
        "cat t.txt | anvil template --env",
    )

    source = parser.add_argument_group("Sources")
    source.add_argument("--var", "-v", action="key_value", dest="vars",
                        default={}, metavar="K=V", help="set a variable")
    source.add_argument("--set", action="key_value", dest="set_vars", default={},
                        metavar="K=V", help="alias for --var")
    source.add_argument("--file", "-f", type=any_path, default=None,
                        metavar="PATH", help="template file; - for stdin")
    source.add_argument("--values", type=existing_path, default=None,
                        metavar="PATH", help="load K=V pairs from a file")
    source.add_argument("--json-values", type=existing_path, default=None,
                        metavar="PATH", help="load variables from a JSON file")
    source.add_argument("--env", action="store_true", default=False,
                        help="expose the whole environment")
    source.add_argument("--prefix", default="", metavar="TEXT",
                        help="expose the environment under this prefix")
    source.add_argument("--null", "-z", action="store_true", default=False,
                        help="treat an unset variable as empty rather than an error")

    render = parser.add_argument_group("Rendering")
    render.add_argument("--strict", action="store_true", default=False,
                        help="fail on an unresolved variable")
    render.add_argument("--keep-unset", action="store_true", default=False,
                        help="leave ${name} in the output untouched")
    render.add_argument("--default", default=None, metavar="TEXT",
                        help="value used when a variable is unset")
    render.add_argument("--delimiter", choices=DELIMITERS, default="dollar",
                        metavar="KIND",
                        help="variable syntax (default: ${name})")
    render.add_argument("--indent", type=int, default=None, metavar="N",
                        help="re-indent the result")
    render.add_argument("--output", "-o", type=output_path, default=None,
                        metavar="FILE", help="write to a file")
    parser.add_argument("template", nargs="?", default=None, metavar="TEMPLATE",
                        help="the template text; omit to read stdin")
    parser.set_defaults(func=_template)
    return parser


@register_command("template")
def _template(ctx: Context, args: argparse.Namespace) -> int:
    template = _read_template(args)
    if not template:
        raise InputError("the template is empty", hint="check your input")

    values: dict[str, str] = {}
    values.update(_from_pairs(args.values))
    values.update(_from_json(args.json_values))
    if args.env:
        prefix = args.prefix
        values.update({f"{prefix}{k}": v for k, v in os.environ.items()})

    supplied = dict(args.vars or {})
    supplied.update(args.set_vars or {})
    values.update(supplied)

    text = _render(ctx, args, template, values)
    if args.indent:
        import textwrap

        text = textwrap.indent(text, " " * args.indent)
    _emit(ctx, args, text)
    return 0


def _read_template(args: argparse.Namespace) -> str:
    if args.template is not None:
        return args.template
    if args.file is None:
        import sys

        if sys.stdin.isatty():
            raise UsageError("no template given",
                             hint="pass it as an argument, or use --file PATH")
        return read_text("-")
    return read_text(args.file)


def _from_pairs(path) -> dict[str, str]:
    if not path:
        return {}
    out: dict[str, str] = {}
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise InputError(f"{path}: line is not K=V: {line!r}")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _from_json(path) -> dict[str, str]:
    if not path:
        return {}
    import json as _json

    try:
        data = _json.loads(read_text(path))
    except _json.JSONDecodeError as exc:
        raise InputError(f"{path}: invalid JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise InputError(f"{path}: expected a JSON object")
    return {str(k): _stringify(v) for k, v in data.items()}


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        import json as _json

        return _json.dumps(value, separators=(",", ":"))
    return str(value)


_PATTERNS = {
    "dollar": re.compile(r"\$\{(\w+)\}|\$(\w+)|\$\{([^}]*)\}"),
    "brace": re.compile(r"\{\{(\w+)\}\}"),
    "at": re.compile(r"@(\w+)@"),
    "percent": re.compile(r"%\((\w+)\)s"),
}


def _render(ctx: Context, args: argparse.Namespace, template: str,
            values: dict[str, str]) -> str:
    pattern = _PATTERNS[args.delimiter]

    def substitute(match: re.Match[str]) -> str:
        name = next((g for g in match.groups() if g is not None), "")
        if name in values:
            return values[name]
        if args.default is not None:
            return args.default
        if args.keep_unset:
            return match.group(0)
        if args.null:
            return ""
        raise InputError(
            f"unresolved variable {name!r}",
            hint=(f"pass --var {name}=VALUE, or --default TEXT"
                  f", or --null to blank it"),
        )

    return pattern.sub(substitute, template)


def _emit(ctx: Context, args: argparse.Namespace, text: str) -> None:
    from anvil.core.io import write_output

    if args.output is None or str(args.output) == "-":
        ctx.out(text)
        return
    write_output(args.output, text + "\n")
    ctx.err(term.A.ok(f"wrote {args.output}"))

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _template
