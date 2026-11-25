"""Unified multi-device cloud synchronizer for ~/.agents."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("agents-sync")
except _PackageNotFoundError:  # a source checkout that was never installed
    __version__ = "0.0.0+source"

from . import opdist  # noqa: F401,E402

__all__ = ["opdist"]
