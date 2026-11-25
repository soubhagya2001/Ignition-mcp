"""Compatibility shim for the MCP Python SDK.

The SDK renamed ``FastMCP`` to ``MCPServer`` in v2 (``mcp.server.mcpserver``) and removed
``mcp.server.fastmcp``. Modules should import the server type from here so the project works
with both major versions.

Example:
    >>> from ignition_mcp.mcp_compat import MCPServer, create_server
    >>> mcp = create_server("ignition-mcp")
"""

from __future__ import annotations

try:  # MCP SDK >= 2.0
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef, attr-defined]

__all__ = ["MCPServer", "create_server", "normalize_transport"]


def create_server(name: str, version: str = "") -> MCPServer:
    """Create an MCP server instance using whichever SDK API is installed.

    Args:
        name: Server name advertised during initialization.
        version: Optional server version string (empty lets the SDK decide).

    Returns:
        A configured ``MCPServer`` (SDK v2) or ``FastMCP`` (SDK v1).
    """
    try:
        return MCPServer(name, version=version)
    except TypeError:  # pragma: no cover - SDK v1 signature differences
        return MCPServer(name)


def normalize_transport(transport: str) -> str:
    """Map CLI transport names onto the SDK's transport literals.

    Args:
        transport: One of ``stdio``, ``sse`` or ``http``.

    Returns:
        The transport name understood by the SDK (``http`` becomes ``streamable-http``).
    """
    return "streamable-http" if transport == "http" else transport
