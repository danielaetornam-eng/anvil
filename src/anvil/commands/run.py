"""``anvil run`` -- execute tasks with a bounded worker pool.

Argparse patterns on display here:

* ``nargs='+'`` positionals plus ``nargs=argparse.REMAINDER`` for pass-through;
* ``--jobs`` as a *range* so ``--jobs 4-8`` means "let me choose";
* an option that is meaningful only with another, enforced with
  ``parser.error`` after parsing rather than at import time;
* ``--dry-run`` implemented once in the shared :class:`~anvil.core.app.Context`
  so every command inherits the same semantics.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import ExitCode
from anvil.core.types import duration, positive_int

HELP = "run tasks as shell commands, in parallel or in sequence"


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "run", aliases=list(aliases), help=HELP,
        description="Execute one or more shell commands.\n\n"
                    "Exit status is non-zero if any command fails; with "
                    "`--continue-on-error` every task still runs and the count "
                    "of failures becomes the exit code.",
    )
    parser.add_examples(
        "anvil run --parallel --jobs 8 build test lint",
        "anvil run -- echo hello world",
        "anvil run --dry-run --parallel 'echo a' 'sleep 1'",
    )

    group = parser.add_argument_group("Execution")
    group.add_argument("--parallel", "-P", action="store_true", default=False,
                       help="run tasks concurrently")
    group.add_argument("--jobs", "-j", type=positive_int, default=1,
                       metavar="N", help="maximum concurrent tasks")
    group.add_argument("--continue-on-error", action="store_true", default=False,
                       help="keep going after a failure")
    group.add_argument("--fail-fast", action="store_true", default=False,
                       help="stop scheduling after the first failure")
    group.add_argument("--timeout", type=duration, default=None, metavar="DUR",
                       help="abort a task that exceeds this")
    group.add_argument("--shell", default=None, metavar="PATH",
                       help="shell used to interpret each task")
    group.add_argument("--env", action="comma_list", default=[], metavar="K=V[,K=V]",
                       help="extra environment variables")
    group.add_argument("--cwd", default=None, metavar="DIR",
                       help="working directory for every task")
    group.add_argument("--stdin", action="store_true", default=False,
                       help="let tasks inherit this process's stdin")
    group.add_argument("--capture", action="store_true", default=False,
                       help="capture output instead of streaming it")

    show = parser.add_argument_group("Reporting")
    show.add_argument("--show-command", action="store_true", default=True,
                      help="echo each command before running it")
    show.add_argument("--quiet-command", dest="show_command",
                      action="store_false", help="do not echo commands")
    show.add_argument("--timing", action="store_true", default=False,
                      help="print elapsed time per task")
    show.add_argument("--silent", "-s", action="store_true", default=False,
                      help="suppress per-task output")

    parser.add_argument("tasks", nargs="+", metavar="TASK",
                        help="command to execute; quote it if it has spaces")
    parser.set_defaults(func=run)
    return parser


@dataclass
class Outcome:
    """The result of one task."""

    task: str
    code: int
    seconds: float
    output: str = ""


@register_command("run")
def run(ctx: Context, args: argparse.Namespace) -> int:
    tasks: list[str] = list(args.tasks)
    # Everything after a bare `--` is *one* command, argv and all. argparse
    # also collects those tokens into ``tasks`` (it treats `--`` as a
    # separator), so they must not be treated as extra tasks or the command
    # would run twice.
    tail: list[str] = list(ctx.passthrough)

    if not tasks and not tail:
        raise _no_tasks()

    if args.parallel and args.jobs == 1:
        jobs = min(32, (os.cpu_count() or 1) + 4)
        ctx.info(f"--parallel with no --jobs; using {jobs} workers")

    for task in tasks:
        _check_contradictions(ctx, args, task)

    combined = [shlex.join(tail)] if tail else list(tasks)
    for item in combined:
        _shell_quote_check(item)

    if ctx.dry_run:
        return _dry_run(ctx, args, combined)

    started = time.perf_counter()
    outcomes = (
        _run_parallel(ctx, args, combined) if args.parallel
        else _run_sequential(ctx, args, combined)
    )
    elapsed = time.perf_counter() - started
    return _report(ctx, args, outcomes, elapsed)


def _no_tasks():
    from anvil.core.errors import UsageError

    return UsageError("no tasks given", hint="try 'anvil run -- echo hi'")


def _check_contradictions(ctx: Context, args: argparse.Namespace, task: str) -> None:
    from anvil.core.errors import UsageError

    if args.fail_fast and args.continue_on_error:
        raise UsageError("--fail-fast and --continue-on-error contradict each other",
                         hint="pick one")
    if args.capture and args.stdin:
        raise UsageError("--capture and --stdin are mutually exclusive",
                         hint="--capture buffers output; --stdin passes the terminal through")
    if not args.parallel and args.jobs != 1:
        raise UsageError(
            f"--jobs {args.jobs} only applies with --parallel",
            hint="add --parallel, or drop --jobs",
        )


def _shell_quote_check(task: str) -> None:
    from anvil.core.errors import UsageError

    if not task.strip():
        raise UsageError("empty task", hint="quote the task: -- 'echo  hi'")
    if task.lstrip().startswith("-"):
        raise UsageError(
            f"task {task!r} looks like an option",
            hint="separate it from anvil's own flags with --",
        )


def _dry_run(ctx: Context, args: argparse.Namespace, tasks: list[str]) -> int:
    ctx.out(ctx.renderer.scalar({"mode": "dry-run", "tasks": tasks,
                                 "parallel": bool(args.parallel),
                                 "jobs": int(args.jobs)}))
    for task in tasks:
        ctx.err(term.A.hint(f"would run: {task}"))
    return 0


def _environment(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    for pair in args.env or []:
        key, _, value = pair.partition("=")
        if not key:
            continue
        env[key.strip()] = value
    return env


def _invoke(ctx: Context, args: argparse.Namespace, task: str) -> Outcome:
    shell = args.shell
    command = task if shell is None else f"{shell} -c {shlex.quote(task)}"
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            shell=shell is None,
            cwd=args.cwd,
            env=_environment(args),
            stdin=None if args.stdin else subprocess.DEVNULL,
            capture_output=bool(args.capture or args.silent),
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        return Outcome(task, int(ExitCode.TEMPFAIL),
                       time.perf_counter() - started, "timed out")
    except OSError as exc:
        from anvil.core.errors import translate_os_error

        raise translate_os_error(exc, context=task) from None
    output = (completed.stdout or "") + (completed.stderr or "")
    return Outcome(task, completed.returncode, time.perf_counter() - started, output)


def _run_sequential(ctx: Context, args: argparse.Namespace,
                    tasks: list[str]) -> list[Outcome]:
    outcomes: list[Outcome] = []
    failed = False
    for task in tasks:
        if failed and args.fail_fast:
            break
        if args.show_command:
            ctx.err(term.A.hint(f"$ {task}"))
        outcome = _invoke(ctx, args, task)
        outcomes.append(outcome)
        _echo(ctx, args, outcome)
        if outcome.code != 0:
            failed = True
            if not args.continue_on_error and not args.fail_fast:
                break
    return outcomes


def _run_parallel(ctx: Context, args: argparse.Namespace,
                  tasks: list[str]) -> list[Outcome]:
    outcomes: list[Outcome] = []
    stopped = False

    def task(name: str) -> Outcome:
        if args.show_command:
            ctx.err(term.A.hint(f"$ {name}"))
        return _invoke(ctx, args, name)

    with ThreadPoolExecutor(max_workers=max(1, int(args.jobs))) as pool:
        futures = {pool.submit(task, name): name for name in tasks}
        with ctx.progress(len(tasks), label="running") as bar:
            for future in futures:
                if stopped and args.fail_fast:
                    break
                outcome = future.result()
                outcomes.append(outcome)
                _echo(ctx, args, outcome)
                if outcome.code != 0 and args.fail_fast:
                    stopped = True
                    for pending in futures:
                        pending.cancel()
                bar.advance(1, label=f"{len(outcomes)}/{len(tasks)}")
    return outcomes


def _echo(ctx: Context, args: argparse.Namespace, outcome: Outcome) -> None:
    if args.silent:
        return
    if args.capture:
        if outcome.output:
            ctx.out(outcome.output.rstrip("\n"))
    elif outcome.code != 0:
        ctx.err(term.A.err(f"failed ({outcome.code}): {outcome.task}"))
    if args.timing:
        ctx.err(term.A.hint(f"  {term.human_duration(outcome.seconds)}  {outcome.task}"))


def _report(ctx: Context, args: argparse.Namespace, outcomes: list[Outcome],
            elapsed: float) -> int:
    failures = [o for o in outcomes if o.code != 0]
    table = term.Table(
        [term.Column("task", "task"), term.Column("code", "code", align="right"),
         term.Column("time", "time", align="right")],
    )
    for outcome in outcomes:
        table.add_row([outcome.task, outcome.code,
                       term.human_duration(outcome.seconds)])
    if ctx.output_format == "table" and not args.silent:
        ctx.err(table.render())

    summary = (
        f"{len(outcomes) - len(failures)}/{len(outcomes)} tasks ok in "
        f"{term.human_duration(elapsed)}"
    )
    if failures:
        ctx.err(term.A.err(f"{len(failures)} failed: ") + summary)
        # Return the failure *count* so a caller can distinguish 1 from many.
        return min(255, len(failures)) if args.continue_on_error else (
            failures[0].code or int(ExitCode.FAILURE)
        )
    ctx.err(term.A.ok(summary))
    return 0
