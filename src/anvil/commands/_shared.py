"""Shared argument-validation helpers for the command-execution commands.

``run``, ``bench`` and ``retry`` all take a command to execute and all hit the
same two failure modes: an unquoted command, and ``--`` used in the wrong place.
Reporting them the same way is the point of putting them here.
"""

from __future__ import annotations

import argparse
import shlex

__all__ = ["had_separator", "join_command", "require_exec_form"]


def join_command(command: list[str]) -> str:
    """Render argv back into a copy-pasteable command line."""
    return shlex.join(command)


def require_exec_form(args: argparse.Namespace, command: list[str],
                      *, verb: str = "run", separated: bool = False) -> None:
    """Reject a command that will not work when executed directly.

    ``anvil bench python -c pass`` looks reasonable, but without a ``--``
    separator argparse reads ``-c`` as *bench's own* option and hands over
    ``["python"]``, and with ``--`` the exec'd program would be the literal
    string ``"python -c pass"`` -- a confusing ENOENT. When ``--`` was used the
    user has already disambiguated, so the check is skipped.
    """
    from ..core.errors import UsageError

    if args.shell is not None or separated:
        return
    head = command[0]
    if " " in head or "\t" in head:
        raise UsageError(
            f"{head!r} is not an executable name",
            hint=(f"quote the whole command ({join_command(command)!r}), "
                  f"or pass --shell to interpret it"),
        )
    if len(command) > 1 and command[1].startswith("-"):
        raise UsageError(
            f"{command[1]!r} would be read as {verb}'s own option",
            hint=f"separate the command with -- : anvil {verb} -- {head}",
        )


def had_separator(command: list[str]) -> bool:
    """True when the caller passed a bare ``--`` before the command."""
    return bool(command) and command[0] == "--"
