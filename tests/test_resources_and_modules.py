"""Tests for view resource.json generation/validation, WebDev endpoints, and module install."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from ignition_mcp.autoconnect.local_reader import (
    LocalInstallReader,
    build_resource_json,
    validate_resource_json,
)
from ignition_mcp.modules import gateway, perspective, projects
from ignition_mcp.state import ServerState


class _RecordingMCP:
    """Minimal MCP stand-in that records registered tools."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


def test_write_perspective_view_creates_valid_resource_json(tmp_path: Path) -> None:
    """Writing a Perspective view creates a valid resource.json alongside view.json."""
    reader = LocalInstallReader(tmp_path)
    view = {"root": {"type": "ia.container.flex", "meta": {"name": "root"}, "children": []}}

    view_file = reader.write_perspective_view("Demo", "Main/Home", view)
    assert view_file is not None

    resource = reader.read_view_resource_json("Demo", "Main/Home")
    assert resource is not None
    assert validate_resource_json(resource) == []
    assert "view.json" in resource["files"]


def test_validate_resource_json_rejects_bad_scope_and_files() -> None:
    """Invalid scope or empty files list is reported by validate_resource_json."""
    bad = build_resource_json(["view.json"])
    bad["scope"] = "INVALID"
    bad["files"] = []
    issues = validate_resource_json(bad)
    assert len(issues) == 2


async def test_create_view_resource_json_tool(local_state: ServerState) -> None:
    """create_view_resource_json supports dry_run and confirm=True writes."""
    mcp = _RecordingMCP()
    perspective.register(mcp, local_state)
    tool = mcp.tools["create_view_resource_json"]

    dry = await tool("Dashboard/Overview", dry_run=True)
    assert dry["result"]["status"] == "dry_run"
    assert dry["result"]["valid"] is True

    res = await tool("Dashboard/Overview", confirm=True)
    assert res["result"]["status"] == "written"
    assert res["result"]["verified"] is True


async def test_webdev_endpoint_roundtrip(local_state: ServerState) -> None:
    """create_webdev_endpoint validates scripts, writes files, and reads back."""
    mcp = _RecordingMCP()
    projects.register(mcp, local_state)

    bad = await mcp.tools["create_webdev_endpoint"](
        "api/broken",
        handlers={"doGet": "def doGet(request, session:\n    return {}"},
        confirm=True,
    )
    assert bad["result"]["status"] == "invalid_endpoint"

    created = await mcp.tools["create_webdev_endpoint"](
        "api/health",
        methods=["doGet", "doPost"],
        handlers={"doGet": "def doGet(request, session):\n\treturn {'json': {'ok': True}}\n"},
        confirm=True,
    )
    assert created["result"]["status"] == "written"
    assert created["result"]["verified"] is True

    listed = await mcp.tools["list_webdev_endpoints"]()
    assert "api/health" in listed["result"]["endpoints"]

    read = await mcp.tools["read_webdev_endpoint"]("api/health")
    assert read["result"]["config"]["doGet"]["enabled"] is True
    assert "doGet" in read["result"]["scripts"]
    assert read["result"]["resource"] is not None


async def test_gateway_add_module_local_install(local_state: ServerState, tmp_path: Path) -> None:
    """gateway_add_module validates .modl archives and installs locally when offline."""
    modl = tmp_path / "Sample-Module.modl"
    with zipfile.ZipFile(modl, "w") as zf:
        zf.writestr(
            "module.xml",
            "<modules><module><id>com.example.sample</id><name>Sample</name>"
            "<version>1.0.0</version></module></modules>",
        )

    mcp = _RecordingMCP()
    gateway.register(mcp, local_state)
    add_mod = mcp.tools["gateway_add_module"]

    dry = await add_mod(str(modl), dry_run=True)
    assert dry["result"]["status"] == "dry_run"
    assert dry["result"]["valid"] is True
    assert dry["result"]["module"]["id"] == "com.example.sample"

    installed = await add_mod(str(modl), confirm=True)
    assert installed["result"]["status"] == "installed"
    assert installed["result"]["verified"] is True
    assert installed["result"]["restart_recommended"] is True


async def test_set_and_delete_perspective_page(local_state: ServerState) -> None:
    """set_perspective_page and delete_perspective_page manage page-config/config.json."""
    mcp = _RecordingMCP()
    perspective.register(mcp, local_state)
    set_page = mcp.tools["set_perspective_page"]
    del_page = mcp.tools["delete_perspective_page"]

    bad = await set_page("Dashboard", "Dashboard/Overview", confirm=True)
    assert bad["result"]["status"] == "invalid_page_config"

    dry = await set_page("/overview", "Dashboard/Overview", title="Overview", dry_run=True)
    assert dry["result"]["status"] == "dry_run"
    assert dry["result"]["valid"] is True

    written = await set_page("/overview", "Dashboard/Overview", title="Overview", confirm=True)
    assert written["result"]["status"] == "written"
    assert written["result"]["verified"] is True

    reader = local_state.local_reader
    assert reader is not None
    cfg = reader.read_page_config("TestProject")
    assert cfg is not None
    assert cfg["pages"]["/overview"]["viewPath"] == "Dashboard/Overview"

    removed = await del_page("/overview", confirm=True)
    assert removed["result"]["status"] == "deleted"
    assert removed["result"]["verified"] is True
    cfg_after = reader.read_page_config("TestProject")
    assert cfg_after is not None
    assert "/overview" not in cfg_after["pages"]
