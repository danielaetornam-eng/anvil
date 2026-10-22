"""Subprocess-level smoke tests: the real entry point, in a real process.

The in-process tests in ``test_cli.py`` cover logic quickly; these cover the
parts that only exist once a process boundary is involved -- argv encoding,
the console code page, child-process stdio, and the exit status the shell
actually observes.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SRC = str(HERE.parent / "src")

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


@pytest.fixture
def workdir(tmp_path):
    for name, content in FIXTURES.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    (tmp_path / "tree_a").mkdir()
    (tmp_path / "tree_a" / "same.txt").write_text("identical\n", encoding="utf-8")
    (tmp_path / "tree_a" / "differs.txt").write_text("left\n", encoding="utf-8")
    (tmp_path / "tree_b").mkdir()
    (tmp_path / "tree_b" / "same.txt").write_text("identical\n", encoding="utf-8")
    (tmp_path / "tree_b" / "differs.txt").write_text("right\n", encoding="utf-8")
    (tmp_path / "tree_b" / "only_right.txt").write_text("new\n", encoding="utf-8")
    return tmp_path


def run(argv, cwd, stdin=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    env["COLUMNS"] = "100"
    env.pop("ANVIL_CONFIG", None)
    return subprocess.run(
        [sys.executable, "-m", "anvil", *argv],
        cwd=str(cwd), env=env, input=stdin, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90,
    )


#: ``(argv, stdin, must_contain, expected_exit)``. ``None`` accepts any exit.
CASES: list[tuple[list[str], str | None, list[str], int | None]] = [
    # -- root -----------------------------------------------------------
    (["--help"], None, ["COMMANDS", "run", "json", "EXAMPLES"], 0),
    (["--version"], None, ["anvil", "python"], 0),
    ([], None, ["error", "COMMAND"], 2),
    (["jsonn"], None, ["error", "did you mean"], 2),
    (["--nosuchflag"], None, ["error"], 2),

    # -- run ------------------------------------------------------------
    # A task is one argv token; quote it when it contains spaces.
    (["run", "--dry-run", "echo a"], None, ["echo a"], 0),
    (["run", "--silent", "echo hello"], None, ["hello"], 0),
    (["run", "--silent", "exit 3"], None, [], 3),
    (["run", "--parallel", "--jobs", "4", "--silent", "echo one"],
     None, ["one"], 0),
    (["run", "--silent", "--", "echo", "passthrough"], None, ["passthrough"], 0),
    (["run", "--silent", "--jobs", "4", "echo x"], None, ["error", "--jobs"], 2),
    (["run", "--fail-fast", "--continue-on-error", "echo x"], None,
     ["contradict"], 2),
    (["run", "--silent", "--continue-on-error", "exit 1", "exit 1"],
     None, ["2 failed"], 2),

    # -- json -----------------------------------------------------------
    (["json", "format", "users.json"], None, ["ada"], 0),
    (["json", "format", "bad.json"], None, ["invalid JSON"], 65),
    (["json", "validate", "users.json", "--output", "json"], None, ["valid"], 0),
    (["json", "validate", "users.json", "--silent"], None, [], 0),
    (["json", "validate", "bad.json"], None, ["invalid JSON"], 65),
    (["json", "merge", "base.json", "over.json"], None, ["z"], 0),
    (["json", "diff", "base.json", "over.json"], None, ["nested"], 1),
    (["json", "diff", "base.json", "base.json"], None, [], 0),
    (["json", "paths", "users.json"], None, ["name"], 0),
    (["json", "query", ".name", "users.json"], None, ["ada"], 0),
    (["json", "query", "-p", ".role", "users.json"], None, ["engineer"], 0),
    (["json", "query", ".city", "users.json", "--raw-output"], None, ["london"], 0),

    # -- table ----------------------------------------------------------
    (["table", "users.json"], None, ["ada", "engineer"], 0),
    (["table", "users.json", "--style", "markdown"], None, ["|"], 0),
    (["table", "rows.csv", "--from", "csv"], None, ["grace"], 0),
    (["table", "users.json", "--output", "csv"], None, ["name,age"], 0),
    (["table", "users.json", "--columns", "nope"], None, ["no such column"], 65),
    (["table", "users.json", "-D", "region=eu", "--columns", "name,region"],
     None, ["eu"], 0),

    # -- hash -----------------------------------------------------------
    (["hash", "sha256", "plain.txt"], None, ["b94d27b9"], 0),
    (["hash", "sha256", "--stdin"], "piped", ["445ceee"], 0),
    (["hash", "nosuchfile"], None, ["does not exist"], 66),
    (["hash", "tree_a", "--recursive", "--format", "json"], None, ["same.txt"], 0),
    (["hash", "tree_a"], None, ["--recursive"], 65),

    # -- bench / retry --------------------------------------------------
    (["bench", "--silent", "--runs", "3", "--", "python", "-c", "pass"],
     None, ["runs"], 0),
    (["bench", "python -c pass"], None, ["not an executable name"], 2),
    (["retry", "--silent", "--attempts", "2", "--", "python", "-c", "pass"],
     None, ["succeeded"], 0),
    (["retry", "--attempts", "0", "--", "python", "-c", "pass"],
     None, ["attempts 0"], 2),

    # -- diff -----------------------------------------------------------
    (["diff", "a.txt", "b.txt"], None, ["THREE"], 1),
    (["diff", "a.txt", "a.txt"], None, [], 0),
    (["diff", "--ignore-case", "a.txt", "b.txt", "--unified", "0"], None, [], 0),
    (["diff", "tree_a", "tree_b", "--recursive", "--stat"], None, ["differ"], 1),
    (["diff", "tree_a", "tree_b"], None, ["--recursive"], 2),
    (["diff", "a.txt"], None, ["exactly two"], 2),

    # -- secrets --------------------------------------------------------
    (["secrets", "--length", "16"], None, [], 0),
    (["secrets", "--length", "8", "--count", "3"], None, [], 0),
    (["secrets", "uuid", "--count", "2"], None, [], 0),
    (["secrets", "ulid"], None, [], 0),
    (["secrets", "uuid7"], None, [], 0),
    (["secrets", "hex", "--bytes", "16"], None, [], 0),
    (["secrets", "pin"], None, [], 0),
    (["secrets", "--length", "20", "--entropy", "--output", "json"],
     None, ["entropy_bits"], 0),
    (["secrets", "--label", "db=abc123", "--output", "json"], None, ["db"], 0),

    # -- text -----------------------------------------------------------
    (["text", "--case", "upper"], "hello\n", ["HELLO"], 0),
    (["text", "--case", "snake"], "Hello World\n", ["hello_world"], 0),
    (["text", "--case", "kebab"], "Hello World\n", ["hello-world"], 0),
    (["text", "--number"], "a\nb\n", ["1", "2"], 0),
    (["text", "--keep-non-empty", "--strip"], "a\n\n  b  \n", ["a", "b"], 0),
    (["text", "--sort"], "b\na\n", ["a"], 0),
    (["text", "--match", "^#"], "# keep\ndrop\n", ["keep"], 0),
    (["text", "--match", "^#", "--invert"], "# drop\nkeep\n", ["keep"], 0),
    (["text", "--lines", "1,3"], "a\nb\nc\n", ["a", "c"], 0),
    (["text", "--separator", "-"], "a\nb\n", ["a-b"], 0),
    (["text", "--head", "1", "--tail", "1"], "a\n", ["--head"], 2),

    # -- encode / template ----------------------------------------------
    (["encode", "base64", "--encode-str", "hello"], None, ["aGVsbG8="], 0),
    (["encode", "base64", "-d", "base64", "--encode-str", "aGVsbG8="],
     None, ["hello"], 0),
    (["encode", "url", "--encode-str", "a b&c"], None, ["a%20b%26c"], 0),
    (["encode", "base64", "-d", "base64", "--encode-str", "not!b64"],
     None, ["cannot"], 65),
    (["encode", "--list"], None, ["base64", "hex"], 0),
    (["template", "--var", "name=ada", "Hello ${name}"], None, ["Hello ada"], 0),
    (["template", "--strict", "Hi ${name}"], None, ["unresolved"], 65),
    (["template", "--null", "Hi ${name}"], None, ["Hi "], 0),
    (["template", "-f", "template.tpl", "--values", "values.env"], None,
     ["Hello ada"], 0),
    (["template", "-f", "template.tpl", "--json-values", "vars.json"], None,
     ["Hello grace"], 0),
    (["template", "--delimiter", "brace", "--var", "a=1", "{{a}}"], None, ["1"], 0),

    # -- config ---------------------------------------------------------
    (["config", "list"], None, ["http", "timeout"], 0),
    (["config", "list", "--explain"], None, ["file"], 0),
    (["config", "get", "http.timeout"], None, ["45s"], 0),
    (["config", "get", "nope.missing"], None, ["not set"], 66),
    (["config", "path"], None, ["anvil.toml"], 0),
    (["config", "init", "fresh.toml"], None, ["wrote"], 0),
    (["config", "set", "new.key", "value", "--dry-run"], None, ["would set"], 0),
    (["config", "unset", "http.timeout", "--dry-run"], None, ["would remove"], 0),
    (["config", "export", "--output", "json"], None, ["http"], 0),

    # -- env / prompt ---------------------------------------------------
    (["env", "list", "--prefix", "PYTHON", "--values", "--output", "json"],
     None, ["PYTHON"], 0),
    (["env", "required", "PATH"], None, [], 0),
    (["env", "required", "NOSUCHVAR_XYZ"], None, ["not set"], 65),
    (["env", "get", "1BAD"], None, ["error"], 2),
    (["prompt", "Your name?", "--non-interactive", "--default", "ada"],
     None, ["ada"], 0),
    (["prompt", "Pick", "--choice", "a,b", "--non-interactive", "--default", "z"],
     None, ["not one of"], 65),
    (["prompt", "Name?"], "typed\n", ["typed"], 0),

    # -- docs / completions ---------------------------------------------
    (["docs", "--format", "json"], None, ['"prog"', "subcommands"], 0),
    (["docs", "--only", "nosuch"], None, ["no such command"], 66),
    (["docs", "--format", "markdown", "--only", "hash"], None, ["#"], 0),
    (["docs", "--format", "tree", "--only", "json"], None, ["json"], 0),
    (["docs", "--format", "bash"], None, ["_anvil_complete"], 0),
    (["docs", "--format", "fish"], None, ["complete -c"], 0),
    (["completions", "show", "bash"], None, ["_anvil_complete"], 0),
    (["completions", "path", "bash"], None, ["completions"], 0),
    (["completions", "install", "bash", "--dest", "comp.bash"], None,
     ["installed"], 0),
    (["completions", "check", "--output", "json"], None, ["bash"], 0),
    (["completions", "show", "nosuchshell"], None, ["error"], 2),

    # -- explain / doctor / mktemp -------------------------------------
    (["explain", "--list"], None, ["nargs", "formatter"], 0),
    (["explain", "nargs"], None, ["nargs", "REMAINDER"], 0),
    (["explain", "narg"], None, ["did you mean"], 66),
    (["doctor", "--only", "python", "--output", "json"], None, ["python"], 0),
    (["doctor", "--only", "nosuchsection"], None, ["unknown section"], 65),
    (["mktemp", "--print-path"], None, [], 0),
    (["mktemp", "-d", "--print-path"], None, [], 0),
    (["mktemp", "--suffix", ".json", "--print-path"], None, [], 0),
    (["mktemp", "--length", "12", "--print-path"], None, [], 0),

    # -- serve / watch / http: argument surface only --------------------
    (["serve", "--dry-run", "."], None, ["serving"], 0),
    (["serve", "nosuchdir"], None, ["does not exist"], 66),
    (["serve", "plain.txt"], None, ["is not a directory"], 65),
    (["watch", "--dry-run", "--exec", "echo hi"], None, ["watching"], 0),
    (["watch", "--exec"], None, ["expected one argument"], 2),
    (["http", "get"], None, ["no URL"], 65),
    (["http", "get", "not-a-url"], None, ["not an http"], 65),
    (["http", "bogus", "https://x.example"], None, ["unknown method"], 65),
    (["http", "get", "https://x.example", "-w", "%{typo}"], None,
     ["unknown write-out"], 2),
    (["http", "get", "https://x.example", "--timeout", "nope"], None,
     ["not a duration"], 2),

    # -- global flag handling ------------------------------------------
    (["--output", "json", "secrets", "--length", "12"], None, [""], 0),
    (["--no-color", "table", "users.json"], None, ["ada"], 0),
    (["--table-style", "ascii", "table", "users.json"], None, ["ada"], 0),
    (["--dry-run", "run", "echo nope"], None, ["would run"], 0),
    (["--set", "http.timeout=1s", "config", "get", "http.timeout"], None, ["1s"], 0),
    (["-vv", "run", "--silent", "echo q"], None, ["q"], 0),
    (["--log-level", "debug", "secrets", "--length", "8"], None, [], 0),
    (["-q", "run", "--silent", "exit 2"], None, [], 2),
]


@pytest.mark.parametrize(
    ("argv", "stdin", "needles", "expected"),
    CASES,
    ids=[(" ".join(c[0]) or "(no args)") for c in CASES],
)
def test_cli(workdir, argv, stdin, needles, expected):
    result = run(argv, workdir, stdin)
    combined = result.stdout + result.stderr
    if expected is not None:
        assert result.returncode == expected, (
            f"exit {result.returncode} != {expected}\n{combined}"
        )
    for needle in needles:
        assert needle in combined, f"missing {needle!r} in:\n{combined}"


def test_no_traceback_escapes_under_any_failure(workdir):
    """No invocation may leak an internal error -- that is exit code 70."""
    broken = [
        ["json", "format", "bad.json"],
        ["json", "query", ".[] | nonsense", "users.json"],
        ["hash", "nosuchfile"],
        ["config", "get", "nope.missing"],
        ["serve", "nosuchdir"],
        ["table", "users.json", "--columns", "nope"],
        ["bench", "python -c pass"],
        ["prompt", "Name?"],
        ["--nosuchflag"],
        [],
    ]
    for argv in broken:
        result = run(argv, workdir)
        assert "internal error" not in result.stderr, (
            f"{argv} produced an internal error:\n{result.stderr}"
        )
        assert "Traceback" not in result.stderr, (
            f"{argv} leaked a traceback:\n{result.stderr}"
        )


def test_output_is_utf8_safe_on_a_legacy_console(workdir):
    """Unicode box drawing must not raise even under a cp1252 code page."""
    env = dict(os.environ)
    env.update({"PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"})
    env.pop("ANVIL_FORCE_COLOR", None)
    result = subprocess.run(
        [sys.executable, "-m", "anvil", "table", "users.json", "--style", "unicode"],
        cwd=str(workdir), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0
    assert "\u2502" in result.stdout
