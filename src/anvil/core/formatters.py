"""Help rendering: a colour-aware formatter with its own layout engine.

Stock argparse help is width-correct but flat. Two things are needed to make
it good, and neither is available from the base class:

1. **Colour, without breaking alignment.** ``HelpFormatter`` measures the
   invocation with ``len()``; injecting ANSI codes makes the column
   arithmetic wrong. The base class grew ``_decolor`` only in 3.14, so this
   module owns its own measurement (:func:`term.visible_width`) and its own
   ``_format_action`` rather than relying on version-specific behaviour.

2. **Per-option annotations.** Every action can carry an environment variable,
   a default, a ``since``/``deprecated`` marker, or a group heading. argparse
   has no slot for any of these, so they are folded into the help string after
   ``%``-expansion.

The layout engine below is deliberately *not* argparse's. It computes widths
itself, then colours, so the two concerns cannot interfere.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence

from . import term

__all__ = [
    "EXAMPLES_HEADING",
    "AnvilHelpFormatter",
    "MarkdownHelpFormatter",
    "PlainHelpFormatter",
    "ShowHelpAction",
    "annotated_help",
    "formatter_class",
    "render_examples",
]

EXAMPLES_HEADING = "Examples"
"""Section heading used when a parser supplies ``examples=[...]``."""

_NUMBERED = re.compile(r"^\d+[.)]\s+")


# --------------------------------------------------------------------------- #
# Help-string decoration
# --------------------------------------------------------------------------- #

_MAX_INLINE = 68


def _is_suppress(value: object) -> bool:
    """True for argparse's SUPPRESS sentinel.

    ``argparse.SUPPRESS`` is a plain ``str`` (``'==SUPPRESS=='``), so
    ``isinstance(value, argparse.SUPPRESS.__class__)`` is true for *every*
    string -- which silently hides the default of every string-valued option.
    Compare by value instead.
    """
    return isinstance(value, str) and value == argparse.SUPPRESS


def _fmt_default(default: object, action: argparse.Action) -> str:
    """Render a default the way a human wants to read it back."""
    if _is_suppress(default):
        return ""
    if isinstance(default, (list, tuple, set, frozenset, dict)):
        if not default:
            return ""
        try:
            shown = ", ".join(str(v) for v in default)
        except Exception:
            shown = str(default)
    elif isinstance(default, str):
        shown = default if default else "(empty)"
    else:
        shown = str(default)
    if len(shown) > _MAX_INLINE:
        shown = shown[: _MAX_INLINE - 1] + "\u2026"
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction,
                           argparse._StoreConstAction, argparse._CountAction,
                           argparse._HelpAction, argparse._VersionAction)):
        return "" if shown == "False" else f"default: {shown}"
    if shown == "None":
        return ""
    return f"default: {shown}"


def annotated_help(action: argparse.Action, color: bool = True) -> str:
    """Build the help string for *action*, including trailing annotations.

    Runs after argparse's ``%``-expansion so ``%(default)s`` in user-written
    help still works. Annotations are appended in a fixed order -- required,
    default, env, choices, deprecation -- and dropped entirely when the help is
    ``SUPPRESS``.

    ``color=False`` must be honoured explicitly rather than reading the
    process-wide palette: help is written to *stdout* while diagnostics go to
    *stderr*, and ``NO_COLOR``/``--output plain`` apply to the two streams
    independently. Reading a global here is how a "plain" help screen ends up
    with stray escape codes in it.
    """

    def tint(name: str, text: str) -> str:
        return term.paint(name, text) if color else text

    raw = action.help
    if raw is argparse.SUPPRESS or raw is None:
        return ""
    text = str(raw)
    annotations: list[str] = []

    # Only *options* get a "required" marker. argparse sets ``required=True``
    # automatically for positionals such as ``nargs='+'``, so annotating them
    # marks nearly every positional as required and tells the reader nothing.
    if action.required and action.option_strings:
        annotations.append(tint("err", "required"))

    default = action.default
    if default is not None and not _is_suppress(default):
        shown = _fmt_default(default, action)
        if shown:
            annotations.append(tint("hint", shown))

    env = getattr(action, "anvil_env", None)
    if env:
        annotations.append(tint("cyan", f"${env}"))

    if action.choices:
        try:
            rendered = ", ".join(str(c) for c in action.choices)
        except TypeError:
            rendered = str(action.choices)
        if len(rendered) > _MAX_INLINE:
            rendered = rendered[: _MAX_INLINE - 1] + "\u2026"
        annotations.append(tint("hint", f"one of: {rendered}"))

    since = getattr(action, "anvil_since", None)
    if since:
        annotations.append(tint("gray", f"since {since}"))

    # ``deprecated=True`` on add_argument landed in 3.13; DeprecatedAction sets
    # its own attributes. Support both so the marker appears either way.
    deprecated = getattr(action, "anvil_deprecated", None)
    if deprecated is None and getattr(action, "deprecated", False):
        deprecated = "no longer maintained"
    if deprecated:
        replacement = getattr(action, "anvil_replacement", None)
        note = f"deprecated: {deprecated}"
        if replacement:
            note += f" -> {replacement}"
        annotations.append(tint("dep", note))

    if not annotations:
        return text
    if not text:
        return " ".join(annotations)
    return f"{text} [{'; '.join(annotations)}]"


# --------------------------------------------------------------------------- #
# Markdown-lite for descriptions and epilogs
# --------------------------------------------------------------------------- #


class _Rich:
    """A minimal Markdown subset rendered to ANSI, with wrapping preserved.

    ``argparse`` gives a description to a raw formatter (no wrapping) or a
    wrapping one (no structure). This keeps the author's line breaks *and*
    renders emphasis, inline code, and bullets.
    """

#: Markdown marker -> SGR style name. Colours are applied through
    #: :func:`term.paint`, not the global palette, so a formatter that has
    #: already decided colour=False stays plain.
    INLINE_STYLES = {"`": "cyan", "*": "bold", "_": "underline"}

    def __init__(self, *, enabled: bool = True, width: int = 80,
                 indent: str = "", color: bool | None = None) -> None:
        self.enabled = enabled
        self.width = width
        self.indent = indent
        self.color = color if color is not None else term.supports_color()

    # -- inline ------------------------------------------------------------- #

    def inline(self, text: str) -> str:
        out: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch in ("`", "*", "_"):
                closer = "``" if ch == "`" and text[i:i + 2] == "``" else ch
                end = text.find(closer, i + len(closer))
                if end != -1:
                    body = text[i + len(closer): end]
                    out.append(self._paint(self.INLINE_STYLES[ch], body))
                    i = end + len(closer)
                    continue
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def _paint(self, style: str, body: str) -> str:
        return term.paint(style, body) if self.color else body

    # -- blocks ------------------------------------------------------------- #

    def render(self, text: str) -> str:
        lines = text.expandtabs().splitlines()
        out: list[str] = []
        in_code = False
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                out.append("")
                continue
            if in_code:
                out.append(self.indent + (term.paint("gray", stripped) if self.color else stripped))
                continue
            if not stripped:
                out.append("")
                continue
            indent = len(raw) - len(raw.lstrip())
            base = self.indent + " " * indent
            marker, body = self._marker(stripped)
            if marker is None:
                out.extend(self._wrap(stripped, base))
                continue
            kind, width = marker
            bullet = (term.paint("cyan", "\u2022") if self.color else "-") + " " if kind == "-" else f"{kind.strip()}) "
            pad = base + " " * width
            wrapped = self._wrap(body, pad, first=base)
            out.append(base + bullet + wrapped[0].lstrip())
            out.extend(pad + line for line in wrapped[1:])
        return "\n".join(out)

    def _marker(self, line: str) -> tuple[tuple[str, int] | None, str]:
        if line.startswith(("- ", "* ", "+ ")):
            return ("-", 2), line[2:]
        numbered = _NUMBERED.match(line)
        if numbered:
            return ("numbered", len(numbered.group(0))), line[numbered.end():]
        return None, line

    def _wrap(self, text: str, pad: str, first: str | None = None) -> list[str]:
        lead = first if first is not None else pad
        width = max(20, self.width - term.visible_width(lead))
        return term.wrap_ansi(
            self.inline(text),
            width,
            initial_indent="",
            subsequent_indent=pad[len(lead):] if pad.startswith(lead) else "",
            break_long_words=True,
        )


# --------------------------------------------------------------------------- #
# The formatter
# --------------------------------------------------------------------------- #


class AnvilHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """The workhorse: colourised, width-aware, annotated argparse help.

    Notable departures from the stock behaviour, all deliberate:

    * ``_format_action`` is reimplemented so widths are measured on *visible*
      characters. The base implementation measures ``len()`` and, on 3.14,
      patches around it; owning the calculation is simpler and works on 3.11.
    * Long help texts wrap under a hanging indent aligned to the description
      column, and the last line is never orphaned from its option.
    * ``--flag VALUE`` renders the option and its metavar in different colours
      so the token boundary is visible at a glance.
    * ``%(default)s``, ``$ENV`` and ``required`` are appended automatically, so
      command authors never have to remember to document them.
    """

    def __init__(self, prog: str, indent_increment: int = 2, max_help_position: int = 34,
                 width: int | None = None, *, color: bool | None = None) -> None:
        if width is None:
            width = min(term.terminal_size()[0], 100)
        self.use_color = color if color is not None else term.supports_color()
        super().__init__(prog, indent_increment, max_help_position, width)

    # -- colour plumbing ---------------------------------------------------- #

    def paint(self, name: str, text: str) -> str:
        return term.paint(name, text) if self.use_color else text

    def rich(self, *, indent: str = "", color: bool | None = None) -> _Rich:
        return _Rich(enabled=self.use_color, width=self._width, indent=indent,
                     color=self.use_color if color is None else color)

    def _expand_help(self, action: argparse.Action) -> str:
        raw = action.help
        if raw is argparse.SUPPRESS or raw is None:
            return ""
        text = str(raw)
        if "%" in text:
            params = dict(vars(action))
            params["prog"] = self._prog
            for key, value in list(params.items()):
                if value is argparse.SUPPRESS:
                    params.pop(key, None)
                elif hasattr(value, "__name__"):
                    params[key] = value.__name__
            if params.get("choices") is not None:
                params["choices"] = ", ".join(str(c) for c in params["choices"])
            try:
                text = text % params
            except (KeyError, ValueError, TypeError):
                # A help string with an unknown %(placeholder)s must not take
                # the whole --help screen down with it.
                pass
        return annotated_help(action, color=self.use_color)

    # -- invocations -------------------------------------------------------- #

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            default = self._get_default_metavar_for_positional(action)
            metavar = " ".join(self._metavar_formatter(action, default)(1))
            return self.paint("cyan", metavar)

        parts: list[str] = []
        for option in action.option_strings:
            if option.startswith("--"):
                parts.append(self.paint("bright_green", option))
            elif option.startswith("-"):
                parts.append(self.paint("green", option))
            else:
                parts.append(self.paint("yellow", option))

        if action.nargs == 0:
            return ", ".join(parts)

        default = self._get_default_metavar_for_optional(action)
        args = self._format_args(action, default)
        return ", ".join(parts) + " " + self.paint("bright_white", args)

    # -- the layout engine -------------------------------------------------- #

    def _format_action(self, action: argparse.Action) -> str:
        help_position = min(self._action_max_length + 2, self._max_help_position)
        help_width = max(self._width - help_position, 24)
        action_width = help_position - self._current_indent - 2
        invocation = self._format_action_invocation(action)
        visible = term.visible_width(invocation)

        parts: list[str] = []
        if not action.help:
            parts.append(f"{' ' * self._current_indent}{invocation}\n")
            for subaction in self._iter_indented_subactions(action):
                parts.append(self._format_action(subaction))
            return self._join(parts)

        if visible <= action_width:
            header = (
                f"{' ' * self._current_indent}"
                f"{invocation}{' ' * (action_width - visible)}  "
            )
            first_indent = 0
        else:
            header = f"{' ' * self._current_indent}{invocation}\n"
            first_indent = help_position

        parts.append(header)

        help_text = self._expand_help(action)
        if help_text:
            lines = term.wrap_ansi(
                help_text,
                help_width,
                initial_indent=" " * first_indent,
                subsequent_indent=" " * help_position,
                break_long_words=True,
            )
            parts.append("\n".join(lines) + "\n")
        else:
            parts.append("\n")

        for subaction in self._iter_indented_subactions(action):
            parts.append(self._format_action(subaction))
        return self._join(parts)

    def _join(self, parts: Sequence[str]) -> str:
        # Every part already ends in a newline, so a plain join is correct.
        # ``_join_parts`` exists in 3.14+ but ``_join`` in earlier versions;
        # doing it inline keeps one code path for all supported Pythons.
        return "".join(p for p in parts if p)

    # -- usage -------------------------------------------------------------- #

    def add_usage(self, usage, actions, groups, prefix=None) -> None:
        if prefix is None:
            prefix = "usage: "
        super().add_usage(usage, actions, groups, prefix)

    def _format_usage(self, usage, actions, groups, prefix) -> str:
        """Colour the stock usage line instead of recomputing it.

        Rewriting the usage line by hand looks tempting and is a trap:
        ``add_subparsers`` calls ``format_usage()`` on the parent and uses the
        result as the ``prog`` prefix for every child parser. A custom
        implementation therefore leaks itself into every subcommand's ``prog``
        (``anvil COMMAND run``), and since that string is recomputed on each
        ``format_usage`` call, the damage compounds. Letting argparse compute
        the layout and post-processing the string is both simpler and immune.
        """
        rendered = super()._format_usage(usage, actions, groups, prefix)
        body = rendered[len(prefix):] if rendered.startswith(prefix) else rendered
        # 3.14 colourises the usage line itself when the formatter has colour
        # enabled. Strip first, then repaint the program name, so the result is
        # identical on 3.11-3.13 (no native colour) and 3.14+ (double colour
        # would otherwise appear as "[97m[97manvil[0m run[0m").
        body = term.strip_ansi(body)
        if self.use_color and body.startswith(self._prog):
            body = self.paint("bright_white", self._prog) + body[len(self._prog):]
        return prefix + body

    # -- text blocks -------------------------------------------------------- #

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        return self.rich(indent=indent).render(text)

    def _format_text(self, text: str) -> str:
        if "%" in text:
            text = text % {"prog": self._prog}
        if text is not argparse.SUPPRESS:
            return self.rich().render(text) + "\n\n"
        return ""

    def _get_rich(self) -> _Rich:
        return self.rich()

    # -- headings ----------------------------------------------------------- #

    def start_section(self, heading: str | None) -> None:
        if heading:
            super().start_section(self.paint("bold", heading.upper()))

    def add_argument(self, action: argparse.Action) -> None:
        """Track the widest invocation using *visible* width.

        The stock implementation does ``max(map(len, invocations))``. Because
        this formatter returns coloured invocation strings, the SGR bytes are
        counted and every description column is pushed far to the right --
        enough to wrap help text two or three times too early. ``_decolor`` was
        only added to the base class in 3.14, so the correction is applied here
        for every supported version.
        """
        if action.help is not argparse.SUPPRESS:
            invocation = self._format_action_invocation(action)
            length = term.visible_width(invocation) + self._current_indent
            self._action_max_length = max(self._action_max_length, length)
            self._add_item(self._format_action, [action])

    def add_arguments(self, actions: Iterable[argparse.Action]) -> None:
        super().add_arguments(actions)


# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #


class PlainHelpFormatter(AnvilHelpFormatter):
    """No colour, no bold: for pipes, ``NO_COLOR``, and golden tests."""

    def __init__(self, prog: str, *args: object, **kwargs: object) -> None:
        kwargs["color"] = False
        super().__init__(prog, *args, **kwargs)  # type: ignore[arg-type]


class MarkdownHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Emits Markdown instead of a terminal screen (for man pages and docs).

    Lets ``anvil docs --format markdown`` generate documentation from the same
    parser tree the terminal help is generated from -- one source of truth.
    """

    def __init__(self, prog: str, indent_increment: int = 2, max_help_position: int = 34,
                 width: int | None = None) -> None:
        super().__init__(prog, indent_increment, max_help_position, width or 100)

    def start_section(self, heading: str | None) -> None:
        if heading:
            super().start_section(f"### {heading}")

    def _get_help_string(self, action) -> str:
        return annotated_help(action, color=False)

    def _format_action(self, action: argparse.Action) -> str:
        invocation = term.strip_ansi(self._format_action_invocation(action))
        help_text = self._expand_help(action)
        if not help_text:
            return f"    `{invocation}`\n"
        parts = [f"    `{invocation}`  \n"]
        for line in help_text.splitlines():
            parts.append(f"    {line}\n")
        return "".join(parts)

    def add_usage(self, usage, actions, groups, prefix=None) -> None:
        super().add_usage(usage, actions, groups, "```\nusage:")

    def format_help(self) -> str:
        return super().format_help() + "```\n"

    def _format_usage(self, usage, actions, groups, prefix) -> str:
        return argparse.RawDescriptionHelpFormatter._format_usage(
            self, usage, actions, groups, prefix
        )


