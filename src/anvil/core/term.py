"""Terminal capability detection, ANSI styling, tables, trees, and progress.

Everything in here degrades safely: if stdout is a pipe, colors vanish, box
drawing falls back to ASCII, spinners fall back to periodic log lines, and
progress bars fall back to nothing at all.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import IO

__all__ = [
    "Ansi",
    "Progress",
    "Stream",
    "Table",
    "Tree",
    "cells",
    "format_bytes",
    "format_duration",
    "human_duration",
    "indent",
    "interpolate",
    "paint",
    "slug",
    "strip_ansi",
    "supports_color",
    "terminal_size",
    "truncate",
    "visible_width",
    "wrap_ansi",
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_INTERP_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def strip_ansi(text: str) -> str:
    """Remove every escape sequence from *text*."""
    return _ANSI_RE.sub("", text)


def visible_width(text: str) -> int:
    """Length of *text* as the terminal sees it (escape codes are zero-width).

    Also accounts for East-Asian wide characters, which occupy two cells.
    """
    import unicodedata

    plain = strip_ansi(text)
    width = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left", fill: str = " ") -> str:
    """Pad *text* to *width* visible columns, ignoring escape codes."""
    gap = max(0, width - visible_width(text))
    if align == "right":
        return fill * gap + text
    if align == "center":
        left = gap // 2
        return fill * left + text + fill * (gap - left)
    return text + fill * gap


def truncate(text: str, width: int, ellipsis: str = "\u2026") -> str:
    """Truncate *text* to *width* visible columns, appending *ellipsis*."""
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text
    if width <= len(ellipsis):
        return ellipsis[:width]

    out: list[str] = []
    used = 0
    budget = width - len(ellipsis)
    for ch in text:
        if ch == "\x1b":
            out.append(ch)
            continue
        import unicodedata

        step = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        )
        if used + step > budget:
            break
        out.append(ch)
        used += step
    return "".join(out) + ellipsis


_WRAP_TOKEN = re.compile(r"(\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\S+|\s+)")


def wrap_ansi(
    text: str,
    width: int,
    *,
    initial_indent: str = "",
    subsequent_indent: str = "",
    break_long_words: bool = False,
) -> list[str]:
    """``textwrap.wrap`` that ignores ANSI escapes when measuring.

    The stdlib wrapper counts every character, so eight bytes of colour codes
    silently shorten each line by eight columns -- which is exactly the bug
    that makes coloured help ragged. This version tracks escape sequences
    separately from display width.

    Each emitted line is closed with a reset so a colour opened for a token
    cannot bleed into the line after it, and escapes attach to the word that
    follows them so wrapping never splits a styled run.
    """
    if width < 8:
        width = 8
    limit = width - visible_width(initial_indent)
    tokens = _WRAP_TOKEN.findall(text)
    if not tokens:
        return [initial_indent]

    lines: list[str] = []
    parts: list[str] = []
    pending_space: str | None = None
    used = 0
    lead = initial_indent
    limit = width - visible_width(lead)

    def close() -> None:
        nonlocal parts, used, pending_space, lead, limit
        body = "".join(parts)
        lines.append(lead + body + ("\x1b[0m" if "\x1b" in body else ""))
        parts = []
        used = 0
        pending_space = None
        lead = subsequent_indent
        limit = width - visible_width(lead)

    for token in tokens:
        if token.startswith("\x1b"):
            # Attach styling to the word it precedes so a line break never
            # separates a colour code from the text it colours.
            if parts:
                parts.append(token)
            else:
                pending_space = (pending_space or "") + token
            continue
        if token.isspace():
            if parts:
                pending_space = token
            continue
        token_width = visible_width(token)
        # A token wider than the whole line (a URL, a base64 blob) must still be
        # broken, otherwise it blows the layout out for every other option.
        if break_long_words and token_width > limit:
            for chunk in _hard_split(token, limit):
                if parts:
                    close()
                parts.append(chunk)
                used = visible_width(chunk)
                if used >= limit:
                    close()
            continue
        separator = pending_space or ""
        separator_width = 1 if separator else 0
        if parts and used + separator_width + token_width > limit:
            close()
            separator = ""
        if separator:
            parts.append(separator)
            used += separator_width
            pending_space = None
        if parts:
            parts.append(token)
            used += token_width
        else:
            pending_space = separator + (pending_space or "")
            parts.append(token)
            used += token_width
        if used >= limit and not break_long_words:
            close()

    if parts:
        close()
    if not lines:
        lines.append(initial_indent.rstrip())
    return lines


def _hard_split(token: str, limit: int) -> list[str]:
    """Split a token that cannot fit on one line into ``limit``-wide chunks."""
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for ch in token:
        step = visible_width(ch)
        if used + step > limit and current:
            chunks.append("".join(current))
            current = []
            used = 0
        current.append(ch)
        used += step
    if current:
        chunks.append("".join(current))
    return chunks


def terminal_size(default: tuple[int, int] = (100, 24)) -> tuple[int, int]:
    """Best-effort ``(columns, lines)``, honouring ``COLUMNS``/``LINES``."""
    try:
        cols = int(os.environ["COLUMNS"])
        rows = int(os.environ.get("LINES", default[1]))
        if cols > 0:
            return cols, rows
    except (KeyError, ValueError):
        pass
    try:
        size = shutil.get_terminal_size(fallback=default)
        return max(size.columns, 40), max(size.lines, 10)
    except Exception:
        return default


def supports_color(stream: IO[str] | None = None) -> bool:
    """Decide whether ANSI escapes should be emitted on *stream*."""
    stream = stream if stream is not None else sys.stdout
    force = os.environ.get("ANVIL_FORCE_COLOR") or os.environ.get("FORCE_COLOR")
    if force and force != "0":
        return True
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("ANVIL_NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.name == "nt":
        return _windows_vt_enabled(stream)
    return True


def _windows_vt_enabled(stream: IO[str]) -> bool:
    """Turn on virtual-terminal processing so ANSI works on Windows consoles."""
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    if not (hasattr(sys.stdout, "buffer") or hasattr(sys.stderr, "buffer")):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11 if stream is sys.stdout else -12)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004
        disable_wrap = 0x0008
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt | disable_wrap))
    except Exception:
        return False


def enable_windows_vt() -> None:
    """Idempotently enable ANSI on the real console before the first write."""
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            try:
                _windows_vt_enabled(stream)
            except Exception:
                pass


class Ansi:
    """A tiny, honest SGR palette.

    Codes are applied by attribute access and neutralised by the ``null``
    theme, so the same rendering code path produces plain text when color is
    off. This is why every colour decision here is a method call rather than
    an f-string constant.
    """

    CODES: dict[str, str] = {
        "reset": "\x1b[0m",
        "bold": "\x1b[1m",
        "dim": "\x1b[2m",
        "italic": "\x1b[3m",
        "underline": "\x1b[4m",
        "reverse": "\x1b[7m",
        "strike": "\x1b[9m",
        "black": "\x1b[30m",
        "red": "\x1b[31m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "blue": "\x1b[34m",
        "magenta": "\x1b[35m",
        "cyan": "\x1b[36m",
        "white": "\x1b[37m",
        "gray": "\x1b[90m",
        "grey": "\x1b[90m",
        "bright_red": "\x1b[91m",
        "bright_green": "\x1b[92m",
        "bright_yellow": "\x1b[93m",
        "bright_blue": "\x1b[94m",
        "bright_magenta": "\x1b[95m",
        "bright_cyan": "\x1b[96m",
        "bright_white": "\x1b[97m",
        "bg_red": "\x1b[41m",
        "bg_green": "\x1b[42m",
        "bg_blue": "\x1b[44m",
    }

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    @classmethod
    def detect(cls, stream: IO[str] | None = None) -> Ansi:
        return cls(supports_color(stream))

    def _wrap(self, name: str, text: str) -> str:
        if not self.enabled or name not in self.CODES or not text:
            return text
        return f"{self.CODES[name]}{text}{self.CODES['reset']}"

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in Ansi.CODES:
            raise AttributeError(name)
        return lambda text="": self._wrap(name, str(text))

    def paint(self, spec: str, text: str) -> str:
        """Apply a ``+``-joined spec such as ``"bold+cyan"`` to *text*."""
        out = text
        for name in spec.split("+"):
            if name:
                out = self._wrap(name, out)
        return out

    def ok(self, text: str = "") -> str:
        return self._wrap("green", text)

    def warn(self, text: str = "") -> str:
        return self._wrap("yellow", text)

    def err(self, text: str = "") -> str:
        return self._wrap("red", text)

    def info(self, text: str = "") -> str:
        return self._wrap("cyan", text)

    def hint(self, text: str = "") -> str:
        return self._wrap("gray", text)

    def dep(self, text: str = "") -> str:
        return self._wrap("magenta", text)

    def head(self, text: str = "") -> str:
        return self._wrap("bold+underline", text)


#: Process-wide palette. Commands use this instead of threading a theme around.
A = Ansi(True)


def set_color(enabled: bool) -> None:
    """Flip the process-wide palette (called once, from ``main``)."""
    global A
    A = Ansi(enabled)


def paint(name: str, text: str) -> str:
    """Apply an SGR code by name, *ignoring* the global enable flag.

    Two authorities decide colour in this CLI and conflating them is a real
    bug:

    * the process palette :data:`A` follows the *stream* -- ``NO_COLOR``,
      ``--no-color``, and whether stdout is a terminal;
    * the help formatter decides from *its own* configuration, because
      ``PlainHelpFormatter`` must stay plain even when the rest of the program
      is coloured.

    Help rendering therefore calls :func:`paint` after it has already made the
    decision, instead of reading the global palette.
    """
    if not text:
        return text
    code = Ansi.CODES.get(name)
    if code is None:
        return text
    return f"{code}{text}{Ansi.CODES['reset']}"


def interpolate(template: str, **values: object) -> str:
    """``${name}`` / ``$name`` substitution against *values*.

    Unknown names are left verbatim so a template can contain a literal ``$``
    without blowing up; ``strict=True`` turns them into an error instead.
    """
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        if key in values:
            return str(values[key])
        missing.append(key)
        return match.group(0)

    out = _INTERP_RE.sub(repl, template)
    if missing and values.get("__strict__"):
        raise KeyError(f"unresolved template variables: {', '.join(sorted(set(missing)))}")
    return out


def indent(text: str, prefix: str = "  ", skip_first: bool = False) -> str:
    """Indent every line of *text*."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        out.append(line if (skip_first and i == 0) else prefix + line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

