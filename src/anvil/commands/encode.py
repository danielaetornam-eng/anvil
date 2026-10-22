"""``anvil encode`` -- transform bytes and text encodings."""

from __future__ import annotations

import argparse
import base64
import binascii
import json as _json
import pathlib
import urllib.parse

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, UsageError
from anvil.core.io import read_text
from anvil.core.types import existing_path, output_path

HELP = "base64, hex, url-encode, JSON-escape and more"

ENCODERS = ("base64", "base32", "base16", "hex", "url", "html", "json",
            "unicode-escape", "rot13", "repr", "punycode")
DECODERS = ("base64", "base32", "hex", "url", "html", "json", "unicode-escape")


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "encode", aliases=["enc"], help=HELP,
        description="Encode and decode text and bytes.\n\n"
                    "Reads stdin by default and writes stdout, so it drops "
                    "into a pipeline anywhere.",
    )
    parser.add_examples(
        "printf 'hello' | anvil encode base64",
        "anvil encode --decode base64 aGVsbG8=",
        "anvil encode url --encode-str 'a b&c'",
        "anvil encode --decode hex - < data.bin",
    )

    group = parser.add_argument_group("Direction")
    group.add_argument("--decode", "-d", dest="decode", choices=DECODERS,
                       default=None, metavar="CODEC", help="decode instead of encode")
    group.add_argument("--list", action="store_true", default=False,
                       help="list the available codecs")

    shape = parser.add_argument_group("Shape")
    shape.add_argument("--url-safe", action="store_true", default=False,
                       help="use the URL-safe base64 alphabet")
    shape.add_argument("--no-padding", action="store_true", default=False,
                       help="strip base64 padding")
    shape.add_argument("--upper", action="store_true", default=False,
                       help="uppercase hex output")
    shape.add_argument("--separators", default=None, metavar="CHARS",
                       help="override the JSON separators, e.g. ',\\t'")
    shape.add_argument("--indent", type=int, default=None, metavar="N",
                       help="indent JSON output")
    shape.add_argument("--ensure-ascii", action="store_true", default=False,
                       help="escape non-ASCII in JSON output")

    io_group = parser.add_argument_group("Input")
    io_group.add_argument("-i", "--input", type=existing_path, default=None,
                          metavar="FILE", help="read from a file instead of stdin")
    io_group.add_argument("-o", "--output", type=output_path, default=None,
                          metavar="FILE", help="write to a file instead of stdout")
    io_group.add_argument("--encode-str", default=None, metavar="TEXT",
                          help="encode this literal instead of reading stdin")

    parser.add_argument("codec", nargs="?", default="base64", choices=ENCODERS,
                        help=f"codec: {', '.join(ENCODERS)}")
    parser.set_defaults(func=_encode)
    return parser


@register_command("encode")
def _encode(ctx: Context, args: argparse.Namespace) -> int:
    if args.list:
        ctx.out("\n".join(f"{name:<16} {_describe(name)}" for name in ENCODERS))
        return 0
    if args.encode_str is not None:
        payload = args.encode_str
        binary = False
    elif args.input is not None:
        try:
            payload = pathlib.Path(args.input).read_text(encoding="utf-8")
        except OSError as exc:
            from anvil.core.errors import translate_os_error

            raise translate_os_error(exc, context=str(args.input)) from None
        binary = False
    else:
        import sys

        if sys.stdin.isatty() and not ctx.dry_run:
            raise UsageError("no input given",
                             hint="pipe something in, or use --encode-str TEXT")
        payload = read_text("-")
        binary = False
    del binary

    codec = args.decode or args.codec
    if args.decode and args.decode not in DECODERS:
        raise InputError(f"{args.decode!r} cannot be decoded")
    text = _apply(ctx, args, codec, payload, decode=bool(args.decode))
    _emit(ctx, args, text)
    return 0


def _describe(name: str) -> str:
    return {
        "base64": "RFC 4648 base64", "base32": "RFC 4648 base32",
        "base16": "hexadecimal bytes", "hex": "hexadecimal bytes",
        "url": "percent-encoding", "html": "HTML entity escaping",
        "json": "JSON string escaping", "unicode-escape": "\\uXXXX escapes",
        "rot13": "ROT13 (not encryption)",
        "repr": "Python repr()", "punycode": "IDNA hostname encoding",
    }.get(name, "")


