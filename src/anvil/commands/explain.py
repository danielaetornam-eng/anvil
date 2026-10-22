"""``anvil explain`` -- teach the argparse pattern behind each command.

Every command in this CLI exists to demonstrate a specific argparse technique.
``anvil explain`` names that technique, shows the code that implements it, and
runs the real thing so the reader can see the output rather than imagine it.
"""

from __future__ import annotations

import argparse

from anvil.core import term
from anvil.core.app import Context, register_command
from anvil.core.errors import NotFoundError

HELP = "learn the argparse pattern behind any command"

#: ``pattern -> (summary, argparse API used, code, note)``
PATTERNS: dict[str, dict[str, str]] = {
    "subparsers": {
        "title": "Nested subcommands",
        "summary": "One parser per command, dispatched by name.",
        "api": "add_subparsers(dest=..., metavar=..., title=...)",
        "code": '''\
sub = parser.add_subparsers(dest="command", metavar="COMMAND", title="Commands")
run = sub.add_parser("run", aliases=["r"], help="run tasks")
json_ = sub.add_parser("json")
json_.add_subparsers(dest="json_command", required=True)
query = json_.add_parser("query")
''',
        "note": "`dest` records which subcommand ran, which is how dispatch "
                "stays a table lookup instead of an if/elif chain. "
                "`required=True` on the subparsers action makes the command "
                "mandatory.",
    },
    "nargs": {
        "title": "nargs in all its forms",
        "summary": "One flag, several arities: ?, *, +, N, and REMAINDER.",
        "api": "nargs='?' | '*' | '+' | N | REMAINDER | PARSER",
        "code": '''\
p.add_argument("--maybe", nargs="?", const="default")   # flag, or a value, or neither
p.add_argument("--many", nargs="*")                     # zero or more
p.add_argument("--some", nargs="+")                     # one or more
p.add_argument("--pair", nargs=2)                       # exactly two
p.add_argument("rest", nargs=argparse.REMAINDER)        # everything after --
''',
        "note": "`nargs='?'` needs `const=` to say what happens when the flag "
                "is given with no value. `REMAINDER` stops option parsing "
                "entirely, which is what `docker run` relies on -- it only "
                "works reliably as the final positional.",
    },
    "type": {
        "title": "Custom type= callables",
        "summary": "Convert and validate in one place, with good errors.",
        "api": "type=fn, raising argparse.ArgumentTypeError",
        "code": '''\
def byte_size(value: str) -> int:
    match = re.match(r"^(\\d+)\\s*([kmgt]i?b?)$", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError(f"{value!r} is not a size (try 10MB)")
    return int(match[1]) * UNITS[match[2].lower()]

p.add_argument("--max-size", type=byte_size)
''',
        "note": "Raise ArgumentTypeError, never ValueError: argparse turns the "
                "former into `argument --max-size: <your message>` and lets the "
                "traceback of the latter escape.",
    },
    "action": {
        "title": "Built-in and custom actions",
        "summary": "Actions decide what happens to parsed values.",
        "api": "action='append' | 'store_const' | 'count' | BooleanOptionalAction",
        "code": '''\
p.add_argument("--tag", action="append")        # collect into a list
p.add_argument("--level", action="count", default=0)  # -v -v -v
p.add_argument("--old", action="store_const", const="legacy")
p.add_argument("--color", action=argparse.BooleanOptionalAction, default=True)
''',
        "note": "`argparse.BooleanOptionalAction` (3.9+) turns one option into "
                "`--color/--no-color`, which is the only correct way to express "
                "a boolean flag with a default.",
    },
    "custom-action": {
        "title": "Custom Action subclasses",
        "summary": "Extend argparse when you need to do, not just convert.",
        "api": "class MyAction(argparse.Action)",
        "code": '''\
class CommaListAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=1, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        current = getattr(namespace, self.dest, None) or []
        current += [p for p in values[0].split(",") if p]
        setattr(namespace, self.dest, current)

p.add_argument("--include", action=CommaListAction, default=[])
''',
        "note": "An Action sees *all* the values of one flag at once, so it is "
                "the only way to implement `--bounds 0-10` as a single token. "
                "Do I/O in `__call__`, never in `__init__`, so building the "
                "parser stays side-effect free.",
    },
    "mutex": {
        "title": "Mutually exclusive groups",
        "summary": "Two flags that cannot both appear.",
        "api": "add_mutually_exclusive_group(required=False)",
        "code": '''\
shape = p.add_mutually_exclusive_group()
shape.add_argument("--json", action="store_true")
shape.add_argument("--yaml", action="store_true")
shape.add_argument("--csv", action="store_true")
''',
        "note": "Groups nest, and argparse renders them as a separate help "
                "section. Note that argparse checks exclusivity *after* "
                "parsing, so the error message is the standard usage error.",
    },
    "parents": {
        "title": "Shared argument sets with parents=",
        "summary": "One definition, reused by many parsers.",
        "api": "parents=[shared], or add_argument_group",
        "code": '''\
common = ArgumentParser(add_help=False)
common.add_argument("-v", "--verbose", action="count", default=0)

alpha = ArgumentParser(parents=[common])
beta  = ArgumentParser(parents=[common])
''',
        "note": "`parents` copies actions *by reference at construction time*, "
                "so a parser cannot be a parent of itself and later mutations "
                "do not propagate. Use `add_argument_group` for grouping and "
                "`parents` for genuine reuse.",
    },
    "formatter": {
        "title": "Custom help formatters",
        "summary": "Own the layout when the stock one is not enough.",
        "api": "formatter_class=MyFormatter, overriding _format_action",
        "code": '''\
class MyFormatter(argparse.RawDescriptionHelpFormatter):
    def _format_action(self, action):
        # width must be measured on *visible* characters, because the
        # invocation string may contain ANSI escapes.
        header = self._format_action_invocation(action)
        if visible_width(header) <= self._action_max_length + 2:
            return f"{header:>{self._action_max_length + 2}}  {self._expand_help(action)}"
        return f"{header}\\n{' ' * (self._action_max_length + 4)}"
''',
        "note": "`argparse.HelpFormatter._decolor` only exists in 3.14. "
                "Measuring with `len()` on coloured text pushes the help "
                "column off screen on 3.11-3.13.",
    },
    "env": {
        "title": "Environment variables for every option",
        "summary": "Auto-derive $PROG_COMMAND_OPTION for each flag.",
        "api": "default= + a post-parse pass over parser._actions",
        "code": '''\
for action in parser._actions:
    if not action.option_strings or action.default is not None:
        continue
    name = max(action.option_strings, key=len)          # prefer the long flag
    variable = "ANVIL_" + name.lstrip("-").replace("-", "_").upper()
    raw = os.environ.get(variable)
    if raw is not None and action.type:
        namespace.__dict__[action.dest] = action.type(raw)
''',
        "note": "This has to run *after* parsing. `default=os.environ.get(...)` "
                "is evaluated while the parser is constructed, which is before "
                "`--config` has been read -- so a config-file value could never "
                "be overridden by an explicit flag.",
    },
    "config": {
        "title": "Configuration layering",
        "summary": "defaults < file < environment < flags.",
        "api": "a private bootstrap parser + a merged mapping",
        "code": '''\
boot = ArgumentParser(add_help=False)
boot.add_argument("--config")
boot.add_argument("--set", action="append", dest="overrides")
known, _ = boot.parse_known_args(argv)      # bootstrap pass

config = load(known.config)                  # file layer
config.update(env_layer())                   # env layer
config.update(known.overrides)               # flag layer
''',
        "note": "argparse cannot do this itself: it has exactly one `default` "
                "slot per action, resolved before the config file is known. "
                "A throwaway parser with `parse_known_args` is the standard "
                "bootstrap.",
    },
    "errors": {
        "title": "Exit codes and friendly errors",
        "summary": "Raise instead of exiting, and classify the failure.",
        "api": "exit_on_error=False + error() raising, parser.exit() raising",
        "code": '''\
class Parser(ArgumentParser):
    def error(self, message):
        raise UsageError(message)      # not sys.exit(2)

    def exit(self, status=0, message=None):
        raise ExitRequested(status, message)

try:
    namespace = parser.parse_args(argv)
except UsageError as exc:
    print(exc, file=sys.stderr)
    return 64                            # sysexits.h EX_USAGE
''',
        "note": "`exit_on_error=False` makes `ArgumentError` reachable instead "
                "of a direct `sys.exit`. Owning the exit path means exit codes "
                "become part of your API instead of an implementation detail.",
    },
    "suggest": {
        "title": "Did-you-mean suggestions",
        "summary": "Overriding error() buys you a better message.",
        "api": "difflib.get_close_matches inside error()",
        "code": '''\
def error(self, message):
    if "invalid choice" in message:
        value = message.split("'")[1]
        close = difflib.get_close_matches(value, self.option_strings, 1)
        if close:
            message += f"\\ndid you mean {close[0]}?"
    raise ParseFailure(message, parser=self)
''',
        "note": "Prefer candidates that *start with* what the user typed: for a "
                "partial flag, the intended option nearly always shares a "
                "prefix even when its edit distance is not minimal.",
    },
    "completion": {
        "title": "Shell completion from introspection",
        "summary": "Generate scripts from the live parser, never by hand.",
        "api": "reading parser._actions and the subparsers' choices mapping",
        "code": '''\
action = next(a for a in parser._actions
              if isinstance(a, argparse._SubParsersAction))
for name, child in action.choices.items():
    print(name, child.description)
''',
        "note": "Hand-written completions rot on the first flag rename. "
                "Generated ones cannot, because they come from the same "
                "`add_argument` calls that build `--help`.",
    },
    "intermixed": {
        "title": "Options after positionals",
        "summary": "parse_intermixed_args when the grammar allows it.",
        "api": "parse_intermixed_args / parse_known_intermixed_args",
        "code": '''\
# anvil run build --jobs 8 test --jobs 4 lint
args = parser.parse_intermixed_args(argv)

# Caveat: it refuses parsers that also have subparsers, and it cannot
# mix a positionals-consuming action with nargs=REMAINDER.
''',
        "note": "Plain `parse_args` stops parsing options at the first "
                "positional, so `prog a --flag b` puts `--flag` into the "
                "positional list. `parse_intermixed_args` fixes that, but only "
                "for parsers without subparsers -- a documented argparse "
                "limitation, not a bug.",
    },
    "debounce": {
        "title": "Deprecated flags that still work",
        "summary": "Keep old flags alive with a warning and a replacement.",
        "api": "deprecated=True (3.13+ for subcommands) or a custom Action",
        "code": '''\
p.add_argument("--old-name", action=DeprecatedAction,
               replacement="--new-name", removed_in="2.0",
               help="(deprecated)")
''',
        "note": "argparse's own `deprecated=` covers subcommands from 3.13. For "
                "options you still need an Action, and it should say what the "
                "replacement is *and* when it goes away.",
    },
    "help-data": {
        "title": "--help as data",
        "summary": "Reflect the parser tree into JSON for tooling.",
        "api": "reading action attributes: option_strings, nargs, choices, ...",
        "code": '''\
def dump(parser):
    return [{
        "flags": a.option_strings,
        "metavar": a.metavar,
        "type": getattr(a.type, "__name__", None),
        "nargs": a.nargs,
        "required": a.required,
        "default": a.default,
        "choices": list(a.choices) if a.choices else None,
    } for a in parser._actions if a.help is not argparse.SUPPRESS]
''',
        "note": "This is the highest-leverage trick in the whole module: one "
                "source of truth feeds the help screen, the docs, the "
                "completions, and the tests that assert the interface.",
    },
}


