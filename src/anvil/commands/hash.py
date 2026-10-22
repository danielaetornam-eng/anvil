"""``anvil hash`` -- digest files, streams, and strings.

Argparse patterns: a positional that is *optional* (``nargs='?'``) with a
``const``, ``type=`` on a ``nargs='+'`` list, ``action='append'`` for algorithms,
and a mutually exclusive group separating "produce a digest" from "verify one".
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import os
import pathlib
import sys

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import InputError, NotFoundError
from anvil.core.types import positive_int

HELP = "hash and verify file digests"

ALGORITHMS = ("md5", "sha1", "sha256", "sha512", "blake2b", "blake2s", "crc32")
_CHUNK = 1024 * 256


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "hash", aliases=list(aliases), help=HELP,
        description="Compute and verify digests.\n\n"
                    "Reads from stdin when no file is given, so it composes: "
                    "`tar -cf - dir | anvil hash sha256`.",
    )
    parser.add_examples(
        "anvil hash sha256 big.iso",
        "anvil hash --stdin < package.tar.gz",
        "anvil hash --verify sha256:5f2b...  release.tar.gz",
        "anvil hash sha256 dir/ --recursive",
    )

    group = parser.add_argument_group("Selection")
    group.add_argument("-a", "--algorithm", action="append", dest="algorithms",
                       choices=ALGORITHMS, default=None, metavar="NAME",
                       help="digest to compute; repeatable. default: sha256")
    group.add_argument("--stdin", action="store_true", default=False,
                       help="read from stdin even if files are given")
    group.add_argument("-r", "--recursive", action="store_true", default=False,
                       help="descend into directories")
    group.add_argument("--follow", action="store_true", default=False,
                       help="follow symbolic links")
    group.add_argument("--exclude", action="comma_list", default=[],
                       metavar="GLOB[,GLOB]", help="skip matching names")
    group.add_argument("--include-hidden", action="store_true", default=False,
                       help="do not skip dotfiles")

    shape = parser.add_argument_group("Output shape")
    shape.add_argument("--silent", "-q", action="store_true", default=False,
                       help="print nothing; use the exit code")
    shape.add_argument("--length", type=positive_int, default=None, metavar="N",
                       help="truncate the hex digest")
    shape.add_argument("--upper", action="store_true", default=False,
                       help="uppercase hex")
    shape.add_argument("--no-separator", dest="separator", action="store_false",
                       default=True, help="print bare digests, one per line")
    shape.add_argument("--prefix-name", action="store_true", default=False,
                       help="prefix each digest with its file name")
    shape.add_argument("--format", choices=("plain", "table", "json", "yaml",
                                            "csv", "tsv", "markdown", "html",
                                            "latex", "jsonl", "tree"),
                       default=None, metavar="FORMAT",
                       help="output format (default: plain)")

    verify = parser.add_argument_group("Verification")
    verify.add_argument("--verify", metavar="ALGO:DIGEST", default=None,
                        help="compare against this digest instead of printing")
    verify.add_argument("--hmac-key", default=None, metavar="KEY",
                        help="compute a keyed digest instead")

    # One variadic positional rather than two. A separate ``ALGORITHM``
    # positional would consume the first *filename* whenever ``-a`` is used and
    # reject it as an invalid algorithm, so the two spellings could not
    # coexist. Splitting the list here keeps ``hash sha256 f``, ``hash -a
    # sha256 f`` and ``hash f`` all working.
    parser.add_argument("targets", nargs="*", metavar="[ALGORITHM] [FILE ...]",
                        help="optional leading algorithm "
                             f"({', '.join(ALGORITHMS)}), then the files; "
                             "omit to read stdin")
    parser.set_defaults(func=_hash)
    return parser


@register_command("hash")
def _hash(ctx: Context, args: argparse.Namespace) -> int:
    targets = list(args.targets)
    leading = targets[0] if targets and targets[0] in ALGORITHMS else None
    files = targets[1:] if leading else targets

    algorithms = list(args.algorithms or [])
    if leading and leading not in algorithms:
        algorithms.insert(0, leading)
    if not algorithms:
        algorithms = ["sha256"]
    key = args.hmac_key.encode("utf-8") if args.hmac_key else None
    if args.hmac_key:
        algorithms = ["sha256"]  # only HMAC-SHA* is meaningful here

    if args.verify:
        algorithm, _, expected = args.verify.partition(":")
        if not expected:
            raise InputError(
                "--verify expects ALGO:DIGEST",
                hint="for example --verify sha256:5f2b...",
            )
        if algorithm not in ALGORITHMS:
            raise InputError(f"unknown algorithm {algorithm!r}",
                             hint=f"choose from {', '.join(ALGORITHMS)}")

    targets = _collect(ctx, args, files)
    if not targets:
        digests = {
            algorithm: _digest_stream(_stdin_bytes(), algorithm, key)
            for algorithm in algorithms
        }
        return _emit(ctx, args, [("<stdin>", digests)], algorithms,
                     verify=args.verify)

    failures = 0
    rows: list[tuple[str, dict[str, str]]] = []
    with ctx.progress(len(targets), label="hashing") as bar:
        for name in targets:
            try:
                digests = {
                    algorithm: _digest_file(name, algorithm, key)
                    for algorithm in algorithms
                }
            except OSError as exc:
                from anvil.core.errors import translate_os_error

                raise translate_os_error(exc, context=name) from None
            rows.append((name, digests))
            bar.advance(1, label=pathlib.Path(name).name)
            if args.verify:
                algorithm, _, expected = args.verify.partition(":")
                actual = digests[algorithm]
                if not hmac.compare_digest(actual, expected.lower()):
                    ctx.err(term.A.err(f"MISMATCH  {name}"))
                    failures += 1
                elif not args.silent:
                    ctx.err(term.A.ok(f"ok        {name}"))
    if not args.silent:
        _emit(ctx, args, rows, algorithms, verify=args.verify)
    return 1 if failures else 0


def _collect(ctx: Context, args: argparse.Namespace, files: list[str]) -> list[str]:
    import fnmatch

    if args.stdin or not files:
        return []
    out: list[str] = []
    for item in files:
        if pathlib.Path(item).is_dir():
            if not args.recursive:
                raise InputError(
                    f"{item} is a directory",
                    hint="add --recursive to descend into it",
                )
            out += _walk(item, args, fnmatch)
        elif pathlib.Path(item).exists():
            out.append(item)
        else:
            raise NotFoundError(f"{item} does not exist", hint="check the path")
    return out


def _walk(root: str, args: argparse.Namespace, fnmatch) -> list[str]:
    found: list[str] = []
    for base, dirs, files in os.walk(root, followlinks=args.follow):
        if not args.include_hidden:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        dirs.sort()
        for name in sorted(files):
            if not args.include_hidden and name.startswith("."):
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in args.exclude):
                continue
            found.append(os.path.join(base, name))
    return found


def _stdin_bytes():
    """Return stdin as a binary *stream*, whatever shape ``sys.stdin`` has.

    ``sys.stdin.buffer`` is absent whenever stdin has been wrapped or replaced
    -- by a test harness, a GUI runner, or an embedding framework -- so falling
    back to ``sys.stdin.read().encode()`` keeps the command working instead of
    failing with ``AttributeError``.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer
    data = sys.stdin.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    return io.BytesIO(data)


