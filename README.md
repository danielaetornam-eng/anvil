# anvil

**A production-grade command-line tool built entirely on the standard library's
`argparse`. Zero dependencies. Every pattern that matters, plus the sharp edges
that the official documentation only mentions once.**

`anvil` is two things at once: a genuinely useful developer toolbox, and a
reference implementation of every argparse technique you will ever need. Run
`anvil explain <pattern>` to read the code for any technique with the real
thing executing beside it.

```
anvil json query '.users[] | select(.age > 30)' users.json
anvil http get https://api.example.com/health --retry 3 -w '%{http_code}'
anvil run --parallel --jobs 8 build test lint
anvil table data.csv --style markdown --total score=sum
anvil docs --format json | jq '.subcommands | keys'
```

---

## Install

```bash
pip install -e .          # requires Python 3.11+ (3.11-3.14 supported)
anvil --help
```

No runtime dependencies. The only import outside the standard library is
`tomllib`, and only for TOML config files.

---

## Why this exists

`argparse` is the most misused module in the standard library. Its power is
obscured by ~40 keyword arguments, a handful of behaviours that differ between
Python versions, and several sharp edges that fail silently rather than
loudly. Almost every real-world `argparse` bug falls into one of these:

| Symptom | Actual cause |
|---|---|
| `AttributeError: 'Namespace' object has no attribute 'passthrough'` | `nargs='+'` swallows every token after `--` |
| `unknown action 'comma_list'` inside `add_argument_group` | groups have their **own** registry, unrelated to the parser's |
| `nargs for store actions must be != 0` | copying a flag action and forwarding `nargs=0` |
| `unrecognized arguments: --colums` | argparse validates `choices` *before* your `Action.__call__` |
| Help output ragged under colour | `len()` counts ANSI bytes; `_decolor` only exists in 3.14 |
| `anvil --dry-run x` ignored by `anvil x` | a subparser's defaults overwrite the root's parsed values |

Each of these is implemented correctly here, and each is documented at the
point where it bites.

---

## What's in the box

- **11 core modules** — parser class, error taxonomy, help formatter, types,
  actions, config layering, stdio, logging, introspection, completion, dispatch.
- **44 `type=` converter classes, 38 pre-built instances** — byte sizes,
  compound durations, intervals, ports, semver, ULIDs, permission modes,
  regexes, more.
- **16 custom `Action` subclasses** — comma lists, `KEY=VALUE` maps,
  GNU-style `-v`/`-q`, self-naming flags, lazily-opened output files,
  atomic writes, secret redaction, environment fallbacks.
- **22 commands**, each a working demonstration of specific argparse techniques.
- **498 tests** (365 unit + 133 subprocess-level), ruff clean.

---

## The pattern matrix

Every row is live code. `anvil explain <pattern>` prints the implementation
with commentary; `--where` points at the command that uses it.

### Parsing

| Pattern | Where | Note |
|---|---|---|
| Nested subparsers | `anvil json query` | 3 levels deep, `dest` drives dispatch |
| `aliases=` on subcommands | `anvil serve server` | listed in `--help` |
| `nargs='?'` + `const=` | `anvil prompt --choice a,b` | flag with an optional value |
| `nargs='*'` / `'+'` / `N` | `--exclude`, `TASK`, `--bounds 0 10` | |
| `nargs=REMAINDER` | `anvil bench -- cmd --flag` | only reliable as the sole positional |
| `argparse.REMAINDER` caveat | `anvil run` | **`nargs='+'` before it starves it** |
| `--` split before parsing | `Context.passthrough` | what docker/git/kubectl actually do |
| `parse_known_args` | `--next` | curl-style request chaining |
| `parse_intermixed_args` | — | documented in `anvil explain intermixed` |
| `prefix_chars`, `allow_abbrev` | `--no-` prefixes, abbreviation | |

### Types and validation

