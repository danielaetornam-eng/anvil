"""``anvil json`` -- query, validate, merge, and transform JSON documents.

Argparse patterns on display:

* a two-level subparser tree (``anvil json query``, ``anvil json merge``, ...);
* ``action='store_true'`` pairs for mutually exclusive modes, wrapped in a
  ``mutually_exclusive_group``;
* ``nargs='?'`` with ``const`` to express "optional value";
* a **recursive parser**: the query language itself is parsed with a private
  ``ArgumentParser``, which is the cleanest way to give a mini-language real
  error messages and ``--help``.
"""

from __future__ import annotations

import argparse
import json

from anvil.core import term
from anvil.core.app import Context
from anvil.core.errors import InputError, UsageError
from anvil.core.io import read_text
from anvil.core.types import output_format as output_format_type

HELP = "query, validate, merge, diff and reshape JSON"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "json", aliases=list(aliases), help=HELP,
        description="Work with JSON documents.\n\n"
                    "Reads from files or stdin; `-` means stdin and omitting the "
                    "file also means stdin, so `cat x.json | anvil json format` "
                    "works.",
    )
    parser.add_examples(
        "anvil json format data.json --indent 4",
        "anvil json query -p '.users[] | select(.age > 30)' data.json",
        "anvil json merge base.json override.json --array replace",
        "anvil json validate --strict --output compact big.json",
    )

    subs = parser.add_subparsers(dest="json_command", metavar="SUBCOMMAND")
    subs.required = True

    _format_cmd(subs, prog)
    _query_cmd(subs, prog)
    _validate_cmd(subs, prog)
    _merge_cmd(subs, prog)
    _diff_cmd(subs, prog)
    _paths_cmd(subs, prog)
    return parser


# --------------------------------------------------------------------------- #
# format
# --------------------------------------------------------------------------- #


def _format_cmd(subs, prog: str) -> None:
    p = subs.add_parser("format", help="pretty-print or compact JSON",
                        description="Re-emit a JSON document in a chosen shape.")
    p.add_argument("--indent", type=int, default=2, metavar="N",
                   help="spaces per level (0 compacts)")
    p.add_argument("--sort-keys", action="store_true", default=False,
                   help="emit object keys in sorted order")
    p.add_argument("--ascii", action="store_true", default=False,
                   help="escape non-ASCII as \\uXXXX")
    p.add_argument("file", nargs="?", default="-", metavar="FILE",
                   help="input file, or - for stdin")
    p.set_defaults(func=_format)


def _format(ctx: Context, args: argparse.Namespace) -> int:
    data = _load(args.file, None)
    text = json.dumps(
        data,
        indent=args.indent if args.indent > 0 else None,
        sort_keys=args.sort_keys,
        ensure_ascii=args.ascii,
        separators=(",", ": ") if args.indent > 0 else (",", ":"),
    )
    ctx.out(text)
    return 0


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


def _validate_cmd(subs, prog: str) -> None:
    p = subs.add_parser("validate", help="check JSON validity",
                        description="Report whether input is valid JSON, "
                                    "optionally enforcing strictness.")
    strict = p.add_mutually_exclusive_group()
    strict.add_argument("--strict", action="store_true", default=False,
                        help="reject duplicate object keys")
    strict.add_argument("--no-strict", dest="strict", action="store_false",
                        help="allow duplicate object keys (default)")
    p.add_argument("--silent", "-q", action="store_true", default=False,
                   help="print nothing; use the exit code")
    p.add_argument("--show-paths", action="store_true", default=False,
                   help="list every path in the document")
    p.add_argument("file", nargs="?", default="-", metavar="FILE")
    p.set_defaults(func=_validate)


