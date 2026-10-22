"""Four-layer configuration: defaults < config file < environment < flags.

The layering is the whole point, and the precedence is not negotiable:
an explicit command-line flag always beats an environment variable, which
always beats a config file, which always beats a built-in default.

The subtlety is that argparse gives you exactly one ``default=`` slot and
evaluates it while the parser is being *built*. That is too early to express
this precedence, because the config file is not known until ``--config`` has
been read from the command line. So the parser is built with ``None``
defaults and the whole stack is resolved once, after parsing, in
:meth:`Config.resolve` -- the same reason
:func:`anvil.core.actions.apply_env_fallbacks` works the way it does.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .errors import ConfigError, UsageError

__all__ = [
    "Config",
    "ConfigFile",
    "assign",
    "coerce",
    "deep_merge",
    "discover",
    "dot_get",
    "dot_set",
    "flatten",
    "load",
    "parse_set",
    "redact",
    "search_paths",
    "unflatten",
]

#: Environment variable naming a config file to use, highest precedence.
ENV_CONFIG = "ANVIL_CONFIG"
#: Prefix for ``ANVIL_<SECTION>__<KEY>``-style environment overrides.
ENV_PREFIX = "ANVIL_"
#: Separator between section and key in environment variable names.
ENV_SECTION_SEP = "__"

_SUFFIXES = (".toml", ".json", ".yaml", ".yml")
_BASENAMES = ("anvil", ".anvil")


# --------------------------------------------------------------------------- #
# Dotted-path helpers
# --------------------------------------------------------------------------- #


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """``{'a': {'b': 1}}`` -> ``{'a.b': 1}``."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


def unflatten(data: Mapping[str, Any]) -> dict[str, Any]:
    """``{'a.b': 1}`` -> ``{'a': {'b': 1}}``."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        dot_set(out, key, value)
    return out


def dot_get(data: Mapping[str, Any], path: str, default: Any = None) -> Any:
    """Read a dotted path, returning *default* if any segment is missing."""
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def dot_set(data: dict[str, Any], path: str, value: Any) -> None:
    """Write a dotted path, creating intermediate mappings as needed."""
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[parts[-1]] = value


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* onto *base*, returning a new dict.

    Mappings merge key-wise; every other type is replaced wholesale. Lists are
    *replaced* rather than concatenated, because concatenation makes it
    impossible to clear a list from a higher-precedence layer.
    """
    out = dict(base)
    for key, value in overlay.items():
        existing = out.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            out[key] = deep_merge(existing, value)
        else:
            out[key] = value
    return out


def assign(data: dict[str, Any], path: str, value: Any) -> None:
    """Alias for :func:`dot_set` that reads better at call sites."""
    dot_set(data, path, value)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _load_toml(text: str, source: pathlib.Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        raise ConfigError(
            "TOML config requires Python 3.11+",
            hint="use a .json config file, or upgrade Python",
        ) from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from None


def _load_json(text: str, source: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{source}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    return data


def _load_yaml(text: str, source: pathlib.Path) -> dict[str, Any]:
    """A deliberately tiny YAML subset loader.

    Supports nested mappings, ``- item`` sequences, and scalars. It is *not* a
    YAML implementation -- quoting, anchors, multi-document, and flow style are
    out of scope. JSON is a subset of YAML, so anything exotic should be written
    as JSON instead; the error message says so.
    """
    root: dict[str, Any] = {}
    lines = text.expandtabs().splitlines()
    meaningful = [i for i, line in enumerate(lines)
                  if line.strip() and not line.strip().startswith("#")]
    index_of_next = {meaningful[j]: (meaningful[j + 1] if j + 1 < len(meaningful) else None)
                     for j in range(len(meaningful))}

    # (indent, container) stack; the root mapping sits at indent -1.
    stack: list[tuple[int, Any]] = [(-1, root)]
    for number, raw in enumerate(lines):
        line = raw.split(" #", 1)[0].rstrip() if " #" in raw else raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        container = stack[-1][1]
        if body.startswith("- "):
            if not isinstance(container, list):
                raise ConfigError(
                    f"{source}:{number + 1}: list item outside a list",
                    hint="YAML support is a subset; use JSON for complex documents",
                )
            container.append(_yaml_scalar(body[2:].strip()))
            continue
        if ":" not in body:
            raise ConfigError(
                f"{source}:{number + 1}: expected 'key: value', got {body!r}"
            )
        key, _, value = body.partition(":")
        key = key.strip().strip("'\"")
        value = value.strip()
        if value == "":
            # A key with no value opens a block. Look at the next meaningful
            # line to decide whether that block is a sequence or a mapping --
            # guessing a mapping is what makes `hosts:\n  - a` fail.
            following = index_of_next.get(number)
            upcoming = lines[following].strip() if following is not None else ""
            child: Any = [] if upcoming.startswith("- ") else {}
            if isinstance(container, dict):
                container[key] = child
            stack.append((indent, child))
        elif isinstance(container, dict):
            container[key] = _yaml_scalar(value)
        else:
            raise ConfigError(
                f"{source}:{number + 1}: cannot set a key on a list item"
            )
    return root


def _yaml_scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1] == text[0] and len(text) >= 2:
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_yaml_scalar(p) for p in inner.split(",")]
    return text


