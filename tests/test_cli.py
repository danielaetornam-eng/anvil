"""End-to-end tests: the real parser tree, invoked in-process.

These exercise the whole stack -- config discovery, parsing, dispatch, output
formatting, and the exit-code contract -- without spawning a subprocess.
"""

from __future__ import annotations

import json

import pytest

from anvil.core.errors import ExitCode
from conftest import run, subparser

# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


def test_every_registered_command_has_a_parser():
    from anvil.commands import COMMANDS, available

    root = __import__("anvil.core.app", fromlist=["build_parser"]).build_parser()
    action = next(a for a in root._actions
                  if isinstance(getattr(a, "choices", None), dict))
    for name in available():
        assert name in COMMANDS
        assert name in action.choices, f"{name} has no parser"


def test_every_command_documents_itself():
    from anvil.commands import available

    for name in available():
        parser = subparser(name)
        assert parser.description, f"{name} has no description"
        assert parser.examples, f"{name} has no examples"
        for action in parser._actions:
            if action.help is None and action.option_strings:
                assert action.dest == "help", f"{name}: {action.dest} undocumented"


def test_root_help_lists_every_command(workdir):
    from anvil.commands import available

    result = run("--help")
    assert result.code == 0
    for name in available():
        assert name in result.out


def test_version_exits_zero(workdir):
    result = run("--version")
    assert result.code == 0
    assert "anvil" in result.out


# --------------------------------------------------------------------------- #
# The exit-code contract
# --------------------------------------------------------------------------- #


def test_no_command_is_a_usage_error(workdir):
    result = run()
    assert result.code == ExitCode.USAGE
    assert "error" in result.err


def test_unknown_command_suggests_a_near_miss(workdir):
    result = run("jsonn")
    assert result.code == ExitCode.USAGE
    assert "did you mean" in result.err
    assert "json" in result.err


def test_unknown_flag_is_a_usage_error(workdir):
    result = run("secrets", "--nosuchflag")
    assert result.code == ExitCode.USAGE
    assert "Traceback" not in result.err


def test_bad_data_uses_the_data_code(workdir):
    result = run("json", "format", "bad.json")
    assert result.code == ExitCode.DATA
    assert "invalid JSON" in result.err


def test_missing_input_uses_the_no_input_code(workdir):
    result = run("hash", "sha256", "nosuchfile")
    assert result.code == ExitCode.NO_INPUT
    assert "does not exist" in result.err


def test_a_failing_subcommand_propagates_its_code(workdir):
    result = run("run", "--silent", "exit 3")
    assert result.code == 3


# --------------------------------------------------------------------------- #
# Global flags work anywhere
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("argv", [
    ["--dry-run", "run", "echo hi"],
    ["run", "--dry-run", "echo hi"],
])
def test_dry_run_works_before_or_after_the_command(workdir, argv):
    result = run(*argv)
    assert result.code == 0
    assert "would run" in result.out + result.err


def test_output_flag_works_after_the_command(workdir):
    result = run("json", "format", "users.json", "--output", "json")
    assert result.code == 0
    assert json.loads(result.out)


def test_output_flag_works_before_the_command(workdir):
    result = run("--output", "json", "json", "format", "users.json")
    assert result.code == 0
    assert json.loads(result.out)


def test_no_color_strips_escapes(workdir):
    result = run("--no-color", "table", "users.json")
    assert "\x1b[" not in result.out


def test_set_overrides_a_config_value(workdir):
    result = run("--set", "http.timeout=1s", "config", "get", "http.timeout")
    assert result.code == 0
    assert "1s" in result.out


# --------------------------------------------------------------------------- #
# json
# --------------------------------------------------------------------------- #


def test_json_format_reindents(workdir):
    result = run("json", "format", "users.json")
    assert result.code == 0
    assert "\n  " in result.out


def test_json_validate_reports_ok(workdir):
    result = run("json", "validate", "users.json", "--output", "json")
    assert result.json()["valid"] is True


def test_json_query_selects_fields(workdir):
    result = run("json", "query", ".name", "users.json")
    assert json.loads(result.out) == ["ada", "grace", "alan", "edsger"]


def test_json_query_supports_stages(workdir):
    result = run("json", "query", ".[] | select(.age > 45) | map(.name)",
                 "users.json", "--compact")
    assert json.loads(result.out) == ["edsger"]


def test_json_query_indexes_lists(workdir):
    result = run("json", "query", ".0.name", "users.json", "--compact")
    assert json.loads(result.out) == "ada"


def test_json_query_rejects_an_unknown_stage(workdir):
    result = run("json", "query", ".[] | nonsense", "users.json")
    assert result.code == ExitCode.DATA
    assert "unknown query stage" in result.err


