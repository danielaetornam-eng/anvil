"""``python -m anvil`` and the ``anvil`` console script."""

from __future__ import annotations

import sys

from anvil.core.app import main


def entry() -> int:
    """Console-script entry point.

    ``traceback_on_error`` defaults to False: a user-facing CLI should print a
    clean message for expected failures. Setting ``ANVIL_TRACEBACK=1`` in an
    environment where you are debugging gets the traceback back.
    """
    import os

    trace = os.environ.get("ANVIL_TRACEBACK") not in (None, "", "0", "false")
    return main(traceback_on_error=trace)


if __name__ == "__main__":
    sys.exit(entry())


def _main() -> None:
    """Alias used by ``[project.scripts]``."""
    sys.exit(entry())
