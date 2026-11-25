"""MCP server bootstrap.

Responsibilities:

1. Load configuration and configure stderr logging (stdout belongs to the MCP transport).
2. Create the MCP server through the version-compatible shim.
3. Run AutoConnect so tools have a session before the first call.
4. Register every enabled module.
5. Serve over ``stdio``, ``sse`` or ``streamable-http``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys

from ignition_mcp.autoconnect.engine import AutoConnectEngine
from ignition_mcp.config import IgnitionMCPConfig, load_config
from ignition_mcp.mcp_compat import MCPServer, create_server, normalize_transport
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

#: Modules registered by default, in registration order. ``ai_control`` is always loaded.
MODULES: list[str] = [
    "gateway",
    "tags",
    "perspective",
    "docs",
    "scripting",
    "alarms",
    "database",
    "history",
    "projects",
    "analysis",
    "icons",
    "security",
    "historians",
    "devices",
    "simulators",
]


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8080,
    log_level: str | None = None,
) -> None:
    """Start the MCP server.

    Args:
        transport: ``stdio`` (default), ``sse`` or ``http``.
        host: Bind host for network transports.
        port: Bind port for network transports.
        log_level: Optional log-level override.
    """
    cfg = load_config(transport=transport, host=host, port=port, log_level=log_level)
    _configure_logging(cfg.log_level)
    logger.info("Starting Ignition MCP v%s (transport=%s)", _version(), cfg.transport)

    try:
        asyncio.run(_async_main(cfg))
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        logger.info("Interrupted, shutting down")


async def _async_main(cfg: IgnitionMCPConfig) -> None:
    """Create the server, connect, register modules and serve until shutdown."""
    mcp = create_server("ignition-mcp", version=_version())
    state = ServerState(config=cfg)
    state.session.readonly = cfg.readonly

    engine = AutoConnectEngine(cfg)
    try:
        await engine.connect(state)
    except Exception:
        logger.exception("AutoConnect failed; starting with local-only tools")

    _register_all_modules(mcp, state, cfg)

    try:
        await _serve(mcp, cfg)
    finally:
        if state.has_gateway:
            await state.gateway.aclose()


async def _serve(mcp: MCPServer, cfg: IgnitionMCPConfig) -> None:
    """Run the MCP server with the requested transport."""
    transport = normalize_transport(cfg.transport)

    # FastMCP exposes host/port through settings; MCPServer accepts them as run kwargs.
    settings = getattr(mcp, "settings", None)
    if settings is not None:
        for attribute, value in (("host", cfg.host), ("port", cfg.port)):
            if hasattr(settings, attribute):
                setattr(settings, attribute, value)

    logger.info("Serving MCP over %s", transport)
    if transport == "stdio":
        await mcp.run_stdio_async()
    elif transport == "sse":
        await _run_network_transport(mcp.run_sse_async, cfg)
    else:
        await _run_network_transport(mcp.run_streamable_http_async, cfg)


async def _run_network_transport(runner: object, cfg: IgnitionMCPConfig) -> None:
    """Call a network transport runner, tolerating SDK signature differences."""
    try:
        await runner(host=cfg.host, port=cfg.port)  # type: ignore[operator]
    except TypeError:  # pragma: no cover - older SDK reads host/port from settings
        await runner()  # type: ignore[operator]


def _register_all_modules(mcp: MCPServer, state: ServerState, cfg: IgnitionMCPConfig) -> None:
    """Import and register module tools, honouring ``IGNITION_MODULES``.

    Args:
        mcp: MCP server instance.
        state: Shared server state captured by every tool closure.
        cfg: Resolved configuration (for the module allow-list).
    """
    enabled = set(cfg.enabled_modules or [])
    registered: dict[str, int] = {}

    def should_enable(name: str) -> bool:
        return not enabled or name in enabled

    from ignition_mcp.modules import ai_control

    ai_control.register(mcp, state)
    registered["ai_control"] = 1

    for module_name in MODULES:
        if not should_enable(module_name):
            logger.debug("Skipping module %s (not in IGNITION_MODULES)", module_name)
            continue
        try:
            module = importlib.import_module(f"ignition_mcp.modules.{module_name}")
            module.register(mcp, state)
            registered[module_name] = 1
        except Exception:
            logger.exception("Failed to register module %s", module_name)

    logger.info("Registered %d modules (%s)", len(registered), ", ".join(sorted(registered)))


def _configure_logging(level: str) -> None:
    """Configure stderr logging; stdout is reserved for the MCP protocol."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )


def _version() -> str:
    """Return the package version."""
    from ignition_mcp import __version__

    return __version__