def _validate(ctx: Context, args: argparse.Namespace) -> int:
    text = read_text(args.file)
    duplicates: list[str] = []

    def hook(pairs: list[tuple[str, object]]) -> dict:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen and args.strict:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    try:
        data = json.loads(text, object_pairs_hook=hook)
    except json.JSONDecodeError as exc:
        if not args.silent:
            ctx.out(ctx.renderer.scalar({
                "valid": False,
                "error": exc.msg,
                "line": exc.lineno,
                "column": exc.colno,
            }))
        raise InputError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            hint="check for a trailing comma or an unquoted key",
        ) from None

    if args.show_paths:
        for path, _kind, _value in _walk_paths(data):
            ctx.out(path)
    elif not args.silent:
        ctx.out(ctx.renderer.scalar({
            "valid": True,
            "duplicates": duplicates or None,
            "size_bytes": len(text.encode("utf-8")),
        }))
    if duplicates:
        raise InputError(f"duplicate keys: {', '.join(sorted(set(duplicates))[:5])}",
                         hint="drop --strict to allow them")
    return 0


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #


def _merge_cmd(subs, prog: str) -> None:
    p = subs.add_parser("merge", help="deep-merge documents",
                        description="Merge two JSON documents, deepest keys last.")
    arrays = p.add_mutually_exclusive_group()
    arrays.add_argument("--array", choices=("replace", "concat", "unique"),
                        default="replace",
                        help="how to combine arrays (default: replace)")
    arrays.add_argument("--delete", action="store_true", default=False,
                        help="keys mapped to null delete instead of merging")
    p.add_argument("--output", "-o", type=output_format_type, default="auto",
                   metavar="FORMAT", help="output format for the merged document")
    p.add_argument("base", metavar="BASE")
    p.add_argument("overlay", metavar="OVERLAY")
    p.set_defaults(func=_merge)


def _merge(ctx: Context, args: argparse.Namespace) -> int:
    base = _load(args.base, None)
    overlay = _load(args.overlay, None)
    merged = _deep_merge(base, overlay, arrays=args.array, delete=args.delete)
    ctx.out(_render(ctx, merged))
    return 0


def _deep_merge(base, overlay, *, arrays: str = "replace", delete: bool = False):
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            if delete and value is None:
                out.pop(key, None)
            elif key in out:
                out[key] = _deep_merge(out[key], value, arrays=arrays, delete=delete)
            else:
                out[key] = value
        return out
    if isinstance(base, list) and isinstance(overlay, list):
        if arrays == "concat":
            return base + overlay
        if arrays == "unique":
            seen: list[object] = []
            for item in base + overlay:
                if item not in seen:
                    seen.append(item)
            return seen
        return overlay
    return overlay


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def _diff_cmd(subs, prog: str) -> None:
    p = subs.add_parser("diff", help="compare two documents",
                        description="Structural diff; reports additions, "
                                    "removals and changes.")
    p.add_argument("--path", default=None, metavar="PATH",
                   help="compare only this subtree")
    p.add_argument("--ignore-order", action="store_true", default=False,
                   help="compare arrays as multisets")
    p.add_argument("left", metavar="LEFT")
    p.add_argument("right", metavar="RIGHT")
    p.set_defaults(func=_diff)


def _diff(ctx: Context, args: argparse.Namespace) -> int:
    left = _load(args.left, None)
    right = _load(args.right, None)
    if args.path:
        left = _dig(left, args.path)
        right = _dig(right, args.path)
    changes = _structural_diff("", left, right, ignore_order=args.ignore_order)
    table = term.Table(
        [term.Column("path", "path"), term.Column("change", "change"),
         term.Column("from", "from"), term.Column("to", "to")],
    )
    for change in changes:
        table.add_row([change["path"] or "/", change["op"], change["from"],
                       change["to"]])
    ctx.render(table if table else "")
    return 0 if not changes else 1


def _structural_diff(path: str, left, right, *, ignore_order: bool = False):
    changes: list[dict[str, str]] = []
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in right:
                changes.append({"path": child, "op": "removed",
                                "from": _brief(left[key]), "to": ""})
            elif key not in left:
                changes.append({"path": child, "op": "added",
                                "from": "", "to": _brief(right[key])})
            else:
                changes += _structural_diff(child, left[key], right[key],
                                             ignore_order=ignore_order)
    elif isinstance(left, list) and isinstance(right, list):
        if ignore_order:
            if sorted(map(_canon, left)) != sorted(map(_canon, right)):
                changes.append({"path": path or "/", "op": "reordered",
                                "from": _brief(left), "to": _brief(right)})
        else:
            for index in range(max(len(left), len(right))):
                child = f"{path}[{index}]"
                if index >= len(right):
                    changes.append({"path": child, "op": "removed",
                                    "from": _brief(left[index]), "to": ""})
                elif index >= len(left):
                    changes.append({"path": child, "op": "added",
                                    "from": "", "to": _brief(right[index])})
                else:
                    changes += _structural_diff(child, left[index], right[index],
                                                 ignore_order=ignore_order)
    elif left != right:
        changes.append({"path": path or "/", "op": "changed",
                        "from": _brief(left), "to": _brief(right)})
    return changes