def test_json_query_rejects_a_bad_filter(workdir):
    result = run("json", "query", ".[] | select(age ~~ 3)", "users.json")
    assert result.code == ExitCode.DATA


def test_json_merge_deep_merges(workdir):
    result = run("json", "merge", "base.json", "over.json", "--output", "json")
    merged = json.loads(result.out)
    assert merged["nested"] == {"x": 1, "y": 20, "z": 30}
    assert merged["list"] == [9]


def test_json_diff_reports_changes_and_exit_code(workdir):
    result = run("json", "diff", "base.json", "over.json", "--output", "json")
    assert result.code == 1
    paths = {row["path"] for row in json.loads(result.out)}
    assert "nested.y" in paths


def test_json_diff_identical_is_success(workdir):
    result = run("json", "diff", "base.json", "base.json", "--output", "json")
    assert result.code == 0


# --------------------------------------------------------------------------- #
# table
# --------------------------------------------------------------------------- #


def test_table_renders_a_table_even_when_piped(workdir):
    """``auto`` must not collapse a table into whitespace when stdout is a pipe."""
    result = run("table", "users.json")
    assert "name" in result.out and "ada" in result.out


def test_table_honours_columns_and_excludes(workdir):
    result = run("table", "users.json", "--columns", "name,age")
    assert "role" not in result.out
    result = run("table", "users.json", "--exclude", "role,city")
    assert "role" not in result.out and "name" in result.out


def test_table_adds_a_constant_column(workdir):
    result = run("table", "users.json", "-D", "region=eu", "--columns",
                 "name,region")
    assert "eu" in result.out


def test_table_rejects_an_unknown_column(workdir):
    result = run("table", "users.json", "--columns", "nope")
    assert result.code == ExitCode.DATA
    assert "no such column" in result.err


def test_table_csv_output(workdir):
    result = run("table", "users.json", "--output", "csv", "--columns", "name")
    assert result.out.splitlines()[0] == "name"


def test_table_detects_csv_without_being_told(workdir):
    result = run("table", "rows.csv")
    assert "ada" in result.out


# --------------------------------------------------------------------------- #
# text, encode, template, secrets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("args", "expected"), [
    (["--case", "upper"], "HELLO WORLD"),
    (["--case", "snake"], "hello_world"),
    (["--case", "kebab"], "hello-world"),
])
def test_text_case_transforms(workdir, args, expected):
    assert run("text", *args, stdin="Hello World\n").out.strip() == expected


def test_text_numbering_and_filtering(workdir):
    assert run("text", "--number", stdin="a\nb\n").out.split() == ["1", "a", "2", "b"]
    assert run("text", "--match", "^#", stdin="# keep\ndrop\n").out.strip() == "# keep"


def test_text_head_and_tail_conflict(workdir):
    result = run("text", "--head", "1", "--tail", "1")
    assert result.code == ExitCode.USAGE


def test_encode_round_trips_base64(workdir):
    encoded = run("encode", "base64", "--encode-str", "hello").out.strip()
    assert encoded == "aGVsbG8="
    decoded = run("encode", "base64", "-d", "base64", "--encode-str",
                  encoded).out.strip()
    assert decoded == "hello"


def test_encode_rejects_bad_input(workdir):
    result = run("encode", "base64", "-d", "base64", "--encode-str", "not!b64")
    assert result.code == ExitCode.DATA
    assert "cannot decode" in result.err


def test_template_substitutes_and_can_be_strict(workdir):
    assert run("template", "--var", "n=ada", "Hi ${n}").out.strip() == "Hi ada"
    strict = run("template", "--strict", "Hi ${n}")
    assert strict.code == ExitCode.DATA
    assert "unresolved variable" in strict.err
    assert run("template", "--null", "Hi ${n}").out.strip() == "Hi"


def test_template_reads_variables_from_files(workdir):
    result = run("template", "-f", "template.tpl", "--values", "values.env")
    assert "Hello ada, welcome to london!" in result.out
    result = run("template", "-f", "template.tpl", "--json-values", "vars.json")
    assert "Hello grace, welcome to new york!" in result.out


def test_secrets_respects_length_and_alphabet(workdir):
    result = run("secrets", "--length", "24", "--charset", "hex")
    assert len(result.out.strip()) == 24
    assert all(c in "0123456789abcdef" for c in result.out.strip())


def test_secrets_uuid_and_ulid_shapes(workdir):
    uuid = run("secrets", "uuid", "--count", "3").out.split()
    assert len(uuid) == 3 and all(len(u) == 36 for u in uuid)
    assert len(run("secrets", "ulid").out.strip()) == 26
    assert run("secrets", "uuid7").out.count("-") == 4


