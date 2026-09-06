"""consumer-reports-mcp: Consumer Reports ratings as MCP tools."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # pyproject.toml is the single source of truth. Hardcoding the version here meant the
    # number the server advertises to MCP clients drifted from the released one the moment
    # anybody bumped one and not the other — which is exactly what happened (0.1.0 vs 0.1.6).
    __version__ = _version("consumer-reports-mcp")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