def _canon(value):
    return json.dumps(value, sort_keys=True, default=str)


def _brief(value, limit: int = 48) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


def _paths_cmd(subs, prog: str) -> None:
    p = subs.add_parser("paths", help="list every path in a document",
                        description="Enumerate every JSONPath-like path in a "
                                    "document; useful for building masks.")
    p.add_argument("--depth", type=int, default=None, metavar="N",
                   help="stop descending past this depth")
    p.add_argument("--leaves-only", action="store_true", default=False,
                   help="only report paths that end at a scalar")
    p.add_argument("file", nargs="?", default="-", metavar="FILE")
    p.set_defaults(func=_paths)


def _paths(ctx: Context, args: argparse.Namespace) -> int:
    data = _load(args.file, None)
    rows = [{"path": path, "type": kind, "value": value}
            for path, kind, value in _walk_paths(data, args.depth, args.leaves_only)]
    if ctx.output_format in ("table", "auto") and ctx.color:
        ctx.render(ctx.table().add_dicts(rows))
    else:
        ctx.out("\n".join(r["path"] for r in rows))
    return 0


def _walk_paths(data, prefix: str = "", depth: int | None = None,
                leaves_only: bool = False, _level: int = 0):
    level = 0 if depth is None else _level
    if isinstance(data, dict):
        if not data and not leaves_only:
            yield prefix or "/", "object", "{}"
        for key, value in data.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_paths(value, child, depth, leaves_only, level + 1)
    elif isinstance(data, list):
        if not data and not leaves_only:
            yield prefix or "/", "array", "[]"
        for index, value in enumerate(data):
            child = f"{prefix}[{index}]"
            yield from _walk_paths(value, child, depth, leaves_only, level + 1)
    else:
        kind = {bool: "boolean", int: "number", float: "number",
                str: "string", type(None): "null"}.get(type(data), "unknown")
        yield prefix or "/", kind, data


# --------------------------------------------------------------------------- #
# query  (a real mini-language, parsed by a nested argparse parser)
# --------------------------------------------------------------------------- #


def _query_cmd(subs, prog: str) -> None:
    p = subs.add_parser(
        "query", aliases=["q"], help="select parts of a document",
        description="Evaluate a small query language against JSON.\n\n"
                    "Grammar:\n"
                    "  .key            child of the current node\n"
                    "  [n]             index (negative counts from the end)\n"
                    "  [*] or .[]      every element\n"
                    "  ..key           recursive descent\n"
                    "  {a,b}           multiple children\n"
                    "  | select(FILTER) keep elements where FILTER is true\n"
                    "  | map(FILTER)    project each element\n"
                    "  | sort[BY]       sort by a child key\n"
                    "  | first / last   take one end of a sequence\n"
                    "  | count          number of elements\n"
                    "  | keys / values  object keys or values\n"
                    "  | unique         drop duplicates\n",
        formatter_class=_sub_formatter(),
    )
    p.add_examples(
        "anvil json query '.users[] | select(.age > 30)' users.json",
        "anvil json query '..email' config.json",
        "anvil json query '.items | sort(-price) | first' feed.json",
    )
    p.add_argument("-p", "--path", default=None, metavar="QUERY",
                   help="query string (may also be given as the first argument)")
    p.add_argument("--slurp", action="store_true", default=False,
                   help="wrap the whole input in a list before querying")
    p.add_argument("--raw-output", action="store_true", default=False,
                   help="print scalars bare instead of JSON-encoded")
    p.add_argument("--compact", action="store_true", default=False,
                   help="emit compact JSON")
    p.add_argument("query_or_file", nargs="?", default=None, metavar="QUERY")
    p.add_argument("file", nargs="?", default="-", metavar="FILE")
    p.set_defaults(func=_query)


