"""Generate real bash/zsh/fish completions from the live parser tree.

Hand-written completion scripts rot the moment a flag is renamed. These are
*generated*, so they cannot: the script is a by-product of the same
``add_argument`` calls that build ``--help``. The generation happens in
:func:`render` and the installation happens in :func:`install` -- kept apart so
the generation is testable without touching a user's shell profile.

The runtime side is a small ``complete -F``/``compdef`` function that asks the
CLI itself what is valid, which is the only design that stays correct when the
parser tree changes dynamically.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import shutil
import stat
import sys
import tempfile
from collections.abc import Sequence

from .errors import ConfigError
from .introspect import ParserNode

__all__ = [
    "SHELLS",
    "completion_paths",
    "install",
    "render",
    "runtime_script",
]

SHELLS = ("bash", "zsh", "fish")


# --------------------------------------------------------------------------- #
# The runtime: how the shell asks us what is valid
# --------------------------------------------------------------------------- #

_RUNTIME_PY = '''\
"""Completion helper installed by %(prog)s. Generated; edits will be lost."""

import os
import sys

sys.path.insert(0, %(src)r)


def main() -> int:
    argv = sys.argv[1:]
    shell = os.environ.get("ANVIL_COMPLETE_SHELL", "bash")
    words = argv
    current = ""
    if words and words[-1] == "":
        words, current = words[:-1], ""

    from %(module)s import complete
    try:
        for suggestion in complete(words, current, shell=shell):
            print(suggestion)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_BASH = r'''
_anvil_complete() {
    local IFS=$'\n'
    local cur
    cur="${COMP_WORDS[COMP_CWORD]}"
    COMPREPLY=( $(%(helper)s complete "${COMP_WORDS[@]:1}" "$cur" bash 2>/dev/null) )
}
complete -o default -o bashdefault -F _anvil_complete %(prog)s
'''

_ZSH = r'''
#compdef %(prog)s

_anvil_complete() {
    local -a completions
    local line
    local -a words
    words=("${(@)words[2,-1]}")
    words[-1]="${words[-1]}__ANVIL_EMPTY__"
    for line in $(%(helper)s complete "${words[@]}" "" zsh 2>/dev/null); do
        completions+=("$line")
    done
    _describe '%(prog)s' completions
}
compdef _anvil_complete %(prog)s
'''

_FISH = r'''
function __anvil_complete
    set -l tokens (commandline -opc) (commandline -ct)
    set -l current (commandline -ct)
    set -l args (string split " " (string join " " $tokens[2..-1]))
    %(helper)s complete $args $current fish 2>/dev/null
end
complete -c %(prog)s -f -a '(__anvil_complete)'
'''


def runtime_script(prog: str, *, module: str = "anvil.completion_probe",
                   src: str | None = None) -> str:
    """The small Python program the shell calls back into."""
    return _RUNTIME_PY % {
        "prog": prog,
        "module": module,
        "src": src or str(pathlib.Path(__file__).resolve().parents[1]),
    }


def render(shell: str, prog: str = "anvil", *, module: str = "anvil.completion_probe",
           src: str | None = None) -> str:
    """Produce a complete, self-contained completion script for *shell*."""
    if shell not in SHELLS:
        raise ConfigError(
            f"unknown shell {shell!r}",
            hint=f"choose one of {', '.join(SHELLS)}",
        )
    interpreter = sys.executable or "python3"
    # Quote the whole command: the interpreter path and the source directory
    # both routinely contain spaces, and an unquoted one breaks the shell's
    # word splitting.
    helper = " ".join(
        shlex.quote(part) for part in (interpreter, str(completion_probe_path(src)))
    )
    template = {"bash": _BASH, "zsh": _ZSH, "fish": _FISH}[shell]
    # Substitute into the template *before* concatenating: the runtime script
    # is itself %-formatted and contains literal % characters, so formatting
    # the combined string would corrupt it.
    body = template % {"prog": prog, "helper": helper}
    return runtime_script(prog, module=module, src=src) + body


def completion_probe_path(src: str | None = None) -> pathlib.Path:
    """Where the generated probe script is written."""
    base = pathlib.Path(src) if src else pathlib.Path(__file__).resolve().parents[1]
    return base / "completion_probe.py"


# --------------------------------------------------------------------------- #
# Installation
# --------------------------------------------------------------------------- #


def completion_paths(shell: str, prog: str = "anvil",
                     environ: dict[str, str] | None = None) -> pathlib.Path:
    """The conventional install location for *shell* on this machine."""
    env = environ if environ is not None else os.environ
    home = pathlib.Path(env.get("HOME") or pathlib.Path.home())
    if shell == "bash":
        base = env.get("XDG_DATA_HOME")
        root = pathlib.Path(base) if base else home / ".local" / "share"
        return root / "bash-completion" / "completions" / prog
    if shell == "zsh":
        base = env.get("ZDOTDIR")
        root = pathlib.Path(base) if base else home
        return root / ".zsh" / "completions" / ("_" + prog)
    if shell == "fish":
        base = env.get("XDG_CONFIG_HOME")
        root = pathlib.Path(base) if base else home / ".config"
        return root / "fish" / "completions" / (prog + ".fish")
    raise ConfigError(f"unknown shell {shell!r}")


def _shell_rc_hint(shell: str, path: pathlib.Path) -> str:
    if shell == "bash":
        return (f"add this to ~/.bashrc:\n"
                f"    source {path}")
    if shell == "zsh":
        return (f"add this to ~/.zshrc:\n"
                f"    fpath=({path.parent} $fpath)\n"
                f"    autoload -Uz compinit && compinit")
    return (f"fish will pick it up automatically from:\n    {path}")


def install(shell: str, prog: str = "anvil", *, dest: str | pathlib.Path | None = None,
            module: str = "anvil.completion_probe", src: str | None = None,
            dry_run: bool = False) -> tuple[pathlib.Path, pathlib.Path]:
    """Write the probe and the completion script; return both paths.

    Writes to a temporary file and renames, so an interrupted install cannot
    leave a half-written completion script that silently disables completion.
    """
    script = render(shell, prog, module=module, src=src)
    target = pathlib.Path(dest) if dest else completion_paths(shell, prog)
    probe = completion_probe_path(src)
    if not dry_run:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(runtime_script(prog, module=module, src=src), encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", delete=False, dir=str(target.parent), encoding="utf-8",
            suffix=".tmp",
        ) as handle:
            handle.write(script)
            temporary = pathlib.Path(handle.name)
        temporary.replace(target)
        target.chmod(target.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
    return probe, target


def uninstall(shell: str, prog: str = "anvil",
              environ: dict[str, str] | None = None) -> bool:
    """Remove the completion script; returns whether anything was deleted."""
    path = completion_paths(shell, prog, environ)
    if path.exists():
        path.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# The completion engine (used by the probe, and unit-testable directly)
# --------------------------------------------------------------------------- #


def complete(words: Sequence[str], current: str, *, shell: str = "bash",
             prog: str = "anvil") -> list[str]:
    """Return completion candidates for a partially typed command line.

    *words* are the already-typed words *after* the program name; *current* is
    the word under the cursor, which may be empty.
    """
    from .app import build_parser

    root = build_parser(prog=prog)
    node: ParserNode = ParserNode(root)
    typed: list[str] = []
    options: list[str] = []
    only_options = False

    def flush(word: str) -> None:
        nonlocal node
        if word == "--":
            only_options = True
            return
        if not word.startswith("-"):
            child = node.children.get(word)
            if child is not None and not only_options:
                node = child
                typed.append(word)
                return
            return
        options.append(word)

    for word in list(words) + ([current] if current else []):
        if word == current and current:
            options.append(word)
        else:
            flush(word)

    if current.startswith("-"):
        return _filter(node.option_flags() + _global_flags(), current)
    if only_options:
        return _filter(node.option_flags() + _global_flags(), current)

    candidates = list(node.children)
    # Offer positionals of the current level so `anvil js<TAB>` helps.
    positional_hints = [
        action.metavar or action.dest
        for action in node.parser._actions
        if not action.option_strings and action.help is not argparse.SUPPRESS
    ]
    return _filter(candidates + positional_hints, current)


def _global_flags() -> list[str]:
    return ["--help", "--version", "--verbose", "--quiet", "--no-color",
            "--output", "--log-level", "--config"]


def _filter(candidates: Sequence[str], current: str) -> list[str]:
    if not current:
        return sorted(set(candidates))
    lowered = current.lower()
    return sorted({c for c in candidates if c.lower().startswith(lowered)})


def which(shell: str) -> str | None:
    """Locate a shell executable, for the doctor command."""
    return shutil.which(shell) or shutil.which(f"{shell}.exe")


def shell_of(env_var: str = "SHELL") -> str | None:
    """Guess the user's interactive shell from the environment."""
    name = pathlib.Path(os.environ.get(env_var, "")).name
    return name if name in SHELLS else None
