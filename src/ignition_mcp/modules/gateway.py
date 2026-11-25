"""Gateway and diagnostics.

Reads gateway health, version, modules, projects, logs and performance. Logs and project
metadata come from the local install whenever possible (no auth); HTTP endpoints are used
only when the gateway is remote or a live value is required.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, unsupported
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

#: Log levels from least to most severe, with the single-letter prefixes used by wrapper.log.
LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
LEVEL_LETTERS = {"D": "DEBUG", "I": "INFO", "W": "WARN", "E": "ERROR", "F": "ERROR"}

#: Runtime trial/license state. Readable without an API token; reset requires an admin session.
TRIAL_PATH = "/data/api/v1/trial"
TRIAL_RESET_SECONDS = 7200

#: Full Gateway backup download. Produces a ``.gwbk`` and needs an admin session.
GATEWAY_BACKUP_PATH = "/data/api/v1/backup"
BACKUP_TIMEOUT_SECONDS = 300.0


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def gateway_status() -> dict[str, Any]:
        """Check gateway health.

        Returns:
            Reachability, raw gateway status, and local-install availability.
        """
        if not state.has_gateway:
            reader = state.local_reader
            if reader is not None:
                info = await reader.read()
                return ok(
                    state,
                    "gateway_status",
                    {
                        "status": "local_only",
                        "message": "Gateway HTTP is unreachable; local filesystem access works.",
                        "install_path": info.install_path,
                        "gateway_url": info.gateway_url,
                        "projects": info.projects,
                    },
                )
            return state.require_connection() or state.tool_response({"status": "not_connected"})

        try:
            payload = await state.gateway.get_json("/data/status")
        except GatewayError as exc:
            return error_response(state, exc, "gateway_status")

        return ok(
            state,
            "gateway_status",
            {
                "reachable": True,
                "gateway_url": state.session.gateway_url,
                "status": payload,
            },
        )

    @mcp.tool()
    async def gateway_trial_status() -> dict[str, Any]:
        """Read the gateway's trial/license state.

        Uses the public ``/data/api/v1/trial`` route, so no API token is needed for the read.
        Trial state is runtime-only and cannot be derived from local project files.

        Returns:
            License mode, trial state, seconds remaining, and expiry/emergency flags.
        """
        if not state.has_gateway:
            return state.tool_response(
                {
                    "status": "unavailable",
                    "message": "Trial state is runtime-only; connect to the gateway to read it.",
                    "local_install": state.session.local_read_available,
                    "hint": "Set IGNITION_GATEWAY_URL or run the MCP on the gateway host.",
                }
            )
        try:
            payload = await _get_trial(state)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_trial_status")
        return ok(
            state,
            "gateway_trial_status",
            {
                "status": "ok",
                "source": "gateway_api",
                **_trial_summary(payload),
                "raw": payload,
            },
        )

    @mcp.tool()
    async def gateway_reset_trial(confirm: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Reset the Ignition trial timer after it has expired.

        Ignition allows unlimited trial resets, but only after expiry. This tool refuses
        pre-emptive resets (which are against the license agreement). No API token is
        required: when the POST needs authentication, it logs in with the configured
        Gateway username/password session.

        Args:
            confirm: Must be ``True`` to reset.
            dry_run: Report whether a reset is currently allowed.

        Returns:
            Reset acknowledgement, a not-expired payload, or an authentication hint.
        """
        guard = state.write_guard("gateway_reset_trial", confirm, dry_run)
        if guard:
            return guard
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})

        try:
            before = await _get_trial(state)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_reset_trial")

        summary = _trial_summary(before)
        if not summary["expired"]:
            if dry_run:
                return state.tool_response({"status": "dry_run", "reset_allowed": False, **summary})
            return state.tool_response(
                {
                    "status": "not_expired",
                    "message": "Trial reset is only allowed after the trial expires.",
                    **summary,
                }
            )

        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "reset_allowed": True,
                    "would_reset_seconds": TRIAL_RESET_SECONDS,
                    **summary,
                }
            )

        body = {**before, "trialSecondsLeft": TRIAL_RESET_SECONDS, "expired": False}
        try:
            response = await _post_trial(state, body)
        except GatewayError as exc:
            if exc.status in (401, 403):
                state.record_action("gateway_reset_trial", "authentication_required")
                return state.tool_response(
                    {
                        "status": "authentication_required",
                        "http_status": exc.status,
                        "message": "The gateway rejected the trial reset.",
                        "hint": (
                            "Set IGNITION_USERNAME/IGNITION_PASSWORD for an administrator "
                            "Gateway session. An API token is not required."
                        ),
                    }
                )
            return _gateway_or_unsupported(state, exc, "gateway_reset_trial")

        after = None
        with contextlib.suppress(GatewayError):
            after = await _get_trial(state)
        after_summary = _trial_summary(after) if after is not None else None
        verified = bool(
            after_summary
            and not after_summary["expired"]
            and after_summary["seconds_remaining"] > 0
        )
        state.record_action("gateway_reset_trial", "ok", verified=verified)
        return state.tool_response(
            {
                "status": "reset",
                "before": summary,
                "after": after_summary,
                "verified": verified,
                "response": response,
            }
        )

    @mcp.tool()
    async def gateway_info() -> dict[str, Any]:
        """Get gateway version, edition, auth method and install details.

        Returns:
            Version/edition information from the session and local install.
        """
        session = state.session
        reader = state.local_reader
        info = await reader.read() if reader is not None else None

        payload: dict[str, Any] = {
            "gateway_url": session.gateway_url,
            "version": session.gateway_version or (info.gateway_version if info else ""),
            "edition": session.gateway_edition,
            "auth_method": session.auth_method,
            "auth_verified": session.auth_verified,
            "connected": session.is_connected,
            "local_install": None,
        }
        if info is not None:
            payload["local_install"] = {
                "install_path": info.install_path,
                "data_path": f"{info.install_path}/data",
                "http_port": info.http_port,
                "https_port": info.https_port,
                "ssl_enabled": info.ssl_enabled,
                "projects_readable_locally": True,
            }
        return ok(state, "gateway_info", payload)

    @mcp.tool()
    async def gateway_modules() -> dict[str, Any]:
        """List installed Ignition modules.

        Returns:
            Module names, from ``config.idb`` locally or the gateway API remotely.
        """
        reader = state.local_reader
        if reader is not None:
            info = await reader.read()
            if info.installed_modules:
                return ok(
                    state,
                    "gateway_modules",
                    {"source": "local_config_db", "modules": info.installed_modules},
                )

        if not state.has_gateway:
            return unsupported(
                state,
                "gateway_modules",
                hint="Set IGNITION_GATEWAY_URL or IGNITION_INSTALL_PATH to read the module list.",
            )
        try:
            _, payload = await state.gateway.first_available(
                ["/gateway/modules", "/modules", "/gateway/info"]
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_modules")
        return ok(
            state,
            "gateway_modules",
            {"source": "gateway_api", "modules": _as_items(payload, "modules")},
        )

    @mcp.tool()
    async def gateway_projects() -> dict[str, Any]:
        """List projects with enabled state and inheritance parent.

        Returns:
            Project entries from disk when available, otherwise from the gateway API.
        """
        reader = state.local_reader
        if reader is not None:
            projects = []
            for name in reader.list_projects():
                meta = reader.read_project_meta(name)
                chain = reader.inheritance_chain(name)
                projects.append(
                    {
                        "name": name,
                        "enabled": bool(meta.get("enabled", True)),
                        "parent": chain[1] if len(chain) > 1 else None,
                    }
                )
            if projects:
                return ok(
                    state,
                    "gateway_projects",
                    {"source": "local_filesystem", "projects": projects},
                )

        if not state.has_gateway:
            return unsupported(
                state,
                "gateway_projects",
                hint="Point IGNITION_INSTALL_PATH at the Ignition install to read projects.",
            )
        try:
            _, payload = await state.gateway.first_available(["/perspective/projects", "/projects"])
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_projects")
        return ok(
            state,
            "gateway_projects",
            {"source": "gateway_api", "projects": _as_items(payload, "projects")},
        )

    @mcp.tool()
    async def gateway_logs(
        level: str = "WARN",
        keyword: str | None = None,
        lines: int = 100,
    ) -> dict[str, Any]:
        """Search or tail gateway logs.

        Args:
            level: Minimum level to include (DEBUG/INFO/WARN/ERROR).
            keyword: Case-insensitive substring filter.
            lines: Number of trailing log lines to inspect.

        Returns:
            Matching log lines and the source that produced them.
        """
        threshold = LEVEL_ORDER.get(level.upper(), LEVEL_ORDER["WARN"])
        reader = state.local_reader
        if reader is not None:
            raw = reader.read_log_lines(max(lines * 10, 500))
            matches = _filter_log_lines(raw, threshold, keyword)[-lines:]
            if raw:
                return ok(
                    state,
                    "gateway_logs",
                    {
                        "source": "local_wrapper_log",
                        "level": level.upper(),
                        "keyword": keyword,
                        "lines": matches,
                        "count": len(matches),
                    },
                )

        if not state.has_gateway:
            return unsupported(
                state,
                "gateway_logs",
                hint="Read logs locally by setting IGNITION_INSTALL_PATH, or connect a gateway.",
            )
        try:
            _, payload = await state.gateway.first_available(
                ["/gateway/logs", "/logs"],
                params={"level": level.upper(), "maxRows": lines},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_logs")
        return ok(
            state,
            "gateway_logs",
            {"source": "gateway_api", "level": level.upper(), "logs": payload},
        )

    @mcp.tool()
    async def gateway_diagnostics() -> dict[str, Any]:
        """Identify configuration problems and failed components.

        Returns:
            A list of issues with severity and a summary of the checks performed.
        """
        issues: list[dict[str, str]] = []
        checks: dict[str, Any] = {}

        if not state.session.is_connected:
            issues.append(
                {
                    "severity": "warning",
                    "message": "No live gateway connection; only local filesystem tools work.",
                }
            )
        checks["gateway_connected"] = state.session.is_connected

        reader = state.local_reader
        if reader is not None:
            checks["local_install"] = reader.root.exists()
            checks["config_db_readable"] = reader.config_db.exists()
            projects = reader.list_projects()
            checks["projects_found"] = len(projects)
            if not projects:
                issues.append(
                    {
                        "severity": "warning",
                        "message": "No project directories found under data/projects.",
                    }
                )
            if not reader.config_db.exists():
                issues.append(
                    {
                        "severity": "warning",
                        "message": "data/db/config.idb was not found; module/project metadata "
                        "may be incomplete.",
                    }
                )
        else:
            checks["local_install"] = False

        if state.has_gateway:
            try:
                status = await state.gateway.get_json("/data/status")
                checks["status"] = status
                if isinstance(status, dict):
                    for key in ("faults", "errors", "warnings"):
                        value = status.get(key)
                        if value:
                            issues.append(
                                {
                                    "severity": "error" if key != "warnings" else "warning",
                                    "message": f"Gateway status reports {key}: {value}",
                                }
                            )
            except GatewayError as exc:
                issues.append({"severity": "error", "message": exc.message})

        return ok(
            state,
            "gateway_diagnostics",
            {
                "issues": issues,
                "issue_count": len(issues),
                "checks": checks,
            },
        )

    @mcp.tool()
    async def gateway_performance() -> dict[str, Any]:
        """Inspect gateway resource usage (CPU, heap, threads, sessions).

        Returns:
            Metrics extracted from the gateway status payload.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ["/gateway/status", "/gateway/performance", "/data/status"]
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_performance")

        metrics: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in (
                "cpu",
                "cpuUsage",
                "memory",
                "heap",
                "usedHeap",
                "maxHeap",
                "threads",
                "threadCount",
                "sessions",
                "connectedClients",
            ):
                if key in payload:
                    metrics[key] = payload[key]
        return ok(
            state,
            "gateway_performance",
            {"metrics": metrics, "raw": payload if not metrics else None},
        )

    @mcp.tool()
    async def gateway_backup_status() -> dict[str, Any]:
        """Check the last gateway backup status.

        Returns:
            Backup metadata when the gateway exposes it.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ["/gateway/backup", "/gateway/backup/status", "/redundancy/backupstatus"]
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_backup_status")
        return ok(state, "gateway_backup_status", {"backup": payload})

    @mcp.tool()
    async def gateway_backup(
        output_path: str | None = None,
        include_peer_local: bool = False,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Download a full Gateway backup (``.gwbk``).

        The archive includes projects, tags, Gateway configuration and credentials, so treat
        the output as sensitive. It requires an administrator Gateway session; no API token
        is required. Use ``dry_run`` to preview, then ``confirm=True`` to download.

        Args:
            output_path: Destination file or existing directory. Defaults to
                ``backups/<gateway>_gateway_backup_<timestamp>.gwbk``.
            include_peer_local: Include peer-local data in a redundant pair.
            confirm: Must be ``True`` to download.
            dry_run: Preview the endpoint and destination without downloading.

        Returns:
            Backup path, byte count and verification status.
        """
        destination = _backup_destination(state, output_path)
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "endpoint": GATEWAY_BACKUP_PATH,
                    "would_save": str(destination),
                    "include_peer_local": include_peer_local,
                }
            )
        if not confirm:
            return state.tool_response(
                {
                    "status": "requires_confirmation",
                    "operation": "gateway_backup",
                    "message": "Call gateway_backup again with confirm=True to download the backup.",
                    "destination": str(destination),
                }
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})

        try:
            size = await _download_gateway_backup(state, destination, include_peer_local)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_backup")
        except OSError as exc:
            return state.tool_response(
                {"status": "write_failed", "path": str(destination), "message": str(exc)}
            )
        if size <= 0:
            return state.tool_response(
                {"status": "empty_backup", "path": str(destination), "bytes": size}
            )
        try:
            verified = destination.stat().st_size == size
        except OSError:
            verified = False
        return ok(
            state,
            "gateway_backup",
            {
                "status": "backup_created",
                "path": str(destination),
                "bytes": size,
                "include_peer_local": include_peer_local,
                "verified": verified,
            },
        )

    @mcp.tool()
    async def gateway_redundancy_status() -> dict[str, Any]:
        """Get redundancy role and synchronisation state.

        Returns:
            Redundancy payload when the gateway is redundant.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ["/gateway/redundancy", "/redundancy/status"]
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_redundancy_status")
        return ok(state, "gateway_redundancy_status", {"redundancy": payload})

    @mcp.tool()
    async def gateway_restart(confirm: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Restart the gateway. Disconnects every client session.

        Args:
            confirm: Must be ``True`` to execute the restart.
            dry_run: Preview the impact without restarting.

        Returns:
            Restart acknowledgement or a guard payload.
        """
        guard = state.write_guard("gateway_restart", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "would_do": "POST gateway restart",
                    "impact": "All client sessions disconnect; the gateway restarts.",
                }
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ["/gateway/restart", "/gateway/reboot"], method="POST"
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "gateway_restart")
        state.record_action("gateway_restart", "ok")
        return state.tool_response({"status": "restart_requested", "response": payload})

    @mcp.tool()
    async def gateway_add_module(
        modl_path: str,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Install an Ignition ``.modl`` module via the Gateway API or local ``user-lib/modules``.

        Args:
            modl_path: Path to the ``.modl`` archive on the MCP host.
            confirm: Must be ``True`` to install the module.
            dry_run: Validate the ``.modl`` archive and preview the target without installing.

        Returns:
            Installation result, module metadata, and whether a ``gateway_restart`` is needed.
        """
        guard = state.write_guard("gateway_add_module", confirm, dry_run)
        if guard:
            return guard

        archive = Path(modl_path)
        meta, issues = await anyio.to_thread.run_sync(_inspect_modl, archive)
        reader = state.local_reader
        local_dest = reader.root / "user-lib" / "modules" / archive.name if reader else None

        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "modl_path": str(archive),
                    "valid": not issues,
                    "validation_issues": issues,
                    "module": meta,
                    "target": (
                        "gateway_api"
                        if state.has_gateway
                        else (str(local_dest) if local_dest else "unavailable")
                    ),
                }
            )
        if issues:
            return state.tool_response(
                {"status": "invalid_module", "modl_path": str(archive), "issues": issues}
            )

        if state.has_gateway:
            payload = await anyio.to_thread.run_sync(archive.read_bytes)
            try:
                endpoint, response = await state.gateway.first_available(
                    ("/modules/install", "/gateway/modules/install", "/data/api/v1/modules"),
                    method="POST",
                    content=payload,
                )
            except GatewayError as exc:
                if exc.status != 404 or local_dest is None:
                    return _gateway_or_unsupported(state, exc, "gateway_add_module")
            else:
                state.record_action("gateway_add_module", "ok", module=archive.name, via="api")
                return state.tool_response(
                    {
                        "status": "installed",
                        "via": "gateway_api",
                        "endpoint": endpoint,
                        "module": meta,
                        "response": response,
                        "restart_recommended": True,
                    }
                )

        if local_dest is None:
            return unsupported(
                state,
                "gateway_add_module",
                hint="Connect to a Gateway or set IGNITION_INSTALL_PATH to install a .modl file.",
            )

        try:
            await anyio.to_thread.run_sync(_copy_modl_file, archive, local_dest)
        except OSError as exc:
            return state.tool_response(
                {"status": "write_failed", "path": str(local_dest), "message": str(exc)}
            )
        state.record_action("gateway_add_module", "ok", module=archive.name, via="local_install")
        return state.tool_response(
            {
                "status": "installed",
                "via": "local_filesystem",
                "path": str(local_dest),
                "module": meta,
                "verified": local_dest.is_file(),
                "restart_recommended": True,
            }
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _backup_destination(state: ServerState, output_path: str | None) -> Path:
    """Resolve the destination for a Gateway backup."""
    if output_path:
        path = Path(output_path)
        if path.is_dir():
            return path / _backup_filename(state)
        return path
    return Path("backups") / _backup_filename(state)


def _backup_filename(state: ServerState) -> str:
    """Build a filesystem-safe backup filename from the gateway host and timestamp."""
    host = urlparse(state.session.gateway_url).hostname or "gateway"
    safe_host = re.sub(r"[^A-Za-z0-9_.-]+", "_", host).strip("._") or "gateway"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{safe_host}_gateway_backup_{stamp}.gwbk"


async def _download_gateway_backup(
    state: ServerState, destination: Path, include_peer_local: bool
) -> int:
    """Download a Gateway backup, retrying once with a Gateway session if required."""
    params = {"includePeerLocal": "true"} if include_peer_local else None
    timeout = max(state.config.timeout, BACKUP_TIMEOUT_SECONDS)
    try:
        return await state.gateway.download(
            GATEWAY_BACKUP_PATH, destination, params=params, request_timeout=timeout
        )
    except GatewayError as exc:
        if exc.status not in (401, 403) or not _has_session_credentials(state):
            raise
        if await state.gateway.authenticate() != "session_login":
            raise
        return await state.gateway.download(
            GATEWAY_BACKUP_PATH, destination, params=params, request_timeout=timeout
        )


async def _get_trial(state: ServerState) -> Any:
    """Read trial state, retrying once with a Gateway session if required."""
    try:
        return await state.gateway.get_json(TRIAL_PATH)
    except GatewayError as exc:
        if exc.status not in (401, 403) or not _has_session_credentials(state):
            raise
        if await state.gateway.authenticate() != "session_login":
            raise
        return await state.gateway.get_json(TRIAL_PATH)


async def _post_trial(state: ServerState, body: dict[str, Any]) -> Any:
    """POST a trial reset, retrying once with a Gateway session if required."""
    try:
        return await state.gateway.post_json(TRIAL_PATH, body)
    except GatewayError as exc:
        if exc.status not in (401, 403) or not _has_session_credentials(state):
            raise
        if await state.gateway.authenticate() != "session_login":
            raise
        return await state.gateway.post_json(TRIAL_PATH, body)


def _has_session_credentials(state: ServerState) -> bool:
    """Return whether username/password login is configured (never log the values)."""
    return bool(state.config.username and state.config.password)


def _trial_summary(payload: Any) -> dict[str, Any]:
    """Normalise the trial payload into a small, stable summary."""
    if not isinstance(payload, dict):
        return {
            "license_mode": "unknown",
            "trial_status": "unknown",
            "trial_state": None,
            "seconds_remaining": 0,
            "hours_remaining": 0.0,
            "expired": False,
            "emergency": False,
            "development": False,
        }
    try:
        seconds = int(payload.get("trialSecondsLeft") or 0)
    except (TypeError, ValueError):
        seconds = 0
    expired = bool(payload.get("expired"))
    mode = str(payload.get("licenseMode") or "unknown")
    if mode.lower() == "trial":
        trial_status = "expired" if expired else "active"
    else:
        trial_status = "emergency" if payload.get("emergency") else "licensed"
    return {
        "license_mode": mode,
        "trial_status": trial_status,
        "trial_state": payload.get("trialState"),
        "seconds_remaining": seconds,
        "hours_remaining": round(seconds / 3600, 2),
        "expired": expired,
        "emergency": bool(payload.get("emergency")),
        "development": bool(payload.get("development")),
    }


def _filter_log_lines(lines: list[str], threshold: int, keyword: str | None) -> list[str]:
    """Filter wrapper.log lines by severity and keyword.

    Args:
        lines: Raw log lines.
        threshold: Minimum severity index from :data:`LEVEL_ORDER`.
        keyword: Optional case-insensitive substring.

    Returns:
        Matching lines, preserving order.
    """
    needle = keyword.lower() if keyword else None
    matches: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        level = LEVEL_LETTERS.get(stripped[:1].upper(), "INFO")
        if LEVEL_ORDER.get(level, 1) < threshold:
            continue
        if needle and needle not in line.lower():
            continue
        matches.append(line)
    return matches


def _as_items(payload: Any, key: str) -> list[Any]:
    """Extract a list from a payload that may be a list or a dict wrapper."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for candidate in (key, "results", "items", "data"):
            value = payload.get(candidate)
            if isinstance(value, list):
                return value
    return []


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "This endpoint is not present on the connected gateway. Inspect "
                "/openapi.json on the gateway or use local filesystem tools."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)


def _normalise_level(value: str) -> str:
    """Normalise a log level string for comparisons."""
    return re.sub(r"[^A-Z]", "", value.upper()) or "WARN"


def _inspect_modl(archive: Path) -> tuple[dict[str, Any], list[str]]:
    """Validate a ``.modl`` archive and extract ``module.xml`` metadata when present."""
    issues: list[str] = []
    if not archive.is_file():
        return {}, [f"Module file '{archive}' does not exist."]
    if archive.suffix.lower() != ".modl":
        issues.append(f"Expected a .modl file, got '{archive.name}'.")
    if not zipfile.is_zipfile(archive):
        issues.append(f"'{archive.name}' is not a valid ZIP/.modl archive.")
        return {}, issues

    meta: dict[str, Any] = {"filename": archive.name, "size_bytes": archive.stat().st_size}
    with zipfile.ZipFile(archive) as zf:
        if "module.xml" in zf.namelist():
            try:
                root = ET.fromstring(zf.read("module.xml"))
                for tag in ("id", "name", "version", "requiredIgnitionVersion"):
                    elem = root.find(f".//{tag}")
                    if elem is not None and elem.text:
                        meta[tag] = elem.text.strip()
            except ET.ParseError:
                issues.append("module.xml inside the .modl archive is malformed XML.")
    return meta, issues


def _copy_modl_file(src: Path, dest: Path) -> None:
    """Copy a ``.modl`` file into ``user-lib/modules``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