def test_secrets_alphabet_flags_compose():
    """Case flags narrow the alphabet rather than conflicting silently."""
    digits = run("secrets", "--charset", "alnum", "--digits", "--length", "8")
    assert digits.code == 0
    assert all(c.isdigit() for c in digits.out.strip())


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_config_reads_the_discovered_file(workdir):
    result = run("config", "get", "http.timeout")
    assert result.code == 0 and "45s" in result.out


def test_config_explains_provenance(workdir):
    result = run("config", "list", "--explain", "--output", "json")
    assert any("file" in str(row.values()) for row in json.loads(result.out))


def test_config_redacts_credentials(workdir):
    result = run("config", "list", "--output", "json")
    assert "super-secret-value" not in result.out


def test_config_show_secrets_opts_in(workdir):
    result = run("config", "list", "--show-secrets", "--output", "json")
    assert "super-secret-value" in result.out


def test_config_set_dry_run_writes_nothing(workdir):
    before = (workdir / "anvil.toml").read_text(encoding="utf-8")
    result = run("config", "set", "new.key", "1", "--dry-run")
    assert result.code == 0 and "would set" in result.err
    assert (workdir / "anvil.toml").read_text(encoding="utf-8") == before


def test_config_set_then_get(workdir):
    assert run("config", "set", "new.key", "value", "--yes").code == 0
    assert "value" in run("config", "get", "new.key").out


def test_config_get_missing_uses_the_no_input_code(workdir):
    result = run("config", "get", "nope.missing")
    assert result.code == ExitCode.NO_INPUT


def test_config_bad_file_is_a_config_error(workdir):
    (workdir / "broken.toml").write_text("= = =", encoding="utf-8")
    result = run("--config", "broken.toml", "config", "list")
    assert result.code == ExitCode.CONFIG


def test_config_init_creates_a_documented_file(workdir, tmp_path):
    result = run("config", "init", "fresh.toml")
    assert result.code == 0
    text = (workdir / "fresh.toml").read_text(encoding="utf-8")
    assert "[http]" in text and "# anvil configuration" in text


# --------------------------------------------------------------------------- #
# run / bench / retry
# --------------------------------------------------------------------------- #


def test_run_executes_and_reports(workdir):
    result = run("run", "--capture", "echo hello")
    assert result.code == 0 and "hello" in result.out


def test_run_continue_on_error_returns_the_failure_count(workdir):
    result = run("run", "--silent", "--continue-on-error", "exit 1", "exit 1")
    assert result.code == 2
    assert "2 failed" in result.err


def test_run_fail_fast_stops_early(workdir):
    result = run("run", "--silent", "--fail-fast", "exit 1", "echo never")
    assert result.code == 1
    assert "never" not in result.out


def test_run_rejects_jobs_without_parallel(workdir):
    result = run("run", "--silent", "--jobs", "4", "echo x")
    assert result.code == ExitCode.USAGE
    assert "--jobs" in result.err


def test_run_rejects_contradictory_flags(workdir):
    result = run("run", "--fail-fast", "--continue-on-error", "echo x")
    assert result.code == ExitCode.USAGE


def test_run_treats_dash_dash_as_one_command(workdir):
    result = run("run", "--capture", "--", "echo", "passthrough")
    assert result.code == 0 and "passthrough" in result.out


def test_bench_reports_percentiles(workdir):
    result = run("bench", "--silent", "--runs", "3", "--", "python", "-c", "pass")
    report = json.loads(result.out)
    assert report["runs"] == 3
    assert {"min", "median", "mean", "p90"} <= set(report)


def test_bench_needs_a_separator_for_flags(workdir):
    result = run("bench", "python -c pass")
    assert result.code == ExitCode.USAGE
    assert "--" in result.err


def test_retry_gives_up_with_the_last_code(workdir):
    result = run("retry", "--silent", "--attempts", "2", "--",
                 "python", "-c", "raise SystemExit(4)")
    assert result.code == 4
    assert "still failing" in result.err


def test_retry_succeeds_immediately(workdir):
    result = run("retry", "--silent", "--attempts", "2", "--",
                 "python", "-c", "pass")
    assert result.code == 0 and "succeeded" in result.err


def test_retry_only_retries_listed_codes(workdir):
    result = run("retry", "--silent", "--attempts", "3", "--retry-on", "1", "--",
                 "python", "-c", "raise SystemExit(7)")
    assert result.code == 7
    assert "--retry-on" in result.err


