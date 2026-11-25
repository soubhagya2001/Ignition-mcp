"""AI control and context layer.

These tools are the agent's own session management: connect/refresh, inspect status, switch
project or gateway, discover gateways, toggle writes, and review the audit trail. They are
always registered, regardless of ``IGNITION_MODULES``.
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio

from ignition_mcp.autoconnect.engine import AutoConnectEngine
from ignition_mcp.autoconnect.network_scan import mdns_discover, port_scan
from ignition_mcp.capabilities import gateway_capabilities
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.safety import list_snapshots, restore_snapshot, snapshot_root
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def mcp_designer_status() -> dict[str, Any]:
        """Report running Ignition Designer processes and their project window titles.

        This provides process/session discovery only. It does not click or edit the Designer UI,
        because that requires separate Windows UI-automation integration and is unsafe to assume.

        Returns:
            Designer process details and whether Designer is running.
        """
        if sys.platform.startswith("win"):
            processes = await anyio.to_thread.run_sync(_windows_designer_processes)
        elif sys.platform.startswith("linux"):
            processes = await anyio.to_thread.run_sync(_linux_designer_processes)
        else:
            processes = []

        running = bool(processes)
        state.record_action("mcp_designer_status", "ok" if running else "not_running")
        return state.tool_response(
            {
                "status": "running" if running else "not_running",
                "processes": processes,
                "ui_control_supported": False,
                "hint": (
                    "Use mcp_designer_status to discover running Designer sessions. "
                    "Editing the Designer UI is not implemented."
                ),
            }
        )

    @mcp.tool()
    async def mcp_connect() -> dict[str, Any]:
        """Connect or refresh the Ignition gateway session and return full context.

        Call this at the start of a conversation, or after the gateway was restarted. When a
        session already exists this is cheap; otherwise discovery runs again.

        Returns:
            Gateway context, capabilities, and connection status.
        """
        if not state.session.is_connected:
            await AutoConnectEngine(state.config).connect(state)

        capabilities = await _build_capabilities(mcp, state)
        capabilities["gateway"] = await gateway_capabilities(state)
        ctx = state.session.to_ctx()
        ctx["capabilities"] = capabilities
        status = "connected" if state.session.is_connected else "local_only"
        state.record_action("mcp_connect", status)
        return {"status": status, **ctx}

    @mcp.tool()
    async def mcp_status() -> dict[str, Any]:
        """Report session status without any network I/O.

        Returns:
            Connection, auth, project and read-only state.
        """
        return state.tool_response(
            {
                "connected": state.session.is_connected,
                "gateway_client_available": state.has_gateway,
                "session_valid": state.session.is_token_valid,
                "gateway_url": state.session.gateway_url,
                "auth_method": state.session.auth_method,
                "auth_verified": state.session.auth_verified,
                "active_project": state.session.active_project,
                "readonly": state.session.readonly,
                "local_read_available": state.session.local_read_available,
            }
        )

    @mcp.tool()
    async def mcp_set_project(project: str) -> dict[str, Any]:
        """Switch the active project context.

        Args:
            project: Project name. Must be known to the gateway or the local install.

        Returns:
            The new active project and its inheritance parent.
        """
        known = set(state.session.projects)
        reader = state.local_reader
        if reader is not None:
            known |= set(reader.list_projects())

        if known and project not in known:
            return state.tool_response(
                {
                    "status": "not_found",
                    "message": f"Project '{project}' was not found.",
                    "available_projects": sorted(known),
                }
            )

        state.session.active_project = project
        state.session.parent_project = None
        if reader is not None:
            chain = reader.inheritance_chain(project)
            state.session.parent_project = chain[1] if len(chain) > 1 else None

        state.record_action("mcp_set_project", "ok", project=project)
        return state.tool_response(
            {
                "status": "ok",
                "active_project": project,
                "parent_project": state.session.parent_project,
            }
        )

    @mcp.tool()
    async def mcp_list_capabilities() -> dict[str, Any]:
        """List the tools registered in this MCP session and what they can do.

        Returns:
            Tool names plus write/local-read capability flags.
        """
        payload = await _build_capabilities(mcp, state)
        payload["gateway"] = await gateway_capabilities(state)
        return state.tool_response(payload)

    @mcp.tool()
    async def mcp_discover_gateways(scan_subnet: bool = False) -> dict[str, Any]:
        """Discover Ignition gateways on this machine and the local network.

        Args:
            scan_subnet: Also scan the local /24 subnet (slow; off by default).

        Returns:
            Found gateway URLs with the discovery sources that produced them.
        """
        from ignition_mcp.autoconnect.network_scan import get_local_ip, subnet_scan

        mdns_urls = await mdns_discover(timeout=state.config.scan_timeout)
        scan_urls = await port_scan(
            ["localhost", "127.0.0.1"], [8088, 8043, 8060], timeout=state.config.scan_timeout
        )
        subnet_urls: list[str] = []
        if scan_subnet:
            subnet_urls = await subnet_scan(get_local_ip(), [8088, 8043], timeout=0.5)

        found = list(dict.fromkeys(mdns_urls + scan_urls + subnet_urls))
        reader = state.local_reader
        local_install = None
        if reader is not None:
            info = await reader.read()
            local_install = {
                "install_path": info.install_path,
                "gateway_url": info.gateway_url,
                "projects": info.projects,
            }

        return state.tool_response(
            {
                "found_gateways": found,
                "current_gateway": state.session.gateway_url,
                "discovery_sources": {
                    "mdns": mdns_urls,
                    "port_scan": scan_urls,
                    "subnet_scan": subnet_urls,
                },
                "local_install": local_install,
            }
        )

    @mcp.tool()
    async def mcp_set_gateway(url: str) -> dict[str, Any]:
        """Switch to another gateway by URL.

        Args:
            url: Gateway base URL, e.g. ``http://10.0.1.50:8088``.

        Returns:
            The new session context, or an error payload when the gateway is unreachable.
        """
        engine = AutoConnectEngine(state.config)
        if not await engine.connect_to(state, url):
            state.record_action("mcp_set_gateway", "failed", url=url)
            return state.tool_response(
                {
                    "status": "failed",
                    "message": f"Could not connect to '{url}'.",
                    "hint": (
                        "Check the URL, that the gateway is running, and that "
                        "IGNITION_USERNAME/IGNITION_PASSWORD are valid."
                    ),
                }
            )
        state.record_action("mcp_set_gateway", "ok", url=url)
        return state.tool_response(
            {"status": "ok", "gateway_url": state.session.gateway_url}, **state.session.to_ctx()
        )

    @mcp.tool()
    async def mcp_enable_writes() -> dict[str, Any]:
        """Enable write tools for this session.

        Individual writes still require ``confirm=True``.

        Returns:
            The updated write mode.
        """
        state.session.readonly = False
        state.record_action("mcp_enable_writes", "ok")
        return state.tool_response(
            {
                "status": "ok",
                "readonly_mode": False,
                "message": "Writes enabled. Each write still requires confirm=True.",
            }
        )

    @mcp.tool()
    async def mcp_disable_writes() -> dict[str, Any]:
        """Re-enable read-only protection for this session.

        Returns:
            The updated write mode.
        """
        state.session.readonly = True
        state.record_action("mcp_disable_writes", "ok")
        return state.tool_response({"status": "ok", "readonly_mode": True})

    @mcp.tool()
    async def mcp_audit_log(limit: int = 50) -> dict[str, Any]:
        """Return this session's tool-action audit trail.

        Args:
            limit: Maximum entries to return (newest last).

        Returns:
            Recorded tool actions with status and non-secret details.
        """
        entries = state.audit_payload(limit)
        return state.tool_response(
            {"entries": entries, "count": len(entries), "total_recorded": len(state.audit)}
        )

    @mcp.tool()
    async def mcp_list_write_snapshots(limit: int = 50) -> dict[str, Any]:
        """List snapshots taken before local writes.

        Args:
            limit: Maximum number of snapshots to return, newest first.

        Returns:
            Snapshot manifests plus the store location.
        """
        snapshots = list_snapshots(limit)
        return state.tool_response(
            {
                "snapshots": snapshots,
                "count": len(snapshots),
                "root": str(snapshot_root()),
            }
        )

    @mcp.tool()
    async def mcp_restore_write_snapshot(
        snapshot_id: str,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Restore a file from a write snapshot.

        Args:
            snapshot_id: Snapshot id from mcp_list_write_snapshots.
            confirm: Must be ``True`` to restore.
            dry_run: Preview the restore without changing the target file.

        Returns:
            Restore acknowledgement or a guard payload.
        """
        guard = state.write_guard("mcp_restore_write_snapshot", confirm, dry_run)
        if guard:
            return guard
        if dry_run:
            snapshots = list_snapshots(limit=200)
            snapshot = next(
                (item for item in snapshots if item.get("snapshot_id") == snapshot_id),
                None,
            )
            if snapshot is None:
                return state.tool_response({"status": "not_found", "snapshot_id": snapshot_id})
            return state.tool_response(
                {
                    "status": "dry_run",
                    "snapshot_id": snapshot_id,
                    "would_restore": snapshot.get("path"),
                    "operation": snapshot.get("operation"),
                }
            )
        result = restore_snapshot(snapshot_id)
        if result.get("status") != "restored":
            state.record_action(
                "mcp_restore_write_snapshot", str(result.get("status")), snapshot_id=snapshot_id
            )
            return state.tool_response(result)
        state.record_action(
            "mcp_restore_write_snapshot",
            "ok",
            snapshot_id=snapshot_id,
            path=result.get("path"),
        )
        return state.tool_response(result)


