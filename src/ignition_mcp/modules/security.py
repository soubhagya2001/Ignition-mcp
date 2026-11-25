"""Users, security and identity.

User sources, identity providers, roles and audit data all live on the gateway. The audit
log is read-only here; every create/edit call goes through the write guard.
"""

from __future__ import annotations

import logging
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, unsupported
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

USER_SOURCE_CANDIDATES = ("/security/user-sources", "/security/usersources")
IDP_CANDIDATES = ("/security/identity-providers", "/security/identityproviders")
ROLE_CANDIDATES = ("/security/roles",)
AUDIT_CANDIDATES = ("/audit/query", "/security/audit/query")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_user_sources() -> dict[str, Any]:
        """List configured user sources.

        Returns:
            User source entries.
        """
        return await _get(state, "list_user_sources", USER_SOURCE_CANDIDATES, "user_sources")

    @mcp.tool()
    async def get_user_source(name: str) -> dict[str, Any]:
        """Get a user source's configuration.

        Args:
            name: User source name.

        Returns:
            The user source configuration.
        """
        return await _get(
            state,
            "get_user_source",
            tuple(f"{path}/{name}" for path in USER_SOURCE_CANDIDATES),
            "user_source",
        )

    @mcp.tool()
    async def create_user_source(
        name: str,
        type: str,
        config: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a user source.

        Args:
            name: User source name.
            type: Source type (``INTERNAL``, ``AD``, ``DB``, ...).
            config: Source configuration.
            confirm: Must be ``True`` to create.
            dry_run: Preview without creating.

        Returns:
            Creation acknowledgement or a preview payload.
        """
        guard = state.write_guard("create_user_source", confirm, dry_run)
        if guard:
            return guard
        body = {"name": name, "type": type, "config": config}
        if dry_run:
            return state.tool_response({"status": "dry_run", "would_create": body})
        try:
            _, payload = await state.gateway.first_available(
                USER_SOURCE_CANDIDATES, method="POST", json_body=body
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "create_user_source")
        state.record_action("create_user_source", "ok", name=name)
        return state.tool_response({"status": "created", "name": name, "response": payload})

    @mcp.tool()
    async def edit_user_source(
        name: str,
        config: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Update a user source.

        Args:
            name: User source name.
            config: New configuration.
            confirm: Must be ``True`` to update.
            dry_run: Preview without updating.

        Returns:
            Update acknowledgement or a preview payload.
        """
        guard = state.write_guard("edit_user_source", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            return state.tool_response({"status": "dry_run", "would_update": name})
        try:
            _, payload = await state.gateway.first_available(
                tuple(f"{path}/{name}" for path in USER_SOURCE_CANDIDATES),
                method="PUT",
                json_body=config,
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "edit_user_source")
        state.record_action("edit_user_source", "ok", name=name)
        return state.tool_response({"status": "updated", "name": name, "response": payload})

    @mcp.tool()
    async def list_identity_providers() -> dict[str, Any]:
        """List identity providers (SAML, OIDC, LDAP).

        Returns:
            Identity provider entries.
        """
        return await _get(state, "list_identity_providers", IDP_CANDIDATES, "identity_providers")

    @mcp.tool()
    async def get_identity_provider(name: str) -> dict[str, Any]:
        """Get an identity provider's configuration.

        Args:
            name: Identity provider name.

        Returns:
            The provider configuration.
        """
        return await _get(
            state,
            "get_identity_provider",
            tuple(f"{path}/{name}" for path in IDP_CANDIDATES),
            "identity_provider",
        )

    @mcp.tool()
    async def create_identity_provider(
        name: str,
        type: str,
        config: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create an identity provider.

        Args:
            name: Provider name.
            type: Provider type (``SAML``, ``OIDC``, ``LDAP``).
            config: Provider configuration.
            confirm: Must be ``True`` to create.
            dry_run: Preview without creating.

        Returns:
            Creation acknowledgement or a preview payload.
        """
        guard = state.write_guard("create_identity_provider", confirm, dry_run)
        if guard:
            return guard
        body = {"name": name, "type": type, "config": config}
        if dry_run:
            return state.tool_response({"status": "dry_run", "would_create": body})
        try:
            _, payload = await state.gateway.first_available(
                IDP_CANDIDATES, method="POST", json_body=body
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "create_identity_provider")
        state.record_action("create_identity_provider", "ok", name=name)
        return state.tool_response({"status": "created", "name": name, "response": payload})

    @mcp.tool()
    async def edit_identity_provider(
        name: str,
        config: dict[str, Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Update an identity provider.

        Args:
            name: Provider name.
            config: New configuration.
            confirm: Must be ``True`` to update.
            dry_run: Preview without updating.

        Returns:
            Update acknowledgement or a preview payload.
        """
        guard = state.write_guard("edit_identity_provider", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            return state.tool_response({"status": "dry_run", "would_update": name})
        try:
            _, payload = await state.gateway.first_available(
                tuple(f"{path}/{name}" for path in IDP_CANDIDATES),
                method="PUT",
                json_body=config,
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "edit_identity_provider")
        state.record_action("edit_identity_provider", "ok", name=name)
        return state.tool_response({"status": "updated", "name": name, "response": payload})

    @mcp.tool()
    async def list_roles() -> dict[str, Any]:
        """List security roles.

        Returns:
            Role entries.
        """
        return await _get(state, "list_roles", ROLE_CANDIDATES, "roles")

    @mcp.tool()
    async def get_audit_profile_data(
        start_date: str | None = None,
        end_date: str | None = None,
        actor: str = "*",
        action: str = "*",
        max_results: int = 500,
    ) -> dict[str, Any]:
        """Read audit log entries.

        Args:
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            actor: Actor filter (``*`` for all).
            action: Action filter (``*`` for all).
            max_results: Maximum entries.

        Returns:
            Audit entries.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "actor": actor,
            "action": action,
            "maxResults": max_results,
        }
        try:
            _, payload = await state.gateway.first_available(
                AUDIT_CANDIDATES, method="POST", json_body=body
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "get_audit_profile_data")
        entries = _items(payload)
        return ok(
            state,
            "get_audit_profile_data",
            {"entries": entries, "count": len(entries), "raw": payload},
        )


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
    items = _items(payload)
    return ok(state, tool, {key: items if items else payload, "count": len(items)})


def _items(payload: Any) -> list[Any]:
    """Extract a list from a security payload."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "userSources", "identityProviders", "roles"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint="The security API is not exposed on this gateway or edition.",
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