_LOADERS = {
    ".toml": _load_toml,
    ".json": _load_json,
    ".yaml": _load_yaml,
    ".yml": _load_yaml,
}


class ConfigFile:
    """One discovered configuration file and the data it yielded."""

    def __init__(self, path: pathlib.Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @property
    def exists(self) -> bool:
        return self.path is not None

    def __repr__(self) -> str:
        return f"<ConfigFile {self.path}>"


def search_paths(cwd: pathlib.Path | None = None,
                 environ: Mapping[str, str] | None = None) -> list[pathlib.Path]:
    """Every place a config file may live, most specific first.

    The order encodes precedence: an explicit environment override, then the
    current directory (so a repo can ship its own settings), then the user's
    XDG config, then the platform home directory.
    """
    env = environ if environ is not None else os.environ
    cwd = cwd if cwd is not None else pathlib.Path.cwd()
    found: list[pathlib.Path] = []

    explicit = env.get(ENV_CONFIG)
    if explicit:
        candidate = pathlib.Path(explicit).expanduser()
        found.append(candidate if candidate.suffix else candidate.with_suffix(".toml"))
        return found

    for base in (".anvil.toml", "anvil.toml", ".anvil.json", "anvil.json"):
        found.append(cwd / base)
    xdg = env.get("XDG_CONFIG_HOME")
    roots = [pathlib.Path(xdg).expanduser() if xdg else None]
    if os.name == "nt":
        appdata = env.get("APPDATA")
        roots.append(pathlib.Path(appdata).expanduser() if appdata else None)
    else:
        roots.append(pathlib.Path.home() / ".config")
    for root in roots:
        if root is not None:
            found.append(root / "anvil" / "config.toml")
            found.append(root / "anvil" / "config.json")
    home = pathlib.Path.home()
    found.append(home / ".anvil.toml")
    found.append(home / ".config" / "anvil.toml")
    return found


def discover(cwd: pathlib.Path | None = None,
             environ: Mapping[str, str] | None = None,
             explicit: str | pathlib.Path | None = None) -> ConfigFile | None:
    """Return the first config file that exists, or ``None``.

    ``ANVIL_CONFIG`` naming a file that does not exist is an *error*, not a
    silent fallback: a typo in a config path must not quietly change behaviour.
    """
    env = environ if environ is not None else os.environ
    if explicit is not None:
        path = pathlib.Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return _read(path)
    if env.get(ENV_CONFIG):
        path = pathlib.Path(env[ENV_CONFIG]).expanduser()
        if not path.exists():
            raise ConfigError(
                f"${ENV_CONFIG} points at a file that does not exist: {path}",
                hint="unset the variable or fix the path",
            )
        return _read(path)
    for candidate in search_paths(cwd, env):
        if candidate.is_file():
            return _read(candidate)
    return None


def _read(path: pathlib.Path) -> ConfigFile:
    loader = _LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ConfigError(
            f"unsupported config format {path.suffix!r} for {path}",
            hint="use one of " + ", ".join(_SUFFIXES),
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    return ConfigFile(path, loader(text, path))


def load(path: str | pathlib.Path) -> dict[str, Any]:
    """Read one config file by path."""
    return _read(pathlib.Path(path).expanduser()).data


# --------------------------------------------------------------------------- #
# Environment and flag overlays
# --------------------------------------------------------------------------- #


def env_overrides(environ: Mapping[str, str] | None = None, *,
                  prefix: str = ENV_PREFIX,
                  section_sep: str = ENV_SECTION_SEP) -> dict[str, Any]:
    """Turn ``ANVIL_HTTP__TIMEOUT=5s`` into ``{'http': {'timeout': '5s'}}``.

    Every value stays a *string*. Coercion happens in :meth:`Config.resolve`,
    once the parser's declared types are known -- guessing here is how a config
    value silently becomes the wrong type.
    """
    env = environ if environ is not None else os.environ
    out: dict[str, Any] = {}
    for name, value in env.items():
        if not name.startswith(prefix) or name == ENV_CONFIG:
            continue
        body = name[len(prefix):]
        if not body:
            continue
        lowered = body.lower()
        if section_sep in lowered:
            section, _, key = lowered.partition(section_sep)
        elif "_" in lowered:
            section, _, key = lowered.partition("_")
        else:
            section, key = "global", lowered
        section = section.strip("_")
        key = key.strip("_")
        if not section or not key:
            continue
        node = out.setdefault(section, {})
        if isinstance(node, dict):
            node[key.replace("_", "-")] = value
    return out


def parse_set(assignments: Iterable[str] | None,
              separator: str = "=") -> dict[str, Any]:
    """Turn ``--set http.timeout=5s --set 'ui.theme="dark"'`` into nested data.

    Values are parsed as JSON when possible and kept as strings otherwise, so
    ``--set n=5`` yields an int and ``--set s=5`` (unquoted) yields ``"5"``.
    """
    out: dict[str, Any] = {}
    for item in assignments or ():
        key, sep, value = str(item).partition(separator)
        if not sep:
            raise UsageError(
                f"--set expects KEY{separator}VALUE, got {item!r}",
                hint="for example --set http.timeout=30s",
            )
        key = key.strip()
        if not key:
            raise UsageError(f"--set has an empty key in {item!r}")
        dot_set(out, key, coerce(value.strip()))
    return out


def coerce(value: str) -> Any:
    """Best-effort scalar typing for config/flag strings.

    Deliberately asymmetric about ints and floats, because the failure modes
    differ. An int is converted only when the text is its own canonical form,
    so ``007``, ``+5`` and ``1_0`` stay strings -- they are far more likely to
    be an identifier or a version than a number. A float is converted whenever
    it parses, since trailing zeros (``0.50``) carry no ambiguity.
    """
    text = value.strip()
    if text[:1] in "\"'" and text[-1:] == text[:1] and len(text) >= 2:
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    digits = text.lstrip("+-")
    if digits.isdigit():
        # Only convert when the text is its own canonical form, so ``007``
        # stays a string: it is far more likely to be an identifier.
        return int(text) if str(int(text)) == text else text
    try:
        number = float(text)
    except ValueError:
        return text
    if number != number or number in (float("inf"), float("-inf")):
        return text
    return number


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_SECRET_HINTS = (
    "token", "secret", "password", "passwd", "pwd", "key", "credential",
    "auth", "cookie", "session", "api_key", "apikey", "private", "cert",
)


def looks_secret(name: str) -> bool:
    """True when a config key's name suggests it holds a credential."""
    lowered = name.lower().replace("-", "_")
    return any(hint in lowered for hint in _SECRET_HINTS)


def redact(data: Mapping[str, Any], *, extra: Iterable[str] = ()) -> dict[str, Any]:
    """Copy *data* with secret-looking values replaced.

    ``anvil config show`` must be safe to paste into a bug report, which means
    it can never print a value whose *name* suggests it is a credential.
    """
    forced = {k.lower().replace("-", "_") for k in extra}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            out[key] = redact(value, extra=forced)
        elif looks_secret(key) or key.lower().replace("-", "_") in forced:
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# The resolved stack
# --------------------------------------------------------------------------- #

#: Names of the layers, lowest precedence first.
LAYERS = ("defaults", "file", "env", "flags")


class Config:
    """The resolved configuration for one invocation.

    Keeps each layer separately so ``anvil config show --explain`` can show
    *which* layer supplied a value -- the single most useful thing you can tell
    someone debugging a CLI.
    """

    def __init__(
        self,
        *,
        defaults: Mapping[str, Any] | None = None,
        file_data: Mapping[str, Any] | None = None,
        file_path: pathlib.Path | None = None,
        env_data: Mapping[str, Any] | None = None,
        flag_data: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.defaults = dict(defaults or {})
        self.file = dict(file_data or {})
        self.file_path = file_path
        self.env = dict(env_data or {})
        self.flags = dict(flag_data or {})
        self.environ = dict(environ if environ is not None else os.environ)
        self._resolved: dict[str, Any] | None = None
        self._provenance: dict[str, str] = {}

    # -- construction ------------------------------------------------------- #

    @classmethod
    def load(
        cls,
        *,
        path: str | pathlib.Path | None = None,
        overrides: Sequence[str] | None = None,
        defaults: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
        cwd: pathlib.Path | None = None,
    ) -> Config:
        """Build a stack from the environment, without needing a parser yet."""
        env = environ if environ is not None else os.environ
        discovered = discover(cwd=cwd, environ=env, explicit=path)
        return cls(
            defaults=defaults,
            file_data=discovered.data if discovered else {},
            file_path=discovered.path if discovered else None,
            env_data=env_overrides(env),
            flag_data=parse_set(overrides),
            environ=env,
        )

    # -- resolution --------------------------------------------------------- #

    def resolve(self) -> dict[str, Any]:
        """Merge every layer and record where each key came from."""
        merged: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        for name, layer in (
            ("defaults", self.defaults),
            ("file", self.file),
            ("env", self.env),
            ("flags", self.flags),
        ):
            if not layer:
                continue
            merged = deep_merge(merged, layer)
            for key in flatten(layer):
                provenance[key] = name
        self._resolved = merged
        self._provenance = provenance
        return merged

    @property
    def data(self) -> dict[str, Any]:
        if self._resolved is None:
            self.resolve()
        return self._resolved or {}

    def get(self, path: str, default: Any = None) -> Any:
        """Read a dotted key from the merged stack."""
        return dot_get(self.data, path, default)

    def source(self, path: str) -> str | None:
        """Which layer supplied *path*: ``flags``, ``file``, ``env``, ..."""
        if self._provenance:
            self.resolve()
        return self._provenance.get(path)

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def with_flags(self, flag_data: Mapping[str, Any]) -> Config:
        """Return a copy with the flags layer replaced."""
        clone = Config(
            defaults=self.defaults,
            file_data=self.file,
            file_path=self.file_path,
            env_data=self.env,
            flag_data=flag_data,
            environ=self.environ,
        )
        return clone

    # -- reporting ---------------------------------------------------------- #

    def explain(self, prefix: str = "") -> list[tuple[str, Any, str]]:
        """``(dotted_key, value, source)`` triples for every leaf."""
        rows = []
        for key, value in sorted(flatten(self.data).items()):
            shown = "<redacted>" if looks_secret(key.rsplit(".", 1)[-1]) else value
            rows.append((prefix + key, shown, self.source(key) or "?"))
        return rows

    def layers(self) -> dict[str, dict[str, Any]]:
        return {
            "defaults": self.defaults,
            "file": self.file,
            "env": self.env,
            "flags": self.flags,
        }

    def __contains__(self, path: object) -> bool:
        return dot_get(self.data, str(path), _MISSING) is not _MISSING

    def __repr__(self) -> str:
        return f"<Config file={self.file_path!r} keys={len(self.data)}>"


_MISSING = object()
