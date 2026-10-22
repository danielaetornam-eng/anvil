"""stdio discipline and the output-format renderers.

Two responsibilities, both easy to get wrong:

* **Never lose the user's data.** Data goes to stdout, diagnostics to stderr,
  always. ``| head``, ``> file``, and ``2>/dev/null`` are the contract; a CLI
  that prints a warning to stdout corrupts every pipeline it appears in.
* **Degrade the format, never the content.** When stdout is a pipe, tables
  become plain lines and colour disappears -- but the same rows appear.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
from collections.abc import Iterator, Sequence
from typing import IO, Any

from . import term
from .errors import InputError, NotFoundError, translate_os_error

__all__ = [
    "Reader",
    "Renderer",
    "open_input",
    "read_text",
    "resolve_format",
    "stdin_data",
    "stdout_is_tty",
    "write_output",
    "yaml_dump",
]


def stdout_is_tty() -> bool:
    """True when stdout is an interactive terminal."""
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def resolve_format(requested: str, *, is_tty: bool | None = None,
                   hints: Sequence[str] = ()) -> str:
    """Turn ``--output auto`` into a concrete format.

    ``auto`` prefers the richest format a terminal can render. The *hints* are
    the caller's opinion about the data: ``anvil table`` passes ``("table",)``
    so that redirecting to a file still yields a table rather than ``plain``.
    """
    tty = stdout_is_tty() if is_tty is None else is_tty
    if requested != "auto":
        return requested
    if hints and tty:
        return hints[0]
    if tty:
        return "table"
    # A pipe gets the most machine-friendly thing that stays human-readable.
    for candidate in ("table", "plain"):
        if candidate in hints:
            return candidate
    return hints[0] if hints else "plain"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #


def open_input(path: str | os.PathLike[str] | None, *, encoding: str = "utf-8") -> IO[str]:
    """Open *path* for reading, or return ``sys.stdin`` for ``-``/``None``.

    Stdin is decoded as UTF-8 regardless of the platform default, because a
    UTF-8 pipe on Windows would otherwise be mangled by the console code page.
    """
    if path is None or str(path) == "-":
        if stdin_is_tty() and path is None:
            raise UsageHint("no input given and stdin is a terminal")
        return _utf8_stdin()
    resolved = pathlib.Path(path).expanduser()
    try:
        return open(resolved, encoding=encoding, errors="replace")
    except OSError as exc:
        raise translate_os_error(exc, context=str(resolved)) from None


class UsageHint(InputError):
    """Input is missing but the user did something they probably did not mean."""


def _utf8_stdin() -> IO[str]:
    try:
        if sys.stdin.encoding and sys.stdin.encoding.lower() not in ("utf-8", "utf8"):
            return io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8",
                                    errors="replace", newline="")
    except Exception:
        pass
    return sys.stdin


def read_text(path: str | os.PathLike[str] | None = None, *,
              encoding: str = "utf-8-sig", strip: bool = False) -> str:
    """Read a whole file (or stdin) as text.

    The default codec is ``utf-8-sig`` rather than ``utf-8``: a leading
    byte-order mark is invisible in an editor and fatal to ``json.loads``, so
    reading it transparently removes an entire class of "works on Linux, fails
    on my machine" failures caused by files authored on Windows. Writing still
    uses plain UTF-8 with no BOM.
    """
    if path is not None and str(path) != "-":
        resolved = pathlib.Path(path).expanduser()
        try:
            text = resolved.read_text(encoding=encoding)
        except FileNotFoundError:
            raise NotFoundError(f"{resolved} does not exist",
                                hint="check the path") from None
        except IsADirectoryError:
            raise InputError(f"{resolved} is a directory, not a file") from None
        except UnicodeDecodeError as exc:
            raise InputError(
                f"{resolved} is not valid {encoding} text: {exc.reason}",
                hint="check the file's encoding",
            ) from None
        except OSError as exc:
            raise translate_os_error(exc, context=str(resolved)) from None
        return text.strip() if strip else text

    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None and sys.stdin.encoding and sys.stdin.encoding.lower() not in (
        "utf-8", "utf8"
    ):
        raw = buffer.read()
        text = raw.decode(encoding, errors="replace")
    else:
        text = sys.stdin.read()
    return text.strip() if strip else text


def stdin_data() -> bytes:
    """Read all of stdin as bytes."""
    buffer = getattr(sys.stdin, "buffer", None)
    return buffer.read() if buffer is not None else sys.stdin.read().encode()


class Reader:
    """Iterate over lines of a file or stdin, handling encoding and errors."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *,
                 encoding: str = "utf-8", skip_header: bool = False) -> None:
        self.path = path
        self.encoding = encoding
        self.skip_header = skip_header
        self._handle: IO[str] | None = None
        self._iter: Iterator[str] | None = None

    def __enter__(self) -> Iterator[str]:
        self._handle = open_input(self.path, encoding=self.encoding)
        self._iter = iter(self._handle)
        if self.skip_header:
            next(self._iter, None)
        return self._iter

    def __exit__(self, *exc: object) -> None:
        if self._handle is not None and self._handle is not sys.stdin:
            self._handle.close()
        self._handle = None


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def write_output(target: Any, text: str) -> int:
    """Write *text* to ``-``/stdout/a file handle/a path, creating parents."""
    if target is None or target == "-":
        stream = term.Stream(sys.stdout)
        written = stream.write(text)
        stream.flush()
        return written
    if hasattr(target, "write"):
        written = target.write(text)
        with contextlib.suppress(Exception):
            target.flush()
        return written
    path = pathlib.Path(str(target)).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise translate_os_error(exc, context=str(path)) from None
    return len(text)


