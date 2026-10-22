"""Custom :class:`argparse.Action` subclasses.

An ``Action`` is the right extension point when you need to *do* something
with the parsed values rather than merely convert them. Every class here
follows the same contract:

* accept ``option_strings``/``dest`` positionally and ``**kwargs`` for the rest,
  then forward to ``argparse.Action.__init__``;
* set ``self.nargs`` explicitly when the action consumes a different number of
  values than ``None`` -- argparse decides how many tokens to read from
  ``action.nargs`` *before* calling ``__call__``;
* do I/O and side effects in ``__call__`` only, never in ``__init__``, so that
  building a parser (which happens even for ``--help``) stays side-effect free.
"""

from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import re
import sys
import warnings
from collections.abc import Callable, Iterable, Sequence
from typing import IO, Any

__all__ = [
    "AppendUniqueAction",
    "AtomicOutput",
    "AutoNameAction",
    "CommaListAction",
    "DeprecatedAction",
    "EnvFallbackAction",
    "ExistingDirAction",
    "ExistingFileAction",
    "FormatTemplateAction",
    "KeyValueAction",
    "LazyOutputFile",
    "OutputFileAction",
    "PatternListAction",
    "RangeAction",
    "RemainderAction",
    "SecretValue",
    "ThresholdAction",
    "VerboseCountAction",
    "VerbosityAction",
    "apply_env_fallbacks",
    "confirm",
    "convert_string_defaults",
    "prompt_password",
    "register_all",
]


# --------------------------------------------------------------------------- #
# Store-like actions
# --------------------------------------------------------------------------- #


class AppendUniqueAction(argparse.Action):
    """``action='append_unique'``: accumulate values, ignoring duplicates.

    Ordering is preserved. This is the behaviour ``append`` should have had,
    and it is the reason ``--include a --include a`` is not a bug report.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        current = getattr(namespace, self.dest, None) or []
        if not isinstance(current, list):
            current = [current]
        if values not in current:
            current.append(values)
        setattr(namespace, self.dest, current)


class CommaListAction(argparse.Action):
    """``action='comma_list'``: one flag, many values, split on commas.

    ``--include a,b --include c`` -> ``['a', 'b', 'c']``. Pairs with
    ``nargs=1`` so a following positional is never mistaken for a value --
    the bug that makes plain ``append`` unusable for optional lists.
    """

    def __init__(self, option_strings, dest, *, empty_ok: bool = False, **kwargs):
        self.empty_ok = empty_ok
        super().__init__(option_strings, dest, nargs=kwargs.pop("nargs", 1), **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        raw = values[0] if isinstance(values, list) else values
        current = getattr(namespace, self.dest, None) or []
        if not isinstance(current, list):
            current = [current]
        if raw is None or raw == "":
            if not self.empty_ok:
                parser.error(f"argument {option_string or self.dest}: expected a value")
            setattr(namespace, self.dest, current)
            return
        for piece in str(raw).split(","):
            piece = piece.strip()
            if piece:
                current.append(piece)
        setattr(namespace, self.dest, current)


def _key_value_splitter(separators: str = "=:") -> Callable[[str], tuple[str, str, str]]:
    """Build a ``text -> (key, separator, value)`` splitter.

    Shared with the ``KeyValue`` *type* so that ``--set k=v`` and
    ``--header 'Name: value'`` can never disagree about where the key ends.
    """

    def split(text: str) -> tuple[str, str, str]:
        positions = [(text.find(sep), sep) for sep in separators
                     if text.find(sep) != -1]
        if not positions:
            return text, "", ""
        index, sep = min(positions)
        return text[:index], sep, text[index + len(sep):]

    return split


class KeyValueAction(argparse.Action):
    """``action='key_value'``: ``--set a=1 --set b=2`` -> ``{'a': '1', 'b': '2'}``.

    Later occurrences win. Splitting is delegated to the ``KeyValue`` *type* so
    that ``--set k=v`` and ``-H 'Name: value'`` accept the same separators and
    the same escaping rules; having two implementations of "where does the key
    end" is how ``--header`` ends up rejecting ``Accept: json``.
    """

    def __init__(self, option_strings, dest, *, parse: Callable[[str], Any] | None = None,
                 separators: str = "=:", **kwargs):
        self.parse = parse
        self.splitter = _key_value_splitter(separators)
        kwargs.pop("nargs", None)
        super().__init__(option_strings, dest, nargs=1, **kwargs)

    def _split(self, item: str) -> tuple[str, str, str]:
        return self.splitter(item)

    def __call__(self, parser, namespace, values, option_string=None):
        raw = values[0] if isinstance(values, list) else values
        if isinstance(raw, (list, tuple)):
            items: list[Any] = list(raw)
        else:
            items = [raw]
        current = dict(getattr(namespace, self.dest, None) or {})
        for item in items:
            if self.parse is not None:
                parsed = self.parse(item)
            else:
                key, sep, value = self._split(str(item))
                if not sep:
                    parser.error(
                        f"argument {option_string or self.dest}: expected "
                        f"KEY=VALUE, got {item!r}"
                    )
                    return
                parsed = (key, value[1:] if sep == ":" and value.startswith(" ")
                          else value)
            if isinstance(parsed, tuple) and len(parsed) == 2:
                current[parsed[0]] = parsed[1]
            elif isinstance(parsed, dict):
                current.update(parsed)
            else:
                parser.error(
                    f"argument {option_string or self.dest}: expected KEY=VALUE, "
                    f"got {item!r}"
                )
        setattr(namespace, self.dest, current)


class AutoNameAction(argparse.Action):
    """``action='auto_name'``: use the option string itself as the key.

    ``--maintainer=ana --support-url=https://...`` produces
    ``{'maintainer': 'ana', 'support_url': 'https://...'}`` with no repeated
    key literal. This is what makes ``--define foo=1`` style flags possible.

    The option prefix is read from the *parser*, because ``Action`` has no
    ``prefix_chars`` of its own -- it is a property of the container.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if not self.option_strings:
            parser.error(f"{self.dest}: auto_name requires an option string")
        # Prefer the long form: `-D region=eu` must yield the key "region",
        # not "D". argparse passes whichever spelling was typed, so the long
        # option has to be looked up rather than assumed.
        long_forms = [o for o in self.option_strings if o.startswith("--")]
        spelling = long_forms[0] if long_forms else self.option_strings[0]
        prefix = parser.prefix_chars or "-"
        key = spelling.lstrip(prefix).replace("-", "_")
        current = dict(getattr(namespace, self.dest, None) or {})
        current[key] = values[0] if isinstance(values, list) and len(values) == 1 else values
        setattr(namespace, self.dest, current)


