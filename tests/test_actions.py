"""Unit tests for the custom ``Action`` layer."""

from __future__ import annotations

import argparse

import pytest

from anvil.core import actions as A


@pytest.fixture
def P():
    """A parser class with every custom action registered."""
    class Registered(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            A.register_all(self)

    return Registered


# --------------------------------------------------------------------------- #
# store-like
# --------------------------------------------------------------------------- #


def test_append_unique_skips_duplicates(P):
    p = P(prog="t", add_help=False)
    p.add_argument("--tag", action="append_unique", default=[])
    assert p.parse_args(["--tag", "a", "--tag", "b", "--tag", "a"]).tag == ["a", "b"]


def test_comma_list_splits_and_accumulates(P):
    p = P(prog="t", add_help=False)
    p.add_argument("--include", action="comma_list", default=[])
    p.add_argument("rest", nargs="*")
    ns = p.parse_args(["--include", "a,b", "--include", "c", "tail"])
    assert ns.include == ["a", "b", "c"]
    # The whole point of nargs=1: a following positional is not eaten.
    assert ns.rest == ["tail"]


def test_comma_list_rejects_empty(P, capsys):
    p = P(prog="t", add_help=False)
    p.add_argument("--include", action="comma_list", default=[])
    with pytest.raises(SystemExit):
        p.parse_args(["--include", ""])


def test_key_value_accumulates_into_dict(P):
    p = P(prog="t", add_help=False)
    p.add_argument("--set", action="key_value", default={}, dest="overrides")
    ns = p.parse_args(["--set", "a=1", "--set", "b=2", "--set", "a=3"])
    assert ns.overrides == {"a": "3", "b": "2"}


def test_key_value_requires_equals(P, capsys):
    p = P(prog="t", add_help=False)
    p.add_argument("--set", action="key_value", default={}, dest="overrides")
    with pytest.raises(SystemExit):
        p.parse_args(["--set", "novalue"])
    assert "KEY=VALUE" in capsys.readouterr().err


def test_auto_name_uses_the_flag_as_the_key(P):
    p = P(prog="t", add_help=False)
    p.add_argument("--maintainer", action="auto_name", default={}, dest="people")
    p.add_argument("--support-url", action="auto_name", default={}, dest="people")
    ns = p.parse_args(["--maintainer", "ana", "--support-url", "https://x"])
    assert ns.people == {"maintainer": "ana", "support_url": "https://x"}


def test_auto_name_prefers_the_long_option(P):
    """``-D`` must not produce a column literally named ``D``."""
    p = P(prog="t", add_help=False)
    p.add_argument("-D", "--define", action="auto_name", default={}, dest="defines")
    assert p.parse_args(["-D", "v"]).defines == {"define": "v"}


def test_pattern_list_compiles_and_validates(P, capsys):
    import re

    p = P(prog="t", add_help=False)
    p.add_argument("--match", action="pattern_list", default=[])
    ns = p.parse_args(["--match", r"^\d+$"])
    assert isinstance(ns.match[0], re.Pattern)
    assert ns.match[0].match("42")
    with pytest.raises(SystemExit):
        p.parse_args(["--match", "["])
    assert "invalid pattern" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# counters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("argv", "expected"), [
    ([], 0), (["-v"], 1), (["-v", "-v", "-v"], 3),
    (["-q"], -1), (["-v", "-q", "-q"], -1),
])
def test_verbosity_counts_both_directions(P, argv, expected):
    p = P(prog="t", add_help=False)
    p.add_argument("-v", "-q", "--verbosity", action="verbosity", dest="verbosity")
    assert p.parse_args(argv).verbosity == expected


