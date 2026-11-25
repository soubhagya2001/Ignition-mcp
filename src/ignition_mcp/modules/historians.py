"""Historians and storage providers."""

from __future__ import annotations

import logging
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, unsupported
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

HISTORIAN_CANDIDATES = ("/historians", "/historian/providers")
PROVIDER_CANDIDATES = ("/history/providers", "/historian/providers")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_historians() -> dict[str, Any]:
        """List configured tag historians.

        Returns:
            Historian entries.
        """
        return await _get(state, "list_historians", HISTORIAN_CANDIDATES, "historians")

    @mcp.tool()
    async def get_historian(name: str) -> dict[str, Any]:
        """Get a historian's configuration.

        Args:
            name: Historian name.

        Returns:
            The historian configuration.
        """
        return await _get(
            state,
            "get_historian",
            tuple(f"{path}/{name}" for path in HISTORIAN_CANDIDATES),
            "historian",
        )

    @mcp.tool()
    async def create_historian(
        name: str,
        type: str,
        config: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a historian.

        Args:
            name: Historian name.
            type: Historian type (``SQLITE``, ``MYSQL``, ...).
            config: Historian configuration.
            confirm: Must be ``True`` to create.
            dry_run: Preview without creating.

        Returns:
            Creation acknowledgement or a preview payload.
        """
        guard = state.write_guard("create_historian", confirm, dry_run)
        if guard:
            return guard
        body = {"name": name, "type": type, "config": config}
        if dry_run:
            return state.tool_response({"status": "dry_run", "would_create": body})
        try:
            _, payload = await state.gateway.first_available(
                HISTORIAN_CANDIDATES, method="POST", json_body=body
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "create_historian")
        state.record_action("create_historian", "ok", name=name)
        return state.tool_response({"status": "created", "name": name, "response": payload})

    @mcp.tool()
    async def edit_historian(
        name: str,
        config: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Update a historian.

        Args:
            name: Historian name.
            config: New configuration.
            confirm: Must be ``True`` to update.
            dry_run: Preview without updating.

        Returns:
            Update acknowledgement or a preview payload.
        """
        guard = state.write_guard("edit_historian", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            return state.tool_response({"status": "dry_run", "would_update": name})
        try:
            _, payload = await state.gateway.first_available(
                tuple(f"{path}/{name}" for path in HISTORIAN_CANDIDATES),
                method="PUT",
                json_body=config,
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "edit_historian")
        state.record_action("edit_historian", "ok", name=name)
        return state.tool_response({"status": "updated", "name": name, "response": payload})

    @mcp.tool()
    async def get_historian_status(name: str) -> dict[str, Any]:
        """Check historian health, lag and backfill queue.

        Args:
            name: Historian name.

        Returns:
            Status payload from the gateway.
        """
        return await _get(
            state,
            "get_historian_status",
            tuple(f"{path}/{name}/status" for path in HISTORIAN_CANDIDATES),
            "status",
        )

    @mcp.tool()
    async def list_history_providers() -> dict[str, Any]:
        """List history providers.

        Returns:
            Provider entries.
        """
        return await _get(state, "list_history_providers", PROVIDER_CANDIDATES, "providers")


async def _get(
    state: ServerState, tool: str, candidates: tuple[str, ...], key: str
) -> dict[str, Any]:
    """GET the first available candidate and wrap the result under ``key``."""
    if not state.has_gateway:
        return state.require_connection() or state.tool_response({"status": "not_connected"})
    try:
        _, payload = await state.gateway.first_available(candidates)
    except GatewayError as exc:
        return _gateway_or_unsupported(state, exc, tool)
    items: list[Any] = payload if isinstance(payload, list) else []
    return ok(state, tool, {key: items if items else payload, "count": len(items)})


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint="The historian API is not exposed on this gateway or edition.",
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
