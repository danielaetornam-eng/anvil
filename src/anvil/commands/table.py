"""``anvil table`` -- render structured data as a table.

Argparse patterns: ``choices=`` driving a custom formatter, ``nargs='+'`` on an
optional for repeated column flags, an ``auto_name`` action for ``--define
key=value``, and a two-level subparser to pick the *input* format.
"""

from __future__ import annotations

import argparse
import csv
import io
import json

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, UsageError
from anvil.core.io import read_text
from anvil.core.types import csv_list, table_style

HELP = "render JSON/CSV/YAML data as a formatted table"

ALIGNMENTS = ("auto", "left", "right", "center")


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "table", aliases=list(aliases), help=HELP,
        description="Render records as an aligned table.\n\n"
                    "Reads JSON arrays/objects, CSV, or YAML; auto-detects when "
                    "you do not say.",
    )
    parser.add_examples(
        "anvil table users.json",
        "anvil table --from csv --separator ';' data.csv --style markdown",
        "anvil table --columns name,age --align age=right feed.json",
        "anvil table -D label=Total --sort -age data.json",
    )

    src = parser.add_argument_group("Input")
    src.add_argument("--from", dest="source", choices=("auto", "json", "csv",
                                                      "tsv", "yaml", "lines"),
                     default="auto", metavar="KIND",
                     help="input format (default: auto-detect)")
    src.add_argument("--separator", default=",", metavar="CHAR",
                     help="CSV delimiter")
    src.add_argument("--header", action="store_true", default=False,
                     help="treat the first line as column names")
    src.add_argument("--no-header", dest="header", action="store_false",
                     help="the first line is data, not a header")

    view = parser.add_argument_group("Shape")
    view.add_argument("--columns", type=csv_list, default=None, metavar="C[,C]",
                      help="columns to include, in order")
    view.add_argument("--exclude", action="comma_list", default=[],
                      metavar="C[,C]", help="columns to drop")
    view.add_argument("--sort", action="comma_list", default=[], metavar="C[,C]",
                      help="sort keys; prefix with - for descending")
    view.add_argument("--limit", type=int, default=None, metavar="N",
                      help="show at most N rows")
    view.add_argument("--align", action="comma_list", default=[],
                      metavar="C=ALIGN", help="per-column alignment")
    view.add_argument("--rename", action="key_value", default={},
                      metavar="OLD=NEW", help="rename a column")
    view.add_argument("-D", "--define", action="key_value", dest="defines",
                      default={}, metavar="KEY=VALUE",
                      help="add a constant column named KEY")
    view.add_argument("--total", action="key_value", default={},
                      metavar="C=SUM", help="append a totals row for a column")

    paint = parser.add_argument_group("Appearance")
    paint.add_argument("--style", type=table_style, default=None,
                       metavar="STYLE", help="box drawing style")
    paint.add_argument("--numbers", dest="numbers", action="store_true",
                       default=False, help="right-align numeric columns")
    paint.add_argument("--width", type=int, default=None, metavar="N",
                       help="maximum total width")
    paint.add_argument("--max-column-width", type=int, default=None, metavar="N",
                       help="truncate any column to N characters")
    paint.add_argument("--row-numbers", action="store_true", default=False,
                       help="print an index column")

    parser.add_argument("file", nargs="?", default="-", metavar="FILE",
                        help="input file, or - for stdin")
    parser.set_defaults(func=_table)
    return parser


@register_command("table")
def _table(ctx: Context, args: argparse.Namespace) -> int:
    text = read_text(args.file)
    if not text.strip():
        raise InputError("input is empty", hint="nothing to tabulate")
    records = _parse(ctx, args, text)
    if not records:
        ctx.err(term.A.warn("no rows"))
        return 0

    # ``-D key=value`` adds a constant column, so the values have to be in the
    # data *before* columns are chosen -- otherwise ``--columns name,region``
    # cannot see a column the user just asked for.
    if args.defines:
        records = [dict(record, **args.defines) for record in records]

    # This command's whole purpose is a table, so `auto` must resolve to
    # `table` even when stdout is a pipe; the format only becomes plain when
    # the user explicitly asks for it.
    if ctx.renderer.requested == "auto":
        ctx.renderer.format = args.style or "table"

    columns = _choose_columns(args, records)
    rows = [[_cell(record.get(name)) for name in columns] for record in records]

    table = term.Table(
        [term.Column(name, str(args.rename.get(name, name)),
                     align=_align(args, name, rows, index))
         for index, name in enumerate(columns)],
        style=args.style or ctx.option("table_style") or "unicode",
        max_width=args.width,
        row_numbers=args.row_numbers,
    )
    for row in rows:
        table.add_row(row)

    totals = _totals(args, records, columns)
    if totals is not None:
        table.add_row(totals)
    if args.sort:
        table.sort(*args.sort)
    if args.limit is not None:
        table.truncate(args.limit)
    if args.max_column_width:
        for column in table.columns:
            column.max_width = args.max_column_width

    ctx.render(table)
    return 0