def _windows_designer_processes() -> list[dict[str, Any]]:
    """Return visible Designer windows and Java process entries on Windows."""
    processes: list[dict[str, Any]] = []
    try:
        output = subprocess.check_output(
            [
                "tasklist",
                "/FO",
                "CSV",
                "/NH",
                "/FI",
                "IMAGENAME eq java.exe",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in output.splitlines():
            parts: list[str] = next(csv.reader([line]), [])
            if parts:
                processes.append(
                    {
                        "process_name": parts[0],
                        "pid": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                    }
                )
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            enum_windows = ctypes.windll.user32.EnumWindows
            get_window_text = ctypes.windll.user32.GetWindowTextW
            get_window_text_length = ctypes.windll.user32.GetWindowTextLengthW
            is_window_visible = ctypes.windll.user32.IsWindowVisible
            enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            found_titles: list[str] = []

            @enum_proc
            def collect(hwnd: int, _lparam: int) -> bool:
                if is_window_visible(hwnd):
                    length = get_window_text_length(hwnd)
                    if length:
                        buffer = ctypes.create_unicode_buffer(length + 1)
                        get_window_text(hwnd, buffer, length + 1)
                        title = buffer.value
                        if "ignition designer" in title.lower():
                            found_titles.append(title)
                return True

            enum_windows(collect, 0)
            processes.extend({"window_title": title} for title in found_titles)
        except (AttributeError, OSError):
            pass
    return processes


def _linux_designer_processes() -> list[dict[str, Any]]:
    """Return likely Designer processes from Linux /proc."""
    processes: list[dict[str, Any]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = (
                Path(f"/proc/{entry}/cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
        except OSError:
            continue
        lowered = cmdline.lower()
        if "ignition" in lowered and ("designer" in lowered or "launch" in lowered):
            processes.append({"pid": int(entry), "command": cmdline.strip()})
    return processes


async def _build_capabilities(mcp: MCPServer, state: ServerState) -> dict[str, Any]:
    """Introspect the MCP server for its registered tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.

    Returns:
        Tool names plus capability flags.
    """
    tools: list[str] = []
    try:
        for tool in await mcp.list_tools():
            tools.append(str(getattr(tool, "name", tool)))
    except Exception:
        logger.debug("Could not introspect MCP tools", exc_info=True)

    return {
        "tools": sorted(tools),
        "tool_count": len(tools),
        "write_enabled": not state.session.readonly,
        "local_read": state.session.local_read_available,
        "gateway_connected": state.session.is_connected,
        "auth_method": state.session.auth_method,
        "active_project": state.session.active_project,
    }