# --------------------------------------------------------------------------- #
# diff, hash
# --------------------------------------------------------------------------- #


def test_diff_reports_changes_with_exit_one(workdir):
    result = run("diff", "a.txt", "b.txt")
    assert result.code == 1
    assert "-three" in result.out and "+THREE" in result.out


def test_diff_identical_files_succeed(workdir):
    assert run("diff", "a.txt", "a.txt").code == 0


def test_diff_ignore_case(workdir):
    assert run("diff", "--ignore-case", "a.txt", "b.txt", "--unified", "0").code == 0


def test_diff_needs_exactly_two_paths(workdir):
    result = run("diff", "a.txt")
    assert result.code == ExitCode.USAGE


def test_hash_matches_the_known_digest(workdir):
    result = run("hash", "sha256", "plain.txt")
    assert result.out.strip() == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9")


def test_hash_accepts_multiple_algorithms(workdir):
    result = run("hash", "-a", "sha256", "-a", "md5", "plain.txt",
                 "--format", "json")
    rows = json.loads(result.out)
    assert set(rows[0]) >= {"file", "sha256", "md5"}
    assert rows[0]["md5"] != rows[0]["sha256"]


def test_hash_reads_stdin(workdir):
    result = run("hash", "sha256", "--stdin", stdin="piped")
    assert result.out.strip().startswith("445ceee")


def test_hash_verifies_a_digest(workdir):
    good = run("hash", "sha256", "plain.txt").out.strip()
    assert run("hash", "--verify", f"sha256:{good}", "plain.txt",
               "--silent").code == 0
    bad = run("hash", "--verify", "sha256:" + "0" * 64, "plain.txt").code
    assert bad == 1


# --------------------------------------------------------------------------- #
# docs, completions, doctor, explain
# --------------------------------------------------------------------------- #


def test_docs_emits_valid_json_describing_the_tree(workdir):
    result = run("docs", "--format", "json")
    payload = json.loads(result.out)
    assert payload["prog"] == "anvil"
    assert "run" in payload["subcommands"]
    run_payload = payload["subcommands"]["run"]
    assert any("--jobs" in o["flags"] for o in run_payload["options"])


def test_docs_can_target_one_command(workdir):
    result = run("docs", "--format", "json", "--only", "json", "--depth", "0")
    assert json.loads(result.out)["prog"].endswith("json")


def test_docs_rejects_an_unknown_command(workdir):
    result = run("docs", "--only", "nosuch")
    assert result.code == ExitCode.NO_INPUT
    assert "no such command" in result.err


def test_docs_markdown_is_markdown(workdir):
    result = run("docs", "--format", "markdown", "--only", "hash")
    assert result.out.startswith("#")


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_scripts_are_generated_and_well_formed(workdir, shell):
    from anvil.core.completion import render

    script = run("completions", "show", shell).out
    assert script
    assert render(shell, "anvil") == script
    # The runtime half must be valid Python.
    head = script.split("\n_anvil_complete")[0]
    compile(head.split("if __name__")[0].replace("import os\nimport sys", "import os\nimport sys"),
            "<completion>", "exec")
    assert "%(" not in script, "unsubstituted template placeholder leaked out"


def test_completions_rejects_an_unknown_shell(workdir):
    assert run("completions", "show", "nosuchshell").code == ExitCode.USAGE


def test_completion_paths_are_absolute(workdir):
    for shell in ("bash", "zsh", "fish"):
        path = run("completions", "path", shell).out.strip()
        assert path and "anvil" in path


def test_doctor_reports_python_version(workdir):
    result = run("doctor", "--only", "python", "--output", "json")
    findings = json.loads(result.out)
    assert any(f["section"] == "python" and f["status"] == "ok" for f in findings)


def test_doctor_rejects_an_unknown_section(workdir):
    result = run("doctor", "--only", "nosuchsection")
    assert result.code == ExitCode.DATA


def test_doctor_tag_uses_the_flag_name_as_the_key(workdir):
    result = run("doctor", "--only", "python", "--tag", "host=ci",
                 "--output", "json")
    assert all(f.get("host") == "ci" for f in json.loads(result.out))


def test_explain_lists_and_teaches(workdir):
    listing = run("explain", "--list")
    assert listing.code == 0
    assert "nargs" in listing.out and "formatter" in listing.out
    detail = run("explain", "nargs")
    assert detail.code == 0
    assert "REMAINDER" in detail.out + detail.err


def test_explain_suggests_a_near_miss(workdir):
    result = run("explain", "narg")
    assert result.code == ExitCode.NO_INPUT
    assert "did you mean" in result.err