def _digest_stream(stream, algorithm: str, key: bytes | None) -> str:
    if algorithm == "crc32":
        import zlib

        checksum = 0
        while True:
            block = stream.read(_CHUNK)
            if not block:
                break
            checksum = zlib.crc32(block, checksum)
        return f"{checksum & 0xFFFFFFFF:08x}"
    digest = hmac.new(key, digestmod=algorithm) if key else hashlib.new(algorithm)
    while True:
        block = stream.read(_CHUNK)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def _digest_file(path: str, algorithm: str, key: bytes | None) -> str:
    if algorithm == "crc32":
        with open(path, "rb") as handle:
            return _digest_stream(handle, algorithm, key)
    digest = hmac.new(key, digestmod=algorithm) if key else hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _shape(value: str, args: argparse.Namespace) -> str:
    if args.length:
        value = value[: args.length]
    return value.upper() if args.upper else value


def _emit(ctx: Context, args: argparse.Namespace, rows, algorithms,
          *, verify: str | None) -> None:
    shaped = [(name, {a: _shape(v, args) for a, v in digests.items()})
              for name, digests in rows]
    if args.format:
        ctx.renderer.requested = args.format
        ctx.renderer.format = args.format
    fmt = ctx.renderer.format

    if fmt in ("plain", "auto") and args.separator and not args.prefix_name:
        for _, digests in shaped:
            values = [digests[a] for a in algorithms]
            ctx.out(" ".join(values) if len(values) > 1 else values[0])
        return

    ctx.tabular()
    table = term.Table([term.Column("file", "file")])
    for algorithm in algorithms:
        table.columns.append(term.Column(algorithm, algorithm, align="right"))
    for name, digests in shaped:
        table.add_row([name if args.separator else pathlib.Path(name).name]
                      + [digests[a] for a in algorithms])
    if ctx.output_format == "auto" and args.format:
        ctx.out(table.to_csv())
    else:
        ctx.render(table)

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _hash
