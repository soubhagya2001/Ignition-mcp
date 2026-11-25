"""Shared server state for Ignition MCP.

Every tool receives the same :class:`ServerState` instance (captured in the module
``register()`` closure). It owns:

* the resolved :class:`IgnitionSession` (gateway URL, auth method, projects, capabilities)
* the :class:`~ignition_mcp.client.gateway_api.GatewayClient` used for HTTP calls
* an optional :class:`~ignition_mcp.autoconnect.local_reader.LocalInstallReader`
* a bounded audit log of tool actions, so the agent can review what it did
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ignition_mcp.config import IgnitionMCPConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ignition_mcp.autoconnect.local_reader import LocalInstallReader
    from ignition_mcp.client.gateway_api import GatewayClient

logger = logging.getLogger(__name__)

MAX_AUDIT_ENTRIES = 200


@dataclass
class AuditEntry:
    """A single tool action recorded for the agent's self-audit trail.

    Attributes:
        timestamp: ISO-8601 UTC timestamp.
        tool: Tool name.
        status: Short status such as ``ok``, ``blocked`` or ``gateway_error``.
        detail: Optional structured context (never contains credentials).
    """

    timestamp: str
    tool: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the entry as a plain dict."""
        return {
            "timestamp": self.timestamp,
            "tool": self.tool,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class IgnitionSession:
    """Live Ignition gateway session created by the AutoConnect engine.

    Attributes:
        gateway_url: Resolved gateway base URL.
        username: Authenticated (or anonymous) identity.
        api_token: API key when token auth is used.
        session_cookie: Session cookie value when cookie auth is used.
        token_expires_at: Optional expiry for token-based auth.
        discovery_method: Which discovery step found the gateway.
        auth_method: Which credential strategy succeeded.
        local_install_path: Local Ignition install root, when found.
        local_read_available: Whether project files can be read from disk.
        gateway_version: Gateway version string when known.
        gateway_edition: Gateway edition (Standard/Edge/Maker) when known.
        projects: Known project names.
        active_project: Current project context.
        parent_project: Parent of the active project, when inherited.
        installed_ignition_modules: Installed module names.
        is_connected: Whether a usable gateway connection was resolved.
        auth_verified: Whether authentication was actually confirmed by the gateway.
        readonly: Whether write tools are blocked.
    """

    gateway_url: str = ""
    username: str = ""
    api_token: str | None = None
    session_cookie: str | None = None
    token_expires_at: datetime | None = None

    discovery_method: str = "unknown"
    auth_method: str = "unknown"

    local_install_path: str | None = None
    local_read_available: bool = False

    gateway_version: str = ""
    gateway_edition: str = ""

    projects: list[str] = field(default_factory=list)
    active_project: str | None = None
    parent_project: str | None = None
    installed_ignition_modules: list[str] = field(default_factory=list)

    is_connected: bool = False
    auth_verified: bool = True
    readonly: bool = True

    @property
    def is_token_valid(self) -> bool:
        """Return whether the session credential is still believed valid."""
        if not self.token_expires_at:
            # Cookie/session auth has no local expiry; validity is confirmed by the gateway.
            return True
        return datetime.now() < self.token_expires_at

    def to_ctx(self) -> dict[str, Any]:
        """Build the ``_ctx`` block attached to every tool response.

        Returns:
            A JSON-serialisable context describing the current session.
        """
        warnings: list[str] = []
        if self.token_expires_at:
            remaining = (self.token_expires_at - datetime.now()).total_seconds()
            if remaining < 300:
                warnings.append(f"Session token expires in {int(remaining)}s")
        if not self.is_connected:
            warnings.append(
                "Not connected to a gateway. Set IGNITION_GATEWAY_URL or install Ignition "
                "locally; read-only local tools still work when an install is detected."
            )

        hints: list[str] = []
        if self.parent_project:
            hints.append(
                f"Project '{self.active_project}' inherits from '{self.parent_project}' — "
                "check the parent project for missing resources"
            )
        if self.local_read_available:
            hints.append(
                "Local filesystem read active — views, scripts and queries are read from disk"
            )
        if "Tag Historian" in self.installed_ignition_modules:
            hints.append("Tag Historian installed — history queries are available")
        if self.readonly:
            hints.append("Read-only mode is on; call mcp_enable_writes before writing")
        if not self.auth_verified and self.auth_method == "basic":
            warnings.append(
                "Basic auth was configured but not verified; if a call returns 401, use "
                "IGNITION_USERNAME/PASSWORD against a gateway with session login enabled"
            )

        return {
            "gateway_url": self.gateway_url,
            "gateway_version": self.gateway_version,
            "gateway_edition": self.gateway_edition,
            "connected_via": self.discovery_method,
            "auth_method": self.auth_method,
            "auth_verified": self.auth_verified,
            "session_valid": self.is_connected and self.is_token_valid,
            "session_expires_at": (
                self.token_expires_at.isoformat() if self.token_expires_at else None
            ),
            "active_project": self.active_project,
            "parent_project": self.parent_project,
            "available_projects": self.projects,
            "installed_ignition_modules": self.installed_ignition_modules,
            "local_read_available": self.local_read_available,
            "local_install_path": self.local_install_path,
            "readonly_mode": self.readonly,
            "warnings": warnings,
            "ai_hints": hints,
        }


@dataclass
class ServerState:
    """Top-level state shared by every registered tool.

    Args:
        config: Resolved server configuration.
        session: Session object; replaced by AutoConnect on success.
    """

    config: IgnitionMCPConfig
    session: IgnitionSession = field(default_factory=IgnitionSession)
    audit: list[AuditEntry] = field(default_factory=list, repr=False)
    _gateway: GatewayClient | None = field(default=None, repr=False)

    # -- connections -------------------------------------------------------

    @property
    def gateway(self) -> GatewayClient:
        """Return the gateway HTTP client.

        Raises:
            RuntimeError: If AutoConnect has not produced a client yet.
        """
        if self._gateway is None:
            raise RuntimeError(
                "No gateway client available. Set IGNITION_GATEWAY_URL or call mcp_connect."
            )
        return self._gateway

    @gateway.setter
    def gateway(self, client: GatewayClient | None) -> None:
        self._gateway = client

    @property
    def has_gateway(self) -> bool:
        """Return whether an HTTP client is available."""
        return self._gateway is not None

    @property
    def local_reader(self) -> LocalInstallReader | None:
        """Return a local install reader when local reads are possible, else ``None``."""
        if not self.session.local_read_available or not self.session.local_install_path:
            return None
        from pathlib import Path

        from ignition_mcp.autoconnect.local_reader import LocalInstallReader

        return LocalInstallReader(Path(self.session.local_install_path))

    # -- responses ---------------------------------------------------------

    def tool_response(self, result: Any, **extra_ctx: Any) -> dict[str, Any]:
        """Wrap a tool result with the AI context block.

        Args:
            result: Tool-specific payload.
            **extra_ctx: Extra keys merged into the ``_ctx`` block.

        Returns:
            ``{"result": ..., "_ctx": {...}}``.
        """
        ctx = self.session.to_ctx()
        ctx.update(extra_ctx)
        return {"result": result, "_ctx": ctx}

    def require_connection(self) -> dict[str, Any] | None:
        """Return an error envelope when no gateway connection is available.

        Returns:
            A tool response dict to return early, or ``None`` when connected.
        """
        if self.has_gateway:
            return None
        return self.tool_response(
            {
                "status": "not_connected",
                "message": "No Ignition gateway connection is available.",
                "hint": (
                    "Set IGNITION_GATEWAY_URL (and IGNITION_API_TOKEN), install Ignition on "
                    "this machine, or call mcp_connect to retry discovery."
                ),
            }
        )

    # -- write safety ------------------------------------------------------

    def write_guard(self, operation: str, confirm: bool, dry_run: bool) -> dict[str, Any] | None:
        """Check whether a write may proceed.

        Args:
            operation: Tool name being guarded.
            confirm: The agent's explicit confirmation flag.
            dry_run: Whether the caller only wants a preview.

        Returns:
            ``None`` when the write may proceed; otherwise a tool response to return early.
        """
        if dry_run:
            return None

        if self.session.readonly:
            self.record_action(operation, "blocked", reason="readonly_mode")
            return self.tool_response(
                {
                    "status": "blocked",
                    "reason": "readonly_mode",
                    "operation": operation,
                    "message": (
                        "Write operations are disabled. Set IGNITION_READONLY=false, or call "
                        "mcp_enable_writes for this session."
                    ),
                }
            )

        if not confirm:
            self.record_action(operation, "requires_confirmation")
            return self.tool_response(
                {
                    "status": "requires_confirmation",
                    "operation": operation,
                    "message": (
                        f"Call '{operation}' again with confirm=True to execute this write. "
                        "Use dry_run=True first to preview the change."
                    ),
                }
            )

        return None

    # -- audit -------------------------------------------------------------

    def record_action(self, tool: str, status: str, **detail: Any) -> None:
        """Append an entry to the session audit log.

        Args:
            tool: Tool name.
            status: Short status string.
            **detail: Extra structured context; credentials must never be included.
        """
        self.audit.append(
            AuditEntry(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                tool=tool,
                status=status,
                detail=detail,
            )
        )
        if len(self.audit) > MAX_AUDIT_ENTRIES:
            del self.audit[: len(self.audit) - MAX_AUDIT_ENTRIES]

    def audit_payload(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent audit entries.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            A list of audit entry dicts, newest last.
        """
        return [entry.to_dict() for entry in self.audit[-limit:]]
