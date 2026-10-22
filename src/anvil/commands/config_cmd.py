"""``anvil config`` -- read and write layered configuration."""

from __future__ import annotations

import argparse
import json as _json
import os
import pathlib

from anvil.core import config as cfg
from anvil.core import term
from anvil.core.actions import AtomicOutput
from anvil.core.app import Context
from anvil.core.errors import ConfigError, NotFoundError
from anvil.core.io import write_output

HELP = "inspect and edit anvil's configuration"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "config", aliases=list(aliases), help=HELP,
        description="Read and write configuration.\n\n"
                    "Precedence, lowest first: built-in defaults, config file, "
                    "environment, command-line flags.",
    )
    parser.add_examples(
        "anvil config list --explain",
        "anvil config get http.timeout",
        "anvil config set http.timeout 45s",
        "anvil config path",
    )

    subs = parser.add_subparsers(dest="config_command", metavar="SUBCOMMAND")
    subs.required = True
    _list(subs)
    _get(subs)
    _set(subs)
    _unset(subs)
    _path(subs)
    _init(subs)
    _export(subs)
    return parser


def _list(subs) -> None:
    p = subs.add_parser("list", aliases=["ls", "show"], help="show all settings",
                        description="Print every resolved setting.")
    p.add_argument("--explain", action="store_true", default=False,
                   help="show which layer supplied each value")
    p.add_argument("--show-secrets", action="store_true", default=False,
                   help="do not redact values that look like credentials")
    p.add_argument("--section", default=None, metavar="NAME",
                   help="limit to one section")
    p.set_defaults(func=_do_list)


def _get(subs) -> None:
    p = subs.add_parser("get", help="read one setting",
                        description="Print the value of a dotted key.")
    p.add_argument("--default", default=None, metavar="VALUE",
                   help="printed when the key is unset")
    p.add_argument("--source", action="store_true", default=False,
                   help="also print the layer that supplied it")
    p.add_argument("key", metavar="KEY", help="dotted key, e.g. http.timeout")
    p.set_defaults(func=_do_get)


def _set(subs) -> None:
    p = subs.add_parser("set", help="write one setting",
                        description="Write a dotted key to the config file.")
    p.add_argument("--global", dest="global_", action="store_true", default=False,
                   help="write to the user config instead of ./anvil.toml")
    p.add_argument("--json", dest="as_json", action="store_true", default=False,
                   help="parse the value as JSON")
    p.add_argument("--append", action="store_true", default=False,
                   help="append to a list instead of replacing")
    p.add_argument("key", metavar="KEY")
    p.add_argument("value", metavar="VALUE")
    p.set_defaults(func=_do_set)


def _unset(subs) -> None:
    p = subs.add_parser("unset", aliases=["rm"], help="delete one setting",
                        description="Remove a dotted key from the config file.")
    p.add_argument("--global", dest="global_", action="store_true", default=False,
                   help="operate on the user config instead of the local one")
    p.add_argument("key", metavar="KEY")
    p.set_defaults(func=_do_unset)


def _path(subs) -> None:
    p = subs.add_parser("path", help="print the config file in use",
                        description="Print the path of the config file that "
                                    "would be loaded, if any.")
    p.set_defaults(func=_do_path)


def _init(subs) -> None:
    p = subs.add_parser("init", help="create a starter config file",
                        description="Write a documented starter config.")
    p.add_argument("--format", choices=("toml", "json"), default="toml",
                   help="file format")
    p.add_argument("--force", action="store_true", default=False,
                   help="overwrite an existing file")
    p.add_argument("path", nargs="?", default="anvil.toml", metavar="PATH")
    p.set_defaults(func=_do_init)