def _sub_formatter():
    from anvil.core.formatters import AnvilHelpFormatter

    return AnvilHelpFormatter


def _filter_parser() -> argparse.ArgumentParser:
    """A throwaway parser used to validate one ``select()`` filter.

    Built on demand rather than at import time so that a module-level parser
    cannot be mutated by an earlier command run in the same process (a real
    hazard when tests share an interpreter).
    """
    parser = argparse.ArgumentParser(prog="FILTER", add_help=False,
                                     exit_on_error=False, allow_abbrev=False)
    parser.add_argument("field", metavar=".FIELD")
    parser.add_argument("operator", choices=[">", "<", ">=", "<=", "==", "!="])
    parser.add_argument("value", metavar="VALUE", nargs="?", default="")
    return parser


def _tokenize(expression: str) -> list[str]:
    """Split a filter, keeping quoted runs together.

    ``.name == "ada lovelace"`` must not become four tokens.
    """
    import shlex

    try:
        return shlex.split(expression)
    except ValueError:
        return expression.split()


def _query(ctx: Context, args: argparse.Namespace) -> int:
    # With --path, the single positional is the file; without it, the first
    # positional is the query and the second (optional) is the file.
    if args.path:
        query = args.path
        path = args.query_or_file or "-"
    else:
        query = args.query_or_file
        path = args.file
    if not query:
        raise UsageError("no query given",
                         hint="try 'anvil json query .field data.json'")

    data = _load(path, None)
    if args.slurp:
        data = [data]
    results = evaluate(data, query)
    if args.raw_output and not isinstance(results, (list, dict)):
        ctx.out(str(results))
        return 0
    indent = None if args.compact else 2
    ctx.out(json.dumps(results, indent=indent, default=str, ensure_ascii=False))
    return 0


def evaluate(data, query: str) -> object:
    """Evaluate *query* against *data*. Public so the docs can call it."""
    head, _, tail = query.partition("|")
    segments = [s.strip() for s in head.split(".") if s.strip()]
    if not segments and head.strip() not in ("", "*"):
        segments = [head.strip()]
    current: object = data
    for segment in segments:
        current = _descend(current, segment)
    for stage in (s.strip() for s in tail.split("|")):
        if stage:
            current = _apply_stage(current, stage)
    return current


def _descend(node: object, segment: str) -> object:
    if segment in ("", "*", "[]"):
        return _as_list(node)
    if ".." in segment:
        _, _, rest = segment.partition("..")
        return _descend(node, rest) if rest else _recursive(node)
    # A leading "." is path syntax, not part of the key: "map(.name)" must not
    # look up the literal key ".name".
    segment = segment[1:] if segment.startswith(".") else segment
    if segment.startswith("[") and segment.endswith("]"):
        return _index(node, segment[1:-1])
    if "{" in segment and "}" in segment:
        keys = segment[segment.index("{") + 1: segment.rindex("}")].split(",")
        out = []
        for key in (k.strip() for k in keys):
            if isinstance(node, dict):
                out.append(node.get(key))
            elif isinstance(node, list):
                out += [item.get(key) for item in node if isinstance(item, dict)]
        return out
    if "[" in segment and segment.endswith("]"):
        name, _, index = segment.partition("[")
        return _index(_descend(node, name), index[:-1])
    # A bare integer indexes a list, so ``.0.name`` and ``[0].name`` agree.
    if isinstance(node, list) and segment.lstrip("-").isdigit():
        return _index(node, segment)
    if isinstance(node, dict):
        return node.get(segment)
    if isinstance(node, list):
        return [item.get(segment) for item in node if isinstance(item, dict)]
    return None


def _as_list(node: object) -> list:
    return node if isinstance(node, list) else ([] if node is None else [node])


