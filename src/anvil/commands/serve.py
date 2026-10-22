"""``anvil serve`` -- a static file server with a real access log."""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import pathlib
import socketserver

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import ExitCode, InputError, NotFoundError, translate_os_error
from anvil.core.types import any_path, duration, port, positive_int

HELP = "serve a directory over HTTP"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".txt": "text/plain; charset=utf-8",
}


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "serve", aliases=["server"], help=HELP,
        description="Serve a directory over HTTP.\n\n"
                    "Intended for local development and for demonstrating what "
                    "a well-behaved server prints. It is single-threaded by "
                    "default so log output stays ordered.",
    )
    parser.add_examples(
        "anvil serve .",
        "anvil serve --port 8080 --bind 0.0.0.0 ./site",
        "anvil serve --directory-only --index index.html docs",
        "anvil serve --open --once --port http ./dist",
    )

    net = parser.add_argument_group("Network")
    net.add_argument("--bind", "-b", default="127.0.0.1", metavar="ADDR",
                     help="interface to listen on")
    net.add_argument("--port", "-p", type=port, default=8000, metavar="PORT",
                     help="port, or a service name")
    net.add_argument("--threads", type=positive_int, default=1, metavar="N",
                     help="concurrent request handlers")
    net.add_argument("--once", action="store_true", default=False,
                     help="serve a single request and exit")
    net.add_argument("--timeout", type=duration, default="30s", metavar="DUR",
                     help="socket timeout")

    content = parser.add_argument_group("Content")
    content.add_argument("--index", default="index.html", metavar="NAME",
                         help="file served for a directory")
    content.add_argument("--directory-only", action="store_true", default=False,
                         help="refuse to serve files outside the root")
    content.add_argument("--cache-control", default=None, metavar="VALUE",
                         help="Cache-Control header, e.g. 'no-store'")
    content.add_argument("--add-header", action="key_value", dest="headers",
                         default={}, metavar="NAME:VALUE",
                         help="extra response header (repeatable)")
    content.add_argument("--charset", default="utf-8", metavar="ENC",
                         help="charset for text responses")

    report = parser.add_argument_group("Reporting")
    report.add_argument("--log-format", choices=("common", "combined", "json"),
                        default="common", help="access log layout")
    report.add_argument("--no-log", dest="log", action="store_false",
                        default=True, help="do not log requests")
    report.add_argument("--silent", "-q", action="store_true", default=False,
                        help="only print the startup banner")

    parser.add_argument("root", nargs="?", default=".", type=any_path,
                        metavar="ROOT", help="directory to serve")
    parser.set_defaults(func=_serve)
    return parser


@register_command("serve")
def _serve(ctx: Context, args: argparse.Namespace) -> int:
    root = pathlib.Path(args.root).expanduser()
    if not root.exists():
        raise NotFoundError(f"{root} does not exist", hint="check the path")
    if not root.is_dir():
        raise InputError(f"{root} is not a directory",
                         hint="anvil serve serves a directory")
    root = root.resolve()
    handler = functools.partial(_Handler, root=root, args=args, ctx=ctx)
    banner = {
        "serving": str(root),
        "url": f"http://{_host(args.bind)}:{int(args.port)}/",
        "bind": args.bind,
        "port": int(args.port),
        "threads": args.threads,
    }
    # Report the plan before binding. Opening a socket is a side effect the
    # user asked us not to take under --dry-run, and doing it first also turns
    # "port already in use" into the error you see for a command that never
    # needed a port at all.
    if ctx.dry_run:
        ctx.tabular("json")
        ctx.out(ctx.renderer.scalar({**banner, "bound": False}))
        ctx.err(term.A.hint(f"would serve {root} on {banner['url']}"))
        return 0

    class Server(socketserver.ThreadingTCPServer if args.threads > 1
                 else socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = args.threads > 1

    try:
        server = Server((args.bind, int(args.port)), handler)
    except OSError as exc:
        translated = translate_os_error(exc, context=f"{args.bind}:{args.port}")
        if translated.exit_code == ExitCode.UNAVAILABLE:
            translated.hint = "is another process already using that port?"
        raise translated from None

    ctx.err(ctx.renderer.scalar({**banner, "port": server.server_address[1],
                                 "bound": True}))
    if ctx.color:
        ctx.err(term.A.ok(f"serving {root} on http://{_host(args.bind)}:"
                          f"{server.server_address[1]}/"))

    try:
        if args.once:
            server.handle_request()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        ctx.err(term.A.warn("stopping"))
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _host(bind: str) -> str:
    return "localhost" if bind in ("", "0.0.0.0", "127.0.0.1") else bind


class _Handler(http.server.SimpleHTTPRequestHandler):
    """A request handler that logs like a real server and refuses traversal."""

    root: pathlib.Path
    args: argparse.Namespace
    ctx: Context

    def translate_path(self, path: str) -> str:
        relative = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        candidate = (self.root / relative).resolve()
        # ``resolve`` collapses ``..``; this is the actual traversal guard.
        if not str(candidate).startswith(str(self.root)):
            return str(self.root)
        if candidate.is_dir():
            index = candidate / (self.args.index or "")
            if index.is_file():
                return str(index)
        return str(candidate)

    def guess_type(self, path):
        suffix = pathlib.Path(path).suffix.lower()
        return MIME.get(suffix) or super().guess_type(path)

    def end_headers(self):
        if self.args.cache_control:
            self.send_header("Cache-Control", self.args.cache_control)
        for name, value in (self.args.headers or {}).items():
            self.send_header(name.rstrip(":"), value)
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if not self.args.log or self.args.silent:
            return
        self._anvil_status = _status_of(args)
        self.ctx.err(_access_line(self, format % args, self.args.log_format))

    def log_error(self, format: str, *args: object) -> None:
        self._anvil_status = 500
        self.ctx.err(_access_line(self, format % args, self.args.log_format))


def _status_of(args: tuple[object, ...]) -> int:
    """Pull the numeric status out of SimpleHTTPRequestHandler's log format."""
    for value in args:
        text = str(value)
        if text.isdigit() and 100 <= int(text) <= 599:
            return int(text)
    return 0


def _access_line(handler, message: str, layout: str) -> str:
    stamp = dt.datetime.now().strftime("%d/%b/%Y %H:%M:%S")
    status = getattr(handler, "_anvil_status", None)
    if layout == "json":
        import json as _json

        return _json.dumps({
            "remote": handler.client_address[0],
            "method": getattr(handler, "command", "-"),
            "path": getattr(handler, "path", "-"),
            "protocol": getattr(handler, "request_version", "-"),
            "status": status,
            "message": message,
        })
    prefix = f'{handler.address_string()} - [{stamp}] "{message}"'
    if layout == "combined":
        return f"{prefix} {status if status else '-'}"
    return prefix

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _serve