def register(subparsers, *, prog: str, aliases=(), tags=()) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "explain", aliases=list(aliases), help=HELP,
        description="Explain an argparse pattern, with runnable code.\n\n"
                    "Every command in this CLI is a working demonstration of "
                    "one of these.",
    )
    parser.add_examples(
        "anvil explain",
        "anvil explain nargs",
        "anvil explain --list",
        "anvil explain config --where",
    )
    parser.add_argument("--list", action="store_true", default=False,
                        help="list every pattern")
    parser.add_argument("--where", action="store_true", default=False,
                        help="show which command demonstrates each pattern")
    parser.add_argument("--no-code", action="store_true", default=False,
                        help="omit the code sample")
    parser.add_argument("pattern", nargs="?", default=None, metavar="PATTERN",
                        help="pattern to explain")
    parser.set_defaults(func=_explain)
    return parser


@register_command("explain")
def _explain(ctx: Context, args: argparse.Namespace) -> int:
    from anvil.core.errors import similar

    if args.list or not args.pattern:
        return _list_patterns(ctx, args)
    name = args.pattern.strip()
    entry = PATTERNS.get(name)
    if entry is None:
        guess = similar(name, PATTERNS, limit=1)
        raise NotFoundError(
            f"no pattern named {name!r}",
            hint=(f"did you mean {guess[0]!r}? try 'anvil explain --list'"
                  if guess else "try 'anvil explain --list'"),
        )

    if ctx.output_format in ("json", "yaml", "jsonl", "csv", "tsv"):
        ctx.out(ctx.renderer.scalar(
            {"pattern": name, **{k: v for k, v in entry.items() if k != "code"}}
        ))
        return 0

    # Human form: a titled block rather than a repr() of the record.
    ctx.err(term.A.head(entry["title"]))
    ctx.err("")
    for label in ("summary", "api", "note"):
        ctx.err(f"  {term.A.info(label + ':')} {entry[label]}")
        ctx.err("")
    if not args.no_code:
        ctx.err(f"  {term.A.bold('code')}")
        for line in entry["code"].rstrip("\n").splitlines():
            ctx.err(f"    {term.A.hint(line)}".rstrip())
        ctx.err("")
    if args.where:
        shown = _demonstrations(name)
        ctx.err(f"  {term.A.bold('demonstrated by')}")
        for command in shown:
            ctx.err(f"    {term.A.info(command)}")
        ctx.err("")
    ctx.out(name)
    return 0


