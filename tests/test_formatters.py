"""Tests for help rendering: colour, width, annotations, and argparse quirks."""

from __future__ import annotations

import argparse
import functools

import pytest

from anvil.core import term
from anvil.core.errors import AnvilArgumentParser
from anvil.core.formatters import (
    AnvilHelpFormatter,
    PlainHelpFormatter,
    render_examples,
)


def build(color=False, **kwargs):
    parser = AnvilArgumentParser(
        prog="anvil",
        description="A tool.",
        formatter_class=AnvilHelpFormatter if color else PlainHelpFormatter,
        **kwargs,
    )
    return parser


def test_usage_lists_options_and_the_command_slot():
    parser = build()
    parser.add_argument("--jobs", "-j", type=int, default=1)
    parser.add_subparsers(dest="command", metavar="COMMAND")
    usage = parser.format_usage()
    assert "anvil" in usage
    assert "--jobs" in usage
    assert "COMMAND" in usage


def test_suppressed_options_never_appear():
    parser = build()
    parser.add_argument("--visible", help="shown")
    parser.add_argument("--hidden", help=argparse.SUPPRESS)
    text = parser.format_help()
    assert "--visible" in text
    assert "--hidden" not in text


def test_string_defaults_are_shown_verbatim():
    """The help must read ``default: 30s``, not the converted ``30.0``."""
    parser = build()
    parser.add_argument("--timeout", default="30s", help="a timeout")
    assert "default: 30s" in parser.format_help()


def test_annotations_are_appended_automatically():
    parser = build()
    parser.add_argument("--jobs", type=int, default=4, help="worker count")
    parser.add_argument("--name", required=True, help="who")
    parser.add_argument("--mode", choices=["a", "b"], help="mode")
    text = parser.format_help()
    assert "default: 4" in text
    assert "required" in text
    assert "one of: a, b" in text


def test_deprecated_marker_appears():
    parser = build()
    parser.add_argument("--legacy", help="old way", deprecated=True)
    assert "deprecated" in parser.format_help()


def test_env_var_marker_appears():
    parser = build()
    parser.add_argument("--token", help="auth", anvil_env="ANVIL_TOKEN")
    assert "$ANVIL_TOKEN" in parser.format_help()


def test_flag_values_render_nothing_extra():
    parser = build()
    parser.add_argument("--verbose", action="store_true", help="chatty")
    parser.add_argument("--level", action="count", default=0, help="level")
    text = parser.format_help()
    assert "default: False" not in text


def test_positionals_are_not_marked_required():
    """argparse sets ``required=True`` for nargs='+'; saying so is noise."""
    parser = build()
    parser.add_argument("items", nargs="+", help="things")
    assert "required" not in parser.format_help()


def test_suppress_is_not_confused_with_a_string_default():
    """``SUPPRESS`` is a plain str; a naive isinstance check eats every default.

    ``isinstance(default, argparse.SUPPRESS.__class__)`` is true for *any*
    string, which silently removes the "default:" annotation from every option
    whose default happens to be text -- including ``--output auto``.
    """
    parser = build()
    parser.add_argument("--mode", default="auto", help="mode")
    parser.add_argument("--gone", default=argparse.SUPPRESS, help="no default")
    text = parser.format_help()
    assert "default: auto" in text
    assert text.count("default:") == 1


def test_colour_output_contains_escapes_and_plain_does_not():
    plain = build(False)
    plain.add_argument("--x", help="x")
    assert "\x1b[" not in plain.format_help()

    # argparse instantiates formatter_class(prog) only, so colour has to be
    # forced through a partial rather than passed as a parser keyword.
    coloured = AnvilArgumentParser(
        prog="anvil", description="A tool.",
        formatter_class=functools.partial(AnvilHelpFormatter, color=True),
    )
    coloured.add_argument("--x", help="x")
    coloured_text = coloured.format_help()
    assert "\x1b[" in coloured_text
    # Colour must not disturb the layout: stripping it yields the same bytes.
    assert term.strip_ansi(coloured_text) == plain.format_help()