def _export(subs) -> None:
    p = subs.add_parser("export", help="emit resolved settings as a file",
                        description="Write the fully resolved configuration out "
                                    "so another tool can consume it.")
    p.add_argument("--include-secrets", action="store_true", default=False,
                   help="do not redact secret-looking values")
    p.add_argument("--merge", action="store_true", default=False,
                   help="include every layer instead of just the merged result")
    p.add_argument("out", nargs="?", default="-", metavar="PATH",
                   help="destination, or - for stdout")
    p.set_defaults(func=_do_export)


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #


def _do_list(ctx: Context, args: argparse.Namespace) -> int:
    stack = ctx.config
    if args.show_secrets:
        # Work from the raw merged data. ``explain()`` redacts, so re-reading it
        # would hand back the placeholders rather than the real values.
        rows = [(key, value, stack.source(key) or "?")
                for key, value in sorted(cfg.flatten(stack.data).items())]
    else:
        rows = stack.explain()
    if args.section:
        prefix = f"{args.section}."
        rows = [r for r in rows if r[0].startswith(prefix)]
    if not args.explain:
        rows = [(key, value, "") for key, value, _ in rows]
    ctx.tabular()
    table = term.Table(
        [term.Column("key", "key"), term.Column("value", "value")]
        + ([term.Column("source", "from")] if args.explain else []),
    )
    for row in rows:
        table.add_row(list(row))
    if not table:
        ctx.err(term.A.hint("no settings configured"))
        return 0
    ctx.render(table)
    if stack.file_path:
        ctx.err(term.A.hint(f"from {stack.file_path}"))
    return 0


def _do_get(ctx: Context, args: argparse.Namespace) -> int:
    value = ctx.config.get(args.key)
    if value is None:
        value = args.default
    if value is None:
        raise NotFoundError(f"{args.key} is not set",
                            hint=f"run 'anvil config set {args.key} VALUE'")
    if args.source:
        ctx.out(ctx.renderer.scalar({
            args.key: value,
            "from": ctx.config.source(args.key) or "unset",
        }))
    else:
        ctx.out(ctx.renderer.scalar(value))
    return 0


def _do_set(ctx: Context, args: argparse.Namespace) -> int:
    target = _target(ctx, args)
    data = _read(target)
    value = cfg.coerce(args.value) if args.as_json else args.value
    if args.append:
        existing = cfg.dot_get(data, args.key)
        current = list(existing) if isinstance(existing, list) else ([existing]
                                                                    if existing else [])
        current.append(value)
        value = current
    if ctx.dry_run:
        ctx.err(term.A.hint(f"would set {args.key} in {target}"))
        ctx.out(ctx.renderer.scalar({args.key: value}))
        return 0
    cfg.dot_set(data, args.key, value)
    _write(target, data)
    ctx.err(term.A.ok(f"set {args.key} in {target}"))
    ctx.out(ctx.renderer.scalar({args.key: value}))
    return 0


def _do_unset(ctx: Context, args: argparse.Namespace) -> int:
    target = _target(ctx, args)
    data = _read(target)
    if not _drop(data, args.key):
        raise NotFoundError(f"{args.key} is not set in {target}")
    if ctx.dry_run:
        ctx.err(term.A.hint(f"would remove {args.key} from {target}"))
        return 0
    _write(target, data)
    ctx.err(term.A.ok(f"removed {args.key} from {target}"))
    return 0


def _drop(data: dict, path: str) -> bool:
    parts = path.split(".")
    current: object = data
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    if isinstance(current, dict):
        return current.pop(parts[-1], _SENTINEL) is not _SENTINEL
    return False


def _do_path(ctx: Context, args: argparse.Namespace) -> int:
    path = ctx.config.file_path
    if path is None:
        ctx.err(term.A.hint("no config file found; 'anvil config init' to create one"))
        return 1
    ctx.out(str(path))
    return 0


_STARTER_TOML = """\
# anvil configuration
#
# Precedence, lowest first:
#   built-in defaults  <  this file  <  $ANVIL_* env vars  <  command-line flags
#
# Override any key for one invocation with:  anvil --set http.timeout=10s ...

[http]
timeout = "30s"
retries = 3
user-agent = "anvil/1.0"

[table]
style = "unicode"
max-width = 100

[output]
format = "auto"

[logging]
level = "warning"
"""