def test_explain_where_points_at_a_real_command(workdir):
    result = run("explain", "nargs", "--where")
    text = result.out + result.err
    assert "anvil run" in text


# --------------------------------------------------------------------------- #
# env, prompt, mktemp
# --------------------------------------------------------------------------- #


def test_env_list_filters_and_redacts(workdir):
    result = run("env", "list", "--prefix", "PATH", "--values", "--output", "json")
    rows = json.loads(result.out)
    assert any(r["name"].startswith("PATH") for r in rows)


def test_env_required_gates_on_presence(workdir):
    assert run("env", "required", "PATH").code == 0
    missing = run("env", "required", "NOSUCHVAR_XYZ")
    assert missing.code == ExitCode.DATA
    assert "not set" in missing.err


def test_env_get_redacts_by_default(workdir):
    result = run("env", "get", "PATH")
    assert result.code == 0


def test_env_rejects_an_invalid_variable_name(workdir):
    assert run("env", "get", "1BAD").code == ExitCode.USAGE


def test_prompt_non_interactive_requires_a_default(workdir):
    result = run("prompt", "Name?")
    assert result.code == ExitCode.DATA
    assert "--default" in result.err
    assert run("prompt", "Name?", "--non-interactive",
               "--default", "ada").out.strip() == "ada"


def test_prompt_reads_piped_input(workdir):
    assert run("prompt", "Name?", stdin="typed\n").out.strip() == "typed"


def test_prompt_validates_a_choice_default(workdir):
    result = run("prompt", "Pick", "--choice", "a,b", "--non-interactive",
                 "--default", "z")
    assert result.code == ExitCode.DATA
    assert "not one of" in result.err


def test_mktemp_creates_a_path_only_when_written(workdir):
    result = run("mktemp", "--print-path")
    assert result.code == 0
    import pathlib
    assert pathlib.Path(result.out.strip()).exists()


def test_mktemp_directory(workdir):
    import pathlib
    path = run("mktemp", "-d", "--print-path").out.strip()
    assert pathlib.Path(path).is_dir()


def test_mktemp_suffix(workdir):
    assert run("mktemp", "--suffix", ".json", "--print-path").out.strip().endswith(".json")


# --------------------------------------------------------------------------- #
# http: argument handling only (no network)
# --------------------------------------------------------------------------- #


def test_http_requires_a_url(workdir):
    result = run("http", "get")
    assert result.code == ExitCode.DATA
    assert "no URL" in result.err


@pytest.mark.parametrize("url", ["not-a-url", "ftp://example.com"])
def test_http_rejects_a_non_http_url(workdir, url):
    result = run("http", "get", url)
    assert result.code == ExitCode.DATA
    assert "not an http" in result.err


def test_http_rejects_an_unknown_method(workdir):
    result = run("http", "bogus", "https://x.example")
    assert result.code == ExitCode.DATA
    assert "unknown method" in result.err


def test_http_validates_write_out_at_parse_time(workdir):
    """A typo in the output template must not wait for the network."""
    result = run("http", "get", "https://x.example", "-w", "%{typo}")
    assert result.code == ExitCode.USAGE
    assert "unknown write-out" in result.err


def test_http_timeout_is_type_checked(workdir):
    result = run("http", "get", "https://x.example", "--timeout", "nope")
    assert result.code == ExitCode.USAGE
    assert "not a duration" in result.err


def test_http_parses_its_own_options_not_passthrough(workdir):
    """A REMAINDER positional after positionals would swallow our own flags."""
    from anvil.core.app import build_parser

    parser = build_parser()
    namespace = parser.parse_args(["http", "get", "https://x.example",
                                   "--retry", "3", "-H", "A: b"])
    assert namespace.retry == 3
    assert namespace.headers == {"A": "b"}


# --------------------------------------------------------------------------- #
# serve / watch: dry-run only
# --------------------------------------------------------------------------- #


def test_serve_dry_run_does_not_bind(workdir):
    result = run("serve", "--dry-run", ".")
    assert result.code == 0
    assert "serving" in result.out
    assert json.loads(result.out)["bound"] is False


def test_serve_reports_a_missing_root(workdir):
    result = run("serve", "nosuchdir")
    assert result.code == ExitCode.NO_INPUT


def test_serve_reports_a_file_root(workdir):
    result = run("serve", "plain.txt")
    assert result.code == ExitCode.DATA
    assert "not a directory" in result.err


def test_watch_dry_run(workdir):
    result = run("watch", "--dry-run", "--exec", "echo hi")
    assert result.code == 0 and "watching" in result.out


def test_watch_requires_exec(workdir):
    assert run("watch").code == ExitCode.USAGE
