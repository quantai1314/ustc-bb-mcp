"""USTC Blackboard MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of truth for the release version.
    __version__ = version("ustc-bb-mcp")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0+unknown"
