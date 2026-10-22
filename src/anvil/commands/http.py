"""``anvil http`` -- a curl-shaped HTTP client on ``urllib``.

Argparse patterns: the headline ``nargs=REMAINDER`` pass-through (curl's ``--``
resets option parsing for the wrapped tool), a ``--write-out`` template
validated at parse time, repeatable ``-H`` headers accumulating into a dict,
and ``--next`` giving one parser the semantics of a whole curl session.
"""

from __future__ import annotations

import argparse
import json as _json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import AnvilError, ExitCode, InputError, TimeoutError_
from anvil.core.types import byte_size, duration, non_negative_int

HELP = "make HTTP requests with retries, redirects and curl-style output"

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")
USER_AGENT = "anvil/1.0 (+stdlib urllib)"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "http", aliases=list(aliases), help=HELP,
        description="A small HTTP client.\n\n"
                    "Everything after `--` is handed to the server untouched, "
                    "exactly like `curl`. Use `--next` to chain several "
                    "requests in one invocation.",
    )
    parser.add_examples(
        "anvil http get https://example.com",
        "anvil http post https://api.example.com/items -H 'X-Key: abc' --data '{}'",
        "anvil http get https://example.com -w '%{http_code} %{time_total}s'",
        "anvil http --next get https://a.example --next get https://b.example",
    )

    net = parser.add_argument_group("Network")
    net.add_argument("--timeout", type=duration, default="30s", metavar="DUR",
                     help="total deadline per request")
    net.add_argument("--connect-timeout", type=duration, default=None,
                     metavar="DUR", help="override just the connect phase")
    net.add_argument("--retry", type=non_negative_int, default=0, metavar="N",
                     help="retry attempts after the first failure")
    net.add_argument("--retry-delay", type=duration, default="500ms",
                     metavar="DUR", help="base backoff between attempts")
    net.add_argument("--retry-max-delay", type=duration, default="10s",
                     metavar="DUR", help="backoff ceiling")
    net.add_argument("--retry-on", type=_retry_codes, default="429,500,502,503,504",
                     metavar="CODES", help="statuses worth retrying")
    net.add_argument("--max-time", type=duration, default=None, metavar="DUR",
                     help="deadline across all retries")
    net.add_argument("--follow", "-L", action="store_true", default=False,
                     help="follow 3xx redirects")
    net.add_argument("--max-redirects", type=non_negative_int, default=10,
                     metavar="N", help="redirect limit")
    net.add_argument("--proxy", default=None, metavar="URL", help="proxy URL")
    net.add_argument("--insecure", "-k", action="store_true", default=False,
                     help="skip TLS verification (dangerous)")
    net.add_argument("--compressed", action="store_true", default=False,
                     help="ask for a compressed response")

    body = parser.add_argument_group("Request")
    body.add_argument("-H", "--header", action="key_value", dest="headers",
                      default={}, metavar="NAME:VALUE",
                      help="extra header (repeatable; -X clears a default)")
    body.add_argument("-d", "--data", default=None, metavar="BODY",
                      help="request body")
    body.add_argument("--data-binary", default=None, metavar="PATH",
                      help="read the body from a file")
    body.add_argument("--json", dest="json_body", default=None, metavar="BODY",
                      help="JSON body; sets Content-Type and Accept")
    body.add_argument("--form", action="key_value", dest="form", default={},
                      metavar="K=V", help="url-encoded form fields")
    body.add_argument("--form-string", action="key_value", dest="form_strings",
                      default={}, metavar="K=V",
                      help="form fields sent verbatim (no escaping)")
    body.add_argument("-u", "--user", default=None, metavar="USER:PASS",
                      help="basic auth")
    body.add_argument("-e", "--referer", default=None, metavar="URL",
                      help="Referer header")

    out = parser.add_argument_group("Output")
    out.add_argument("-o", "--save", default=None, metavar="PATH",
                     help="write the body to a file instead of stdout")
    out.add_argument("-i", "--include", action="store_true", default=False,
                     help="include response headers in the output")
    out.add_argument("-s", "--silent", action="store_true", default=False,
                     help="suppress the progress line on stderr")
    out.add_argument("-S", "--show-error", action="store_true", default=False,
                     help="show the error message even in silent mode")
    out.add_argument("--fail-with-body", action="store_true", default=False,
                     help="keep the body when the status is an error")
    out.add_argument("-w", "--write-out", action="write_out", default="",
                     metavar="FORMAT", help="curl-style output template")
    out.add_argument("--max-size", type=byte_size, default=None, metavar="SIZE",
                     help="abort if the body exceeds this")

    session = parser.add_argument_group("Session")
    session.add_argument("--next", action="append", dest="sessions",
                         default=[], metavar="ARGS", help=argparse.SUPPRESS)

    parser.add_argument("method", nargs="?", default="GET", metavar="METHOD",
                        help=f"one of: {', '.join(METHODS)}")
    parser.add_argument("url", nargs="?", default=None, metavar="URL")
    # No REMAINDER positional here on purpose. A REMAINDER positional begins
    # capturing as soon as the positionals before it are satisfied, so
    # `--timeout 5s` after the URL would be swallowed as a "passthrough" token
    # instead of being parsed -- silently disabling the tool's own options.
    # Everything after a bare `--` is already split off by the app and exposed
    # as ctx.passthrough.
    parser.set_defaults(func=_http)
    return parser


