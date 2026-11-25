"""Simulators and test tags (Phase 4, not yet implemented)."""

from __future__ import annotations

from typing import Any

from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import unsupported
from ignition_mcp.state import ServerState

_HINT = "Phase 4: simulator creation is not implemented yet."


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_simulators() -> dict[str, Any]:
        """List simulator devices (not yet implemented)."""
        return unsupported(state, "list_simulators", hint=_HINT, priority="phase4")

    @mcp.tool()
    async def create_simulator(
        name: str, confirm: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Create a simulator device (not yet implemented)."""
        guard = state.write_guard("create_simulator", confirm, dry_run)
        if guard:
            return guard
        return unsupported(state, "create_simulator", hint=_HINT, priority="phase4", name=name)

    @mcp.tool()
    async def create_simulator_tags(
        simulator: str,
        tags: list[dict[str, Any]],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create tags on a simulator (not yet implemented)."""
        guard = state.write_guard("create_simulator_tags", confirm, dry_run)
        if guard:
            return guard
        return unsupported(
            state,
            "create_simulator_tags",
            hint=_HINT,
            priority="phase4",
            simulator=simulator,
            tag_count=len(tags),
        )