def render_examples(examples: Sequence[str], *, width: int | None = None,
                    color: bool | None = None) -> str:
    """Render a standalone examples block.

    Deliberately *not* part of ``HelpFormatter.format_help``. On Python 3.14
    ``add_subparsers`` computes each child's ``prog`` from the parent's
    ``format_help()`` output, so anything appended there becomes a permanent
    part of every subcommand's program name -- ``anvil run`` would silently
    become ``anvil\n\nEXAMPLES\n  $ ... run``. Keeping the footer outside the
    formatter is what makes ``format_help()`` a pure function again.
    """
    if not examples:
        return ""
    if width is None:
        width = min(term.terminal_size()[0], 100)
    use_color = color if color is not None else term.supports_color()
    heading = (term.paint("bold", EXAMPLES_HEADING.upper()) if use_color
                    else EXAMPLES_HEADING.upper())
    lines = [heading, ""]
    body_width = max(20, width - 4)
    for example in examples:
        prefix = term.paint("gray", "$ ") if use_color else "$ "
        body = term.paint("bright_white", example) if use_color else example
        wrapped = term.wrap_ansi(prefix + body, body_width, subsequent_indent="  ",
                                 break_long_words=True)
        for line in wrapped:
            lines.append("  " + line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class ShowHelpAction(argparse._HelpAction):
    """``-h``/``--help`` that also prints a parser's example block.

    Extends the stock action rather than replacing the rendering path, so
    ``--help`` still goes through ``parser.format_help()`` and every argparse
    feature (sections, usage, epilog) keeps working.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help=None, **kwargs):
        self.examples: Sequence[str] = kwargs.pop("examples", ())
        self.footer: str | None = kwargs.pop("footer", None)
        super().__init__(option_strings, dest, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, argparse.SUPPRESS)
        width = min(term.terminal_size()[0], 100)
        text = parser.format_help()
        extras = [
            render_examples(self.examples or getattr(parser, "examples", ()),
                            width=width),
            self.footer or "",
        ]
        tail = "\n".join(part for part in extras if part).strip("\n")
        if tail:
            text = text.rstrip("\n") + "\n\n" + tail + "\n"
        parser._print_message(text, sys.stdout)
        parser.exit()


def _find_subparsers(actions):
    """Locate the ``_SubParsersAction`` in a flat action list, if any."""
    for action in actions or ():
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def formatter_class(color: bool = True) -> type[argparse.HelpFormatter]:
    """Pick a formatter class appropriate for *color*."""
    return AnvilHelpFormatter if color else PlainHelpFormatter