def test_verbose_count_is_clamped(P):
    p = P(prog="t", add_help=False)
    p.add_argument("-v", action="verbose_count", levels=[0, 10, 20, 30], dest="v")
    assert p.parse_args(["-v"] * 9).v == 3


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_existing_file_action_resolves(P, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("hi", encoding="utf-8")
    p = P(prog="t", add_help=False)
    p.add_argument("path", action="existing_file")
    assert p.parse_args([str(target)]).path == target.resolve()
    with pytest.raises(SystemExit):
        p.parse_args([str(tmp_path / "nope")])
    with pytest.raises(SystemExit):
        p.parse_args([str(tmp_path)])


def test_existing_dir_action(P, tmp_path):
    p = P(prog="t", add_help=False)
    p.add_argument("--dir", action="existing_dir")
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        p.parse_args(["--dir", str(target)])


def test_range_action_accepts_three_spellings(P):
    p = P(prog="t", add_help=False)
    p.add_argument("--bounds", action="range", low=0, high=None)
    assert p.parse_args(["--bounds", "0", "10"]).bounds == (0.0, 10.0)
    assert p.parse_args(["--bounds", "0-"]).bounds == (0.0, None)
    assert p.parse_args(["--bounds", "1-5"]).bounds == (1.0, 5.0)
    # A leading '-' needs '=' because argparse reads it as an option otherwise.
    assert p.parse_args(["--bounds=-2-"]).bounds == (-2.0, None)
    with pytest.raises(SystemExit):
        p.parse_args(["--bounds", "10", "0"])


def test_threshold_actions_share_one_dict(P):
    p = P(prog="t", add_help=False)
    p.add_argument("--warning", action="threshold", threshold_name="warn")
    p.add_argument("--critical", action="threshold", threshold_name="crit")
    ns = p.parse_args(["--warning", "80", "--critical", "95"])
    assert ns.thresholds == {"warn": "80", "crit": "95"}


def test_format_template_action_validates_placeholders(P, capsys):
    p = P(prog="t", add_help=False)
    p.add_argument("-w", "--write-out", action="write_out", default="")
    assert p.parse_args(["-w", "%{http_code} %{time_total}s"]).write_out
    with pytest.raises(SystemExit):
        p.parse_args(["-w", "%{typo}"])
    assert "unknown write-out" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# output files
# --------------------------------------------------------------------------- #


def test_lazy_output_file_does_not_touch_disk_until_written(tmp_path):
    target = tmp_path / "sub" / "out.txt"
    handle = A.LazyOutputFile(target)
    assert not handle.opened
    assert not target.parent.exists()
    handle.write("data\n")
    handle.close()
    assert handle.opened
    assert target.read_text(encoding="utf-8") == "data\n"
    assert "1 lines" in handle.summary()


def test_output_file_action_maps_dash_to_stdout():
    import sys

    class P(argparse.ArgumentParser):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            A.register_all(self)

    p = P(prog="t", add_help=False)
    p.add_argument("-o", "--output", action="output_file")
    assert p.parse_args(["-o", "-"]).output is sys.stdout


def test_atomic_output_commits_on_success(tmp_path):
    target = tmp_path / "atomic.txt"
    with A.AtomicOutput(target) as handle:
        handle.write("complete")
    assert target.read_text(encoding="utf-8") == "complete"


def test_atomic_output_leaves_nothing_behind_on_failure(tmp_path):
    target = tmp_path / "atomic.txt"
    with pytest.raises(RuntimeError), A.AtomicOutput(target) as handle:
        handle.write("partial")
        raise RuntimeError("boom")
    assert not target.exists()
    assert not list(tmp_path.glob(".*tmp"))


# --------------------------------------------------------------------------- #
# environment, defaults, deprecation
# --------------------------------------------------------------------------- #


def test_env_fallback_only_fills_missing(P, monkeypatch):
    from anvil.core.types import port

    env = {"ANVIL_TEST_PORT": "9000"}
    p = P(prog="t", add_help=False)
    p.add_argument("--port", action="env_fallback", env="ANVIL_TEST_PORT", type=port)
    ns = A.apply_env_fallbacks(p, p.parse_args([]), env)
    assert ns.port == 9000
    # An explicit flag must win over the environment.
    ns = A.apply_env_fallbacks(p, p.parse_args(["--port", "1234"]), env)
    assert ns.port == 1234


def test_convert_string_defaults_applies_type(P):
    from anvil.core.types import duration

    p = P(prog="t", add_help=False)
    p.add_argument("--timeout", type=duration, default="30s")
    ns = A.convert_string_defaults(p, p.parse_args([]))
    assert ns.timeout == 30.0


def test_deprecated_action_warns(P):
    p = P(prog="t", add_help=False)
    p.add_argument("--old", action="deprecated", replacement="--new",
                   removed_in="2.0")
    with pytest.warns(DeprecationWarning, match="use --new instead"):
        p.parse_args(["--old"])


def test_remainder_strips_the_separator(P):
    p = P(prog="t", add_help=False)
    p.add_argument("cmd")
    p.add_argument("rest", action="remainder")
    assert p.parse_args(["run", "-x", "--y"]).rest == ["-x", "--y"]


def test_secret_value_redacts_but_stays_a_string():
    secret = A.SecretValue("hunter2")
    assert isinstance(secret, str)
    assert "hunter2" not in repr(secret)
    assert "hunter2" not in f"{secret}"
    assert "hunter2" not in f"{secret}"
    assert secret.reveal() == "hunter2"


def test_confirm_handles_no_tty(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert A.confirm("ok?", default=True) is True
    assert "not a terminal" in capsys.readouterr().err
    assert A.confirm("ok?", assume_yes=True, default=False) is True


def test_register_all_is_idempotent(P):
    p = P(prog="t", add_help=False)
    A.register_all(p)
    A.register_all(p)
    p.add_argument("--x", action="comma_list", default=[])
    assert p.parse_args(["--x", "a"]).x == ["a"]
