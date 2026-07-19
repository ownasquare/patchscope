"""PatchScope package metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("patchscope")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    __version__ = "0.1.1"

__all__ = ["__version__"]