class PatternListAction(argparse.Action):
    """``action='pattern_list'``: accumulate compiled regexes from strings.

    Validation happens at parse time (so a typo is a usage error with exit
    code 2, not a traceback later) while the namespace still holds usable
    compiled patterns.
    """

    def __init__(self, option_strings, dest, *, flags: int = 0, **kwargs):
        self.flags = flags
        super().__init__(option_strings, dest, nargs=kwargs.pop("nargs", None) or 1, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        raw = values[0] if isinstance(values, list) and len(values) == 1 else values
        if isinstance(raw, (list, tuple)):
            raw = ",".join(str(r) for r in raw)
        current = list(getattr(namespace, self.dest, None) or [])
        for piece in str(raw).split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                current.append(re.compile(piece, self.flags))
            except re.error as exc:
                parser.error(
                    f"argument {option_string or self.dest}: invalid pattern "
                    f"{piece!r} at position {exc.pos}: {exc.msg}"
                )
        setattr(namespace, self.dest, current)


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #


class VerbosityAction(argparse.Action):
    """``action='verbosity'``: ``-v`` counts up, ``-q`` counts down.

    One action, two option strings, sign chosen by ``option_string``. This is
    the only way to get true GNU-style repeatable verbosity in argparse;
    ``count=True`` cannot also implement ``--quiet``.
    """

    #: Maps an option string to the delta it applies.
    STEPS = {"v": +1, "q": -1}

    def __init__(self, option_strings, dest, *, default: int = 0, **kwargs):
        kwargs.pop("nargs", None)
        super().__init__(option_strings, dest, nargs=0, default=default, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        base = getattr(namespace, self.dest, None)
        if base is None:
            base = self.default if self.default is not None else 0
        delta = self.STEPS.get((option_string or "").lstrip("-"), +1)
        setattr(namespace, self.dest, base + delta)


class VerboseCountAction(argparse.Action):
    """``action='verbose_count'``: repeated ``-v`` up to a ceiling, clamped.

    The clamp matters: ``-vvvv`` should not become DEBUG+3, which is not a
    level any ``logging`` call understands.
    """

    def __init__(self, option_strings, dest, *, levels: Sequence[int] | None = None,
                 default: int = 0, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=default, **kwargs)
        self.levels = tuple(levels) if levels else (10, 20, 30, 40, 50)

    def __call__(self, parser, namespace, values, option_string=None):
        current = getattr(namespace, self.dest, None) or self.default or 0
        index = min(current + 1, len(self.levels) - 1)
        setattr(namespace, self.dest, index)


# --------------------------------------------------------------------------- #
# Validation actions (positional or optional)
# --------------------------------------------------------------------------- #


class ExistingFileAction(argparse.Action):
    """Validate existence at parse time; works for positionals too.

    Note ``nargs=None``: the value is a plain string here, so argparse passes
    ``values`` as a string rather than a list. An action that assumes a list
    silently breaks the moment someone switches it to ``nargs='?'``.
    """

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.pop("type", None)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, _validate(parser, self, values, "file", option_string))


class ExistingDirAction(ExistingFileAction):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, _validate(parser, self, values, "dir", option_string))