def _apply(ctx: Context, args: argparse.Namespace, codec: str, payload: str,
           *, decode: bool) -> str:
    try:
        return _transform(codec, payload, decode=decode,
                          url_safe=args.url_safe, no_padding=args.no_padding,
                          upper=args.upper, separators=args.separators,
                          indent=args.indent, ensure_ascii=args.ensure_ascii)
    except (binascii.Error, ValueError, UnicodeError, LookupError) as exc:
        raise InputError(f"cannot {(decode and 'decode') or 'encode'} {codec}: {exc}",
                         hint="check the input is valid for this codec") from None


def _transform(codec: str, payload: str, *, decode: bool, url_safe: bool = False,
               no_padding: bool = False, upper: bool = False,
               separators: str | None = None, indent: int | None = None,
               ensure_ascii: bool = False) -> str:
    if codec in ("base64", "base32", "hex", "base16"):
        return _bytes_codec(codec, payload, decode=decode, url_safe=url_safe,
                            no_padding=no_padding, upper=upper)
    if codec == "url":
        return (urllib.parse.unquote(payload) if decode
                else urllib.parse.quote(payload, safe=""))
    if codec == "html":
        return (payload.encode("ascii", "xmlcharrefreplace").decode("ascii")
                if decode else _html_escape(payload))
    if codec == "json":
        if decode:
            return json_dumps(_json.loads(payload), indent=indent,
                              ensure_ascii=ensure_ascii)
        return _json.dumps(payload, ensure_ascii=ensure_ascii)
    if codec == "unicode-escape":
        return payload.encode("ascii", "backslashreplace").decode("unicode-escape") \
            if decode else payload.encode("unicode-escape").decode("ascii")
    if codec == "rot13":
        import codecs as _codecs

        return _codecs.decode(payload, "rot13")
    if codec == "repr":
        return repr(payload) if not decode else ast_literal(payload)
    if codec == "punycode":
        return (payload.encode("idna").decode("ascii") if not decode
                else payload.encode("ascii").decode("idna"))
    raise ValueError(f"unknown codec {codec}")


def _bytes_codec(codec: str, payload: str, *, decode: bool, url_safe: bool,
                 no_padding: bool, upper: bool) -> str:
    stripped = payload.strip()
    if decode:
        if codec == "hex" or codec == "base16":
            raw = binascii.unhexlify(stripped)
        elif codec == "base32":
            raw = base64.b32decode(stripped + "=" * (-len(stripped) % 8))
        else:
            padding = "=" * (-len(stripped) % 4)
            if url_safe:
                raw = base64.urlsafe_b64decode(stripped + padding)
            else:
                raw = base64.b64decode(stripped + padding, validate=True)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex() if not upper else raw.hex().upper()
    raw = payload.encode("utf-8")
    if codec == "hex" or codec == "base16":
        text = raw.hex()
    elif codec == "base32":
        text = base64.b32encode(raw).decode("ascii")
    else:
        text = (base64.urlsafe_b64encode(raw) if url_safe
                else base64.b64encode(raw)).decode("ascii")
        if no_padding:
            text = text.rstrip("=")
    return text.upper() if upper else text


def _html_escape(text: str) -> str:
    import html

    return html.escape(text, quote=True)


def json_dumps(value: object, *, indent: int | None = None,
               ensure_ascii: bool = False, separators: str | None = None) -> str:
    kwargs: dict = {"ensure_ascii": ensure_ascii, "default": str}
    if indent is not None:
        kwargs["indent"] = indent
    if separators:
        kwargs["separators"] = tuple(separators)
    return _json.dumps(value, **kwargs)


def ast_literal(text: str) -> str:
    import ast

    try:
        return str(ast.literal_eval(text.strip()))
    except (SyntaxError, ValueError) as exc:
        raise InputError(f"not a Python literal: {exc}") from None


def _emit(ctx: Context, args: argparse.Namespace, text: str) -> None:
    from anvil.core.io import write_output

    if args.output is None or str(args.output) == "-":
        ctx.out(text)
        return
    write_output(args.output, text + "\n")
    ctx.err(term.A.ok(f"wrote {args.output}"))

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _encode
