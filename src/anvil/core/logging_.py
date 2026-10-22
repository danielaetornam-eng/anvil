"""Logging configuration driven entirely by CLI flags.

The interesting decision is that a ``logging.Handler`` is installed on *stderr*
only, and the default level is WARNING rather than INFO. A CLI that prints
progress chatter to stdout has already broken every pipeline that uses it;
reserving stderr for diagnostics is the only arrangement that lets
``anvil json query ... > out.json`` be correct.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import time
from typing import IO

from . import term
from .types import log_level as log_level_type

__all__ = [
    "Timer",
    "add_logging_arguments",
    "level_for",
    "setup_logging",
]

LOGGER_NAME = "anvil"
_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRIT",
}


class _Formatter(logging.Formatter):
    """``HH:MM:SS LEVEL message`` with the level colourised when possible."""

    def __init__(self, *, color: bool = True, timestamps: bool = True) -> None:
        super().__init__()
        self.color = color
        self.timestamps = timestamps

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        level = _LEVEL_NAMES.get(record.levelno, record.levelname)
        painted = (
            term.A.gray(level) if record.levelno >= logging.ERROR else term.A.cyan(level)
        ) if self.color else level
        if self.timestamps:
            stamp = term.A.gray(time.strftime("%H:%M:%S", time.localtime(record.created)))
            return f"{stamp} {painted} {message}"
        return f"{painted} {message}"


def add_logging_arguments(parser: argparse.ArgumentParser, *,
                          include_verbosity: bool = True) -> None:
    """Add the global diagnostics flags to *parser*.

    Kept here so every entry point -- including a plugin that builds its own
    parser -- gets an identical diagnostics vocabulary.
    """
    group = parser.add_argument_group("Diagnostics")
    if include_verbosity:
        group.add_argument(
            "-v", "--verbose", "-q", "--quiet",
            action="verbosity", dest="verbosity", default=0,
            help="repeat for more detail (-vv is debug, -q silences)",
        )
    group.add_argument(
        "--log-level", type=log_level_type, default=None, metavar="LEVEL",
        help="minimum level to report", anvil_env="ANVIL_LOG_LEVEL",
    )
    group.add_argument(
        "--log-format", choices=("auto", "plain", "json"), default="auto",
        help="log record format",
    )
    group.add_argument(
        "--log-file", default=None, metavar="PATH",
        help="also append records to a file",
    )
    group.add_argument(
        "--show-env", action="store_true",
        help="record the parsed namespace (may contain secrets)",
    )
    group.add_argument(
        "--trace", action="store_true",
        help="re-raise unexpected exceptions with a full traceback",
    )
    group.add_argument(
        "--profile", action="store_true",
        help="print import and execution timings to stderr on exit",
    )


_VERBOSITY_TO_LEVEL = {
    -3: logging.CRITICAL + 10,
    -2: logging.CRITICAL,
    -1: logging.ERROR,
    0: logging.WARNING,
    1: logging.INFO,
    2: logging.DEBUG,
    3: logging.DEBUG,
}


def level_for(verbosity: int = 0, explicit: int | None = None) -> int:
    """Resolve an effective level from verbosity and an explicit override.

    An explicit ``--log-level`` always wins; verbosity only decides the level
    when no explicit level was given. That precedence is the difference between
    ``-v --log-level error`` doing what it looks like it does and not.
    """
    if explicit is not None:
        return int(explicit)
    if verbosity <= -3:
        return logging.CRITICAL + 10
    if verbosity == -2:
        return logging.CRITICAL
    if verbosity == -1:
        return logging.ERROR
    if verbosity == 0:
        return logging.WARNING
    return logging.DEBUG


def setup_logging(
    *,
    verbosity: int = 0,
    level: int | None = None,
    log_format: str = "auto",
    log_file: str | pathlib.Path | None = None,
    stream: IO[str] | None = None,
    color: bool | None = None,
    timestamps: bool = True,
) -> logging.Logger:
    """Install handlers and return the application logger.

    Idempotent: calling it twice replaces the handlers rather than duplicating
    every line, which is what happens when a command re-initialises after
    parsing ``--log-level`` from a config file.
    """
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with __import__("contextlib").suppress(Exception):
            handler.close()

    resolved = level_for(verbosity, level)
    logger.setLevel(resolved)
    logger.propagate = False

    err = stream if stream is not None else sys.stderr
    use_color = term.supports_color(err) if color is None else color

    if log_format == "json":
        handler: logging.Handler = _JsonHandler(err)
    else:
        handler = logging.StreamHandler(err)
        handler.setFormatter(
            _Formatter(color=use_color,
                       timestamps=timestamps and log_format == "auto")
        )
    handler.setLevel(resolved)
    logger.addHandler(handler)

    if log_file:
        path = pathlib.Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(resolved)
        # Always plain and always timestamped: log files get `cat`, `grep` and
        # shipped to a collector, none of which can parse escape codes.
        file_handler.setFormatter(_Formatter(color=False, timestamps=True))
        logger.addHandler(file_handler)

    return logger


class _JsonHandler(logging.Handler):
    """Emit one JSON object per record, for log shippers."""

    def __init__(self, stream: IO[str]) -> None:
        super().__init__()
        self.stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        import json

        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        self.stream.write(json.dumps(payload, default=str) + "\n")


def get_logger(suffix: str | None = None) -> logging.Logger:
    """Return the application logger, optionally a named child."""
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)


class Timer:
    """Measure a block and report through the logger.

    ``with Timer("http request") as t: ...; t.seconds`` -- the elapsed time is
    always available even if the block raised, which is what you need when the
    interesting case is the slow failure.
    """

    def __init__(self, label: str, *, logger: logging.Logger | None = None) -> None:
        self.label = label
        self.logger = logger or get_logger()
        self.seconds = 0.0
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self._start
        self.logger.debug("%s took %s", self.label, term.human_duration(self.seconds))

    def __float__(self) -> float:
        return self.seconds