_SI_UNITS = ("B", "kB", "MB", "GB", "TB", "PB")
_IEC_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(n: float, *, binary: bool = False, precision: int = 1) -> str:
    """``1536`` -> ``'1.5 kB'`` (SI) or ``'1.5 KiB'`` (IEC).

    The unit labels differ between the two scales, and labelling 1024-based
    values ``KB`` is how "my 4GB drive only shows 3.7GB" happens.
    """
    step = 1024.0 if binary else 1000.0
    units = _IEC_UNITS if binary else _SI_UNITS
    value = float(n)
    for unit in units:
        if abs(value) < step or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= step
    return f"{value} {units[-1]}"


def human_duration(seconds: float) -> str:
    """``93784`` -> ``'26h 3m 4s'``; always at most two units."""
    seconds = float(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds < 1e-3:
        return f"{sign}{seconds * 1e6:.0f}\u00b5s"
    if seconds < 1:
        return f"{sign}{seconds * 1e3:.1f}ms"
    if seconds < 60:
        return f"{sign}{seconds:.2f}s".replace(".00s", "s")
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{sign}{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{sign}{hours}h {minutes}m {secs}s"
    days, hours = divmod(hours, 24)
    return f"{sign}{days}d {hours}h {minutes}m"


def format_duration(seconds: float) -> str:
    """Fixed-ish representation used by ``bench``."""
    return f"{seconds:.6f}"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def cells(row: Iterable[object]) -> list[str]:
    """Coerce an arbitrary row into a list of display strings."""
    if isinstance(row, str):
        return [row]
    out = []
    for value in row:
        if value is None:
            out.append("")
        elif isinstance(value, bool):
            out.append("true" if value else "false")
        elif isinstance(value, float):
            out.append(f"{value:.6g}")
        else:
            out.append(str(value))
    return out


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


@dataclass
class Column:
    """Column layout metadata for :class:`Table`."""

    key: str
    title: str = ""
    align: str = "auto"  # auto | left | right | center
    max_width: int | None = None
    min_width: int = 0
    style: str = ""
    wrap: bool = False
    hidden: bool = False


_BOXES: dict[str, dict[str, str]] = {
    "unicode": {
        "tl": "\u250c", "tr": "\u2510", "bl": "\u2514", "br": "\u2518",
        "h": "\u2500", "v": "\u2502", "lt": "\u251c", "rt": "\u2524",
        "tt": "\u252c", "bt": "\u2534", "x": "\u253c", "nm": "\u2500",
        "mh": "\u2550",
    },
    "ascii": {
        "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
        "lt": "+", "rt": "+", "tt": "+", "bt": "+", "x": "+", "nm": "-",
        "mh": "=",
    },
    "ascii-light": {
        "tl": "", "tr": "", "bl": "", "br": "", "h": "", "v": "|",
        "lt": "", "rt": "", "tt": "", "bt": "", "x": "", "nm": "-", "mh": "=",
    },
    "markdown": {
        "tl": "", "tr": "", "bl": "", "br": "", "h": "", "v": "|",
        "lt": "|", "rt": "|", "tt": "|", "bt": "|", "x": "|", "nm": "-",
        "mh": "-",
    },
    "none": {
        "tl": "", "tr": "", "bl": "", "br": "", "h": " ", "v": " ", "lt": "",
        "rt": "", "tt": "", "bt": "", "x": "", "nm": "-", "mh": "-",
    },
}


@dataclass
class Table:
    """A width-aware table renderer with eleven output flavours.

    ``style`` picks the box glyphs; the ``to_*`` methods pick the
    *serialisation* (terminal box, markdown pipe table, CSV, TSV, HTML,
    LaTeX, JSON). Those are two independent axes and every real CLI
    eventually needs both, which is why they are separated here.
    """

    columns: list[Column]
    style: str
    max_width: int | None
    show_header: bool
    show_rows: bool
    zebra: bool
    row_numbers: bool
    _rows: list[list[str]]

    def __init__(
        self,
        columns: Sequence[Column] | None = None,
        *,
        style: str = "unicode",
        max_width: int | None = None,
        show_header: bool = True,
        show_rows: bool = True,
        zebra: bool = False,
        row_numbers: bool = False,
    ) -> None:
        self.columns = list(columns or [])
        self.style = style if style in _BOXES else "unicode"
        self.max_width = max_width
        self.show_header = show_header
        self.show_rows = show_rows
        self.zebra = zebra
        self.row_numbers = row_numbers
        self._rows: list[list[str]] = []

    # -- construction ------------------------------------------------------- #

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Sequence[object]],
        *,
        titles: Sequence[str] | None = None,
        **kwargs: object,
    ) -> Table:
        """Build a table from raw rows, auto-detecting numeric alignment."""
        rows = [list(r) for r in rows]
        width = max((len(r) for r in rows), default=len(titles or ()))
        titles = list(titles) if titles else [f"col{i}" for i in range(width)]
        titles += [f"col{i}" for i in range(len(titles), width)]
        cols = []
        for i in range(width):
            numeric = all(
                _is_numeric(r[i]) for r in rows if i < len(r) and str(r[i]).strip() != ""
            )
            cols.append(
                Column(
                    key=str(titles[i]),
                    title=str(titles[i]),
                    align="right" if numeric and rows else "left",
                )
            )
        table = cls(cols, **kwargs)  # type: ignore[arg-type]
        table.add_rows(rows)
        return table

    def add(self, *values: object) -> Table:
        self._rows.append(cells(values))
        return self

    def add_row(self, values: Sequence[object]) -> Table:
        self._rows.append(cells(values))
        return self

    def add_rows(self, rows: Iterable[Sequence[object]]) -> Table:
        for row in rows:
            self.add_row(row)
        return self

    def add_dicts(self, records: Iterable[dict[str, object]], keys: Sequence[str] | None = None):
        """Add rows from mappings, preserving first-seen key order."""
        records = list(records)
        if keys is None:
            keys = []
            for record in records:
                for key in record:
                    if key not in keys:
                        keys.append(key)
        if not self.columns:
            for key in keys:
                self.columns.append(Column(key=str(key), title=str(key)))
        for record in records:
            self._rows.append(cells(record.get(k) for k in keys))
        return self

    def sort(self, *keys: str, reverse: bool = False) -> Table:
        if not keys:
            return self
        indexes = []
        for key in keys:
            name = key.lstrip("-+")
            for i, col in enumerate(self.columns):
                if col.key == name or col.title == name:
                    indexes.append((i, key.startswith("-")))
        if not indexes:
            return self
        for index, desc in reversed(indexes):
            self._rows.sort(key=lambda r, i=index: _sort_key(r[i] if i < len(r) else ""),
                            reverse=desc)
        return self

    def truncate(self, limit: int) -> Table:
        """Keep at most *limit* rows."""
        if limit >= 0:
            del self._rows[limit:]
        return self

    def rows(self) -> list[list[str]]:
        """A copy of the raw row data."""
        return [list(row) for row in self._rows]

    def __len__(self) -> int:
        return len(self._rows)

    def __bool__(self) -> bool:
        return bool(self._rows)

    # -- rendering ---------------------------------------------------------- #

    def _visible(self) -> list[Column]:
        return [c for c in self.columns if not c.hidden]

    def _layout(self) -> tuple[list[int], list[int]]:
        """Return ``(visible_columns, natural_widths)`` after width negotiation."""
        cols = self._visible()
        natural: list[int] = []
        for index, col in enumerate(cols):
            widest = visible_width(col.title)
            for row in self._rows:
                if index < len(row):
                    widest = max(widest, visible_width(row[index]))
            if col.max_width:
                widest = min(widest, col.max_width)
            natural.append(max(widest, col.min_width))
        return cols, natural

    def _shrink(self, widths: list[int], cols: list[Column], budget: int) -> list[int]:
        """Greedily steal space from the widest flexible columns until *budget* fits."""
        guards = {c.key for c in cols if c.max_width is None}
        overflow = sum(widths) - budget
        while overflow > 0:
            candidates = [
                i
                for i, (w, c) in enumerate(zip(widths, cols, strict=False))
                if c.key in guards and w > 6
            ]
            if not candidates:
                break
            widest = max(candidates, key=lambda i: widths[i])
            widths[widest] -= 1
            overflow -= 1
        return widths

    def render(self, max_width: int | None = None) -> str:
        """Render as a terminal table with box drawing."""
        cols, widths = self._layout()
        if not cols:
            return ""
        budget = max_width if max_width is not None else self.max_width
        if budget is None:
            budget = terminal_size()[0]
        prefix = 4 if self.row_numbers else 0
        widths = self._shrink(widths, cols, budget - prefix - 3 * len(cols) - 1)
        box = _BOXES[self.style]

        head_cells = [truncate(c.title, w, "") for c, w in zip(cols, widths, strict=False)]
        if self.row_numbers:
            head_cells.insert(0, "#")
            widths.insert(0, 4)

        lines: list[str] = []
        border = lambda row_value, m, r, fill: (  # noqa: E731
            row_value + m.join(fill * (w + 2) for w in widths) + r
        )
        if self.style in ("ascii", "unicode"):
            lines.append(border(box["tl"], box["tt"], box["tr"], box["h"]))
        if self.show_header and any(t.strip() for t in head_cells):
            lines.extend(self._line(head_cells, widths, cols, header=True))
            if self.style in ("ascii", "unicode"):
                lines.append(border(box["lt"], box["x"], box["rt"], box["nm"]))
        if self.show_rows:
            for n, row in enumerate(self._rows, 1):
                out = [truncate(row[i] if i < len(row) else "", w, "")
                       for i, w in enumerate(widths)]
                if self.row_numbers:
                    out.insert(0, str(n))
                lines.extend(self._line(out, widths, cols, zebra=bool(self.zebra and n % 2 == 0)))
        if self.style in ("ascii", "unicode"):
            lines.append(border(box["bl"], box["bt"], box["br"], box["h"]))
        return "\n".join(_rstrip_ansi(line) for line in lines).rstrip()

    def _line(
        self,
        values: list[str],
        widths: list[int],
        cols: list[Column],
        *,
        header: bool = False,
        zebra: bool = False,
    ) -> list[str]:
        box = _BOXES[self.style]
        effective = list(cols)
        if self.row_numbers:
            effective = [Column(key="#", title="#", align="right"), *effective]

        wrapped = [str(values[i]).split("\n") for i in range(len(values))]
        height = max((len(w) for w in wrapped), default=1)
        out = []
        for row_index in range(height):
            cells_ = []
            for i, (col, width) in enumerate(zip(effective, widths, strict=False)):
                raw = wrapped[i][row_index] if row_index < len(wrapped[i]) else ""
                style = "bold" if header else (col.style or ("gray" if zebra else ""))
                align = col.align if col.align != "auto" else "left"
                cells_.append(A.paint(style, pad(truncate(raw, width, ""), width, align)))
            if self.style == "markdown":
                if header and row_index == 0:
                    out.append("| " + " | ".join(cells_) + " |")
                    out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
                else:
                    out.append("| " + " | ".join(cells_) + " |")
            else:
                out.append(
                    box["v"] + box["v"].join(f" {c} " for c in cells_) + box["v"]
                )
        return [_rstrip_ansi(line) for line in out]

    # -- serialisations ----------------------------------------------------- #

    def to_csv(self, delimiter: str = ",") -> str:
        import csv

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        if self.show_header:
            writer.writerow([c.title for c in self._visible()])
        for row in self._rows:
            writer.writerow(row)
        return buf.getvalue().rstrip("\n")

    def to_html(self) -> str:
        cols = self._visible()
        parts = ["<table>", "  <thead>", "    <tr>"]
        parts += [f"      <th>{_escape(c.title)}</th>" for c in cols]
        parts += ["    </tr>", "  </thead>", "  <tbody>"]
        for row in self._rows:
            parts.append("    <tr>")
            for i in range(len(cols)):
                parts.append(f"      <td>{_escape(row[i] if i < len(row) else '')}</td>")
            parts.append("    </tr>")
        parts += ["  </tbody>", "</table>"]
        return "\n".join(parts)

    def to_latex(self) -> str:
        cols = self._visible()
        spec = "".join("row_value" if c.align != "right" else "r" for c in cols)
        parts = [
            "\\begin{tabular}{" + spec + "}",
            "\\toprule",
            " & ".join(_escape(c.title) for c in cols) + " \\\\",
            "\\midrule",
        ]
        for row in self._rows:
            parts.append(
                " & ".join(_escape(row[i] if i < len(row) else "") for i in range(len(cols)))
                + " \\\\"
            )
        parts += ["\\bottomrule", "\\end{tabular}"]
        return "\n".join(parts)

    def to_json(self, pretty: bool = True) -> str:
        import json

        keys = [c.key for c in self._visible()]
        records = [
            {keys[i] if i < len(keys) else f"col{i}": row[i] for i in range(len(row))}
            for row in self._rows
        ]
        return json.dumps(records, indent=2 if pretty else None, default=str)

    def to_markdown(self) -> str:
        clone = Table(self._visible(), style="markdown")
        clone._rows = [list(r) for r in self._rows]
        clone.show_rows = True
        return clone.render()


