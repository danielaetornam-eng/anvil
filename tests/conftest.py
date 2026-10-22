"""Shared pytest fixtures and helpers.

The ``run`` helper invokes the CLI in-process, capturing stdout, stderr and the
exit code. That is only possible because every parser raises instead of calling
``sys.exit`` -- which is the single biggest testability win argparse can buy,
and the reason :mod:`anvil.core.errors` exists.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys
from dataclasses import dataclass

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from anvil.core import term  # noqa: E402
from anvil.core.app import build_parser, main  # noqa: E402


@dataclass
class Result:
    """The outcome of one in-process CLI invocation."""

    code: int
    out: str
    err: str

    @property
    def output(self) -> str:
        return self.out

    def json(self):
        return json.loads(self.out)

    def __contains__(self, needle: str) -> bool:
        return needle in self.out or needle in self.err


@pytest.fixture(autouse=True)
def _quiet_colour(monkeypatch):
    """Deterministic, escape-free output for every assertion."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.delenv("ANVIL_FORCE_COLOR", raising=False)
    term.set_color(False)
    yield
    term.set_color(False)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """An isolated working directory with a fixture file tree."""
    monkeypatch.chdir(tmp_path)
    for name, content in FIXTURES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


FIXTURES = {
    "users.json": (
        '[{"name":"ada","age":36,"role":"engineer","city":"london"},'
        '{"name":"grace","age":45,"role":"admiral","city":"new york"},'
        '{"name":"alan","age":41,"role":"engineer","city":"london"},'
        '{"name":"edsger","age":72,"role":"engineer","city":"rotterdam"}]\n'
    ),
    "base.json": '{"a":1,"nested":{"x":1,"y":2},"list":[1,2,3]}',
    "over.json": '{"a":2,"nested":{"y":20,"z":30},"list":[9]}',
    "bad.json": '{"a":1,}',
    "rows.csv": "name,age,score\nada,36,99.5\ngrace,45,88\nalan,41,91\n",
    "a.txt": "one\ntwo\nthree\n",
    "b.txt": "one\ntwo\nTHREE\n",
    "plain.txt": "hello world",
    "values.env": "# comment\nname=ada\ncity=london\n",
    "template.tpl": "Hello ${name}, welcome to ${city}!\n",
    "vars.json": '{"name":"grace","city":"new york"}',
    "anvil.toml": ('[http]\ntimeout = "45s"\nretries = 5\n\n'
                   '[api]\ntoken = "super-secret-value"\n'),
}


def run(*argv: str, stdin: str | None = None) -> Result:
    """Invoke the CLI in-process and capture both streams and the exit code."""
    from anvil.core.app import _bootstrap, _dispatch
    from anvil.core.errors import run_cli

    out, err = io.StringIO(), io.StringIO()
    old = (sys.stdout, sys.stderr, sys.stdin)
    # Always install a StringIO: pytest replaces sys.stdin with an object that
    # raises on read, which would make every "reads from stdin" path untestable.
    sys.stdin = io.StringIO(stdin or "")
    sys.stdout, sys.stderr = out, err
    pending: BaseException | None = None
    holder: dict = {}
    try:
        _parser, ctx, namespace = _bootstrap(list(argv), holder=holder)
        # Context holds its streams as dataclass fields, whose defaults were
        # bound to the real sys.stdout at import time. Redirect them explicitly
        # rather than relying on module-level swapping.
        ctx.stdout, ctx.stderr = out, err
        code = _dispatch(ctx, namespace)
    except BaseException as exc:
        pending = exc
        code = 0
    finally:
        sys.stdout, sys.stderr, sys.stdin = old

    if pending is not None:
        sink_out, sink_err = io.StringIO(), io.StringIO()

        def handler(_ignored=None):
            raise pending

        # Pass the parser exactly as main() does, so usage lines and
        # "did you mean" hints render on parse failures.
        code = run_cli(handler, list(argv), stdout=sink_out, stderr=sink_err,
                       parser=lambda: holder.get("parser"))
        out.write(sink_out.getvalue())
        err.write(sink_err.getvalue())
    return Result(int(code or 0), out.getvalue(), err.getvalue())


def parser() -> argparse.ArgumentParser:
    """A freshly built root parser, for structural assertions."""
    return build_parser()


def subparser(name: str) -> argparse.ArgumentParser:
    """The parser for one top-level command."""
    root = build_parser()
    action = next(a for a in root._actions
                  if isinstance(getattr(a, "choices", None), dict))
    return action.choices[name]


def main_argv(*argv: str) -> int:
    """Call the real ``main`` (used where the process exit path matters)."""
    return main(list(argv))