class RangeAction(argparse.Action):
    """Accept ``--bounds 0 10``, ``--bounds 0-10``, or the open ``--bounds 5-``.

    Three token shapes for one flag. ``type=`` cannot do this -- it sees one
    token at a time and never learns whether a sibling is coming, and
    ``nargs=2`` cannot do it either because ``0-10`` is a *single* argv token.

    ``nargs='+'`` is what makes the shapes work; the action then folds whatever
    it received down to exactly two bounds. Declaring ``nargs=2`` here would
    have rejected the one-token form at the argparse layer, before
    ``__call__`` ever ran.

    Accepted: ``0 10``, ``0-10``, ``0-`` (open upper), ``-2-`` (negative lower,
    open upper), and ``5`` (lower bound only, when ``low`` supplies the rest).
    Because argparse rejects bare tokens that begin with ``-`` unless they look
    like negative numbers, a negative lower bound must use the ``=`` form:
    ``--bounds=-2-``.
    """

    #: One token: ``low<sep>high``, either side optionally signed and open.
    _PAIR = re.compile(r"^(?P<low>-?\d[\d.]*)?(?P<sep>-)(?P<high>-?\d[\d.]*)?$")

    def __init__(self, option_strings, dest, *, low: float | None = None,
                 high: float | None = None, number: Callable[[str], Any] = float,
                 sep: str = "-", **kwargs):
        kwargs.pop("type", None)
        kwargs.pop("nargs", None)
        super().__init__(option_strings, dest, nargs="+", **kwargs)
        self.low = low
        self.high = high
        self.number = number
        self.sep = sep

    def _convert(self, parser, label: str, raw: str, fallback: float | None, what: str):
        text = str(raw).strip()
        if text == self.sep:
            # An explicit separator means "deliberately unbounded", which is a
            # different statement from "the caller said nothing".
            return None
        if not text:
            if fallback is None:
                parser.error(
                    f"argument {label}: a {what} bound is required "
                    f"(write {self.sep} for open-ended)"
                )
            return fallback
        try:
            return self.number(text)
        except (TypeError, ValueError):
            parser.error(f"argument {label}: {raw!r} is not a valid {what} bound")

    def __call__(self, parser, namespace, values, option_string=None):
        label = option_string or self.dest
        tokens = [str(v) for v in values]
        if len(tokens) > 2:
            parser.error(f"argument {label}: expected at most 2 values, got {len(tokens)}")
            return
        if len(tokens) == 1:
            match = self._PAIR.match(tokens[0].strip())
            if match is None:
                # A lone bound, e.g. ``--bounds 5``: the other side falls back.
                low_s, high_s = tokens[0], ""
            else:
                # An empty side means "explicitly unbounded", not "unspecified".
                low_s = match.group("low") or self.sep
                high_s = match.group("high") or self.sep
        else:
            low_s, high_s = tokens
        low = self._convert(parser, label, low_s, self.low, "lower")
        high = self._convert(parser, label, high_s, self.high, "upper")
        if low is None and high is None:
            parser.error(f"argument {label}: both bounds are open-ended, which matches nothing")
            return
        if low is not None and high is not None and low > high:
            parser.error(f"argument {label}: lower bound {low} exceeds upper bound {high}")
            return
        setattr(namespace, self.dest, (low, high))


