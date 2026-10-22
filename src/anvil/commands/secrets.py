"""``anvil secrets`` -- generate credentials and measure their entropy."""

from __future__ import annotations

import argparse
import base64
import math
import string
import time
import uuid
from secrets import choice as _choice
from secrets import token_bytes as _token_bytes

from anvil.core.app import Context, register_command
from anvil.core.errors import UsageError
from anvil.core.types import non_negative_int, positive_int

HELP = "generate passwords, tokens, UUIDs and ULIDs"

KINDS = ("password", "token", "hex", "base64", "urlsafe", "uuid", "uuid7",
         "ulid", "nanoid", "digits", "pin")
CHARSETS = ("alnum", "alnum-symbol", "ascii", "url", "hex", "digits")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "secrets", aliases=list(aliases), help=HELP,
        description="Generate cryptographically strong values.\n\n"
                    "Built on ``secrets``, never ``random``: a token generator "
                    "that is subtly predictable is worse than none at all.",
    )
    parser.add_examples(
        "anvil secrets --length 32",
        "anvil secrets token --bytes 32 --count 4",
        "anvil secrets uuid --count 1000 --output csv",
        "anvil secrets --password --length 24 --symbols --entropy",
    )

    group = parser.add_argument_group("Shape")
    group.add_argument("--length", "-l", type=positive_int, default=32, metavar="N",
                       help="character length for text kinds")
    group.add_argument("--bytes", "-b", type=positive_int, default=None,
                       metavar="N", help="entropy in bytes for token kinds")
    group.add_argument("--count", "-n", type=non_negative_int, default=1,
                       metavar="N", help="how many values to generate")
    group.add_argument("--charset", choices=CHARSETS, default="alnum",
                       help="character set")
    group.add_argument("--symbols", action="store_true", default=False,
                       help="include punctuation")
    group.add_argument("--uppercase", action="store_true", default=False,
                       help="force uppercase")
    group.add_argument("--lowercase", action="store_true", default=False,
                       help="force lowercase")
    group.add_argument("--digits", action="store_true", default=False,
                       help="force digits")
    group.add_argument("--no-ambiguous", action="store_true", default=False,
                       help="drop lookalike characters (0/O, 1/l/I)")
    group.add_argument("--prefix", default="", metavar="TEXT",
                       help="prepend this to every value")
    group.add_argument("--separator", default="\n", metavar="TEXT",
                       help="join values with this instead of newlines")

    meta = parser.add_argument_group("Metadata")
    meta.add_argument("--entropy", action="store_true", default=False,
                      help="also report Shannon entropy in bits")
    meta.add_argument("--label", action="key_value", dest="labels", default={},
                      metavar="NAME=TEXT",
                      help="print a named value as a map; repeatable")
    meta.add_argument("--repeat", action="store_true", default=False,
                      help="fail if any generated value repeats")

    parser.add_argument("kind", nargs="?", default="password", choices=KINDS,
                        help=f"what to generate: {', '.join(KINDS)}")
    parser.set_defaults(func=_generate)
    return parser


@register_command("secrets")
def _generate(ctx: Context, args: argparse.Namespace) -> int:
    alphabet = _alphabet(args)
    wanted: list[str] = list(args.labels) or [""] * max(1, args.count)
    values: list[str] = [f"{args.prefix}{_one(args, alphabet)}" for _ in wanted]

    if args.repeat and len(set(values)) != len(values):
        raise UsageError("generated values repeated",
                         hint="increase --length or --bytes")

    if args.labels:
        ctx.out(ctx.renderer.scalar(dict(zip(args.labels, values, strict=True))))
        return 0

    if args.entropy:
        rows = []
        for value in values:
            bits = _entropy(value)
            pool = len(set(value))
            rows.append({"value": value, "entropy_bits": round(bits, 1),
                         "distinct": pool,
                         "per_char": round(bits / max(1, len(value)), 2)})
        ctx.out(ctx.renderer.scalar(rows[0] if len(rows) == 1 else rows))
        return 0

    ctx.out(args.separator.join(values))
    return 0


def _alphabet(args: argparse.Namespace) -> str:
    if args.charset == "hex":
        base = "0123456789abcdef"
    elif args.charset == "digits":
        base = string.digits
    elif args.charset == "url":
        base = string.ascii_letters + string.digits + "-_"
    elif args.charset == "ascii":
        base = "".join(chr(c) for c in range(33, 127))
    elif args.charset == "alnum-symbol":
        base = string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{};:,.?"
    else:
        base = string.ascii_letters + string.digits
    if not args.symbols and args.charset in ("alnum", "ascii"):
        base = "".join(c for c in base if c.isalnum())
    if args.no_ambiguous:
        base = "".join(c for c in base if c not in "0O1lI|`'\"")
    if args.uppercase:
        base = base.upper()
    if args.lowercase:
        base = base.lower()
    if args.digits:
        base = "".join(c for c in base if c.isdigit())
    if not base:
        raise UsageError("the selected flags leave an empty character set",
                         hint="drop --uppercase, --lowercase or --digits")
    return base


def _one(args: argparse.Namespace, alphabet: str) -> str:
    kind = args.kind
    if kind == "uuid":
        return str(uuid.uuid4())
    if kind == "uuid7":
        return _uuid7()
    if kind == "ulid":
        return _ulid()
    if kind in ("token", "hex", "base64", "urlsafe"):
        raw = _token_bytes(args.bytes or max(16, args.length // 2))
        if kind == "hex":
            return raw.hex()
        if kind == "base64":
            return base64.b64encode(raw).decode("ascii")
        if kind == "urlsafe":
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return "".join(_choice(alphabet) for _ in range(args.length))
    if kind in ("digits", "pin"):
        width = max(4, args.length) if kind == "pin" else args.length
        return "".join(_choice(string.digits) for _ in range(width))
    if kind == "nanoid":
        raw = _token_bytes(args.bytes or 16)
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[: args.length]
    return "".join(_choice(alphabet) for _ in range(args.length))


def _ulid() -> str:
    """A 26-character, lexicographically sortable identifier (ULID spec).

    48 bits of millisecond timestamp in Crockford base32 is exactly 10
    characters, plus 16 characters of randomness. Emitting 11 timestamp
    characters would produce a 27-character "ULID" that no library accepts.
    """
    timestamp = int(time.time() * 1000)
    chars = [_CROCKFORD[(timestamp >> shift) & 31] for shift in range(45, -1, -5)]
    chars += [_CROCKFORD[byte & 31] for byte in _token_bytes(16)]
    return "".join(chars)


def _uuid7() -> str:
    """A time-ordered UUID following the draft version 7 layout."""
    raw = bytearray(uuid.uuid4().bytes)
    milliseconds = int(time.time() * 1000)
    raw[0] = (milliseconds >> 40) & 0xFF
    raw[1] = (milliseconds >> 32) & 0xFF
    raw[2] = (milliseconds >> 24) & 0xFF
    raw[3] = (milliseconds >> 16) & 0xFF
    raw[4] = (milliseconds >> 8) & 0xFF
    raw[5] = milliseconds & 0xFF
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _entropy(value: str) -> float:
    """Shannon entropy of *value*, in bits."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((c / total) * math.log2(c / total) for c in counts.values()) * total

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _generate
