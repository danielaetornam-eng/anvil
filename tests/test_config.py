"""Tests for configuration layering and the dotted-path helpers."""

from __future__ import annotations

import pytest

from anvil.core import config as cfg
from anvil.core.errors import ConfigError, UsageError


def test_flatten_and_unflatten_round_trip():
    data = {"a": {"b": {"c": 1}}, "d": 2}
    flat = cfg.flatten(data)
    assert flat == {"a.b.c": 1, "d": 2}
    assert cfg.unflatten(flat) == data


def test_dot_get_walks_and_defaults():
    data = {"a": {"b": 1}}
    assert cfg.dot_get(data, "a.b") == 1
    assert cfg.dot_get(data, "a.missing") is None
    assert cfg.dot_get(data, "missing.deep", "fallback") == "fallback"


def test_dot_set_creates_intermediate_mappings():
    data = {}
    cfg.dot_set(data, "a.b.c", 1)
    assert data == {"a": {"b": {"c": 1}}}


def test_deep_merge_is_recursive_and_non_destructive():
    base = {"a": {"x": 1, "y": 2}, "keep": True}
    overlay = {"a": {"y": 20, "z": 30}}
    merged = cfg.deep_merge(base, overlay)
    assert merged == {"a": {"x": 1, "y": 20, "z": 30}, "keep": True}
    assert base["a"]["y"] == 2


def test_deep_merge_replaces_lists_rather_than_concatenating():
    """Concatenation would make it impossible to clear a list."""
    assert cfg.deep_merge({"l": [1, 2]}, {"l": [9]}) == {"l": [9]}


def test_load_toml(tmp_path):
    path = tmp_path / "a.toml"
    path.write_text('[http]\ntimeout = "30s"\nretries = 3\n', encoding="utf-8")
    assert cfg.load(path) == {"http": {"timeout": "30s", "retries": 3}}


def test_load_json(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"a": {"b": 1}}', encoding="utf-8")
    assert cfg.load(path) == {"a": {"b": 1}}


def test_load_yaml_subset(tmp_path):
    path = tmp_path / "a.yaml"
    path.write_text("http:\n  timeout: 30s\n  retries: 3\n"
                    "hosts:\n  - a\n  - b\n", encoding="utf-8")
    assert cfg.load(path) == {"http": {"timeout": "30s", "retries": 3},
                              "hosts": ["a", "b"]}


def test_bad_toml_is_a_config_error(tmp_path):
    path = tmp_path / "a.toml"
    path.write_text("this is = not = toml", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        cfg.load(path)
    assert "invalid TOML" in info.value.message


def test_unsupported_extension_is_reported(tmp_path):
    path = tmp_path / "a.ini"
    path.write_text("x=1", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        cfg.load(path)
    assert "unsupported config format" in info.value.message


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        cfg.discover(explicit=tmp_path / "nope.toml")


def test_env_config_pointing_nowhere_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as info:
        cfg.discover(environ={"ANVIL_CONFIG": str(tmp_path / "nope.toml")})
    assert "does not exist" in info.value.message


def test_discover_prefers_the_cwd(tmp_path, monkeypatch):
    (tmp_path / "anvil.toml").write_text("[a]\nb = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    found = cfg.discover()
    assert found is not None and found.path.name == "anvil.toml"
    assert found.data == {"a": {"b": 1}}


def test_env_overrides_expand_section_separator():
    env = {"ANVIL_HTTP__TIMEOUT": "5s", "ANVIL_TABLE_STYLE": "ascii",
           "UNRELATED": "x"}
    assert cfg.env_overrides(env) == {"http": {"timeout": "5s"},
                                     "table": {"style": "ascii"}}


def test_parse_set_builds_nested_data():
    assert cfg.parse_set(["http.timeout=5s", "n=3", "flag=true"]) == {
        "http": {"timeout": "5s"}, "n": 3, "flag": True,
    }


def test_parse_set_rejects_a_bare_word():
    with pytest.raises(UsageError) as info:
        cfg.parse_set(["novalue"])
    assert "KEY=VALUE" in info.value.message


def test_coerce_is_conservative():
    assert cfg.coerce("5") == 5
    assert cfg.coerce("5.5") == 5.5
    assert cfg.coerce("true") is True
    assert cfg.coerce('"quoted"') == "quoted"
    assert cfg.coerce("007") == "007"
    assert cfg.coerce("1.2.3") == "1.2.3"
    assert cfg.coerce("2024-01-01") == "2024-01-01"


def test_layer_precedence_is_defaults_file_env_flags():
    stack = cfg.Config(
        defaults={"a": 1, "b": 1, "c": 1, "d": 1},
        file_data={"b": 2, "c": 2, "d": 2},
        env_data={"c": 3, "d": 3},
        flag_data={"d": 4},
    )
    assert stack.resolve() == {"a": 1, "b": 2, "c": 3, "d": 4}
    assert stack.source("a") == "defaults"
    assert stack.source("b") == "file"
    assert stack.source("c") == "env"
    assert stack.source("d") == "flags"


def test_explain_reports_provenance():
    stack = cfg.Config(defaults={"a": 1}, flag_data={"b": 2})
    rows = {k: (v, s) for k, v, s in stack.explain()}
    assert rows["a"] == (1, "defaults")
    assert rows["b"] == (2, "flags")


def test_redact_hides_credential_shaped_keys():
    data = {"api_token": "abc", "password": "hunter2", "normal": "visible",
            "nested": {"secret_key": "xyz"}}
    safe = cfg.redact(data)
    assert safe["api_token"] == "<redacted>"
    assert safe["password"] == "<redacted>"
    assert safe["normal"] == "visible"
    assert safe["nested"]["secret_key"] == "<redacted>"


def test_looks_secret_matches_the_obvious_names():
    assert cfg.looks_secret("api_key")
    assert cfg.looks_secret("DB-PASSWORD")
    assert not cfg.looks_secret("timeout")


def test_load_builds_the_full_stack(tmp_path, monkeypatch):
    (tmp_path / "anvil.toml").write_text('[http]\ntimeout = "10s"\n',
                                         encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANVIL_HTTP__RETRIES", "9")
    stack = cfg.Config.load(overrides=["http.timeout=99s"])
    assert stack.get("http.timeout") == "99s"
    assert stack.get("http.retries") == "9"
    assert stack.file_path is not None