class ThresholdAction(argparse.Action):
    """``--warning 80 --critical 95`` -- several flags, one shared mapping.

    argparse derives ``dest`` from each flag independently, so two flags never
    collide by accident *or* share by default. This action deliberately writes
    to a single ``into`` attribute chosen by the caller, keyed by
    ``threshold_name``, which is how you get ``--warn-level``/``--crit-level``
    to accumulate into one dict.
    """

    def __init__(self, option_strings, dest, *, threshold_name: str | None = None,
                 into: str = "thresholds", **kwargs):
        self.threshold_name = threshold_name
        self.into = into
        kwargs.pop("type", None)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        current = dict(getattr(namespace, self.into, None) or {})
        current[self.threshold_name or self.dest] = values
        setattr(namespace, self.into, current)


class FormatTemplateAction(argparse.Action):
    """``--write-out '{http_code} %{size_download}'`` (curl-compatible).

    The value is validated by *building* a one-shot parser over the known
    placeholders: unknown names become a usage error at parse time rather than
    a literal ``{typo}`` in the output at run time. Supports ``%{json}`` too.
    """

    FIELDS: dict[str, str] = {
        "url_effective": "str", "http_code": "int", "num_redirects": "int",
        "size_download": "int", "size_upload": "int", "size_header": "int",
        "time_total": "float", "time_namelookup": "float", "time_connect": "float",
        "time_starttransfer": "float", "speed_download": "float", "errormsg": "str",
        "json": "json", "stderr": "str", "hostname": "str", "local_ip": "str",
        "num_connects": "int", "scheme": "str", "content_type": "str",
    }

    _TOKEN = re.compile(r"%(?:\{(\w+)\}|(\w+)|(\d?[.\-]*\w))")

    def __call__(self, parser, namespace, values, option_string=None):
        template = str(values)
        for match in self._TOKEN.finditer(template):
            name = match.group(1) or match.group(2)
            if name and name not in self.FIELDS:
                parser.error(
                    f"argument {option_string or self.dest}: unknown write-out "
                    f"variable {name!r} (known: {', '.join(sorted(self.FIELDS))})"
                )
        setattr(namespace, self.dest, template)


# --------------------------------------------------------------------------- #
# Filesystem output
# --------------------------------------------------------------------------- #


class LazyOutputFile:
    """A file handle that does not exist until something is written to it.

    Opening eagerly at parse time breaks the very common
    ``--output out.json --help`` and ``cmd --dry-run --output out.json``
    combinations, and it litters empty files when a command fails early. This
    wrapper defers creation to the first write and creates the parent
    directory only then.
    """

    def __init__(self, path: str | os.PathLike[str], *, mode: str = "w",
                 encoding: str = "utf-8", append: bool = False,
                 on_error: Callable[[Exception], None] | None = None) -> None:
        self.path = pathlib.Path(path)
        self.mode = mode
        self.encoding = encoding
        self.append = append
        self._on_error = on_error
        self._handle: IO[str] | None = None
        self._opened = False
        self.bytes_written = 0
        self.lines_written = 0

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def opened(self) -> bool:
        return self._opened

    def _open(self) -> IO[str]:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            mode = ("a" if self.append else "w") + ("" if "b" in self.mode else "t")
            # No ``with`` block: this object *is* the context manager and owns
            # the handle for its whole lifetime. Closing here would defeat the
            # laziness that is the whole point of the class.
            self._handle = open(  # noqa: SIM115
                self.path, mode, encoding=None if "b" in self.mode else self.encoding
            )
            self._opened = True
        return self._handle

    def write(self, data: str) -> int:
        handle = self._open()
        written = handle.write(data)
        self.bytes_written += written
        self.lines_written += data.count("\n")
        return written

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> LazyOutputFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self):
        return iter(self._open())

    def __repr__(self) -> str:
        state = "open" if self._opened else "pending"
        return f"<LazyOutputFile {self.path} {state}>"

    # -- reporting ---------------------------------------------------------- #

    def summary(self) -> str:
        if not self._opened:
            return f"{self.path} (never written)"
        return f"{self.path} ({self.lines_written} lines, {self.bytes_written} bytes)"