| Pattern | Where | Note |
|---|---|---|
| `type=` raising `ArgumentTypeError` | all 44 converters | **never `ValueError`** |
| Type objects carrying metadata | `ParamType` | `__name__` becomes the metavar |
| Alias + case-folding `type=` | `NormalizedChoice` | must be `type=`, not an `Action` |
| Validating existence at parse time | `existing_file` | |
| Nested argparse inside a `type=` | `KeyValue` | with `exit_on_error=False` + stderr redirect |
| `choices` as a display *and* a check | `--format`, `--charset` | |

### Actions

| Pattern | Where | Note |
|---|---|---|
| `action='append'` / `count` / `store_const` | `--tag`, `-v`, `--legacy` | |
| `BooleanOptionalAction` | — | `action=argparse.BooleanOptionalAction` |
| Custom `Action` (positional *and* optional) | 16 classes | `nargs` must be set explicitly |
| `nargs='+'` to see multiple tokens at once | `RangeAction` (`--bounds 0-10`) | impossible with `type=` |
| One action, several option strings | `VerbosityAction` (`-v`/`-q`) | sign chosen by `option_string` |
| Accumulate into a shared dict | `ThresholdAction` | argparse never shares `dest` |
| Key derived from the flag name | `AutoNameAction` | `--maintainer=x` → `maintainer` |
| Side effects only in `__call__` | every action | `__init__` runs during `--help` |

### Structure

| Pattern | Where | Note |
|---|---|---|
| `parents=` for global flags | every subcommand | **fresh copy per child** — see below |
| `mutually_exclusive_group` | `--json`/`--yaml`/`--csv` | |
| `add_argument_group` | Diagnostics / Output / Shape | |
| `add_subparsers(parser_class=)` | `AnvilArgumentParser` | otherwise children lose everything |
| `fromfile_prefix_chars` | — | |
| `deprecated=True` (3.13+) | subcommands | options still need a custom action |
| Custom `anvil_*` metadata kwargs | `--log-level`, `--output` | intercepted in `add_argument` |

### Output and UX

| Pattern | Where | Note |
|---|---|---|
| Custom `HelpFormatter` | `anvil --help` | own layout engine, own width maths |
| ANSI-aware wrapping | `term.wrap_ansi` | `textwrap` counts escape bytes |
| `width`/`max_help_position` | `AnvilHelpFormatter` | measured on *visible* columns |
| Automatic `[default: …]` annotations | every option | string defaults keep their spelling |
| Did-you-mean on `error()` | every failure | transposition-aware |
| Suggestions across subcommands | `anvil table --colums` | argparse reports leftovers at the root |
| `sysexits.h` exit codes | every command | 0/2/65/66/70/77/78/130 |
| Machine-readable `--help` | `anvil docs --format json` | derived from the same tree |
| Generated shell completions | `anvil completions install` | cannot go stale |
| `_print_message` instead of `print` | help action | keeps `--help` pipe-safe |

---

## The five hard parts

### 1. Global flags that work *anywhere*

Users expect `anvil --dry-run run x` and `anvil run --dry-run x` to be the
same. argparse says otherwise. The fix is `parents=`, but there is a trap:

```python
# WRONG -- one shared parent for every subcommand
shared = build_globals()
sub.add_parser("run", parents=[shared])
sub.add_parser("json", parents=[shared])
```

`conflict_handler='resolve'` calls `action.option_strings.remove(...)` **on
the action object it displaces**. Since `parents=` shares those objects, the
first command that redefines a global flag silently strips the option string
from every other command's copy — turning it into an unrecognised positional.

```python
# RIGHT -- a fresh parent per child
def add_parser(name, **kw):
    kw["parents"] = [*(kw.get("parents") or []), build_globals()]
    return original(name, **kw)
```

Copies must also use `default=argparse.SUPPRESS`. `_SubParsersAction` parses
the child's argv into a fresh namespace and copies *every* attribute over the
parent's — including defaults. A real default there resets whatever the root
already decided, so `anvil --dry-run run x` would run `x` for real.

### 2. Help text that is actually readable