_DEMONSTRATIONS = {
    "subparsers": ["anvil json", "anvil config list"],
    "nargs": ["anvil run", "anvil http", "anvil explain"],
    "type": ["anvil http --timeout", "anvil hash --length", "anvil run --jobs"],
    "action": ["anvil secrets", "anvil run --parallel"],
    "custom-action": ["anvil table -D", "anvil config set", "anvil http -w"],
    "mutex": ["anvil diff --ignore-case", "anvil table --style", "anvil json validate"],
    "parents": ["every command (global flags come from the root parser)"],
    "formatter": ["anvil --help"],
    "env": ["anvil --output", "anvil --log-level"],
    "config": ["anvil --set", "anvil config list --explain"],
    "errors": ["every command"],
    "suggest": ["anvil <typo>"],
    "completion": ["anvil completions"],
    "intermixed": ["anvil run build --jobs 8 test"],
    "debounce": ["anvil --legacy"],
    "help-data": ["anvil docs"],
}


def _list_patterns(ctx: Context, args: argparse.Namespace) -> int:
    ctx.tabular()
    table = term.Table([term.Column("pattern", "pattern"),
                        term.Column("title", "what it is")])
    for name in sorted(PATTERNS):
        table.add_row([name, PATTERNS[name]["title"]])
    ctx.render(table)
    return 0


def _demonstrations(name: str) -> list[str]:
    return _DEMONSTRATIONS.get(name, [])

#: Entry point for the command registry; leaf subcommands
#: override it with their own ``set_defaults(func=...)``.
HANDLER = _explain
