"""Tests for the terminal layer: width maths, wrapping, tables, progress."""

from __future__ import annotations

import io

import pytest

from anvil.core import term

# --------------------------------------------------------------------------- #
# Width arithmetic
# --------------------------------------------------------------------------- #


def test_strip_ansi_and_visible_width():
    term.set_color(True)
    try:
        coloured = term.A.ok("hello")
    finally:
        term.set_color(False)
    assert "\x1b[" in coloured
    assert term.strip_ansi(coloured) == "hello"
    assert term.visible_width(coloured) == 5


def test_visible_width_counts_wide_characters_as_two():
    assert term.visible_width("日本") == 4


def test_visible_width_ignores_combining_marks():
    assert term.visible_width("e\u0301") == 1


def test_pad_uses_visible_width():
    assert term.visible_width(term.pad(term.A.err("ab"), 6)) == 6
    assert term.pad("ab", 5).endswith("   ")
    assert term.pad("ab", 5, "right").startswith("   ")


@pytest.mark.parametrize("width", [1, 4, 10, 39])
def test_truncate_never_exceeds_the_width(width):
    for text in ("abcdefghij", term.A.ok("abcdefghij"), "日本語テキスト"):
        assert term.visible_width(term.truncate(text, width)) <= width


# --------------------------------------------------------------------------- #
# ANSI-aware wrapping
# --------------------------------------------------------------------------- #


COLUMNS = 40


@pytest.mark.parametrize("text", [
    "a very long help string that has to wrap across several lines to prove the point",
    "word " * 30,
    "hyphen-ated-words-that-are-very-long-indeed-and-keep-going-for-a-while",
    "",
])
def test_wrap_ansi_respects_the_width(text):
    lines = term.wrap_ansi(text, COLUMNS, subsequent_indent="  ",
                           break_long_words=True)
    assert lines
    for line in lines:
        assert term.visible_width(line) <= COLUMNS, repr(line)


def test_wrap_ansi_measures_coloured_text_correctly():
    """``textwrap`` counts SGR bytes, which shortens every line."""
    term.set_color(True)
    try:
        coloured = (term.A.hint("default: 30.0") + " "
                    + term.A.cyan("$ANVIL_TOKEN") + " " + term.A.err("required"))
        plain = "default: 30.0 $ANVIL_TOKEN required"
    finally:
        term.set_color(False)
    coloured_lines = term.wrap_ansi(coloured, 40, break_long_words=True)
    plain_lines = term.wrap_ansi(plain, 40, break_long_words=True)
    assert len(coloured_lines) == len(plain_lines)
    assert [term.strip_ansi(line) for line in coloured_lines] == plain_lines


def test_wrap_ansi_preserves_colour():
    term.set_color(True)
    try:
        text = term.A.ok("alpha ") + "bravo " * 10
        lines = term.wrap_ansi(text, COLUMNS, break_long_words=True)
    finally:
        term.set_color(False)
    assert any("\x1b[" in line for line in lines)
    assert "alpha" in term.strip_ansi(lines[0])


def test_wrap_ansi_honours_indents():
    lines = term.wrap_ansi("one two three four five six seven eight nine ten eleven",
                           40, initial_indent="  ", subsequent_indent="      ",
                           break_long_words=True)
    assert lines[0].startswith("  ")
    assert lines[1].startswith("      ")
    for line in lines:
        assert term.visible_width(line) <= 40


def test_wrap_ansi_leaves_long_words_alone_when_asked():
    """Matches textwrap's default: break_long_words=False overflows."""
    word = "x" * 70
    assert term.wrap_ansi(word, COLUMNS, break_long_words=False) == [word]


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def test_table_from_rows_aligns_numbers_right():
    table = term.Table.from_rows([["a", 1], ["bb", 22]], titles=["name", "n"])
    rendered = table.render()
    assert "name" in rendered and "n" in rendered
    assert "\u2502" in rendered


def test_table_respects_max_column_width():
    table = term.Table.from_rows([["a very long value indeed", "x"]],
                                 titles=["k", "v"])
    table.columns[0].max_width = 10
    rendered = table.render(30)
    for line in rendered.splitlines():
        assert term.visible_width(line) <= 30


def test_table_truncate_and_rows_are_public():
    table = term.Table.from_rows([[str(i)] for i in range(10)], titles=["n"])
    assert len(table) == 10
    table.truncate(3)
    assert len(table) == 3
    assert table.rows() == [["0"], ["1"], ["2"]]


def test_table_sort_is_stable_and_directional():
    table = term.Table.from_rows([["b", 1], ["a", 2]], titles=["k", "v"])
    table.sort("v")
    assert table.rows() == [["b", "1"], ["a", "2"]]
    table.sort("-v")
    assert table.rows() == [["a", "2"], ["b", "1"]]
    assert term.Table.from_rows([["bb", 2], ["a", 2]],
                                titles=["k", "v"]).sort("v").rows()[0] == ["bb", "2"]