_STARTER_JSON = """\
{
  "http": {"timeout": "30s", "retries": 3, "user-agent": "anvil/1.0"},
  "table": {"style": "unicode", "max-width": 100},
  "output": {"format": "auto"},
  "logging": {"level": "warning"}
}
"""


def _do_init(ctx: Context, args: argparse.Namespace) -> int:
    target = pathlib.Path(args.path).expanduser()
    if (target.exists() and not args.force
            and not ctx.confirm(f"{target} exists; overwrite?", default=False)):
        ctx.err(term.A.hint("cancelled"))
        return 1
    body = _STARTER_JSON if args.format == "json" else _STARTER_TOML
    if ctx.dry_run:
        ctx.out(body)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    ctx.err(term.A.ok(f"wrote {target}"))
    return 0


def _do_export(ctx: Context, args: argparse.Namespace) -> int:
    stack = ctx.config
    if args.merge:
        payload = {name: layer for name, layer in stack.layers().items() if layer}
    else:
        payload = stack.data
    if not args.include_secrets:
        payload = cfg.redact(payload)
    if args.out == "-" or str(args.out) == "-":
        ctx.out(ctx.renderer.scalar(payload))
        return 0
    target = pathlib.Path(args.out).expanduser()
    text = (_json.dumps(payload, indent=2, default=str) if target.suffix == ".json"
            else _toml_dump(payload))
    write_output(target, text + "\n")
    ctx.err(term.A.ok(f"wrote {target}"))
    return 0


def _toml_dump(data: dict) -> str:
    """Serialise a nested mapping to TOML.

    Scalars are emitted before sub-tables at each level, because TOML requires
    a table's own keys to precede its ``[header]``. An empty table is written as
    an explicit header since TOML has no other way to express it.
    """

    def render(node: dict, prefix: str) -> list[str]:
        lines: list[str] = []
        scalars = {k: v for k, v in node.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in node.items() if isinstance(v, dict)}
        for key, value in scalars.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in tables.items():
            name = f"{prefix}{_toml_key(key)}"
            lines.append("")
            if not value:
                lines.append(f"[{name}]")
                continue
            lines.append(f"[{name}]")
            lines += render(value, f"{name}.")
        return lines

    return "\n".join(render(data, "")).strip() + "\n"


def _toml_key(key: object) -> str:
    text = str(key)
    if text and all(c.isalnum() or c in "-_" for c in text):
        return text
    return _json.dumps(text)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return '""'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _json.dumps(str(value))



# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #


def _target(ctx: Context, args: argparse.Namespace) -> pathlib.Path:
    if getattr(args, "global_", False):
        return _user_config_path(ctx.config)
    explicit = ctx.option("config")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    existing = ctx.config.file_path
    if existing:
        return existing
    return pathlib.Path.cwd() / "anvil.toml"


def _user_config_path(stack: cfg.Config) -> pathlib.Path:
    env = stack.environ
    xdg = env.get("XDG_CONFIG_HOME")
    root = pathlib.Path(xdg).expanduser() if xdg else pathlib.Path.home() / ".config"
    if os.name == "nt":
        appdata = env.get("APPDATA")
        if appdata:
            root = pathlib.Path(appdata).expanduser()
    return root / "anvil" / "config.toml"


def _read(target: pathlib.Path) -> dict:
    if not target.exists():
        return {}
    try:
        return cfg.load(target)
    except ConfigError as exc:
        raise ConfigError(f"{target}: {exc.message}", hint=exc.hint) from None


def _write(target: pathlib.Path, data: dict) -> None:
    if target.suffix.lower() == ".json":
        text = _json.dumps(data, indent=2, default=str) + "\n"
    else:
        text = _toml_dump(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    with AtomicOutput(target) as handle:
        handle.write(text)


_SENTINEL = object()

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _do_list