def yaml_dump(data: Any, *, indent: int = 0) -> str:
    """A small YAML emitter for JSON-shaped data.

    ``yaml.dump`` would be a dependency for the sake of a screen format; this
    covers the cases CLIs actually need (mappings, lists, scalars) and emits
    JSON for anything exotic, which is always valid YAML.
    """
    pad = "  " * indent
    if isinstance(data, dict):
        if not data:
            return f"{pad}{{}}\n"
        out = []
        for key, value in data.items():
            rendered_key = key if _plain_scalar(key) else json.dumps(str(key))
            if isinstance(value, (dict, list)) and value:
                out.append(f"{pad}{rendered_key}:\n{yaml_dump(value, indent=indent + 1)}")
            else:
                out.append(f"{pad}{rendered_key}: {_scalar(value)}\n")
        return "".join(out)
    if isinstance(data, (list, tuple)):
        if not data:
            return f"{pad}[]\n"
        out = []
        for item in data:
            if isinstance(item, (dict, list)) and item:
                body = yaml_dump(item, indent=indent + 1)
                lines = body.splitlines()
                out.append(f"{pad}- {lines[0].strip()}\n")
                out.extend(f"{line}\n" for line in lines[1:])
            else:
                out.append(f"{pad}- {_scalar(item)}\n")
        return "".join(out)
    return f"{pad}{_scalar(data)}\n"


def _plain_scalar(value: Any) -> bool:
    text = str(value)
    return bool(text) and all(c.isalnum() or c in "-_." for c in text)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    text = str(value)
    if _plain_scalar(text):
        return text
    return json.dumps(text)


def json_dumps(data: Any, *, pretty: bool = True) -> str:
    return json.dumps(data, indent=2 if pretty else None, default=str,
                      ensure_ascii=False)


class Renderer:
    """Turns a :class:`term.Table` or a raw value into the requested format.

    Constructed once from the resolved ``--output`` value so commands never
    branch on the format themselves.
    """

    def __init__(self, fmt: str = "auto", *, color: bool | None = None,
                 table_style: str = "unicode", max_width: int | None = None) -> None:
        self.requested = fmt
        self.format = resolve_format(fmt)
        self.color = term.supports_color() if color is None else color
        self.table_style = table_style
        self.max_width = max_width

    # -- generic ------------------------------------------------------------ #

    def scalar(self, value: Any, *, plain_key: str = "") -> str:
        """Render a single value. ``plain_key`` is shown as a label in text mode."""
        fmt = self.format
        if fmt == "json":
            return json_dumps(value)
        if fmt in ("jsonl", "ndjson"):
            return json.dumps(value, default=str, ensure_ascii=False)
        if fmt == "yaml":
            return yaml_dump(value).rstrip("\n")
        if fmt in ("csv", "tsv"):
            text = plain_key and f"{plain_key}={value}"
            return text or str(value)
        if fmt == "tree" and isinstance(value, (dict, list)):
            return term.Tree().from_dict(value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def records(self, rows: Sequence[dict[str, Any]]) -> str:
        """Render a list of dicts."""
        fmt = self.format
        if fmt == "json":
            return json_dumps(rows)
        if fmt in ("jsonl", "ndjson"):
            return "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows)
        if fmt == "yaml":
            return yaml_dump(rows).rstrip("\n")
        if fmt == "csv":
            return _delimited(rows, ",")
        if fmt == "tsv":
            return _delimited(rows, "\t")
        if fmt == "tree":
            return term.Tree().from_dict(rows)
        if fmt in ("table", "plain"):
            table = term.Table()
            table.add_dicts(rows)
            return table.render(self.max_width) if fmt == "table" else \
                "\n".join(" ".join(str(v) for v in r.values()) for r in rows)
        return json_dumps(rows)

    def table(self, table: term.Table) -> str:
        """Render a :class:`term.Table` in whatever format was requested."""
        fmt = self.format
        if fmt == "json":
            return table.to_json()
        if fmt in ("jsonl", "ndjson"):
            keys = [c.key for c in table.columns]
            return "\n".join(
                json.dumps({k: row[i] if i < len(row) else None
                            for i, k in enumerate(keys)}, default=str)
                for row in table._rows
            )
        if fmt == "csv":
            return table.to_csv(",")
        if fmt == "tsv":
            return table.to_csv("\t")
        if fmt == "yaml":
            keys = [c.key for c in table.columns]
            return yaml_dump([
                {k: row[i] if i < len(row) else None for i, k in enumerate(keys)}
                for row in table._rows
            ]).rstrip("\n")
        if fmt == "markdown":
            return table.to_markdown()
        if fmt == "html":
            return table.to_html()
        if fmt == "latex":
            return table.to_latex()
        if fmt == "plain":
            return "\n".join(
                "  ".join(row[i] if i < len(row) else "" for i in range(len(table.columns)))
                for row in table._rows
            )
        return table.render(self.max_width)

    def progress_ok(self) -> str:
        """Whether a progress indicator should be shown at all."""
        return "ok" if self.color and stdout_is_tty() else "no"


def _delimited(rows: Sequence[dict[str, Any]], delimiter: str) -> str:
    import csv

    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(keys)
    for row in rows:
        writer.writerow(["" if row.get(k) is None else row.get(k) for k in keys])
    return buffer.getvalue().rstrip("\n")