def _rstrip_ansi(text: str) -> str:
    """Strip trailing spaces that sit *before* a trailing reset sequence."""
    stripped = text.rstrip()
    if stripped == text:
        return text
    return stripped + "\x1b[0m" if text.endswith("\x1b[0m") else stripped


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _is_numeric(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value).strip().replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return False
    return True


def _sort_key(text: str) -> tuple[int, float, str]:
    stripped = str(text).strip()
    if _is_numeric(stripped):
        return (0, float(stripped.replace("_", "")), "")
    return (1, 0.0, stripped.lower())


# --------------------------------------------------------------------------- #
# Trees
# --------------------------------------------------------------------------- #

_TREE_BOX = {
    "unicode": ("\u251c\u2500\u2500 ", "\u2514\u2500\u2500 ", "\u2502   ", "    "),
    "ascii": ("|-- ", "`-- ", "|   ", "    "),
}


@dataclass
class TreeNode:
    """A node in a rendered tree."""

    label: str
    children: list[TreeNode] = field(default_factory=list)
    value: str | None = None
    kind: str = ""

    def add(self, label: str, **kwargs: object) -> TreeNode:
        child = TreeNode(label, **kwargs)  # type: ignore[arg-type]
        self.children.append(child)
        return child


class Tree:
    """Unicode tree renderer for nested mappings and filesystem-like output."""

    def __init__(self, style: str = "unicode", max_depth: int = 32) -> None:
        self.style = style if style in _TREE_BOX else "unicode"
        self.max_depth = max_depth

    def render(self, root: TreeNode) -> str:
        lines: list[str] = []
        self._walk(root, lines, "", depth=0, is_last=True, root=True)
        return "\n".join(lines)

    def from_dict(self, data: dict[str, object]) -> str:
        """Render a nested mapping/sequence straight to a tree string."""
        root = TreeNode("<root>")
        self._fill(root, data)
        return self.render(root)

    def _fill(self, node: TreeNode, data: object) -> None:
        if isinstance(data, dict):
            for key, value in data.items():
                child = node.add(str(key))
                if isinstance(value, (dict, list, tuple)):
                    self._fill(child, value)
                else:
                    child.value = _scalar(value)
        elif isinstance(data, (list, tuple)):
            for i, value in enumerate(data):
                child = node.add(f"[{i}]")
                if isinstance(value, (dict, list, tuple)):
                    self._fill(child, value)
                else:
                    child.value = _scalar(value)
        else:
            node.value = _scalar(data)

    def _walk(
        self,
        node: TreeNode,
        lines: list[str],
        indent: str,
        *,
        depth: int,
        is_last: bool,
        root: bool,
    ) -> None:
        branch, last, pipe, blank = _TREE_BOX[self.style]
        connector = "" if root else (last if is_last else branch)
        if depth > self.max_depth:
            lines.append(indent + connector + A.hint("\u2026"))
            return
        label = A.head(node.label) if root else A.info(node.label)
        if node.value is not None:
            label += A.hint(" = ") + A.ok(node.value)
        lines.append(indent + connector + label)
        child_indent = "" if root else indent + (blank if is_last else pipe)
        for i, child in enumerate(node.children):
            self._walk(
                child, lines, child_indent,
                depth=depth + 1, is_last=i == len(node.children) - 1, root=False,
            )


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #

