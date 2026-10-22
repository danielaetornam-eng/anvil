"""Unit tests for the ``type=`` layer.

The recurring theme: a converter must raise ``ArgumentTypeError`` (never
``ValueError``), and its message must say how to fix the input.
"""

from __future__ import annotations

import argparse

import pytest

from anvil.core import types as T


@pytest.mark.parametrize(("converter", "good", "expected"), [
    (T.byte_size, "10MB", 10_000_000),
    (T.byte_size, "512KiB", 524_288),
    (T.byte_size, "1.5g", 1_500_000_000),
    (T.byte_size, "1024", 1024),
    (T.byte_size, "0", 0),
    (T.duration, "500ms", 0.5),
    (T.duration, "30s", 30.0),
    (T.duration, "5m", 300.0),
    (T.duration, "1h30m", 5400.0),
    (T.duration, "2d", 172_800.0),
    (T.duration, "1w", 604_800.0),
    (T.positive_int, "5", 5),
    (T.positive_int, "1_000", 1000),
    (T.non_negative_int, "0", 0),
    (T.number, "42", 42),
    (T.number, "3.14", 3.14),
    (T.number, "1e6", 1e6),
    (T.fraction, "0.5", 0.5),
    (T.fraction, "50%", 0.5),
    (T.percentage, "75%", 0.75),
    (T.port, "8080", 8080),
    (T.port, "http", 80),
    (T.port, "https", 443),
    (T.csv_list, "a,b,c", ["a", "b", "c"]),
    (T.csv_list, '"x,y",z', ["x,y", "z"]),
    (T.json_value, '{"a":1}', {"a": 1}),
    (T.json_object, '{"a":1}', {"a": 1}),
    (T.key_value, "a=b", ("a", "b")),
    (T.key_value, "a=", ("a", "")),
    (T.key_value, "-X-Api-Key=secret", ("-X-Api-Key", "secret")),
    (T.key_value, "a=b=c", ("a", "b=c")),
    (T.key_value_list, "a=1,b=2", [("a", "1"), ("b", "2")]),
    (T.key_value_list, "flag", [("flag", "")]),
    (T.chmod_mode, "755", 0o755),
    (T.chmod_mode, "0644", 0o644),
    (T.chmod_mode, "0o600", 0o600),
    (T.interval(), "1-3", [1, 2, 3]),
    (T.interval(), "5", [5]),
    (T.interval(), "1,3,5-7", [1, 3, 5, 6, 7]),
    (T.timestamp, "1700000000", 1700000000.0),
    (T.timestamp, "2023-11-14T22:13:20Z", 1700000000.0),
    (T.log_level, "DEBUG", 10),
    (T.log_level, "warn", 30),
    (T.output_format, "yml", "yaml"),
    (T.output_format, "md", "markdown"),
    (T.table_style, "box", "unicode"),
    (T.env_var_name, "FOO_BAR", "FOO_BAR"),
    (T.hostname, "example.com", "example.com"),
    (T.ip_address, "127.0.0.1", "127.0.0.1"),
    (T.url, "https://x.dev/a?b=1", "https://x.dev/a?b=1"),
    (T.email, "a@b.co", "a@b.co"),
    (T.uuid_value, "123e4567-e89b-12d3-a456-426614174000",
     "123e4567-e89b-12d3-a456-426614174000"),
    (T.semver, "1.2.3", (1, 2, 3, "")),
    (T.semver, "1.2.3-rc1", (1, 2, 3, "rc1")),
    (T.sha256_digest, "a" * 64, "a" * 64),
])
def test_converts(converter, good, expected):
    assert converter(good) == expected


@pytest.mark.parametrize(("converter", "bad"), [
    (T.byte_size, "10 furlongs"),
    (T.byte_size, "10 zz"),
    (T.duration, "5 fortnights"),
    (T.duration, "0s"),
    (T.positive_int, "0"),
    (T.positive_int, "abc"),
    (T.non_negative_int, "-1"),
    (T.fraction, "1.5"),
    (T.percentage, "75"),
    (T.port, "0"),
    (T.port, "99999"),
    (T.hostname, "bad_host!"),
    (T.ip_address, "999.1.1.1"),
    (T.url, "notaurl"),
    (T.url, "https://"),
    (T.email, "nope"),
    (T.json_object, "[1]"),
    (T.json_value, "{oops}"),
    (T.key_value, "novalue"),
    (T.regex_string, "[unclosed"),
    (T.sha256_digest, "abc"),
    (T.uuid_value, "nope"),
    (T.semver, "1.2"),
    (T.interval(), "5-1"),
    (T.interval(1, 10), "99"),
    (T.chmod_mode, "999"),
    (T.output_format, "xml"),
    (T.table_style, "neon"),
    (T.env_var_name, "1BAD"),
    (T.callable_ref, "notmodule"),
    (T.callable_ref, "json:no_such_attr"),
])
def test_rejects_with_argument_type_error(converter, bad):
    with pytest.raises(argparse.ArgumentTypeError):
        converter(bad)


@pytest.mark.parametrize(("converter", "bad"), [
    (T.byte_size, "10 furlongs"),
    (T.duration, "5 fortnights"),
    (T.port, "99999"),
    (T.output_format, "xml"),
])
def test_error_message_shows_valid_examples(converter, bad):
    with pytest.raises(argparse.ArgumentTypeError) as info:
        converter(bad)
    assert "try" in str(info.value) or "choose from" in str(info.value)


def test_error_surfaces_as_usage_error_not_traceback(capsys):
    """A ``ValueError`` from ``type=`` would leak a traceback; this must not."""
    parser = argparse.ArgumentParser(prog="t", add_help=False)
    parser.add_argument("--v", type=T.port)
    with pytest.raises(SystemExit) as info:
        parser.parse_args(["--v=99999"])
    assert info.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_param_type_exposes_name_for_metavar():
    assert T.byte_size.__name__ == "SIZE"
    assert T.duration.__name__ == "DURATION"
    assert T.key_value.__name__ == "KEY=VALUE"


def test_range_int_metavar_shows_bounds():
    bounded = T.range_int(1, 10)
    assert bounded.usage() == "1..10"
    assert bounded("5") == 5
    with pytest.raises(argparse.ArgumentTypeError):
        bounded("11")


def test_normalized_choice_accepts_aliases_and_any_case():
    choice = T.NormalizedChoice(["speed", "size"],
                                aliases={"fast": "speed", "small": "size"})
    assert choice("fast") == "speed"
    assert choice("FAST") == "speed"
    assert choice("size") == "size"
    assert choice.canonical == ["speed", "size"]
    with pytest.raises(argparse.ArgumentTypeError) as info:
        choice("speeed")
    assert "did you mean" in str(info.value)


def test_choice_container_supports_in_and_str():
    choice = T.Choice(["a", "b"], aliases={"alpha": "a"})
    assert "a" in choice and "A" in choice and "alpha" in choice
    assert "z" not in choice
    assert str(choice) == "{a,b}"


def test_csv_table_parses_multiple_rows():
    assert T.csv_table("a,b\n1,2") == [["a", "b"], ["1", "2"]]


def test_regex_returns_compiled_pattern():
    pattern = T.regex_string(r"\d+")
    assert pattern.match("42")


def test_optional_wrapper_passes_empty_through():
    wrapped = T.optional(T.port, empty=0)
    assert wrapped("") == 0
    assert wrapped("-") == 0
    assert wrapped("80") == 80
