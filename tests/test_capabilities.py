"""Tests for gateway capability negotiation."""

from __future__ import annotations

from typing import Any

from ignition_mcp.capabilities import feature_matrix, gateway_capabilities
from ignition_mcp.config import IgnitionMCPConfig
from ignition_mcp.state import IgnitionSession, ServerState


def test_feature_matrix_matches_openapi_paths() -> None:
    matrix = feature_matrix(["/tags/read", "/history/query", "/project/export", "/gateway/status"])
    assert matrix["tag_read"]["status"] == "available"
    assert matrix["tag_write"]["status"] == "unverified"
    assert matrix["history"]["status"] == "available"
    assert matrix["projects"]["status"] == "available"


def test_gateway_capabilities_without_gateway() -> None:
    state = ServerState(config=IgnitionMCPConfig())
    state.session = IgnitionSession(local_read_available=True)

    async def run() -> dict[str, Any]:
        return await gateway_capabilities(state)

    import asyncio

    report = asyncio.run(run())
    assert report["gateway_connected"] is False
    assert report["features"]["tag_read"]["status"] == "unknown"
    assert report["target"]["ignition"] == "8.3"


def test_gateway_capabilities_reads_openapi() -> None:
    class FakeGateway:
        async def openapi(self) -> dict[str, Any]:
            return {
                "available": True,
                "paths": ["/tags/read", "/history/query"],
                "path_count": 2,
                "version": "8.3.0",
            }

    state = ServerState(config=IgnitionMCPConfig())
    state.session = IgnitionSession(gateway_version="8.3.0", gateway_edition="Standard")
    state.gateway = FakeGateway()  # type: ignore[assignment]

    import asyncio

    report = asyncio.run(gateway_capabilities(state))
    assert report["openapi"]["available"] is True
    assert report["openapi"]["path_count"] == 2
    assert report["features"]["tag_read"]["status"] == "available"
    assert report["features"]["tag_write"]["status"] == "unverified"
    assert report["version"] == "8.3.0"