class OutputFileAction(argparse.Action):
    """``action='output_file'``: a :class:`LazyOutputFile` in the namespace.

    ``-`` maps to :data:`sys.stdout`, which is what makes
    ``--output -`` behave identically to omitting the flag.
    """

    def __init__(self, option_strings, dest, *, encoding: str = "utf-8",
                 append: bool = False, **kwargs):
        kwargs.pop("type", None)
        super().__init__(option_strings, dest, **kwargs)
        self.encoding = encoding
        self.append = append

    def __call__(self, parser, namespace, values, option_string=None):
        if values == "-":
            setattr(namespace, self.dest, sys.stdout)
            setattr(namespace, f"{self.dest}_path", "-")
            return
        path = pathlib.Path(str(values)).expanduser()
        if path.is_dir():
            parser.error(f"argument {option_string or self.dest}: {path} is a directory")
        setattr(
            namespace,
            self.dest,
            LazyOutputFile(path, encoding=self.encoding, append=self.append),
        )
        setattr(namespace, f"{self.dest}_path", path)


class AtomicOutput:
    """Write to a temporary sibling, then rename -- all-or-nothing.

    ``os.replace`` is atomic within a filesystem, so a crash mid-write can
    never leave a half-written output file where a complete one is expected.
    This is the difference between "the build failed" and "the build silently
    corrupted last week's report".
    """

    def __init__(self, path: pathlib.Path, *, encoding: str = "utf-8") -> None:
        self.path = pathlib.Path(path)
        self.encoding = encoding
        self._tmp: pathlib.Path | None = None
        self._handle: IO[str] | None = None

    def __enter__(self) -> IO[str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        self._handle = open(self._tmp, "w", encoding=self.encoding)
        return self._handle

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            self._handle.close()
        if exc_type is None and self._tmp is not None:
            self._tmp.replace(self.path)
            self._tmp = None
        elif self._tmp is not None:
            self._tmp.unlink(missing_ok=True)
            self._tmp = None


# --------------------------------------------------------------------------- #
# Deprecation, environment, secrets
# --------------------------------------------------------------------------- #


class DeprecatedAction(argparse.Action):
    """Warn (and keep working) when a deprecated flag is used.

    Python 3.13 added a native ``deprecated=`` keyword to
    ``add_parser``. It only covers *subcommands*, so this action remains the
    answer for deprecated *options* -- and it does more: it reports the
    replacement and the removal version, which is all a user actually needs.
    """

    def __init__(self, option_strings, dest, *, message: str = "",
                 replacement: str | None = None, removed_in: str | None = None, **kwargs):
        self.deprecation_message = message
        self.replacement = replacement
        self.removed_in = removed_in
        # A deprecated flag takes no value: it is accepted for compatibility
        # and ignored. Without nargs=0 argparse would insist on a value and
        # reject the very invocation the deprecation is meant to keep working.
        kwargs["nargs"] = 0
        kwargs.setdefault("default", None)
        kwargs.setdefault("help", argparse.SUPPRESS)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        flag = option_string or ", ".join(self.option_strings)
        text = self.deprecation_message or f"{flag} is deprecated"
        if self.replacement:
            text += f"; use {self.replacement} instead"
        if self.removed_in:
            text += f" (removed in {self.removed_in})"
        warnings.warn(text, DeprecationWarning, stacklevel=2)
        setattr(namespace, self.dest, values)


class EnvFallbackAction(argparse.Action):
    """``action='env_fallback'``: environment variable as the default source.

    Precedence is always *flag > environment*. argparse has no native
    mechanism for this. ``default=os.environ.get(...)`` is evaluated while the
    parser is being **constructed**, which is too early: at that point the
    program has not yet read ``--config``, so a config-file value can never be
    overridden by an explicit flag, and ``os.environ`` is snapshotted before
    any test can patch it.

    So this action is a *marker*. It records the variable name on the action and
    leaves the parsed value as ``None``; :func:`apply_env_fallbacks` then fills
    it in after parsing, which is the only point at which the correct
    precedence is knowable.
    """

    def __init__(self, option_strings, dest, *, env: str | None = None,
                 environ: dict[str, str] | None = None, **kwargs):
        self.env = env
        self.environ = environ
        # ``type`` is deliberately *not* dropped: it is what lets the
        # environment value be validated and converted by the same converter
        # the flag uses, so $PORT=8080 yields an int rather than a string.
        kwargs.setdefault("default", None)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)


