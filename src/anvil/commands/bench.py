"""``anvil bench`` -- time a command and report percentiles."""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import statistics
import subprocess
import time

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import AnvilError, InputError, UsageError
from anvil.core.types import duration, json_object, non_negative_int, positive_int

from ._shared import had_separator, require_exec_form

HELP = "time a command and report latency percentiles"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "bench", aliases=list(aliases), help=HELP,
        description="Run a command repeatedly and report the distribution.\n\n"
                    "Reports min/mean/median/p90/p99 rather than a single number, "
                    "because a single timing tells you nothing about a tail.",
    )
    parser.add_examples(
        "anvil bench --runs 50 -- python -c 'import json'",
        "anvil bench --runs 20 --timeout 5s --fail-fast -- ./slow --flag",
        "anvil bench --compare -- python -m json.tool big.json",
    )

    group = parser.add_argument_group("Measurement")
    group.add_argument("--runs", "-n", type=positive_int, default=10, metavar="N",
                       help="measured iterations (default: 10)")
    group.add_argument("--warmup", "-w", type=non_negative_int, default=1,
                       metavar="N", help="unmeasured warmup iterations")
    group.add_argument("--timeout", type=duration, default=None, metavar="DUR",
                       help="abort a run that exceeds this")
    group.add_argument("--fail-fast", action="store_true", default=False,
                       help="stop at the first non-zero exit")
    group.add_argument("--shell", default=None, metavar="PATH",
                       help="run through this shell instead of exec'ing")
    group.add_argument("--cwd", default=None, metavar="DIR",
                       help="working directory for every run")
    group.add_argument("--repeat", type=non_negative_int, default=1, metavar="N",
                       help="repeat the command N times per run")

    out = parser.add_argument_group("Output")
    out.add_argument("--percentiles", default="50,90,99", metavar="LIST",
                     help="which percentiles to report")
    out.add_argument("--compare", action="store_true", default=False,
                     help="also report per-run detail")
    out.add_argument("--silent", "-q", action="store_true", default=False,
                     help="only print the summary")
    out.add_argument("--json-extra", dest="extra", type=json_object, default=None,
                     metavar="JSON", help="extra fields for the JSON report")

    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND",
                        help="the command to time")
    parser.set_defaults(func=_bench)
    return parser


@register_command("bench")
def _bench(ctx: Context, args: argparse.Namespace) -> int:
    raw_command = list(args.command or [])
    separated = had_separator(raw_command)
    command = [c for c in raw_command if c != "--"]
    if not command:
        raise UsageError("no command to benchmark",
                         hint="separate it with -- : anvil bench --runs 5 -- ls")
    require_exec_form(args, command, separated=separated)
    if args.warmup >= args.runs:
        ctx.warn(f"--warmup {args.warmup} >= --runs {args.runs}; timings will be noisy")

    command_line = " ".join(shlex.quote(c) for c in command)
    env = dict(os.environ)
    timings: list[float] = []
    codes: list[int] = []

    for index in range(args.warmup + args.runs):
        measured = index >= args.warmup
        started = time.perf_counter()
        try:
            completed = _invoke(command, args, env)
        except AnvilError:
            raise
        except OSError as exc:
            from anvil.core.errors import translate_os_error

            raise translate_os_error(exc, context=command[0]) from None
        elapsed = time.perf_counter() - started
        codes.append(completed)
        if measured:
            timings.append(elapsed)
            if not args.silent:
                ctx.err(term.A.hint(f"run {len(timings)}/{args.runs}  "
                                    f"{term.human_duration(elapsed)}"))
        if completed != 0:
            if args.fail_fast:
                raise AnvilError(f"command exited {completed} on run {index + 1}",
                                 hint="drop --fail-fast to continue")
            if not args.silent:
                ctx.err(term.A.warn(f"run {index + 1} exited {completed}"))

    # A benchmark report is consumed by tooling more often than read by eye, so
    # `auto` resolves to JSON rather than a table.
    ctx.tabular("json")
    report = _report(ctx, args, command_line, timings, codes)
    if args.compare:
        report["runs"] = [
            {"index": i + 1, "seconds": round(t, 6)} for i, t in enumerate(timings)
        ]
    if args.extra:
        report.update(args.extra)
    ctx.out(ctx.renderer.scalar(report))
    return 0


def _invoke(command: list[str], args: argparse.Namespace, env) -> int:
    line = command if args.shell is None else [args.shell, "-c", shlex.join(command)]
    return subprocess.run(
        line,
        shell=args.shell is not None,
        cwd=args.cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=args.timeout,
    ).returncode


def _report(ctx: Context, args: argparse.Namespace, command_line: str,
            timings: list[float], codes: list[int]) -> dict:
    ordered = sorted(timings)
    report: dict = {
        "command": command_line,
        "runs": len(ordered),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "failures": sum(1 for c in codes if c != 0),
    }
    if not ordered:
        report["error"] = "no successful runs"
        return report
    report |= {
        "min": round(ordered[0], 6),
        "max": round(ordered[-1], 6),
        "mean": round(statistics.fmean(ordered), 6),
        "median": round(statistics.median(ordered), 6),
        "stdev": round(statistics.stdev(ordered), 6) if len(ordered) > 1 else 0.0,
    }
    for label in (args.percentiles or "").split(","):
        label = label.strip()
        if not label:
            continue
        try:
            fraction = float(label) / 100.0
        except ValueError:
            raise InputError(f"--percentiles got {label!r}, expected a number") from None
        if not 0.0 <= fraction <= 1.0:
            raise InputError(f"percentile {label} must be between 0 and 100")
        report[f"p{label}"] = round(_percentile(ordered, fraction), 6)

    if ctx.output_format in ("table", "auto") and ctx.color:
        table = term.Table([term.Column("metric", "metric"),
                            term.Column("value", "value", align="right")])
        for key in ("runs", "min", "median", "mean", "stdev", "max"):
            if key in report:
                value = report[key]
                table.add_row([key, term.human_duration(value)
                               if key not in ("runs",) else value])
        for key in sorted(report):
            if key.startswith("p") and key[1:].isdigit():
                table.add_row([key + "%", term.human_duration(report[key])])
        ctx.err(table.render())
    return report


def _percentile(ordered: list[float], fraction: float) -> float:
    """Linear-interpolation percentile, matching numpy's default method."""
    if not ordered:
        raise InputError("no samples")
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _bench
