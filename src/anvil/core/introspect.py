"""Reflect the live parser tree into machine-readable data.

``--help`` is a program, not a document: it is the only description of the CLI
that can never drift from the implementation. Turning that same tree into JSON
gets you documentation generation, shell completion, config-file schemas, test
assertions about the interface, and wrapper scripts that need to discover the
real grammar -- all derived from one source of truth.

Everything here reads argparse's documented action attributes and tolerates
their absence, so it keeps working as the internals move between versions.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from typing import Any

__all__ = [
    "ParserNode",
    "actions_of",
    "env_var_for",
    "iter_commands",
    "to_dict",
    "walk",
]

_ENV_PARTIALS = ("token", "key", "secret", "password", "auth", "user")


def _nargs_repr(action: argparse.Action) -> Any:
    nargs = action.nargs
    if nargs is None:
        return 1
    if nargs is argparse.REMAINDER:
        return "..."
    if nargs is argparse.PARSER:
        return "command"
    if nargs is argparse.OPTIONAL:
        return "?"
    if nargs is argparse.ZERO_OR_MORE:
        return "*"
    if nargs is argparse.ONE_OR_MORE:
        return "+"
    return nargs


def _type_name(action: argparse.Action) -> str:
    converter = action.type
    if converter is None:
        return "str" if action.nargs != 0 else "flag"
    return getattr(converter, "__name__", None) or type(converter).__name__


def _default_repr(action: argparse.Action) -> Any:
    default = action.default
    if default is argparse.SUPPRESS:
        return None
    if callable(default):
        return "<callable>"
    if isinstance(default, (str, int, float, bool)) or default is None:
        return default
    if isinstance(default, (list, tuple, set)):
        return sorted(str(v) for v in default)
    return str(default)


def env_var_for(prog: str, action: argparse.Action) -> str | None:
    """The environment variable that overrides *action*, if the CLI declares one.

    An action opts in by carrying ``anvil_env``; otherwise one is derived from
    the program and the option name so that ``anvil run --jobs`` implies
    ``ANVIL_RUN_JOBS``.
    """
    explicit = getattr(action, "anvil_env", None)
    if explicit:
        return explicit
    if not action.option_strings:
        return None
    longest = max(action.option_strings, key=len)
    if not longest.startswith("--"):
        return None
    command = _command_segment(prog)
    parts = [p for p in (command, longest.lstrip("-").replace("-", "_")) if p]
    return "ANVIL_" + "_".join(parts).upper()


def _command_segment(prog: str) -> str:
    """``anvil run`` -> ``run``; ``anvil`` -> ``""``."""
    stem = prog.replace("\\", "/").rsplit("/", 1)[-1]
    parts = stem.split()
    return parts[1] if len(parts) > 1 else ""


def actions_of(node: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    """Yield every action attached to *node*, including nested group members."""
    for action in node._actions:
        if action.help is argparse.SUPPRESS:
            continue
        yield action


def _find_group(node: argparse.ArgumentParser, action: argparse.Action) -> str:
    for group in node._action_groups:
        if action in group._group_actions:
            return group.title or "options"
    return "options"


def _mutually_exclusive(node: argparse.ArgumentParser, action: argparse.Action) -> bool:
    return any(action in group._group_actions
               for group in node._mutually_exclusive_groups)


def _describe(node: argparse.ArgumentParser, action: argparse.Action) -> dict[str, Any]:
    is_flag = action.nargs == 0 or isinstance(
        action, (argparse._StoreTrueAction, argparse._StoreFalseAction,
                 argparse._StoreConstAction, argparse._CountAction,
                 argparse._VersionAction)
    )
    entry: dict[str, Any] = {
        "name": action.dest,
        "flags": list(action.option_strings),
        "help": action.help if action.help is not argparse.SUPPRESS else None,
        "metavar": action.metavar or (None if is_flag else _type_name(action).upper()),
        "type": None if is_flag else _type_name(action),
        "nargs": _nargs_repr(action),
        "positional": not action.option_strings,
        "required": bool(action.required and action.option_strings),
        "default": _default_repr(action),
        "group": _find_group(node, action),
        "env": env_var_for(node.prog, action),
    }
    if action.choices:
        entry["choices"] = [str(c) for c in action.choices]
    if action.const is not None:
        entry["const"] = action.const
    if _mutually_exclusive(node, action):
        entry["exclusive"] = True
    if getattr(action, "deprecated", False):
        entry["deprecated"] = True
    for attribute in ("anvil_deprecated", "anvil_replacement", "anvil_since"):
        value = getattr(action, attribute, None)
        if value:
            entry[attribute] = value
    if isinstance(action, argparse._SubParsersAction):
        entry["subcommands"] = sorted(action.choices)
    return entry


def _subparser_action(node: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in node._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def to_dict(node: argparse.ArgumentParser, *, depth: int = 0,
            max_depth: int = 6, include_defaults: bool = True) -> dict[str, Any]:
    """Serialise one parser (and, by default, its whole subtree)."""
    sub_action = _subparser_action(node)
    positionals: list[dict[str, Any]] = []
    optionals: list[dict[str, Any]] = []
    for action in actions_of(node):
        if action is sub_action:
            continue
        described = _describe(node, action)
        (optionals if action.option_strings else positionals).append(described)

    result: dict[str, Any] = {
        "prog": node.prog,
        "description": node.description,
        "epilog": node.epilog,
        "usage": node.format_usage().strip(),
        "positionals": positionals,
        "options": optionals,
        "has_subcommands": sub_action is not None,
    }
    if not include_defaults:
        for entry in positionals + optionals:
            entry.pop("default", None)

    if sub_action is not None and depth < max_depth:
        result["subcommands"] = {
            name: to_dict(child, depth=depth + 1, max_depth=max_depth,
                          include_defaults=include_defaults)
            for name, child in sub_action.choices.items()
        }
    return result


class ParserNode:
    """A light wrapper for walking a parser tree as objects.

    ``node.command`` gives the command name, ``node.children`` the next level,
    and ``node.path`` the full ``anvil json query`` style breadcrumb -- which is
    exactly what completion scripts and doc generators need.
    """

    def __init__(self, parser: argparse.ArgumentParser, name: str = "",
                 parent: ParserNode | None = None) -> None:
        self.parser = parser
        self.name = name
        self.parent = parent
        self.children: dict[str, ParserNode] = {}
        action = _subparser_action(parser)
        if action is not None:
            for child_name, child in action.choices.items():
                self.children[child_name] = ParserNode(child, child_name, self)

    @property
    def path(self) -> str:
        if self.parent is None:
            return self.parser.prog.split()[0]
        return f"{self.parent.path} {self.name}"

    @property
    def command(self) -> str:
        return self.name or self.path.split()[-1]

    @property
    def help(self) -> str:
        for action in self.parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for pseudo in action._choices_actions:
                    if pseudo.dest == self.name:
                        return pseudo.help or ""
        return ""

    def walk(self, depth: int = 0, max_depth: int = 8) -> Iterator[ParserNode]:
        yield self
        if depth >= max_depth:
            return
        for child in self.children.values():
            yield from child.walk(depth + 1, max_depth)

    def find(self, *parts: str) -> ParserNode | None:
        node: ParserNode | None = self
        for part in parts:
            if node is None:
                return None
            node = node.children.get(part)
        return node

    def option_flags(self) -> list[str]:
        flags: list[str] = []
        for action in self.parser._actions:
            if action.help is argparse.SUPPRESS:
                continue
            if isinstance(action, argparse._SubParsersAction):
                continue
            flags.extend(action.option_strings)
        return flags

    def __repr__(self) -> str:
        return f"<ParserNode {self.path!r} children={len(self.children)}>"


def walk(node: ParserNode, max_depth: int = 8) -> Iterator[ParserNode]:
    """Depth-first walk over a :class:`ParserNode` tree."""
    yield from node.walk(0, max_depth)


def iter_commands(node: ParserNode) -> Iterator[ParserNode]:
    """Every *command* (depth >= 1), excluding the root parser."""
    for item in node.walk():
        if item.parent is not None:
            yield item