def apply_env_fallbacks(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
    environ: dict[str, str] | None = None,
) -> argparse.Namespace:
    """Resolve every :class:`EnvFallbackAction` on *parser* from *environ*.

    Only fills ``None`` values, so an explicit flag always wins. Type
    conversion happens here rather than at construction time so that the
    same converter used by ``type=`` validates the environment too.
    """
    env = environ if environ is not None else os.environ
    for action in parser._actions:
        if not isinstance(action, EnvFallbackAction) or not action.env:
            continue
        if getattr(namespace, action.dest, None) is not None:
            continue
        source = action.environ if action.environ is not None else env
        raw = source.get(action.env)
        if raw is None:
            continue
        if action.type is not None:
            try:
                setattr(namespace, action.dest, action.type(raw))
                continue
            except (TypeError, ValueError, argparse.ArgumentTypeError) as exc:
                setattr(
                    namespace,
                    f"{action.dest}_error",
                    f"${action.env}: {exc}",
                )
                continue
        setattr(namespace, action.dest, raw)
    return namespace


class SecretValue(str):
    """A ``str`` that refuses to reveal itself in logs and ``repr`` output.

    Subclassing ``str`` keeps it usable everywhere a string is expected while
    ``__repr__`` and ``__format__`` redact, so an accidental
    ``print(config)`` cannot leak a credential.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return f"<SecretValue {len(self)} chars>"

    def __format__(self, spec: str) -> str:
        return format("<redacted>", spec)

    def reveal(self) -> str:
        """Explicitly unwrap the secret. Call sites should be auditable."""
        return str.__str__(self)


# --------------------------------------------------------------------------- #
# Pass-through
# --------------------------------------------------------------------------- #


class RemainderAction(argparse.Action):
    """``argparse.REMAINDER`` as a named action, with a safe default.

    Declare it as a **positional**::

        p.add_argument("passthrough", action="remainder")
        p.add_argument("cmd")
        # anvil run -- cmd --flag   ->  passthrough == ["--", "--flag"]

    ``REMAINDER`` stops option parsing permanently, which is what ``docker
    run`` and ``kubectl exec`` depend on. Two gotchas handled here:

    * the default is a *shared mutable list* unless one is supplied, so two
      parses in the same process contaminate each other;
    * the ``--`` separator may or may not survive into ``values`` depending on
      the Python version and on whether the user typed it, so it is stripped
      explicitly rather than guessed at.

    It cannot be an *option*: argparse consumes ``--`` itself and never
    dispatches an action for it.
    """

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.pop("nargs", None)
        kwargs.setdefault("default", None)
        super().__init__(option_strings, dest, nargs=argparse.REMAINDER, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        items = list(values) if isinstance(values, (list, tuple)) else [values]
        setattr(namespace, self.dest, [v for v in items if v != "--"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _validate(parser, action, values, kind: str, option_string: str | None):
    """Shared existence/readability check for the validation actions."""
    label = option_string or action.dest
    raw = values[0] if isinstance(values, list) and len(values) == 1 else values
    path = pathlib.Path(str(raw)).expanduser()
    if not path.exists():
        parser.error(f"argument {label}: {path} does not exist")
    if kind == "file" and path.is_dir():
        parser.error(f"argument {label}: {path} is a directory, not a file")
    if kind == "dir" and not path.is_dir():
        parser.error(f"argument {label}: {path} is not a directory")
    if not os.access(path, os.R_OK):
        parser.error(f"argument {label}: {path} is not readable")
    return path.resolve()


def convert_string_defaults(
    parser: argparse.ArgumentParser, namespace: argparse.Namespace
) -> argparse.Namespace:
    """Apply each action's ``type=`` to a *string* default.

    argparse does not convert defaults -- ``--timeout`` with
    ``default="30s"`` yields the string ``"30s"`` unless the flag is given.
    That mismatch is why every hand-rolled CLI ends up writing
    ``default=duration("30s")``, which then renders as ``default: 30.0`` in
    ``--help`` because the converted number has lost its original spelling.

    Converting here gets both: the namespace holds a real ``float`` and the
    help text still shows ``default: 30s``, because ``action.default`` is left
    as the author's string. Runs after parsing, so an explicitly supplied value
    is never touched.
    """
    for action in parser._actions:
        if action.type is None or action.default is None:
            continue
        current = getattr(namespace, action.dest, None)
        if not isinstance(current, str):
            continue
        if action.nargs in ("*", "+") or isinstance(action.default, (list, tuple)):
            continue
        try:
            setattr(namespace, action.dest, action.type(current))
        except (TypeError, ValueError, argparse.ArgumentTypeError):
            # A bad default is a programming error in the CLI, not user input;
            # leave it alone so the command reports it in its own terms.
            continue
    return namespace


def confirm(prompt: str, *, default: bool = False, assume_yes: bool = False,
            stream: IO[str] | None = None) -> bool:
    """A yes/no prompt that survives EOF and honours ``--yes``.

    Every interactive prompt in a CLI needs the same three escapes: EOF must
    not raise ``EOFError``, ``--yes`` must short-circuit, and a non-TTY stdin
    must use the default instead of hanging forever on a pipe.
    """
    if assume_yes:
        return True
    out = stream if stream is not None else sys.stderr
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        if not sys.stdin.isatty():
            out.write(f"{prompt} {suffix} (assuming {'yes' if default else 'no'}: not a terminal)\n")
            return default
        out.write(f"{prompt} {suffix} ")
        out.flush()
        answer = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        out.write("\n")
        return False
    if not answer:
        return default
    answer = answer.strip().lower()
    if not answer:
        return default
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    return default


def prompt_password(prompt: str = "Password: ") -> str:
    """Read a secret without echoing it."""
    try:
        return getpass.getpass(prompt, stream=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        raise argparse.ArgumentTypeError("no password supplied") from None


# --------------------------------------------------------------------------- #
# Registration shorthand: parser.register('action', 'name', Class)
# --------------------------------------------------------------------------- #

EXTRAS: dict[str, type[argparse.Action]] = {
    "append_unique": AppendUniqueAction,
    "comma_list": CommaListAction,
    "key_value": KeyValueAction,
    "auto_name": AutoNameAction,
    "pattern_list": PatternListAction,
    "verbosity": VerbosityAction,
    "verbose_count": VerboseCountAction,
    "existing_file": ExistingFileAction,
    "existing_dir": ExistingDirAction,
    "range": RangeAction,
    "threshold": ThresholdAction,
    "write_out": FormatTemplateAction,
    "output_file": OutputFileAction,
    "deprecated": DeprecatedAction,
    "env_fallback": EnvFallbackAction,
    "remainder": RemainderAction,
}


def register_all(parser: argparse._ActionsContainer) -> argparse._ActionsContainer:
    """Register every action in :data:`EXTRAS` on *parser* (idempotent).

    Written against ``_registries`` directly rather than ``.register()``
    because the two are incompatible across versions: up to 3.13 ``register``
    is a *classmethod* backed by one class-level dict, and in 3.14 it became
    an *instance* method backed by a per-instance dict populated in
    ``__init__``. Touching the mapping works on both.

    Must be called after ``__init__`` (which installs the built-in actions) and
    uses ``setdefault`` so a name never clobbers a stock argparse action.
    """
    registry = parser._registries.setdefault("action", {})
    for name, cls in EXTRAS.items():
        registry.setdefault(name, cls)
    return parser