def _retry_codes(value: str) -> tuple[int, ...]:
    codes: list[int] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            codes.append(int(chunk))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{chunk!r} is not an HTTP status code"
            ) from None
    return tuple(codes)


@register_command("http")
def _http(ctx: Context, args: argparse.Namespace) -> int:
    plan: list[tuple[str, str, list[str]]] = []
    if args.sessions:
        plan.append((args.method, args.url, list(ctx.passthrough)))
        for chunk in args.sessions:
            words = chunk.split() if isinstance(chunk, str) else list(chunk)
            plan.append((words[0] if words else "GET",
                         words[1] if len(words) > 1 else None,
                         words[2:]))
    else:
        plan.append((args.method, args.url, list(ctx.passthrough)))

    results: list[dict[str, Any]] = []
    worst = 0
    for method, url, rest in plan:
        result = _one(ctx, args, method, url, rest)
        results.append(result)
        worst = max(worst, result.get("_code", 0))

    if args.write_out:
        for result in results:
            ctx.out(_write_out(args.write_out, result))
    return worst


def _one(ctx: Context, args: argparse.Namespace, method: str,
         url: str | None, rest: list[str]) -> dict[str, Any]:
    if not url:
        raise InputError("no URL given",
                         hint="try 'anvil http get https://example.com'")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise InputError(
            f"{url!r} is not an http(s) URL",
            hint="supported schemes are http and https",
        )

    verb = (method or "GET").upper()
    if verb not in METHODS:
        raise InputError(
            f"unknown method {method!r}",
            hint=f"choose from {', '.join(METHODS)}",
        )

    payload, content_type = _body(args)
    headers = _headers(args, content_type)
    request = urllib.request.Request(url, data=payload, method=verb)
    for name, value in headers.items():
        request.add_header(name, value)

    attempts = 1 + max(0, int(args.retry))
    deadline = time.monotonic() + args.max_time if args.max_time else None
    retryable = set(args.retry_on or ())
    last: dict[str, Any] = {}
    delay = args.retry_delay

    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            last = _perform(ctx, args, request, verb, url, headers, started)
            last["attempts"] = attempt + 1
            last["_code"] = 0 if last["status"] < 400 else last["status"]
            if last["status"] not in retryable:
                break
        except AnvilError as exc:
            last = {"status": 0, "error": exc.message, "hint": exc.hint,
                    "time_total": time.perf_counter() - started, "url": url,
                    "method": verb}
            if attempt == attempts - 1:
                raise
        if attempt == attempts - 1:
            break
        if deadline and time.monotonic() + delay > deadline:
            break
        time.sleep(delay)
        delay = min(delay * 2, args.retry_max_delay)

    _report(ctx, args, last)
    return last


def _body(args: argparse.Namespace) -> tuple[bytes | None, str | None]:
    if args.json_body is not None:
        try:
            data = _json.dumps(_json.loads(args.json_body)).encode("utf-8")
        except _json.JSONDecodeError as exc:
            raise InputError(f"--json is not valid JSON: {exc.msg}") from None
        return data, "application/json"
    if args.data_binary:
        try:
            with open(args.data_binary, "rb") as handle:
                return handle.read(), None
        except OSError as exc:
            from anvil.core.errors import translate_os_error

            raise translate_os_error(exc, context=args.data_binary) from None
    if args.data is not None:
        return args.data.encode("utf-8"), None
    fields = dict(args.form or {})
    fields.update(args.form_strings or {})
    if fields:
        encoded = urllib.parse.urlencode(fields, quote_via=urllib.parse.quote)
        return encoded.encode("utf-8"), "application/x-www-form-urlencoded"
    return None, None


