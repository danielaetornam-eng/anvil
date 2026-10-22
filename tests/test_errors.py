"""Tests for the error taxonomy, exit codes, and suggestion machinery."""

from __future__ import annotations

import argparse
import io

import pytest

from anvil.core import errors as E

# --------------------------------------------------------------------------- #
# Exit codes
# --------------------------------------------------------------------------- #


def test_exit_codes_match_sysexits_conventions():
    assert E.ExitCode.OK == 0
    assert E.ExitCode.USAGE == 2
    assert E.ExitCode.DATA == 65
    assert E.ExitCode.NO_INPUT == 66
    assert E.ExitCode.CONFIG == 78
    assert E.ExitCode.INTERRUPTED == 130


def test_error_classes_carry_codes_and_hints():
    for cls, code in ((E.UsageError, E.ExitCode.USAGE),
                      (E.InputError, E.ExitCode.DATA),
                      (E.NotFoundError, E.ExitCode.NO_INPUT),
                      (E.ConfigError, E.ExitCode.CONFIG),
                      (E.TimeoutError_, E.ExitCode.TEMPFAIL)):
        exc = cls("boom", hint="try this")
        assert exc.exit_code == code
        assert exc.render().endswith("try this")


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("typed", "expected"), [
    ("--dryrun", "--dry-run"),
    ("-qiet", "--quiet"),
    ("jsonn", "--json"),
    ("--outpt", "--output"),
])
def test_similar_finds_the_intended_option(typed, expected):
    pool = ["--dry-run", "--quiet", "--json", "--output", "--jobs"]
    assert expected in E.similar(typed, pool)


def test_similar_prefers_a_shared_prefix():
    """A partial flag's intent is almost always a prefix match."""
    pool = ["--verify", "--verbose", "--version"]
    assert E.similar("--verb", pool)[0] == "--verbose"


def test_similar_ignores_case_and_separators():
    pool = ["--dry-run"]
    assert E.similar("--DRY_RUN", pool) == ["--dry-run"]


def test_similar_returns_nothing_for_nonsense():
    assert E.similar("zzzzqqq", ["--jobs", "--quiet"]) == []
    assert E.did_you_mean("zzzzqqq", ["--jobs"]) is None


def test_did_you_mean_singular_and_plural():
    single = E.did_you_mean("jsonn", ["json", "table"])
    assert "did you mean" in single
    plural = E.did_you_mean("zzzz", ["alpha", "beta"])
    assert plural is None or "one of" in plural


@pytest.mark.parametrize(("typed", "expected"), [
    ("tset", "text"),      # adjacent transposition, one edit
    ("jsom", "json"),
    ("htpp", "http"),
    ("tabel", "table"),
])
def test_transpositions_are_one_edit(typed, expected):
    """``difflib`` scores a transposition as two substitutions.

    ``difflib.SequenceMatcher(None, "teh", "text").ratio()`` is 0.29, so a
    naive suggester stays silent for the single most common typo there is.
    """
    assert E.similar(typed, ["run", "http", "json", "table", "text", "env"]) == [
        expected
    ]


def test_genuinely_distant_input_gets_no_suggestion():
    """Two edits plus a transposition is too far; guessing would be noise."""
    assert E.similar("teh", ["run", "http", "json", "table", "text"]) == []


def test_owner_elsewhere_finds_a_flag_in_another_subcommand(workdir):
    from conftest import run

    result = run("table", "--colums", "name", "users.json")
    assert result.code == E.ExitCode.USAGE
    assert "--columns" in result.err
    assert "anvil table" in result.err


def test_owner_elsewhere_is_skipped_for_the_own_command(workdir):
    from conftest import run

    result = run("table", "--sort", "name", "users.json")
    assert result.code == 0


# --------------------------------------------------------------------------- #
# OS error translation
# --------------------------------------------------------------------------- #


def test_translate_os_error_maps_errno_to_code_and_hint():
    exc = OSError(2, "No such file or directory")
    exc.errno = 2
    translated = E.translate_os_error(exc, context="data.json")
    assert translated.exit_code == E.ExitCode.NO_INPUT
    assert "data.json" in translated.message
    assert translated.hint


def test_translate_permission_error():
    exc = OSError(13, "Permission denied")
    exc.errno = 13
    assert E.translate_os_error(exc).exit_code == E.ExitCode.NOPERM


# --------------------------------------------------------------------------- #
# The parser raises instead of exiting
# --------------------------------------------------------------------------- #