_SPINNER_FRAMES = "\u25f0\u25f1\u25f2\u25f3"


class Progress:
    """A progress reporter with three tiers of fidelity.

    * TTY: a live single-line bar or spinner, redrawn with CR.
    * Non-TTY: periodic newline-terminated updates (pager-friendly, greppable).
    * ``enabled=False``: nothing but the final summary.

    Nothing here ever writes to stdout if stdout is a pipe and ``quiet`` was
    requested -- that is the whole point of checking ``stream.isatty()``.
    """

    def __init__(
        self,
        total: int | None = None,
        *,
        label: str = "",
        stream: IO[str] | None = None,
        enabled: bool = True,
        style: str = "bar",
        width: int = 28,
        throttle: float = 0.1,
    ) -> None:
        self.total = total
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.style = style
        self.width = width
        self.throttle = throttle
        self.count = 0
        self._start = time.monotonic()
        self._last = 0.0
        self._frame = 0
        self._done = False
        self._tty = False
        try:
            self._tty = bool(self.stream.isatty()) and supports_color(self.stream)
        except Exception:
            self._tty = False
        if not enabled:
            self._tty = False

    def __enter__(self) -> Progress:
        return self

    def __exit__(self, *exc: object) -> None:
        self.finish()

    def advance(self, step: int = 1, *, label: str | None = None) -> None:
        if not self.enabled:
            return
        self.count += step
        if label is not None:
            self.label = label
        self._render(final=False)

    def set(self, count: int, *, label: str | None = None) -> None:
        if not self.enabled:
            return
        self.count = count
        if label is not None:
            self.label = label
        self._render(final=False)

    def message(self, text: str) -> None:
        """Print an above-the-progress message without corrupting the bar."""
        if not self.enabled:
            return
        if self._tty:
            self._clear()
            self._write(A.hint(text) + "\n")
        else:
            self._write(A.hint(text) + "\n")
        self._last = 0.0

    def finish(self, summary: str | None = None) -> None:
        if self._done or not self.enabled:
            return
        self._done = True
        if summary:
            if self._tty:
                self._clear()
                self._write(A.ok(summary) + "\n")
            else:
                self._write(A.ok(summary) + "\n")
            return
        if self.total is not None and self.total > 0:
            self._render(final=True)
        else:
            if self._tty:
                self._clear()
            self._write(A.hint(f"done {self._elapsed()}\n"))

    def _elapsed(self) -> str:
        return human_duration(time.monotonic() - self._start)

    def _clear(self) -> None:
        self._write("\r\x1b[2K")

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def _render(self, *, final: bool) -> None:
        now = time.monotonic()
        if not final and now - self._last < self.throttle:
            return
        self._last = now
        if not self._tty:
            if self.total:
                pct = 100.0 * self.count / max(self.total, 1)
                self._write(f"{self.label} {self.count}/{self.total} ({pct:.0f}%)\n")
            return
        self._clear()
        if self.total:
            frac = min(1.0, self.count / max(self.total, 1))
            filled = int(self.width * frac)
            bar = "\u2588" * filled + "\u2591" * (self.width - filled)
            pct = f"{frac * 100:5.1f}%"
            self._write(f"{A.bold(self.label or 'working')} {A.cyan(bar)} {pct} {self._elapsed()}")
        else:
            self._frame += 1
            spinner = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
            self._write(f"{A.cyan(spinner)} {self.label} {self.count} {self._elapsed()}")
        if final:
            self._write("\n")