`HelpFormatter._format_action` measures `len()` of the invocation string. Feed
it colour and the description column is pushed eight bytes to the right for
every option — enough to wrap help text three times too early. `_decolor` only
exists in 3.14, so `AnvilHelpFormatter` owns its layout and measures on
`term.visible_width`. Wrapping uses `term.wrap_ansi`, which tracks escape
sequences separately from display width.

Annotations (`default:`, `required`, `$ENV`, `one of:`, `deprecated`) are
generated rather than hand-written, so they cannot drift:

```python
parser.add_argument("--token", help="auth token", anvil_env="ANVIL_TOKEN")
# -> --token TOKEN  auth token [$ANVIL_TOKEN]
```

`anvil_*` keywords are stripped in `add_argument` and set on the resulting
action. They must also be stripped in `add_argument_group`, because
`_ArgumentGroup.add_argument` never delegates to its container parser.

### 3. One exit path

```python
class AnvilArgumentParser(argparse.ArgumentParser):
    def error(self, message): raise ParseFailure(message, parser=self)
    def exit(self, status=0, message=None): raise ExitRequested(status, message)
```

With `exit_on_error=False`, every parse failure becomes an exception, so
`run_cli` owns the whole transition to a process status. Commands never call
`sys.exit()`; they return a code or raise a typed error. That is also what
makes the CLI testable in-process — 365 of the 498 tests never spawn a
subprocess.

### 4. Configuration layering

`defaults < file < environment < flags`, resolved *after* parsing. argparse has
one `default=` slot per action and evaluates it while the parser is being
built — before `--config` has been read — so the layering is expressed with a
throwaway bootstrap parser plus a post-parse merge pass.

### 5. `--help` as data

`anvil docs --format json` reflects the live parser tree. Documentation,
completions, config schemas and interface tests are all derived from the same
`add_argument` calls that render `--help`, so none of them can go stale.

---

## Commands

| Command | What it does | Patterns demonstrated |
|---|---|---|
| `run` | task runner, parallel or sequential | `REMAINDER`, `--` split, exit-code aggregation |
| `http` | HTTP client | `REMAINDER`, repeatable headers, retries, curl `--write-out` |
| `json` | query / validate / merge / diff / paths | 3-level subparsers, recursive argparse for a filter DSL |
| `table` | render records as a table | `KEY=VALUE` maps, per-column alignment, 10 output formats |
| `hash` | digests and verification | variadic positional, repeatable `-a`, `--verify` |
| `bench` | latency percentiles | `REMAINDER`, JSON reports |
| `diff` | files and trees | `nargs='?'`, mutually exclusive transforms |
| `serve` | static server | port/service-name `type=`, dry-run before side effects |
| `watch` | rerun on change | debouncing, `--max-runs` |
| `secrets` | passwords, UUIDs, ULIDs | `action='append'`, entropy reporting |
| `text` | line transforms | `nargs` filters, `Interval` type |
| `encode` | base64/hex/url/JSON | `choices=` driving a dispatch table |
| `template` | `${name}` substitution | strict mode, multiple value sources |
| `config` | read/write layered config | atomic writes, redaction |
| `env` | environment inspection | redaction, required-variable gating |
| `doctor` | environment diagnostics | self-referential parser checks |
| `docs` | parser tree as data | introspection, Markdown generation |
| `completions` | shell completion | generated, not hand-written |
| `mktemp` | temporary files | laziness, `--length` renaming |
| `retry` | retry with backoff | jitter, `--retry-on` |
| `prompt` | interactive input | every non-interactive escape |
| `explain` | the pattern catalogue | `--list`, `--where` |

---

## Development

```bash
python -m pytest tests/          # 498 tests
python -m pytest tests/ -k types # a subset
python -m ruff check .
```

`tests/test_cli.py` runs the CLI in-process (fast); `tests/test_smoke.py`
drives `python -m anvil` as a subprocess (real argv, real console encoding,
real exit status).

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unclassified failure |
| 2 | usage error (argparse) |
| 65 | malformed input data |
| 66 | missing input |
| 69 | service unavailable |
| 70 | internal error — a bug |
| 77 | permission denied |
| 78 | bad configuration |
| 130 | interrupted |

---

## Licence

MIT.