def test_type_errors_surface_as_argument_error():
    """``exit_on_error=False`` makes ``ArgumentError`` reachable instead of a
    direct ``sys.exit(2)`` -- that is what lets ``run_cli`` own the exit path."""
    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    parser.add_argument("--x", type=int)
    with pytest.raises(argparse.ArgumentError) as info:
        parser.parse_args(["--x", "nope"])
    assert "invalid int value" in info.value.message


def test_missing_required_argument_raises_argument_error():
    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    parser.add_argument("--needed", required=True)
    with pytest.raises(argparse.ArgumentError) as info:
        parser.parse_args([])
    assert "required" in info.value.message


def test_error_called_by_a_custom_action_becomes_a_parse_failure():
    """``parser.error()`` inside an ``Action`` still routes through the hook."""
    class Strict(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            # nargs is None here, so ``values`` is a plain string, not a list.
            if values == "bad":
                parser.error("that value is not allowed")
            setattr(namespace, self.dest, values)

    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    parser.add_argument("--x", action=Strict)
    with pytest.raises(E.ParseFailure) as info:
        parser.parse_args(["--x", "bad"])
    assert "not allowed" in info.value.message
    assert parser.parse_args(["--x", "fine"]).x == "fine"


def test_parser_exit_raises_exit_requested():
    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    with pytest.raises(E.ExitRequested):
        parser.exit(3)


def test_unknown_argument_raises_rather_than_exiting():
    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    parser.add_argument("--x")
    with pytest.raises(argparse.ArgumentError):
        parser.parse_args(["--nope"])


def test_prefix_abbreviation_is_allowed_by_default():
    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    parser.add_argument("--jobs", type=int)
    assert parser.parse_args(["--job", "2"]).jobs == 2


def test_suggestion_is_computed_for_invalid_choices():
    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("json")
    sub.add_parser("table")
    with pytest.raises(argparse.ArgumentError):
        parser.parse_args(["jsonn"])
    parser.attach_suggestion("invalid choice: 'jsonn' (choose from 'json, table')")
    assert "json" in (parser.suggestion or "")


# --------------------------------------------------------------------------- #
# run_cli
# --------------------------------------------------------------------------- #


def call(handler, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    code = E.run_cli(handler, [], stdout=out, stderr=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def test_run_cli_returns_the_handler_result():
    assert call(lambda _a: 7)[0] == 7
    assert call(lambda _a: None)[0] == 0


def test_run_cli_maps_typed_errors_to_codes():
    code, _, err = call(lambda _a: (_ for _ in ()).throw(E.ConfigError("bad cfg")))
    assert code == E.ExitCode.CONFIG
    assert "bad cfg" in err


def test_run_cli_maps_keyboard_interrupt_to_130():
    code, _, _ = call(lambda _a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert code == E.ExitCode.INTERRUPTED


def test_run_cli_handles_exit_requested():
    code, out, _ = call(lambda _a: (_ for _ in ()).throw(
        E.ExitRequested(3, "bye\n")))
    assert code == 3
    assert out == "bye\n"


def test_run_cli_reports_unexpected_errors_as_internal():
    code, _, err = call(lambda _a: (_ for _ in ()).throw(RuntimeError("kaboom")))
    assert code == E.ExitCode.SOFTWARE
    assert "internal error" in err
    assert "kaboom" in err
    assert "Traceback" not in err


def test_run_cli_reraises_when_traceback_is_requested():
    with pytest.raises(RuntimeError):
        call(lambda _a: (_ for _ in ()).throw(RuntimeError("kaboom")),
             traceback_on_error=True)


def test_run_cli_maps_os_errors():
    exc = OSError(2, "No such file or directory")
    exc.errno = 2
    code, _, err = call(lambda _a: (_ for _ in ()).throw(exc))
    assert code == E.ExitCode.NO_INPUT
    assert "error" in err


def test_run_cli_turns_argument_errors_into_usage_exits():
    parser = E.AnvilArgumentParser(prog="t", add_help=False)
    parser.add_argument("--x", type=int)

    def handler(_a):
        return parser.parse_args(["--x", "nope"])

    out, err = io.StringIO(), io.StringIO()
    code = E.run_cli(handler, [], stdout=out, stderr=err, parser=parser)
    assert code == E.ExitCode.USAGE
    assert "error" in err.getvalue()
    assert "Traceback" not in err.getvalue()
    assert "usage" in err.getvalue()
