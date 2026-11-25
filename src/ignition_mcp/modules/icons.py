"""Icons and assets.

Icon libraries live on the gateway. Usage search runs against local project files so the
agent can answer "where is this icon used?" without the API.
"""

from __future__ import annotations

import logging
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, resolve_project, unsupported
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

ICON_CANDIDATES = ("/icons", "/assets/icons")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_icons(category: str | None = None) -> dict[str, Any]:
        """List available icons.

        Args:
            category: Optional category filter.

        Returns:
            Icon entries.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ICON_CANDIDATES, params={"category": category} if category else None
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "list_icons")
        icons = _items(payload)
        if category:
            icons = [
                icon for icon in icons if str(icon.get("category", "")).lower() == category.lower()
            ]
        return ok(state, "list_icons", {"icons": icons, "count": len(icons)})

    @mcp.tool()
    async def search_icons(query: str) -> dict[str, Any]:
        """Search icons by name or category.

        Args:
            query: Case-insensitive substring.

        Returns:
            Matching icon entries.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(ICON_CANDIDATES)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "search_icons")
        needle = query.lower()
        matches = [
            icon
            for icon in _items(payload)
            if needle in str(icon.get("name", "")).lower()
            or needle in str(icon.get("category", "")).lower()
        ]
        return ok(
            state,
            "search_icons",
            {"query": query, "matches": matches, "count": len(matches)},
        )

    @mcp.tool()
    async def add_icon(
        name: str, svg: str, confirm: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Upload a custom SVG icon.

        Args:
            name: Icon name.
            svg: SVG markup.
            confirm: Must be ``True`` to upload.
            dry_run: Preview without uploading.

        Returns:
            Upload acknowledgement or a preview payload.
        """
        guard = state.write_guard("add_icon", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            return state.tool_response(
                {"status": "dry_run", "would_add": name, "svg_bytes": len(svg.encode("utf-8"))}
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ICON_CANDIDATES, method="POST", json_body={"name": name, "svg": svg}
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "add_icon")
        state.record_action("add_icon", "ok", icon=name)
        return state.tool_response({"status": "created", "icon": name, "response": payload})

    @mcp.tool()
    async def edit_icon(
        name: str, svg: str, confirm: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Replace an existing custom icon.

        Args:
            name: Icon name.
            svg: New SVG markup.
            confirm: Must be ``True`` to update.
            dry_run: Preview without updating.

        Returns:
            Update acknowledgement or a preview payload.
        """
        guard = state.write_guard("edit_icon", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            return state.tool_response(
                {"status": "dry_run", "would_update": name, "svg_bytes": len(svg.encode("utf-8"))}
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                tuple(f"{path}/{name}" for path in ICON_CANDIDATES),
                method="PUT",
                json_body={"svg": svg},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "edit_icon")
        state.record_action("edit_icon", "ok", icon=name)
        return state.tool_response({"status": "updated", "icon": name, "response": payload})

    @mcp.tool()
    async def delete_icon(
        name: str,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete a custom icon.

        Args:
            name: Icon name.
            confirm: Must be ``True`` to delete.
            dry_run: Preview the deletion without changing the gateway.

        Returns:
            Deletion acknowledgement.
        """
        guard = state.write_guard("delete_icon", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "would_delete": name,
                    "gateway": state.session.gateway_url,
                }
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                tuple(f"{path}/{name}" for path in ICON_CANDIDATES), method="DELETE"
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "delete_icon")
        state.record_action("delete_icon", "ok", icon=name)
        return state.tool_response({"status": "deleted", "icon": name, "response": payload})

    @mcp.tool()
    async def get_icon_usage(name: str, project: str | None = None) -> dict[str, Any]:
        """Find project resources that reference an icon.

        Args:
            name: Icon name or path.
            project: Project name; defaults to the active project.

        Returns:
            Matching resources and lines.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "get_icon_usage",
                hint="Icon usage search needs local project files (IGNITION_INSTALL_PATH).",
            )
        chain = reader.inheritance_chain(proj or "")
        matches = reader.search_text(proj or "", name, projects=chain[1:], max_results=300)
        return ok(
            state,
            "get_icon_usage",
            {"icon": name, "project": proj, "matches": matches, "count": len(matches)},
        )


def _items(payload: Any) -> list[dict[str, Any]]:
    """Extract icon entries from a payload."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("icons", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint="The icon library API is not exposed on this gateway.",
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
