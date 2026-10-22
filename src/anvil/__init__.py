"""anvil -- a production-grade CLI built entirely on argparse."""

from __future__ import annotations

from .core.actions import SecretValue
from .core.errors import ExitCode
from .core.term import Table, Tree

__version__ = "1.0.0"
__all__ = ["ExitCode", "SecretValue", "Table", "Tree", "__version__", "main"]


def main(argv=None, **kwargs) -> int:
    """Console-script shim so ``from anvil import main`` works."""
    from .core.app import main as _main

    return _main(argv, **kwargs)
