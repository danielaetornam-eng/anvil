"""Command discovery and the root parser's prose.

Commands are ordinary modules in this package. Each exposes ``register(sub,
parent)`` returning its subparser; :func:`register` and :func:`decorate` wire
them onto the root parser in a fixed, deliberate order.

Importing every module eagerly is the right trade here: a CLI with tens of
commands starts in single-digit milliseconds because the standard library is
already imported, and lazy discovery would only buy back microseconds while
adding a failure mode (a typo in a module name becomes a runtime error).
"""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Sequence

from ..core.app import REGISTRY, Command, register_command  # noqa: F401  (public re-export)

DESCRIPTION = """\
**anvil** is a developer's toolbox behind one consistent command grammar.

HTTP, JSON, hashing, benchmarking, diffing, secret generation, and more --
all with the same flags, the same error messages, and the same exit codes.
Every option is documented by `--help`, and `--help` is also machine readable.
"""

EPILOG = """\
Exit codes:  0 ok | 2 usage | 65 bad data | 66 missing input | 69 unavailable
             70 internal error | 77 permission | 78 bad config | 130 interrupted

Run 'anvil explain <pattern>' to learn the argparse technique behind any command.
"""

EXAMPLES = (
    "anvil json query '.users[] | select(.age > 30)' users.json",
    "anvil http get https://api.example.com/health --retry 3 --write-out '%{http_code}'",
    "anvil run --parallel --jobs 8 build test lint",
    "anvil hash sha256 --stdin < big.iso",
    "anvil table --from json data.json --style markdown",
    "anvil docs --format json | jq '.commands | keys'",
    "anvil completions install bash",
)

#: ``command name -> (module, aliases, help)``. Order is help order, and this
#: single table is the only place a command needs to be declared: the registry,
#: the ``--help`` listing, and the shell completions are all derived from it.
#: A module supplies its handler either with the ``@register_command``
#: decorator or by exposing a module-level ``HANDLER`` attribute.
COMMANDS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "run": ("run", (), "run tasks as shell commands, in parallel or in sequence"),
    "http": ("http", (), "make HTTP requests with retries, redirects and output templates"),
    "json": ("json_cmd", (), "query, validate, merge, diff and reshape JSON"),
    "table": ("table", (), "render JSON/CSV/YAML data as a formatted table"),
    "hash": ("hash", (), "hash and verify file digests"),
    "bench": ("bench", (), "time a command and report latency percentiles"),
    "diff": ("diff", (), "diff files, directories, or streams"),
    "serve": ("serve", ("server",), "serve a directory over HTTP"),
    "watch": ("watch", ("rerun",), "rerun a command whenever files change"),
    "secrets": ("secrets", (), "generate passwords, tokens, UUIDs and ULIDs"),
    "text": ("text", (), "case, pad, wrap, number and filter text lines"),
    "encode": ("encode", ("enc",), "base64, hex, url-encode, JSON-escape and more"),
    "template": ("template", ("tpl",), "render ${name} templates"),
    "config": ("config_cmd", (), "inspect and edit anvil's configuration"),
    "env": ("env_cmd", (), "inspect, compare and validate environment variables"),
    "doctor": ("doctor", (), "check the environment for problems"),
    "docs": ("docs", (), "dump the parser tree as JSON, Markdown, or a stub"),
    "completions": ("completions_cmd", (), "install shell completions"),
    "mktemp": ("mktemp", ("tempfile",), "create a temporary file or directory"),
    "retry": ("retry", ("retry-until",), "retry a command with exponential backoff"),
    "prompt": ("prompt", ("ask",), "ask the user a question, safely in scripts too"),
    "explain": ("explain", (), "learn the argparse pattern behind any command"),
}

#: Backwards-friendly view: command name -> implementing module.
MODULES: dict[str, str] = {name: entry[0] for name, entry in COMMANDS.items()}

_LOADED = False


def load(force: bool = False) -> None:
    """Import every command module once and populate the registry."""
    global _LOADED
    if _LOADED and not force:
        return
    for name, (module_name, aliases, _help) in COMMANDS.items():
        module = importlib.import_module(f"{__name__}.{module_name}")
        if name in REGISTRY:
            continue
        handler = getattr(module, "HANDLER", None)
        if handler is None:
            raise RuntimeError(
                f"module {module_name!r} exposes no HANDLER for command {name!r}"
            )
        REGISTRY.register(name, handler, aliases=aliases)
    _LOADED = True


def register(subparsers: argparse._SubParsersAction, command: Command,
             *, prog: str) -> argparse.ArgumentParser:
    """Attach one :class:`Command` to the root subparsers."""
    module = importlib.import_module(f"{__name__}.{MODULES[command.name]}")
    return module.register(subparsers, prog=prog, aliases=command.aliases,
                           tags=command.tags)


def decorate(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Post-processing hook for the root parser (aliases, groups, aliases)."""
    load()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action.required = True
            break
    return parser


def available() -> Sequence[str]:
    """Command names in help order, after loading."""
    load()
    return REGISTRY.names()
