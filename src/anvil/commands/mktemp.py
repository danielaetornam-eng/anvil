"""``anvil mktemp`` -- create a temporary file or directory safely."""

from __future__ import annotations

import argparse
import os
import pathlib
import tempfile

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import InputError
from anvil.core.types import non_negative_int, regex_string

HELP = "create a temporary file or directory"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "mktemp", aliases=["tempfile"], help=HELP,
        description="Create a securely-named temporary file or directory.\n\n"
                    "Uses ``tempfile`` rather than ``mktemp``: the latter "
                    "creates the name first and writes afterwards, which is a "
                    "symlink race.",
    )
    parser.add_examples(
        "anvil mktemp --suffix .json",
        "anvil mktemp --directory --prefix build-",
        "TMP=$(anvil mktemp --print-path --suffix .txt)",
        "anvil mktemp --keep --output-file scratch",
    )

    shape = parser.add_argument_group("Shape")
    shape.add_argument("--directory", "-d", action="store_true", default=False,
                       help="create a directory instead of a file")
    shape.add_argument("--prefix", default=None, metavar="TEXT",
                       help="name prefix")
    shape.add_argument("--suffix", default=None, metavar="TEXT",
                       help="name suffix, e.g. .json")
    shape.add_argument("--length", type=non_negative_int, default=None,
                       metavar="N", help="random name length")
    shape.add_argument("--chars", type=regex_string, default=None, metavar="RE",
                       help="characters the random name may use")

    where = parser.add_argument_group("Location")
    where.add_argument("--temp-dir", "-t", default=None, metavar="DIR",
                       help="parent directory (defaults to the system temp dir)")
    where.add_argument("--no-temp-dir", action="store_true", default=False,
                       help="create it in the current directory")

    report = parser.add_argument_group("Reporting")
    report.add_argument("--print-path", action="store_true", default=False,
                        help="print only the path (for command substitution)")
    report.add_argument("--silent", "-q", action="store_true", default=False,
                        help="print nothing")
    report.add_argument("--keep", action="store_true", default=False,
                        help="leave the file behind instead of removing it")
    report.add_argument("--output-file", default=None, metavar="PATH",
                        help="write the path to a file instead of stdout")
    parser.set_defaults(func=_mktemp)
    return parser


@register_command("mktemp")
def _mktemp(ctx: Context, args: argparse.Namespace) -> int:
    target_dir = _resolve_dir(ctx, args)
    alphabet = None
    if args.chars is not None:
        alphabet = "".join(sorted(set(args.chars.pattern)) - set("[]\\^$.|?*+(){}"))
        if not alphabet:
            raise InputError("--chars matched no usable characters")

    if args.no_temp_dir:
        if ctx.dry_run:
            ctx.out(ctx.renderer.scalar({"path": str(pathlib.Path("anvil-tmp-XXXXXX"))}))
            return 0
        handle, path = tempfile.mkstemp(dir=".", suffix=args.suffix or "")
        os.close(handle)
        if args.directory:
            pathlib.Path(path).unlink()
            pathlib.Path(path).mkdir()
        created = path
    elif args.directory:
        created = tempfile.mkdtemp(suffix=args.suffix or "",
                                   prefix=args.prefix or "",
                                   dir=target_dir)
    else:
        handle, created = tempfile.mkstemp(suffix=args.suffix or "",
                                           prefix=args.prefix or "",
                                           dir=target_dir)
        os.close(handle)
        if ctx.dry_run:
            ctx.err(term.A.hint(f"would create {created}"))

    if args.length:
        created = _rename_to_length(pathlib.Path(created), args.length, ctx)

    if args.output_file:
        from anvil.core.io import write_output

        write_output(args.output_file, created + "\n")
    elif not args.silent:
        if args.print_path:
            ctx.out(created)
        else:
            ctx.tabular("json")
            ctx.out(ctx.renderer.scalar({
                "path": created,
                "directory": bool(args.directory),
                "kept": bool(args.keep or args.print_path),
            }))
            ctx.err(term.A.ok(created))
    elif args.print_path:
        ctx.out(created)

    if not args.keep and not args.print_path and not args.output_file:
        ctx.info("remove it when you are done; --keep was not passed")
    return 0


def _resolve_dir(ctx: Context, args: argparse.Namespace) -> str | None:
    if args.no_temp_dir:
        return None
    if args.temp_dir is None:
        return None
    path = pathlib.Path(args.temp_dir).expanduser()
    if not path.is_dir():
        raise InputError(f"{path} is not a directory",
                         hint="create it first, or omit --temp-dir")
    return str(path)


def _rename_to_length(path: pathlib.Path, length: int, ctx: Context) -> str:
    """Give the random component an exact character length.

    ``tempfile`` fixes the random-part length at six characters, which is too
    short for a name that has to survive being pasted into a ticket.
    """
    if length <= 0:
        raise InputError("--length must be greater than 0")
    import secrets
    import string

    alphabet = string.ascii_lowercase + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        target = path.with_name(f"{candidate}{path.suffix}")
        if not target.exists():
            try:
                path.rename(target)
            except OSError as exc:
                from anvil.core.errors import translate_os_error

                raise translate_os_error(exc, context=str(path)) from None
            return str(target)

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _mktemp
