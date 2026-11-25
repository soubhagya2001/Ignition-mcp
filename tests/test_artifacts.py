"""Tests for project export and Gateway backup downloads."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import httpx
import respx

from ignition_mcp.client.gateway_api import GatewayClient
from ignition_mcp.config import IgnitionMCPConfig
from ignition_mcp.modules import gateway, projects
from ignition_mcp.state import IgnitionSession, ServerState

EXPORT_URL = "http://gw/data/api/v1/projects/export/Demo"
BACKUP_URL = "http://gw/data/api/v1/backup"


class RecordingMCP:
    """Minimal MCP stand-in that records registered tools."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        """Return a decorator that records the tool function."""

        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


def _build() -> dict[str, Any]:
    state = ServerState(config=IgnitionMCPConfig(readonly=True))
    state.session = IgnitionSession(gateway_url="http://gw", is_connected=True, readonly=True)
    state.gateway = GatewayClient("http://gw", timeout=2)
    mcp = RecordingMCP()
    gateway.register(mcp, state)
    projects.register(mcp, state)
    return mcp.tools


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("project.json", "{}")
    return buffer.getvalue()


@respx.mock
async def test_export_project_saves_zip(tmp_path) -> None:
    archive = _zip_bytes()
    route = respx.get(EXPORT_URL).mock(return_value=httpx.Response(200, content=archive))
    tools = _build()
    destination = tmp_path / "demo.zip"

    response = await tools["export_project"](project="Demo", output_path=str(destination))
    payload = response["result"]
    assert payload["status"] == "exported"
    assert payload["valid_zip"] is True
    assert destination.read_bytes() == archive
    assert route.called


@respx.mock
async def test_create_project_backup_uses_export_endpoint(tmp_path) -> None:
    archive = _zip_bytes()
    route = respx.get(EXPORT_URL).mock(return_value=httpx.Response(200, content=archive))
    tools = _build()
    destination = tmp_path / "demo-backup.zip"

    response = await tools["create_project_backup"](project="Demo", output_path=str(destination))
    payload = response["result"]
    assert payload["status"] == "backup_created"
    assert payload["valid_zip"] is True
    assert destination.read_bytes() == archive
    assert route.called


@respx.mock
async def test_gateway_backup_streams_gwbk(tmp_path) -> None:
    content = b"GWBK-test-backup"
    route = respx.get(BACKUP_URL).mock(return_value=httpx.Response(200, content=content))
    tools = _build()
    destination = tmp_path / "gateway.gwbk"

    response = await tools["gateway_backup"](output_path=str(destination), confirm=True)
    payload = response["result"]
    assert payload["status"] == "backup_created"
    assert payload["verified"] is True
    assert payload["bytes"] == len(content)
    assert destination.read_bytes() == content
    assert route.called


@respx.mock
async def test_gateway_backup_passes_peer_local_flag(tmp_path) -> None:
    route = respx.get(BACKUP_URL).mock(return_value=httpx.Response(200, content=b"GWBK"))
    tools = _build()

    response = await tools["gateway_backup"](
        output_path=str(tmp_path / "gateway.gwbk"),
        include_peer_local=True,
        confirm=True,
    )
    assert response["result"]["status"] == "backup_created"
    assert route.calls.last.request.url.params["includePeerLocal"] == "true"


@respx.mock
async def test_gateway_backup_dry_run_does_not_call(tmp_path) -> None:
    route = respx.get(BACKUP_URL).mock(return_value=httpx.Response(200, content=b"GWBK"))
    tools = _build()

    response = await tools["gateway_backup"](
        output_path=str(tmp_path / "gateway.gwbk"), dry_run=True
    )
    assert response["result"]["status"] == "dry_run"
    assert not route.called


@respx.mock
async def test_gateway_backup_requires_confirmation(tmp_path) -> None:
    route = respx.get(BACKUP_URL).mock(return_value=httpx.Response(200, content=b"GWBK"))
    tools = _build()

    response = await tools["gateway_backup"](output_path=str(tmp_path / "gateway.gwbk"))
    assert response["result"]["status"] == "requires_confirmation"
    assert not route.called


@respx.mock
async def test_gateway_backup_auth_error_leaves_no_file(tmp_path) -> None:
    respx.get(BACKUP_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))
    tools = _build()
    destination = tmp_path / "gateway.gwbk"

    response = await tools["gateway_backup"](output_path=str(destination), confirm=True)
    assert response["result"]["status"] == "gateway_error"
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()