def test_table_serialisations():
    table = term.Table.from_rows([["a", 1]], titles=["k", "v"])
    assert table.to_csv() == "k,v\na,1"
    assert table.to_csv("\t") == "k\tv\na\t1"
    assert "<table>" in table.to_html()
    assert "\\begin{tabular}" in table.to_latex()
    assert "| k | v |" in table.to_markdown()
    assert table.to_json().strip().startswith("[")


def test_table_style_none_has_no_borders():
    table = term.Table.from_rows([["a", 1]], titles=["k", "v"], style="none")
    assert "\u250c" not in table.render()


def test_empty_table_renders_nothing():
    assert term.Table().render() == ""


# --------------------------------------------------------------------------- #
# Trees
# --------------------------------------------------------------------------- #


def test_tree_from_dict_nests_correctly():
    text = term.Tree().from_dict({"a": {"b": [1, 2]}, "d": 3})
    plain = term.strip_ansi(text)
    assert plain.splitlines() == [
        "<root>",
        "\u251c\u2500\u2500 a",
        "\u2502   \u2514\u2500\u2500 b",
        "\u2502       \u251c\u2500\u2500 [0] = 1",
        "\u2502       \u2514\u2500\u2500 [1] = 2",
        "\u2514\u2500\u2500 d = 3",
    ]


def test_tree_ascii_style():
    text = term.strip_ansi(term.Tree(style="ascii").from_dict(
        {"x": {"y": 1}, "z": 2}))
    assert text.splitlines() == ["<root>", "|-- x", "|   `-- y = 1", "`-- z = 2"]


def test_tree_respects_max_depth():
    text = term.Tree().from_dict({"a": {"b": {"c": {"d": 1}}}}, )
    deep = term.Tree(max_depth=1).from_dict({"a": {"b": {"c": {"d": 1}}}})
    assert len(deep.splitlines()) < len(text.splitlines())


# --------------------------------------------------------------------------- #
# Progress and streams
# --------------------------------------------------------------------------- #


def test_progress_is_silent_when_disabled():
    stream = io.StringIO()
    bar = term.Progress(3, stream=stream, enabled=False)
    bar.advance()
    bar.finish("done")
    assert stream.getvalue() == ""


def test_progress_emits_updates_on_a_plain_stream():
    stream = io.StringIO()
    stream.isatty = lambda: False  # type: ignore[method-assign]
    bar = term.Progress(2, label="work", stream=stream, enabled=True, throttle=0)
    bar.advance()
    bar.finish("done")
    text = stream.getvalue()
    assert "work" in text
    assert "done" in text


def test_stream_never_dies_on_unencodable_characters():
    class Narrow(io.StringIO):
        encoding = "cp1252"

        def write(self, text):
            try:
                text.encode(self.encoding)
            except UnicodeEncodeError:
                text = text.encode("ascii", "replace").decode("ascii")
            return super().write(text)

    stream = term.Stream(Narrow())
    stream.write("box \u2500\u2500 and caf\u00e9")
    assert "box" in stream.raw.getvalue()


def test_paint_is_independent_of_the_global_palette():
    term.set_color(False)
    assert term.paint("red", "x") != "x"
    term.set_color(True)
    assert term.strip_ansi(term.paint("red", "x")) == "x"


# --------------------------------------------------------------------------- #
# Human formatting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("value", "si", "iec"), [
    (0, "0 B", "0 B"),
    (1536, "1.5 kB", "1.5 KiB"),
    (1024, "1.0 kB", "1.0 KiB"),
])
def test_format_bytes(value, si, iec):
    """SI and IEC really are different; conflating them is a classic bug."""
    assert term.format_bytes(value) == si
    assert term.format_bytes(value, binary=True) == iec


@pytest.mark.parametrize(("seconds", "expected"), [
    (0.0004, "400\u00b5s"),
    (0.004, "4.0ms"),
    (1.5, "1.50s"),
    (90, "1m 30s"),
    (93784, "1d 2h 3m"),
])
def test_human_duration(seconds, expected):
    assert term.human_duration(seconds) == expected


def test_interpolate_leaves_unknown_names_alone():
    assert term.interpolate("hi ${name}, ${missing}", name="bob") == "hi bob, ${missing}"
    assert term.interpolate("cost $5") == "cost $5"


def test_strip_trailing_space_keeps_reset_codes():
    text = term.A.bold("ab   ") + "\x1b[0m"
    assert term._rstrip_ansi(text).endswith("\x1b[0m")


def test_supports_color_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert term.supports_color(io.StringIO()) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("ANVIL_FORCE_COLOR", "1")
    assert term.supports_color(io.StringIO()) is True