def _parse(ctx: Context, args: argparse.Namespace, text: str) -> list[dict]:
    kind = args.source
    if kind == "auto":
        kind = _sniff(text)
    if kind == "json":
        return _from_json(text)
    if kind in ("csv", "tsv"):
        return _from_csv(text, args.separator if kind == "csv" else "\t", args.header)
    if kind == "yaml":
        return _from_yaml(text)
    if kind == "lines":
        return [{"line": line} for line in text.splitlines() if line.strip()]
    raise UsageError(f"unknown input format {kind!r}",
                     hint="choose from auto, json, csv, tsv, yaml, lines")


def _sniff(text: str) -> str:
    stripped = text.lstrip()
    if stripped[:1] in "[{":
        return "json"
    if stripped.startswith("---"):
        return "yaml"
    if "\t" in text.splitlines()[0] if text.splitlines() else False:
        return "tsv"
    return "csv"


def _from_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            hint="pass --from csv if this is not JSON",
        ) from None
    if isinstance(data, dict):
        for key in ("items", "data", "results", "rows", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return [{"key": k, "value": _cell(v)} for k, v in data.items()]
    if not isinstance(data, list):
        raise InputError(f"expected a JSON array of objects, got {type(data).__name__}",
                         hint="wrap records in an array, or use --from lines")
    return [row if isinstance(row, dict) else {"value": _cell(row)} for row in data]


def _from_csv(text: str, delimiter: str, header: bool) -> list[dict]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []
    if header:
        names = [c.strip() or f"col{i}" for i, c in enumerate(rows[0])]
        return [dict(zip(names, row, strict=False)) for row in rows[1:]]
    width = len(rows[0])
    names = [f"col{i}" for i in range(width)]
    return [dict(zip(names, row, strict=False)) for row in rows]


def _from_yaml(text: str) -> list[dict]:
    import pathlib

    from anvil.core.config import _load_yaml

    data = _load_yaml(text, pathlib.Path("<yaml>"))
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        return [{"key": k, "value": _cell(v)} for k, v in data.items()]
    if isinstance(data, list):
        return [r if isinstance(r, dict) else {"value": _cell(r)} for r in data]
    raise InputError("YAML input must be a mapping or a sequence")


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _choose_columns(args: argparse.Namespace, records: list[dict]) -> list[str]:
    if args.columns:
        names = list(args.columns)
        missing = [n for n in names if n not in records[0]]
        if missing:
            raise InputError(
                f"no such column(s): {', '.join(missing)}",
                hint=f"available: {', '.join(records[0])}",
            )
        return names
    names: list[str] = []
    for record in records:
        for key in record:
            if key not in names:
                names.append(key)
    return [n for n in names if n not in (args.exclude or [])]


def _align(args: argparse.Namespace, name: str, rows: list[list[str]],
          index: int) -> str:
    """Explicit ``--align`` wins; otherwise right-align all-numeric columns."""
    for spec in args.align or []:
        column, _, alignment = spec.partition("=")
        if column == name:
            if alignment not in ALIGNMENTS:
                raise InputError(
                    f"unknown alignment {alignment!r} for column {name!r}",
                    hint=f"choose from {', '.join(ALIGNMENTS)}",
                )
            return alignment
    if args.numbers:
        values = [row[index] for row in rows if index < len(row) and row[index].strip()]
        if values and all(_numeric(v) for v in values):
            return "right"
    return "left"


def _numeric(text: str) -> bool:
    cleaned = text.strip().replace(",", "").replace("_", "").lstrip("-+")
    if not cleaned:
        return False
    try:
        float(cleaned)
    except ValueError:
        return False
    return True


def _totals(args: argparse.Namespace, records: list[dict],
            columns: list[str]) -> list[str] | None:
    if not args.total:
        return None
    out = [""] * len(columns)
    for spec in args.total:
        if spec not in columns:
            raise InputError(f"--total names unknown column {spec!r}",
                             hint=f"available: {', '.join(columns)}")
        total: float = 0.0
        seen = False
        for record in records:
            try:
                total += float(record.get(spec) or 0)
                seen = True
            except (TypeError, ValueError):
                continue
        index = columns.index(spec)
        out[index] = f"{total:g}" if seen else ""
        out[0] = "Total" if out[0] == "" else out[0]
    return out

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _table
