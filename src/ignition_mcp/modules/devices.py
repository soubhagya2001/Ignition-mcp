"""Devices and OPC-UA (Phase 4, not yet implemented).

Device and OPC browsing require the gateway's device API, which is edition-dependent. The
tools are registered so the agent can discover them, but return an explicit ``unsupported``
payload rather than guessing.
"""

from __future__ import annotations

from typing import Any

from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import unsupported
from ignition_mcp.state import ServerState

_HINT = "Phase 4: device/OPC-UA browsing is not implemented yet."


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_devices() -> dict[str, Any]:
        """List configured devices (not yet implemented)."""
        return unsupported(state, "list_devices", hint=_HINT, priority="phase4")

    @mcp.tool()
    async def get_device_status(device_name: str) -> dict[str, Any]:
        """Get a device's connection status (not yet implemented)."""
        return unsupported(
            state, "get_device_status", hint=_HINT, priority="phase4", device=device_name
        )

    @mcp.tool()
    async def browse_opc_server(server_name: str, node_id: str = "i=84") -> dict[str, Any]:
        """Browse an OPC-UA address space (not yet implemented)."""
        return unsupported(
            state, "browse_opc_server", hint=_HINT, priority="phase4", server=server_name
        )

    @mcp.tool()
    async def read_opc_node(server_name: str, node_id: str) -> dict[str, Any]:
        """Read an OPC-UA node (not yet implemented)."""
        return unsupported(state, "read_opc_node", hint=_HINT, priority="phase4", node_id=node_id)

    @mcp.tool()
    async def add_device(
        name: str,
        type: str,
        config: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add a device connection (not yet implemented)."""
        guard = state.write_guard("add_device", confirm, dry_run)
        if guard:
            return guard
        return unsupported(state, "add_device", hint=_HINT, priority="phase4", device=name)