def _index(node: object, raw: str) -> object:
    items = _as_list(node)
    if raw in ("", "*"):
        return items
    if not isinstance(node, list):
        return None
    try:
        position = int(raw)
    except ValueError:
        return None
    try:
        return items[position]
    except IndexError:
        return None


def _recursive(node: object) -> list:
    found: list = []

    def walk(item: object) -> None:
        if isinstance(item, dict):
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(node)
    return found


def _apply_stage(current: object, stage: str) -> object:
    if stage.startswith("select(") and stage.endswith(")"):
        return _select(current, stage[7:-1])
    if stage.startswith("map(") and stage.endswith(")"):
        return [_descend(item, stage[4:-1]) for item in _as_list(current)]
    if stage.startswith("sort"):
        return _sort(current, stage[4:].strip("[]"))
    if stage == "first":
        items = _as_list(current)
        return items[0] if items else None
    if stage == "last":
        items = _as_list(current)
        return items[-1] if items else None
    if stage == "count":
        if isinstance(current, dict):
            return len(current)
        return len(_as_list(current))
    if stage == "keys":
        return sorted(current) if isinstance(current, dict) else []
    if stage == "values":
        return list(current.values()) if isinstance(current, dict) else _as_list(current)
    if stage == "unique":
        seen: list = []
        for item in _as_list(current):
            if item not in seen:
                seen.append(item)
        return seen
    if stage in ("length", "len"):
        return len(current)
    if stage.startswith("keys("):
        return sorted(current) if isinstance(current, dict) else []
    raise InputError(
        f"unknown query stage {stage!r}",
        hint="try select(...), map(...), sort[...], first, last, count, keys, "
             "values, unique",
    )


def _select(current: object, expression: str) -> list:
    """Keep elements whose *field* compares equal to a literal.

    The right-hand side is parsed with a **nested argparse parser**, which is
    the trick that makes a mini-language produce real CLI errors: unknown
    operators, missing operands and stray quotes all surface as
    ``FILTER: error: ...`` instead of a hand-written exception message that
    drifts out of sync with the grammar.
    """
    tokens = _tokenize(expression)
    # The nested parser owns the grammar; we only need the parts back.
    try:
        parsed = _filter_parser().parse_args(tokens)
    except (SystemExit, argparse.ArgumentError):
        raise InputError(
            f"cannot parse filter {expression!r}",
            hint="filters look like '.age > 30' or '.name == \"ada\"'",
        ) from None
    field = parsed.field
    operator = parsed.operator
    wanted = str(parsed.value or "").strip()
    if not field:
        raise InputError(f"filter {expression!r} has no field",
                         hint="filters look like '.age > 30'")
    out: list = []
    for item in _as_list(current):
        value = item.get(field.lstrip(".")) if isinstance(item, dict) else None
        if _compare(value, operator, wanted):
            out.append(item)
    return out


def _compare(value, operator: str, wanted: str) -> bool:
    def coerce(candidate):
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return candidate
        text = str(candidate)
        try:
            return float(text)
        except (TypeError, ValueError):
            return text

    left, right = coerce(value), coerce(wanted)
    try:
        if operator == ">":
            return left > right
        if operator == "<":
            return left < right
        if operator == ">=":
            return left >= right
        if operator == "<=":
            return left <= right
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
    except TypeError:
        return False
    return False


def _sort(current: object, by: str) -> list:
    items = list(_as_list(current))
    descending = by.startswith("-")
    key = by.lstrip("-")
    if not key:
        items.sort(key=lambda v: (isinstance(v, (int, float)) is False, str(v)))
        return items
    def sort_key(value):
        inner = value.get(key) if isinstance(value, dict) else value
        return (inner is None, str(inner) if not isinstance(inner, (int, float)) else inner)
    items.sort(key=sort_key, reverse=descending)
    return items


def _dig(data: object, path: str) -> object:
    current = data
    for part in path.strip().split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = _index(current, part)
        else:
            return None
    return current


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #


def _load(path, hint_path):
    text = read_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}",
            hint=hint_path or "check the file for syntax errors",
        ) from None


def _render(ctx: Context, data) -> str:
    return json.dumps(data, indent=2, default=str, ensure_ascii=False)

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _query
