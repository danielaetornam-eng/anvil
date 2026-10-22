"""Every ``type=`` callable this CLI uses, built as inspectable objects.

The single most important property of an argparse ``type`` is that it must
raise :class:`argparse.ArgumentTypeError` (not ``ValueError``, not ``TypeError``)
with a message that explains *how to fix it*. ``ValueError`` produces a bare
traceback from ``_get_value``; ``ArgumentTypeError`` produces
``argument --foo: <your message>``.

:class:`ParamType` is a callable object rather than a plain function so that it
can carry a human ``__name__`` (argparse uses it as the default metavar), a
custom metavar, and a documentation hint surfaced in ``--help``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import io
import ipaddress
import json
import os
import pathlib
import re
import sys
import urllib.parse
import uuid as _uuid
from collections.abc import Callable, Iterable, Sequence
from enum import Enum
from typing import Any

__all__ = [
    "ByteSize",
    "CaseInsensitive",
    "Choice",
    "Duration",
    "EnumByName",
    "Interval",
    "NormalizedChoice",
    "ParamType",
    "any_path",
    "arg_type",
    "byte_size",
    "chmod_mode",
    "csv_list",
    "csv_table",
    "duration",
    "email",
    "enum_by_name",
    "env_var_name",
    "existing_dir",
    "existing_file",
    "existing_path",
"fraction",
    "interval",
    "ip_address",
    "json_object",
    "json_value",
    "key_value",
    "key_value_list",
    "log_level",
    "non_negative_int",
    "number",
    "output_path",
    "percentage",
    "port",
    "positive_float",
    "positive_int",
    "range_int",
    "readable_file",
    "regex_string",
    "semver",
    "sha256_digest",
    "table_style",
    "timestamp",
    "url",
    "uuid_value",
    "writable_path",
]


class ParamType:
    """Base class: a callable converter with argparse-friendly metadata.

    Subclasses implement :meth:`convert`. The wrapper handles ``None``/empty
    input, coerces the failure into an ``ArgumentTypeError``, and guarantees
    that ``__name__`` exists for metavar generation.
    """

    #: Overrides the metavar shown in ``--help``; ``None`` uses ``__name__``.
    metavar: str | None = None
    #: Appended to the action's help text when the formatter supports it.
    hint: str | None = None

    def __init__(self, name: str | None = None, *, choices: Iterable[Any] | None = None):
        self._name = name or type(self).__name__
        self.choices = list(choices) if choices is not None else None
        # argparse reads ``action.type.__name__`` to build the default metavar,
        # so it must be an instance attribute rather than a method.
        self.__name__ = self.label

    # -- introspection ------------------------------------------------------ #

    @property
    def label(self) -> str:
        """The string argparse should display for this type."""
        return self.metavar or self._name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._name!r}>"

    # -- conversion --------------------------------------------------------- #

    def convert(self, value: str) -> Any:
        raise NotImplementedError

    def __call__(self, value: str) -> Any:
        try:
            return self.convert(value)
        except argparse.ArgumentTypeError:
            raise
        except (ValueError, TypeError) as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
        except (OSError, UnicodeError) as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    def usage(self) -> str:
        """A ``NAME: example`` string for help text."""
        return self.label


def arg_type(cls: type[ParamType], name: str | None = None, **kwargs: Any) -> ParamType:
    """Instantiate a :class:`ParamType` with a stable ``__name__``."""
    return cls(name or cls.__name__, **kwargs)


# --------------------------------------------------------------------------- #
# Filesystem
# --------------------------------------------------------------------------- #

_STDIN_NAMES = {"-", "/dev/stdin", "con:"}


class ExistingFile(ParamType):
    """A readable file that exists right now."""

    hint = "must exist"

    def convert(self, value: str) -> pathlib.Path:
        path = pathlib.Path(value).expanduser()
        if not path.exists():
            raise argparse.ArgumentTypeError(f"{path} does not exist")
        if path.is_dir():
            raise argparse.ArgumentTypeError(f"{path} is a directory, not a file")
        if not os.access(path, os.R_OK):
            raise argparse.ArgumentTypeError(f"{path} is not readable")
        return path.resolve()


class ReadableFile(ExistingFile):
    """Alias emphasising readability (also accepts ``-`` for stdin)."""

    def convert(self, value: str) -> pathlib.Path:
        if value in _STDIN_NAMES:
            return pathlib.Path("-")
        return super().convert(value)


class ExistingDir(ParamType):
    """A directory that exists right now."""

    hint = "must exist"

    def convert(self, value: str) -> pathlib.Path:
        path = pathlib.Path(value).expanduser()
        if not path.exists():
            raise argparse.ArgumentTypeError(f"{path} does not exist")
        if not path.is_dir():
            raise argparse.ArgumentTypeError(f"{path} is not a directory")
        return path.resolve()


class ExistingPath(ParamType):
    """Any existing path, file or directory."""

    def convert(self, value: str) -> pathlib.Path:
        path = pathlib.Path(value).expanduser()
        if not path.exists():
            raise argparse.ArgumentTypeError(f"{path} does not exist")
        return path.resolve()


class WritablePath(ParamType):
    """A path that may be created; parents are optional."""

    hint = "parent directory is created on demand"

    def convert(self, value: str) -> pathlib.Path:
        path = pathlib.Path(value).expanduser()
        if path.exists():
            if path.is_dir():
                raise argparse.ArgumentTypeError(f"{path} is an existing directory")
            if not os.access(path, os.W_OK):
                raise argparse.ArgumentTypeError(f"{path} is not writable")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class OutputPath(ParamType):
    """A writable destination; the literal ``-`` means stdout."""

    hint = "use - for stdout"

    def convert(self, value: str) -> pathlib.Path:
        if value == "-":
            return pathlib.Path("-")
        path = pathlib.Path(value).expanduser()
        if path.is_dir():
            raise argparse.ArgumentTypeError(f"{path} is a directory")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise argparse.ArgumentTypeError(f"cannot use {path}: {exc}") from None
        return path


class AnyPath(ParamType):
    """A path that need not exist, normalised to absolute."""

    def convert(self, value: str) -> pathlib.Path:
        if value == "-":
            return pathlib.Path("-")
        path = pathlib.Path(value).expanduser()
        try:
            return path.resolve()
        except OSError:
            return path.absolute()


existing_file = arg_type(ExistingFile, "FILE")
readable_file = arg_type(ReadableFile, "FILE")
existing_dir = arg_type(ExistingDir, "DIR")
existing_path = arg_type(ExistingPath, "PATH")
writable_path = arg_type(WritablePath, "PATH")
output_path = arg_type(OutputPath, "PATH")
any_path = arg_type(AnyPath, "PATH")


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #


class PositiveInt(ParamType):
    def convert(self, value: str) -> int:
        try:
            number = int(str(value).replace("_", ""), 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
        if number <= 0:
            raise argparse.ArgumentTypeError(f"must be greater than 0, got {number}")
        return number


class NonNegativeInt(ParamType):
    def convert(self, value: str) -> int:
        try:
            number = int(str(value).replace("_", ""), 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
        if number < 0:
            raise argparse.ArgumentTypeError(f"must be 0 or greater, got {number}")
        return number


class PositiveFloat(ParamType):
    def convert(self, value: str) -> float:
        try:
            number = float(str(value).replace("_", ""))
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
        if number <= 0:
            raise argparse.ArgumentTypeError(f"must be greater than 0, got {number}")
        return number


class Number(ParamType):
    """An integer when it looks like one, otherwise a float.

    The classic pattern for "give me whatever the user meant" numeric flags.
    """

    def convert(self, value: str) -> int | float:
        text = str(value).replace("_", "")
        try:
            return int(text, 10)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a number (try 42, 3.14, 1e6, or 0x1f)"
            ) from None


class RangeInt(ParamType):
    """An integer confined to ``[low, high]``."""

    def __init__(self, low: int, high: int, name: str | None = None):
        super().__init__(name or "N")
        self.low = low
        self.high = high

    def convert(self, value: str) -> int:
        try:
            number = int(str(value).replace("_", ""), 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
        if not self.low <= number <= self.high:
            raise argparse.ArgumentTypeError(
                f"must be between {self.low} and {self.high}, got {number}"
            )
        return number

    def usage(self) -> str:
        return f"{self.low}..{self.high}"


class Fraction(ParamType):
    """A float in ``[0.0, 1.0]`` -- accept ``50%`` for convenience."""

    def convert(self, value: str) -> float:
        text = str(value).strip()
        try:
            if text.endswith("%"):
                number = float(text[:-1]) / 100.0
            else:
                number = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
        if not 0.0 <= number <= 1.0:
            raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {number}")
        return number


class Percentage(Fraction):
    """A fraction in ``[0.0, 1.0]`` that *must* be written as a percentage."""

    hint = "write it as a percentage, e.g. 75%"

    def convert(self, value: str) -> float:
        text = str(value).strip()
        if not text.endswith("%"):
            raise argparse.ArgumentTypeError(
                f"{value!r} needs a % sign (did you mean {value}%?)"
            )
        return super().convert(text)


positive_int = arg_type(PositiveInt, "N")
non_negative_int = arg_type(NonNegativeInt, "N")
positive_float = arg_type(PositiveFloat, "F")
number = arg_type(Number, "NUM")
fraction = arg_type(Fraction, "RATIO")
percentage = arg_type(Percentage, "PCT")


def range_int(low: int, high: int, name: str | None = None) -> RangeInt:
    instance = RangeInt(low, high, name)
    instance.__name__ = instance.label
    return instance


# --------------------------------------------------------------------------- #
# Human units
# --------------------------------------------------------------------------- #

_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1000, "kb": 1000, "kib": 1024,
    "m": 1000**2, "mb": 1000**2, "mib": 1024**2,
    "g": 1000**3, "gb": 1000**3, "gib": 1024**3,
    "t": 1000**4, "tb": 1000**4, "tib": 1024**4,
    "p": 1000**5, "pb": 1000**5, "pib": 1024**5,
}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


class ByteSize(ParamType):
    """``10MB``, ``512 KiB``, ``1.5g``, ``1024`` -> an integer byte count.

    Bare numbers are binary-multiplied; suffixed numbers honour their suffix,
    so ``1kb`` is 1000 and ``1kib`` is 1024. This distinction is the bug in
    every hand-rolled size parser, so it is spelled out rather than guessed.
    """

    hint = "accepts 10MB, 512KiB, 1.5g, or a plain byte count"

    def convert(self, value: str) -> int:
        match = _SIZE_RE.match(str(value))
        if not match:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a size (try 10MB, 512KiB, 1.5g)"
            )
        number, unit = match.groups()
        unit = unit.lower()
        if unit not in _SIZE_UNITS:
            raise argparse.ArgumentTypeError(
                f"unknown size unit {unit!r} in {value!r}; "
                f"try one of " + ", ".join(sorted(u for u in _SIZE_UNITS if u))
            )
        return int(float(number) * _SIZE_UNITS[unit])


class Duration(ParamType):
    """Compound durations: ``500ms``, ``30s``, ``5m``, ``1h30m``, ``2d``.

    Composed of segments in descending order, so ``1h30m`` and ``90m`` are
    both accepted and produce the same seconds.
    """

    hint = "accepts 500ms, 30s, 5m, 1h30m, or 2d"

    def convert(self, value: str) -> float:
        text = str(value).strip().lower()
        if not text:
            raise argparse.ArgumentTypeError("duration is empty")
        total = 0.0
        position = 0
        pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|[smhdw])")
        seen_any = False
        while position < len(text):
            match = pattern.match(text, position)
            if not match:
                raise argparse.ArgumentTypeError(
                    f"{value!r} is not a duration (try 500ms, 30s, 5m, 1h30m, 2d)"
                )
            amount, unit = float(match.group(1)), match.group(2)
            total += amount * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
            position = match.end()
            seen_any = True
            while position < len(text) and text[position].isspace():
                position += 1
        if not seen_any or position != len(text):
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a duration (try 500ms, 30s, 5m, 1h30m, 2d)"
            )
        if total <= 0:
            raise argparse.ArgumentTypeError(f"duration must be positive, got {value!r}")
        return total


def _readable_seconds(seconds: float) -> str:
    """Render a seconds count back into the compact form the type accepts."""
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    for unit, scale in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= scale and seconds % scale == 0:
            return f"{int(seconds // scale)}{unit}"
    return f"{seconds:g}s" if seconds % 1 else f"{int(seconds)}s"


byte_size = arg_type(ByteSize, "SIZE")
duration = arg_type(Duration, "DURATION")


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


class Port(ParamType):
    SERVICES = {
        "ftp": 21, "ssh": 22, "smtp": 25, "dns": 53, "http": 80,
        "https": 443, "postgres": 5432, "mysql": 3306, "redis": 6379,
        "http-alt": 8080,
    }

    def convert(self, value: str) -> int:
        text = str(value)
        if text.isdigit() and not text.startswith("0"):
            number = int(text)
        elif text.lower() in self.SERVICES:
            return self.SERVICES[text.lower()]
        else:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a port number or service name "
                f"({', '.join(sorted(self.SERVICES))})"
            )
        if not 1 <= number <= 65535:
            raise argparse.ArgumentTypeError(
                f"port must be 1-65535, got {number} "
                f"(try 8080, or a service name like {', '.join(sorted(self.SERVICES))})"
            )
        return number


class Hostname(ParamType):
    _LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

    def convert(self, value: str) -> str:
        text = str(value)
        if len(text) > 253:
            raise argparse.ArgumentTypeError("hostname is longer than 253 characters")
        for label in text.rstrip(".").split("."):
            if not self._LABEL.match(label):
                raise argparse.ArgumentTypeError(
                    f"{text!r} is not a valid hostname (label {label!r} is invalid)"
                )
        return text


class IpAddress(ParamType):
    """An IPv4/IPv6 address. Unlike ``ipaddress.ip_address`` this is strict."""

    def convert(self, value: str) -> str:
        try:
            return str(ipaddress.ip_address(str(value).strip()))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a valid IPv4 or IPv6 address"
            ) from None


class Url(ParamType):
    """A URL with a scheme and a host."""

    metavar = "URL"

    def convert(self, value: str) -> str:
        text = str(value).strip()
        parsed = urllib.parse.urlparse(text)
        if not parsed.scheme:
            raise argparse.ArgumentTypeError(
                f"{value!r} has no scheme (try https://{text})"
            )
        if not parsed.netloc:
            raise argparse.ArgumentTypeError(f"{value!r} has no host")
        if parsed.scheme in {"http", "https", "ftp", "ws", "wss"} and not parsed.hostname:
            raise argparse.ArgumentTypeError(f"{value!r} has no hostname")
        return text


class Email(ParamType):
    _RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

    def convert(self, value: str) -> str:
        text = str(value).strip()
        if not self._RE.match(text):
            raise argparse.ArgumentTypeError(f"{value!r} is not an email address")
        return text


port = arg_type(Port, "PORT")
hostname = arg_type(Hostname, "HOST")
ip_address = arg_type(IpAddress, "IP")
url = arg_type(Url, "URL")
email = arg_type(Email, "EMAIL")


# --------------------------------------------------------------------------- #
# Structured text
# --------------------------------------------------------------------------- #


class CsvList(ParamType):
    """``a,b,c`` -> ``['a', 'b', 'c']``; quoting honours CSV rules."""

    hint = "comma separated"

    def __init__(self, name: str | None = None, strip: bool = True):
        super().__init__(name or "CSV")
        self.strip = strip

    def convert(self, value: str) -> list[str]:
        text = str(value).strip()
        if not text:
            return []
        reader = csv.reader(io.StringIO(text), skipinitialspace=True)
        try:
            rows = list(reader)
        except csv.Error as exc:
            raise argparse.ArgumentTypeError(f"invalid CSV: {exc}") from None
        if not rows:
            return []
        out = [cell.strip() if self.strip else cell for cell in rows[0]]
        if any("\n" in cell for cell in out):
            raise argparse.ArgumentTypeError("use one line per value, not embedded newlines")
        return out


class CsvTable(ParamType):
    """Parse a multi-line CSV document into a list of rows."""

    hint = "CSV with a header row"

    def convert(self, value: str) -> list[list[str]]:
        reader = csv.reader(io.StringIO(str(value)))
        try:
            return [row for row in reader if row]
        except csv.Error as exc:
            raise argparse.ArgumentTypeError(f"invalid CSV: {exc}") from None


class KeyValue(ParamType):
    """``key=value`` -> ``('key', 'value')``.

    Both ``=`` and ``:`` are accepted separators, because HTTP requires it:
    ``-H 'Accept: application/json'`` has to work next to ``--set k=v``. The
    *earliest* separator wins, so a value may freely contain the other one
    (``Authorization: Bearer a:b``, ``url=https://x``, ``Host: a:8080``).

    Uses argparse *recursively*: a private sub-parser validates the shape, so
    the failure is phrased in the user's own grammar rather than as a bare
    ``ValueError``. The sub-parser runs with ``exit_on_error=False`` and
    ``add_help=False`` so it can never print usage or call ``sys.exit`` --
    argparse raising ``SystemExit`` from inside a ``type=`` callable is the
    classic way to get an untraceable bug.

    Keys beginning with ``-`` (``--header -X-Api-Key=secret``) bypass the
    sub-parser, because argparse would classify them as options.
    """

    metavar = "KEY=VALUE"
    _KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

    def __init__(self, key_name: str = "key", value_name: str = "value",
                 separators: str = "=:"):
        super().__init__(f"{key_name.upper()}={value_name.upper()}")
        self.key_name = key_name
        self.value_name = value_name
        self.separators = separators
        # ``exit_on_error=False`` makes argparse raise ArgumentError instead of
        # calling error(); redirect_stderr covers the paths that still print
        # (missing arguments). Together they guarantee the nested parser is
        # completely silent -- a type= that writes to stderr corrupts the
        # outer parser's error reporting.
        parser = argparse.ArgumentParser(
            add_help=False, prog="", exit_on_error=False, allow_abbrev=False
        )
        parser.add_argument(key_name, metavar=key_name.upper())
        parser.add_argument(value_name, metavar=value_name.upper(), nargs="?", default="")
        self._parser = parser

    def _split(self, text: str) -> tuple[str, str, str]:
        """Partition on whichever configured separator appears earliest."""
        positions = [(text.find(sep), sep) for sep in self.separators
                     if text.find(sep) != -1]
        if not positions:
            return text, "", ""
        index, sep = min(positions)
        return text[:index], sep, text[index + len(sep):]

    def convert(self, value: str) -> tuple[str, str]:
        key, sep, val = self._split(str(value))
        if not sep:
            raise argparse.ArgumentTypeError(
                f"expected {self.key_name.upper()}={self.value_name.upper()}, "
                f"for example {self.key_name}=my{self.value_name}"
            )
        if sep == ":" and val.startswith(" "):
            # HTTP convention: "Accept: application/json" carries one space
            # after the colon which is not part of the value.
            val = val[1:]
        if key.startswith("-"):
            if not self._KEY_RE.match(key):
                raise argparse.ArgumentTypeError(
                    f"{key!r} is not a valid header/option name"
                )
            return key, val
        import contextlib
        import io

        try:
            with contextlib.redirect_stderr(io.StringIO()):
                ns = self._parser.parse_args([key, val])
        except (SystemExit, argparse.ArgumentError):
            raise argparse.ArgumentTypeError(
                f"expected {self.key_name.upper()}={self.value_name.upper()}, "
                f"for example {self.key_name}=my{self.value_name}"
            ) from None
        return getattr(ns, self.key_name), getattr(ns, self.value_name) or ""


class KeyValueList(ParamType):
    """Several ``key=value`` pairs in one argument, separated by commas."""

    def __init__(self, key_name: str = "key", value_name: str = "value"):
        super().__init__(f"{key_name.upper()}={value_name.upper()}[,...]")
        self.pair = KeyValue(key_name, value_name)

    def convert(self, value: str) -> list[tuple[str, str]]:
        text = str(value).strip()
        if not text:
            return []
        # Disambiguate three legitimate shapes:
        #   "a=1,b=2"      -> several pairs  (every chunk carries a separator)
        #   "-H,Accept: x" -> one pair       (the value itself contains commas)
        #   "-H"           -> a valueless pair, i.e. "unset this header"
        chunks = [c.strip() for c in text.split(",")]
        if len(chunks) > 1 and all(self.pair._split(c)[1] for c in chunks):
            return [self.pair(chunk) for chunk in chunks]
        if not self.pair._split(text)[1]:
            return [(text, "")]
        return [self.pair(text)]


class JsonValue(ParamType):
    """Any JSON document, parsed eagerly with a precise error location."""

    def convert(self, value: str) -> Any:
        try:
            return json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(
                f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
            ) from None


class JsonObject(JsonValue):
    metavar = "JSON"

    def convert(self, value: str) -> dict[str, Any]:
        data = super().convert(value)
        if not isinstance(data, dict):
            raise argparse.ArgumentTypeError(
                f"expected a JSON object, got {type(data).__name__}"
            )
        return data


class RegexString(ParamType):
    hint = "Python regular expression"

    def __init__(self, name: str | None = None, allow_empty: bool = False):
        super().__init__(name or "REGEX")
        self.allow_empty = allow_empty
        self._compiled: re.Pattern[str] | None = None

    def convert(self, value: str) -> re.Pattern[str]:
        try:
            self._compiled = re.compile(str(value))
        except re.error as exc:
            raise argparse.ArgumentTypeError(
                f"invalid regex at position {exc.pos}: {exc.msg}"
            ) from None
        if not self.allow_empty and not str(value):
            raise argparse.ArgumentTypeError("regex is empty")
        return self._compiled


class Sha256Digest(ParamType):
    _RE = re.compile(r"^[0-9a-fA-F]{64}$")

    def convert(self, value: str) -> str:
        text = str(value).strip()
        if not self._RE.match(text):
            raise argparse.ArgumentTypeError(
                "expected 64 hexadecimal characters (a sha256 digest)"
            )
        return text.lower()


class UuidValue(ParamType):
    def __init__(self, name: str | None = None, version: int | None = None):
        super().__init__(name or "UUID")
        self.version = version

    def convert(self, value: str) -> str:
        try:
            parsed = _uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            raise argparse.ArgumentTypeError(f"{value!r} is not a UUID") from None
        if self.version is not None and parsed.version != self.version:
            raise argparse.ArgumentTypeError(
                f"expected a version {self.version} UUID, got version {parsed.version}"
            )
        return str(parsed)


_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)


class SemVer(ParamType):
    """A semantic version, with ``--no-prerelease`` handled by the caller."""

    def convert(self, value: str) -> tuple[int, int, int, str]:
        match = _SEMVER_RE.match(str(value).strip())
        if not match:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a semantic version (expected MAJOR.MINOR.PATCH)"
            )
        major, minor, patch, pre, _build = match.groups()
        return int(major), int(minor), int(patch), pre or ""


class Interval(ParamType):
    """``3``, ``1-5``, ``2,4,6`` -> a set of integers.

    The workhorse for ``--lines 1,3,5-9``.
    """

    hint = "e.g. 3, 1-5, 2,4,6 or open-ended 5-"

    def __init__(self, name: str | None = None, lo: int | None = None, hi: int | None = None):
        super().__init__(name or "SPEC")
        self.lo = lo
        self.hi = hi

    def convert(self, value: str) -> list[int]:
        text = str(value).strip()
        if not text:
            raise argparse.ArgumentTypeError("interval is empty")
        out: set[int] = set()
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "-" in chunk[1:]:
                start_s, _, end_s = chunk.partition("-")
                try:
                    start = int(start_s) if start_s.strip() else self.lo
                    end = int(end_s) if end_s.strip() else self.hi
                except ValueError:
                    raise argparse.ArgumentTypeError(
                        f"{chunk!r} is not a range (try 1-5)"
                    ) from None
                if start is None or end is None:
                    raise argparse.ArgumentTypeError(
                        f"{chunk!r} needs a bound on both sides"
                    )
                if end < start:
                    raise argparse.ArgumentTypeError(
                        f"{chunk!r} ends before it starts"
                    )
                if self.hi is not None and end > self.hi:
                    raise argparse.ArgumentTypeError(
                        f"{chunk!r} exceeds the maximum of {self.hi}"
                    )
                out.update(range(start, end + 1))
            else:
                try:
                    number = int(chunk)
                except ValueError:
                    raise argparse.ArgumentTypeError(
                        f"{chunk!r} is not a number or a range"
                    ) from None
                if self.lo is not None and number < self.lo:
                    raise argparse.ArgumentTypeError(
                        f"{number} is below the minimum of {self.lo}"
                    )
                if self.hi is not None and number > self.hi:
                    raise argparse.ArgumentTypeError(
                        f"{number} is above the maximum of {self.hi}"
                    )
                out.add(number)
        if not out:
            raise argparse.ArgumentTypeError(f"{value!r} selected no values")
        return sorted(out)


class Timestamp(ParamType):
    """An epoch number or an ISO-8601 string -> epoch seconds (UTC)."""

    def convert(self, value: str) -> float:
        text = str(value).strip()
        try:
            return float(text)
        except ValueError:
            pass
        import datetime as _dt

        candidate = text.replace("Z", "+00:00")
        try:
            parsed = _dt.datetime.fromisoformat(candidate)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not an epoch or ISO-8601 timestamp"
            ) from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.UTC)
        return parsed.timestamp()


class ChmodMode(ParamType):
    """``755``, ``0755``, ``0o644`` -> an integer mode."""

    def convert(self, value: str) -> int:
        text = str(value).strip()
        if re.fullmatch(r"[0-7]{3,4}", text):
            return int(text, 8)
        if re.fullmatch(r"0o[0-7]{3,4}", text):
            return int(text[2:], 8)
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a mode (try 755, 0755, or 0o644)"
        )


csv_list = arg_type(CsvList, "CSV")
csv_table = arg_type(CsvTable, "CSV")
key_value = KeyValue()
key_value_list = KeyValueList()
json_value = arg_type(JsonValue, "JSON")
json_object = arg_type(JsonObject, "JSON")
regex_string = arg_type(RegexString, "REGEX")
sha256_digest = arg_type(Sha256Digest, "SHA256")
uuid_value = arg_type(UuidValue, "UUID")
semver = arg_type(SemVer, "VERSION")
timestamp = arg_type(Timestamp, "TIME")
chmod_mode = arg_type(ChmodMode, "MODE")


def interval(lo: int | None = None, hi: int | None = None, name: str | None = None) -> Interval:
    instance = Interval(name or "SPEC", lo, hi)
    instance.__name__ = instance.label
    return instance


# --------------------------------------------------------------------------- #
# Choices
# --------------------------------------------------------------------------- #


class Choice:
    """An argparse-compatible ``choices`` container that normalises input.

    ``argparse`` only supports ``in`` membership, so a subclass can accept
    aliases, case-insensitive matching, and still display a curated list. This
    is how you get ``--level DEBUG`` to work while ``--help`` still reads
    ``{debug,info,warning,error}``.
    """

    def __init__(
        self,
        choices: Iterable[Any],
        *,
        case_sensitive: bool = False,
        aliases: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> None:
        self.choices = list(choices)
        self.case_sensitive = case_sensitive
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self.label = label
        self._lookup: dict[str, Any] = {}
        for choice in self.choices:
            self._lookup[self._key(choice)] = choice
        self._lookup.update(self.aliases)

    def _key(self, value: Any) -> str:
        return str(value) if self.case_sensitive else str(value).lower()

    def __contains__(self, value: object) -> bool:
        return self._key(value) in self._lookup

    def __iter__(self):
        return iter(self.choices)

    def __len__(self) -> int:
        return len(self.choices)

    def __str__(self) -> str:
        return self.label or "{" + ",".join(str(c) for c in self.choices) + "}"

    def __repr__(self) -> str:
        return f"Choice({self.choices!r})"

    def resolve(self, value: str) -> Any:
        try:
            return self._lookup[self._key(value)]
        except KeyError:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not one of {self}"
            ) from None


class NormalizedChoice(ParamType):
    """Accept aliases and any casing, store the canonical value.

    ``--mode fast|FAST|speed`` all store ``"speed"`` while ``--help`` still
    advertises the canonical set. This **must** be a ``type=`` and not an
    ``Action``: argparse validates ``choices`` inside ``_get_values``, before
    ``Action.__call__`` ever runs, so a normalising action would reject
    ``fast`` before it had a chance to map it. ``type=`` runs first, which is
    exactly the ordering this pattern depends on.
    """

    def __init__(self, choices: Sequence[Any], *, aliases: dict[str, Any] | None = None,
                 case_sensitive: bool = False, name: str | None = None):
        super().__init__(name or "|".join(str(c) for c in choices))
        self.case_sensitive = case_sensitive
        self._lookup: dict[str, Any] = {}
        for choice in choices:
            self._lookup[self._key(choice)] = choice
        self._lookup.update({self._key(k): v for k, v in (aliases or {}).items()})

    def _key(self, value: Any) -> str:
        return str(value) if self.case_sensitive else str(value).lower()

    @property
    def canonical(self) -> list[Any]:
        """The canonical values, for passing as ``choices=``."""
        return list(dict.fromkeys(self._lookup.values()))

    def convert(self, value: str) -> Any:
        key = self._key(value)
        if key in self._lookup:
            return self._lookup[key]
        import difflib

        close = difflib.get_close_matches(key, list(self._lookup), 1, 0.6)
        hint = f", did you mean {close[0]!r}?" if close else ""
        raise argparse.ArgumentTypeError(
            f"{value!r} is not valid{hint} (expected one of "
            f"{', '.join(repr(str(c)) for c in self.canonical)})"
        )


class CaseInsensitive(ParamType):
    """A ``type=`` that folds case, keeping ``choices`` display tidy."""

    def __init__(self, choices: Sequence[str], name: str | None = None):
        super().__init__(name or "|".join(choices))
        self.choices = list(choices)
        self._folded = {c.lower(): c for c in choices}

    def convert(self, value: str) -> str:
        try:
            return self._folded[str(value).lower()]
        except KeyError:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not one of {', '.join(self.choices)}"
            ) from None


class EnumByName(ParamType):
    """``type=`` driven by an :class:`enum.Enum`, with explicit aliases."""

    def __init__(
        self,
        enum_cls: type[Enum],
        *,
        aliases: dict[str, Enum] | None = None,
        name: str | None = None,
        ignore_case: bool = True,
    ) -> None:
        super().__init__(name or "|".join(m.name.lower() for m in enum_cls))
        self.enum_cls = enum_cls
        self.ignore_case = ignore_case
        self.aliases = dict(aliases or {})
        self.choices = [m.name.lower() for m in enum_cls]

    def convert(self, value: str) -> Enum:
        text = str(value)
        key = text.lower() if self.ignore_case else text
        if key in self.aliases:
            return self.aliases[key]
        for member in self.enum_cls:
            if member.name.lower() == key:
                return member
        raise argparse.ArgumentTypeError(
            f"{value!r} is not one of {', '.join(self.choices)}"
        )


def enum_by_name(enum_cls: type[Enum], **kwargs: Any) -> EnumByName:
    instance = EnumByName(enum_cls, **kwargs)
    instance.__name__ = instance.label
    return instance


class LogLevel(ParamType):
    LEVELS = ("debug", "info", "warning", "error", "critical")

    def __init__(self, name: str | None = None):
        super().__init__(name or "LEVEL")
        self.choices = list(self.LEVELS)

    def convert(self, value: str) -> int:
        import logging

        text = str(value).strip().lower()
        if text in ("warn",):
            text = "warning"
        if text in ("fatal",):
            text = "critical"
        if text in ("quiet", "silent"):
            return logging.CRITICAL + 10
        try:
            return int(getattr(logging, text.upper()))
        except AttributeError:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a log level (choose from {', '.join(self.LEVELS)})"
            ) from None


class OutputFormat(ParamType):
    FORMATS = (
        "auto", "table", "plain", "json", "jsonl", "ndjson",
        "csv", "tsv", "yaml", "markdown", "html", "latex", "tree",
    )

    def __init__(self, name: str | None = None):
        super().__init__(name or "|".join(self.FORMATS))
        self.choices = list(self.FORMATS)

    def convert(self, value: str) -> str:
        text = str(value).strip().lower()
        aliases = {
            "t": "table", "txt": "plain", "text": "plain", "wide": "table",
            "yml": "yaml", "md": "markdown", "htm": "html",
            "l": "plain", "jq": "json", "lines": "plain",
        }
        text = aliases.get(text, text)
        if text not in self.FORMATS:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not an output format (choose from {', '.join(self.FORMATS)})"
            )
        return text


class TableStyle(ParamType):
    STYLES = ("unicode", "ascii", "ascii-light", "markdown", "none")

    def __init__(self, name: str | None = None):
        super().__init__(name or "|".join(self.STYLES))
        self.choices = list(self.STYLES)

    def convert(self, value: str) -> str:
        text = str(value).strip().lower()
        text = {"box": "unicode", "default": "unicode", "grid": "unicode"}.get(text, text)
        if text not in self.STYLES:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a table style (choose from {', '.join(self.STYLES)})"
            )
        return text


class EnvVarName(ParamType):
    """Validate a POSIX environment variable name."""

    _RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def convert(self, value: str) -> str:
        text = str(value)
        if not self._RE.match(text):
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a valid environment variable name "
                "(letters, digits, underscore; must not start with a digit)"
            )
        return text


log_level = LogLevel()
output_format = OutputFormat()
table_style = TableStyle()
env_var_name = arg_type(EnvVarName, "VAR")


# --------------------------------------------------------------------------- #
# Imports and callables
# --------------------------------------------------------------------------- #


class CallableRef(ParamType):
    """``package.module:attribute`` -> the imported callable.

    Deliberately strict: only ``module:attr`` is accepted, and the target must
    be callable. Resolving arbitrary ``eval`` is how CLIs grow RCE.
    """

    metavar = "module:attr"

    def convert(self, value: str) -> Callable[..., Any]:
        import importlib

        text = str(value)
        if text.count(":") != 1:
            raise argparse.ArgumentTypeError(
                f"{value!r} must look like package.module:attribute"
            )
        module_name, _, attr = text.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise argparse.ArgumentTypeError(
                f"cannot import {module_name!r}: {exc}"
            ) from None
        target: Any = module
        for part in attr.split("."):
            try:
                target = getattr(target, part)
            except AttributeError:
                raise argparse.ArgumentTypeError(
                    f"{module_name!r} has no attribute {attr!r}"
                ) from None
        if not callable(target):
            raise argparse.ArgumentTypeError(f"{value!r} is not callable")
        return target


class Base64Blob(ParamType):
    def __init__(self, name: str | None = None, url_safe: bool = False):
        super().__init__(name or "B64")
        self.url_safe = url_safe

    def convert(self, value: str) -> bytes:
        text = str(value)
        padding = "=" * (-len(text) % 4)
        try:
            if self.url_safe:
                return base64.urlsafe_b64decode(text + padding)
            return base64.b64decode(text + padding, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise argparse.ArgumentTypeError(
                f"invalid {'url-safe ' if self.url_safe else ''}base64: {exc}"
            ) from None


callable_ref = CallableRef()


# --------------------------------------------------------------------------- #
# Composition helpers
# --------------------------------------------------------------------------- #


def optional(inner: Callable[[str], Any], empty: Any = None) -> Callable[[str], Any]:
    """Wrap a converter so that ``""``/``-`` yields *empty* instead of raising.

    Pair this with ``nargs='?'`` and ``const=`` to express "optional value".
    """

    def convert(value: str) -> Any:
        if value in ("", "-"):
            return empty
        return inner(value)

    convert.__name__ = getattr(inner, "__name__", "value")
    return convert


def file_or_stdin(path: pathlib.Path, *, encoding: str = "utf-8") -> Any:
    """Open *path* for reading, or return ``sys.stdin`` for ``-``."""
    if str(path) == "-":
        return sys.stdin
    return open(path, encoding=encoding, errors="replace")


def resolve_default(value: Any, root: pathlib.Path | None = None) -> Any:
    """Expand ``~`` and make relative paths absolute against *root*."""
    if isinstance(value, pathlib.Path):
        value = str(value)
    if not isinstance(value, str):
        return value
    expanded = pathlib.Path(os.path.expandvars(value)).expanduser()
    if root is not None and not expanded.is_absolute():
        expanded = root / expanded
    return expanded
