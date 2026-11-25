"""``ignition-mcp --doctor`` diagnostics.

The doctor is deliberately read-only: it runs AutoConnect, reads local install metadata and the
gateway's OpenAPI document, then prints one JSON report. It never writes to a gateway and never
prints passwords or API tokens.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ignition_mcp.autoconnect.engine import AutoConnectEngine
from ignition_mcp.capabilities import gateway_capabilities
from ignition_mcp.config import IgnitionMCPConfig
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)


async def build_report(cfg: IgnitionMCPConfig) -> dict[str, Any]:
    """Collect diagnostics for the current machine and gateway.

    Args:
        cfg: Resolved configuration.

    Returns:
        A JSON-serialisable report with checks, install details, session context and
        capabilities.
    """
    state = ServerState(config=cfg)
    state.session.readonly = cfg.readonly

    connect_error: str | None = None
    try:
        await AutoConnectEngine(cfg).connect(state)
    except Exception as exc:
        connect_error = f"{type(exc).__name__}: {exc}"
        logger.debug("Doctor AutoConnect failed", exc_info=True)

    install: dict[str, Any] | None = None
    reader = state.local_reader
    if reader is not None:
        info = await reader.read()
        install = {
            "path": info.install_path,
            "gateway_url": info.gateway_url,
            "projects": info.projects,
            "db_connections": info.db_connections,
            "modules": info.installed_modules,
            "version": info.gateway_version,
        }

    capabilities = await gateway_capabilities(state)
    openapi = capabilities.get("openapi", {})
    checks = [
        _check("local_install", bool(install), "found" if install else "not found", required=False),
        _check(
            "gateway",
            state.session.is_connected,
            state.session.gateway_url or "not connected",
            required=False,
        ),
        _check(
            "openapi",
            bool(openapi.get("available")),
            f"{openapi.get('path_count', 0)} endpoints",
            required=False,
        ),
        _check(
            "write_mode",
            not state.session.readonly,
            "writes enabled" if not state.session.readonly else "read-only (default)",
            required=False,
        ),
    ]

    usable = bool(install) or state.session.is_connected
    return {
        "status": "ok" if usable else "degraded",
        "error": connect_error,
        "config": {
            "gateway_url": cfg.gateway_url,
            "api_prefix": cfg.api_prefix,
            "readonly": cfg.readonly,
            "transport": cfg.transport,
            "timeout": cfg.timeout,
            "enabled_modules": cfg.enabled_modules,
        },
        "install": install,
        "session": state.session.to_ctx(),
        "capabilities": capabilities,
        "checks": checks,
    }


def _check(name: str, passed: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    """Build one doctor check entry."""
    status = "pass" if passed else ("fail" if required else "warn")
    return {"name": name, "status": status, "detail": detail}


def run_doctor(cfg: IgnitionMCPConfig) -> int:
    """Run diagnostics, print the report and return a process exit code.

    Args:
        cfg: Resolved configuration.

    Returns:
        ``0`` when a local install or gateway is usable, ``1`` otherwise.
    """
    import asyncio

    report = asyncio.run(build_report(cfg))
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["status"] == "ok" else 1


__all__ = ["build_report", "run_doctor"]