class Stream:
    """A stdout/stderr pair that never crashes on encoding problems.

    Windows consoles still default to legacy code pages; Unicode box drawing
    characters and emoji would raise ``UnicodeEncodeError`` deep inside a
    command. This wrapper reconfigures the stream once and swallows nothing.
    """

    def __init__(self, stream: IO[str] | None = None, name: str = "stdout") -> None:
        self.name = name
        self._stream = stream if stream is not None else getattr(sys, name)
        self._reconfigured = False

    @property
    def raw(self) -> IO[str]:
        return self._stream

    def _ensure(self) -> None:
        if self._reconfigured:
            return
        self._reconfigured = True
        try:
            if self._stream.encoding and self._stream.encoding.lower() not in (
                "utf-8", "utf8",
            ):
                self._stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass

    def write(self, text: str) -> int:
        self._ensure()
        try:
            return self._stream.write(text)
        except UnicodeEncodeError:
            return self._stream.write(
                text.encode("ascii", "replace").decode("ascii")
            )

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self._stream.fileno()


def is_broken_pipe(stream: IO[str]) -> bool:
    """True when *stream* died because the reader closed (``head``, ``q``)."""
    try:
        stream.write("")
        stream.flush()
    except (BrokenPipeError, ValueError):
        return True
    except OSError:
        return True
    return False