def _headers(args: argparse.Namespace, content_type: str | None) -> dict[str, str]:
    headers: dict[str, str] = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if content_type:
        headers["Content-Type"] = content_type
    if args.compressed:
        headers["Accept-Encoding"] = "gzip, deflate"
    if args.referer:
        headers["Referer"] = args.referer
    if args.user:
        import base64

        token = base64.b64encode(args.user.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    for name, value in (args.headers or {}).items():
        key = name.rstrip(":").strip()
        if value == "" and key.lower().startswith("x-"):
            continue
        headers[key] = value
    return headers


def _ssl_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    if args.insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return None


def _perform(ctx: Context, args: argparse.Namespace, request, verb: str, url: str,
             headers: dict[str, str], started: float) -> dict[str, Any]:
    opener_handlers: list[Any] = []
    if args.proxy:
        opener_handlers.append(urllib.request.ProxyHandler(
            {"http": args.proxy, "https": args.proxy}))
    else:
        opener_handlers.append(urllib.request.ProxyHandler({}))
    context = _ssl_context(args)
    if context is not None:
        opener_handlers.append(urllib.request.HTTPSHandler(context=context))

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers_, newurl):
            return None

    handlers: list[Any] = list(opener_handlers)
    if not args.follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(request, timeout=args.timeout) as response:
            payload = response.read(_limit(args))
            return _record(verb, url, headers, response.status, dict(response.headers),
                           payload, started, len(getattr(response, "redirects", []) or []))
    except urllib.error.HTTPError as exc:
        payload = exc.read(_limit(args))
        return _record(verb, url, headers, exc.code, dict(exc.headers or {}),
                       payload, started, 0)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise TimeoutError_(f"{url}: timed out after {args.timeout}s",
                                hint="raise --timeout") from None
        raise AnvilError(f"{url}: {reason}", exit_code=ExitCode.UNAVAILABLE,
                         hint="check connectivity and DNS") from None
    except TimeoutError:
        raise TimeoutError_(f"{url}: timed out after {args.timeout}s",
                            hint="raise --timeout") from None
    except OSError as exc:
        from anvil.core.errors import translate_os_error

        raise translate_os_error(exc, context=url) from None


def _limit(args: argparse.Namespace) -> int | None:
    return args.max_size if args.max_size else None


def _record(verb: str, url: str, request_headers: dict[str, str], status: int,
            response_headers: dict[str, str], payload: bytes, started: float,
            redirects: int) -> dict[str, Any]:
    body = payload
    encoding = response_headers.get("Content-Encoding", "")
    if encoding == "gzip" or encoding == "deflate":
        import gzip
        import zlib

        try:
            body = (gzip.decompress(payload) if encoding == "gzip"
                    else zlib.decompress(payload))
        except (OSError, zlib.error):
            body = payload
    return {
        "method": verb,
        "url": url,
        "url_effective": url,
        "status": status,
        "headers": response_headers,
        "request_headers": request_headers,
        "body": body,
        "size_download": len(payload),
        "content_type": response_headers.get("Content-Type", ""),
        "hostname": urllib.parse.urlparse(url).hostname or "",
        "scheme": urllib.parse.urlparse(url).scheme,
        "time_total": time.perf_counter() - started,
        "redirects": redirects,
    }


def _report(ctx: Context, args: argparse.Namespace, result: dict[str, Any]) -> None:
    if "error" in result:
        if not args.silent or args.show_error:
            ctx.err(term.A.err(f"error: {result['error']}"))
            if result.get("hint"):
                ctx.err(term.A.hint(f"hint: {result['hint']}"))
        return
    status = result["status"]
    paint = term.A.ok if 200 <= status < 300 else term.A.warn if status < 400 else term.A.err
    if not args.silent:
        summary = (f"{result['method']} {result['url']} -> {paint(str(status))} "
                   f"{term.format_bytes(result['size_download'])} in "
                   f"{term.human_duration(result['time_total'])}")
        ctx.err(summary)

    if args.save:
        from anvil.core.io import write_output

        write_output(args.save, result["body"].decode("utf-8", "replace"))
        return
    if status >= 400 and not args.fail_with_body:
        return
    if args.include:
        head = "".join(f"{k}: {v}\n" for k, v in result["headers"].items())
        ctx.out(head)
    if result["method"] != "HEAD" and result["body"]:
        ctx.out(result["body"].decode("utf-8", "replace").rstrip("\n"))


def _write_out(template: str, result: dict[str, Any]) -> str:
    """Render a curl-compatible ``--write-out`` template."""
    import re

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name == "json":
            return _json.dumps({k: v for k, v in result.items()
                                if k not in ("body", "headers")}, default=str)
        if name == "errormsg":
            return result.get("error", "")
        mapping = {
            "http_code": result.get("status", 0),
            "url_effective": result.get("url_effective", ""),
            "size_download": result.get("size_download", 0),
            "num_redirects": result.get("redirects", 0),
            "time_total": f"{result.get('time_total', 0.0):.6f}",
            "content_type": result.get("content_type", ""),
            "scheme": result.get("scheme", ""),
            "hostname": result.get("hostname", ""),
            "method": result.get("method", ""),
        }
        return str(mapping.get(name, ""))

    return re.sub(r"%\{(\w+)\}|%(\w+)", substitute, template)

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _http