def test_help_lines_never_exceed_the_width(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(term, "terminal_size", lambda default=None: (80, 24))
    parser = AnvilArgumentParser(prog="anvil", formatter_class=AnvilHelpFormatter,
                                 color=True)
    parser.add_argument("--secret", action="store_true",
                        help=("a very long help string that has to wrap across "
                              "several lines to prove the hanging indent stays "
                              "aligned under colour escapes too"))
    for line in parser.format_help().splitlines():
        assert term.visible_width(line) <= 80, repr(line)


def test_unresolvable_placeholder_does_not_crash_the_help():
    parser = build()
    parser.add_argument("--x", help="references %(nosuchthing)s here")
    assert "references" in parser.format_help()


def test_usage_percent_expansion_works():
    parser = build()
    parser.add_argument("--x", default="v", help="default is %(default)s")
    assert "default: v" in parser.format_help()


def test_examples_render_as_a_separate_block():
    block = render_examples(["anvil run --jobs 8 build", "a-very-long-example-that-wraps-" * 2],
                            width=60, color=False)
    assert "EXAMPLES" in block
    assert "$ anvil run" in block
    for line in block.splitlines():
        assert term.visible_width(line) <= 60


def test_markdown_formatter_emits_markdown():
    from anvil.core.formatters import MarkdownHelpFormatter

    parser = argparse.ArgumentParser(prog="anvil",
                                     formatter_class=MarkdownHelpFormatter)
    parser.add_argument("--jobs", type=int, default=1, help="workers")
    text = parser.format_help()
    assert "`--jobs JOBS`" in text
    assert "workers" in text


def test_subparser_prog_is_not_polluted_by_parent_help():
    """``add_subparsers`` derives child prog from ``format_help()``.

    Anything ``format_help`` appends therefore becomes part of every child's
    program name. The examples block is printed by the help *action* instead.
    """
    parser = build()
    parser.add_examples("anvil run --jobs 8 build")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    child = sub.add_parser("run", help="run things")
    assert child.prog == "anvil run"
    assert "EXAMPLES" not in child.prog


def test_subparser_inherits_the_formatter_and_custom_actions():
    parser = build()
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    child = sub.add_parser("run")
    assert isinstance(child, AnvilArgumentParser)
    assert child.formatter_class is AnvilHelpFormatter
    child.add_argument("--include", action="comma_list", default=[])
    assert child.parse_args(["--include", "a,b"]).include == ["a", "b"]


@pytest.mark.parametrize("flag", ["--output", "--dry-run", "--yes", "--no-color",
                                   "--log-level", "--config", "--set", "-v", "-q"])
def test_global_flags_work_after_the_command_name(flag):
    """Users expect flag position to be irrelevant; users win."""
    from conftest import subparser

    child = subparser("secrets")
    assert flag in {o for a in child._actions for o in a.option_strings}


def test_globals_use_suppress_defaults_so_they_do_not_reset_root_choices():
    """A subparser default must not overwrite what the root already decided."""
    from conftest import subparser

    child = subparser("run")
    for action in child._actions:
        if action.dest in ("dry_run", "output", "yes"):
            assert action.default is argparse.SUPPRESS, action.dest


def test_conflicting_flag_is_resolved_not_fatal():
    parser = AnvilArgumentParser(prog="anvil")
    parser.add_argument("--silent", "-q", action="store_true")
    parser.add_argument("--silent", action="store_true")  # would raise with 'error'
    assert parser.parse_args(["--silent"]).silent is True


def test_command_local_flag_wins_over_the_inherited_global():
    from conftest import subparser

    child = subparser("run")
    own = [a for a in child._actions if "--silent" in a.option_strings]
    assert own and own[0].dest == "silent"
    assert child.parse_args(["--silent", "echo hi"]).silent is True


def test_custom_metadata_keywords_do_not_reach_the_action():
    parser = build()
    action = parser.add_argument("--x", anvil_env="X", anvil_since="1.0")
    assert action.anvil_env == "X"
    assert action.anvil_since == "1.0"


def test_metadata_works_inside_an_argument_group():
    """Groups bypass the parser's ``add_argument``, so they need their own hook."""
    parser = build()
    group = parser.add_argument_group("Grouped")
    action = group.add_argument("--x", help="x", anvil_env="GROUP_X")
    assert action.anvil_env == "GROUP_X"
    assert "$GROUP_X" in parser.format_help()
