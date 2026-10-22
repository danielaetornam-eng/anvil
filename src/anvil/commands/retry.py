"""``anvil retry`` -- run a command until it succeeds, or give up."""

from __future__ import annotations

import argparse
import os
import random
import shlex
import subprocess
import time

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import ExitCode, TimeoutError_, UsageError
from anvil.core.types import csv_list, duration, non_negative_int, percentage

from ._shared import had_separator, require_exec_form

HELP = "retry a command with exponential backoff"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "retry", aliases=["retry-until"], help=HELP,
        description="Run a command repeatedly until it succeeds.\n\n"
                    "Retries only on a non-zero exit, a timeout, or (with "
                    "--retry-on) a specific status -- retrying a deterministic "
                    "failure just wastes time.",
    )
    parser.add_examples(
        "anvil retry --attempts 5 -- curl -fsS https://example.com",
        "anvil retry --until-success --max-time 2m -- ./flaky.sh",
        "anvil retry --attempts 3 --delay 200ms --backoff 3 -- ./task",
    )

    policy = parser.add_argument_group("Policy")
    policy.add_argument("--attempts", "-n", type=non_negative_int, default=3,
                        metavar="N", help="total attempts, not retries")
    policy.add_argument("--until-success", action="store_true", default=False,
                        help="keep retrying until success (still bounded by "
                             "--max-time)")
    policy.add_argument("--delay", type=duration, default="500ms", metavar="DUR",
                        help="initial backoff")
    policy.add_argument("--backoff", type=lambda v: float(v), default=2.0,
                        metavar="FACTOR", help="multiplier per attempt")
    policy.add_argument("--max-delay", type=duration, default="30s", metavar="DUR",
                        help="backoff ceiling")
    policy.add_argument("--jitter", type=percentage, default="20%",
                        metavar="PCT",
                        help="randomise each delay by this fraction")
    policy.add_argument("--max-time", type=duration, default=None, metavar="DUR",
                        help="give up after this much wall clock")
    policy.add_argument("--timeout", type=duration, default=None, metavar="DUR",
                        help="abort a single attempt after this long")
    policy.add_argument("--retry-on", type=csv_list, default=None, metavar="LIST",
                        help="only retry on these exit codes")

    report = parser.add_argument_group("Reporting")
    report.add_argument("--silent", "-q", action="store_true", default=False,
                        help="only report the final outcome")
    report.add_argument("--verbose-attempts", action="store_true", default=False,
                        help="print each attempt's status")
    report.add_argument("--shell", default=None, metavar="PATH",
                        help="run through a shell")
    report.add_argument("--cwd", default=None, metavar="DIR",
                        help="working directory for every attempt")

    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND",
                        help="the command to retry")
    parser.set_defaults(func=_retry)
    return parser


@register_command("retry")
def _retry(ctx: Context, args: argparse.Namespace) -> int:
    raw_command = list(args.command or [])
    separated = had_separator(raw_command)
    command = [c for c in raw_command if c != "--"]
    if not command:
        raise UsageError("no command given", hint="separate it with --")
    if args.attempts == 0 and not args.until_success:
        raise UsageError("--attempts 0 would never run the command")
    if args.backoff < 1:
        raise UsageError(f"--backoff must be at least 1, got {args.backoff}")
    require_exec_form(args, command, verb="retry", separated=separated)

    allowed = set(args.retry_on) if args.retry_on else None
    started = time.monotonic()
    delay = args.delay
    attempts = 0
    last_code = ExitCode.FAILURE

    while True:
        if args.max_time and time.monotonic() - started >= args.max_time:
            raise TimeoutError_(
                f"gave up after {args.max_time}s and {attempts} attempt(s)",
                hint="raise --max-time or lower --backoff",
            )
        attempts += 1
        code, reason = _attempt(ctx, args, command)
        last_code = code
        if ctx.dry_run:
            ctx.err(term.A.hint(f"would attempt {attempts}: {shlex.join(command)}"))
            return 0
        if args.verbose_attempts or not args.silent:
            ctx.err(term.A.hint(
                f"attempt {attempts}: exit {code}"
                + (f" ({reason})" if reason else "")
                + (f" in {term.human_duration(time.monotonic() - started)}")))
        if code == 0:
            ctx.err(term.A.ok(
                f"succeeded on attempt {attempts} after "
                f"{term.human_duration(time.monotonic() - started)}"))
            return 0
        if allowed is not None and code not in allowed:
            ctx.err(term.A.err(
                f"exit {code} is not in --retry-on ({', '.join(map(str, sorted(allowed)))})"))
            return int(code)
        if not args.until_success and attempts >= args.attempts:
            ctx.err(term.A.err(
                f"still failing after {attempts} attempt(s); last exit {code}"))
            return int(code)

        sleep_for = _jitter(delay, args.jitter)
        ctx.err(term.A.hint(f"waiting {term.human_duration(sleep_for)}"))
        time.sleep(sleep_for)
        delay = min(delay * args.backoff, args.max_delay)
    return int(last_code)


def _jitter(delay: float, amount: float) -> float:
    """Spread retries so a fleet of clients does not stampede one server."""
    if amount <= 0:
        return delay
    spread = delay * amount
    return max(0.0, delay + random.uniform(-spread, spread))


def _attempt(ctx: Context, args: argparse.Namespace,
             command: list[str]) -> tuple[int, str]:
    line = command if args.shell is None else [args.shell, "-c", shlex.join(command)]
    try:
        completed = subprocess.run(
            line,
            shell=args.shell is not None,
            cwd=args.cwd,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        if args.timeout is None:
            raise
        return int(ExitCode.TEMPFAIL), f"timed out after {args.timeout}s"
    except OSError as exc:
        from anvil.core.errors import translate_os_error

        raise translate_os_error(exc, context=command[0]) from None
    return completed.returncode, ""

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _retry
