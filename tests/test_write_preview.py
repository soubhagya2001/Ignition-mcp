"""Tests that write tools expose diffs, verification and snapshot tooling."""

from __future__ import annotations

from typing import Any

import pytest

from ignition_mcp.modules import ai_control, icons, perspective, projects, scripting, tags
from ignition_mcp.state import ServerState


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


@pytest.fixture
def tools(local_state: ServerState) -> dict[str, Any]:
    """Register the write-heavy modules against a recording server."""
    mcp = RecordingMCP()
    for module in (perspective, scripting, projects, tags, icons, ai_control):
        module.register(mcp, local_state)
    return mcp.tools


async def test_update_view_dry_run_returns_diff(
    tools: dict[str, Any], local_state: ServerState
) -> None:
    reader = local_state.local_reader
    assert reader is not None
    view = reader.read_perspective_view("TestProject", "Dashboard/Overview")
    assert view is not None
    view.setdefault("custom", {})["newProperty"] = 1

    response = await tools["update_perspective_view"]("Dashboard/Overview", view, dry_run=True)
    payload = response["result"]
    assert payload["status"] == "dry_run"
    assert payload["diff"]["changed"] is True
    assert any(change["path"] == "custom.newProperty" for change in payload["diff"]["changes"])


async def test_script_dry_run_returns_diff(tools: dict[str, Any]) -> None:
    response = await tools["write_project_script"](
        "Util", "import system\n\n\ndef total(a, b):\n    return a + b + 1\n", dry_run=True
    )
    payload = response["result"]
    assert payload["status"] == "dry_run"
    assert "+    return a + b + 1" in payload["diff"]


async def test_resource_dry_run_returns_diff(tools: dict[str, Any]) -> None:
    response = await tools["write_project_resource"](
        "project.json", {"enabled": False, "parent": None}, dry_run=True
    )
    payload = response["result"]
    assert payload["status"] == "dry_run"
    assert "-" in payload["diff"] and "+" in payload["diff"]


async def test_tag_dry_run_without_gateway_is_honest(tools: dict[str, Any]) -> None:
    response = await tools["write_tag"](["[default]Line1/Count"], [5], dry_run=True)
    payload = response["result"]
    assert payload["status"] == "dry_run"
    assert payload["verification"] == "unavailable_without_gateway"
    assert payload["would_write"] == {"[default]Line1/Count": 5}


async def test_snapshot_tools_are_registered(tools: dict[str, Any]) -> None:
    response = await tools["mcp_list_write_snapshots"]()
    payload = response["result"]
    assert payload["count"] == 0
    assert "snapshots" in payload
    assert "root" in payload


async def test_rollback_dry_run_is_preview_only(tools: dict[str, Any]) -> None:
    response = await tools["rollback_project"](
        project="TestProject",
        backup_id="backup-1",
        dry_run=True,
    )
    payload = response["result"]
    assert payload["status"] == "dry_run"
    assert payload["would_roll_back"] == "TestProject"
    assert payload["backup_id"] == "backup-1"


async def test_delete_icon_dry_run_is_preview_only(tools: dict[str, Any]) -> None:
    response = await tools["delete_icon"]("custom-icon", dry_run=True)
    payload = response["result"]
    assert payload["status"] == "dry_run"
    assert payload["would_delete"] == "custom-icon"


async def test_restore_snapshot_dry_run_requires_existing_snapshot(
    tools: dict[str, Any],
) -> None:
    response = await tools["mcp_restore_write_snapshot"](
        snapshot_id="missing",
        dry_run=True,
    )
    payload = response["result"]
    assert payload["status"] == "not_found"


async def test_create_project_dry_run(tools: dict[str, Any]) -> None:
    response = await tools["create_project"](
        name="NewProject",
        title="New Project",
        description="Test project",
        parent="Configuration",
        enabled=True,
        inheritable=False,
        dry_run=True,
    )
    payload = response["result"]
    assert payload["status"] == "dry_run"
    assert payload["valid"] is True
    assert payload["would_create"] == "NewProject"
    assert payload["metadata"]["parent"] == "Configuration"


async def test_create_project_writes_and_verifies(
    tools: dict[str, Any],
    local_state: ServerState,
) -> None:
    response = await tools["create_project"](
        name="NewProject",
        title="New Project",
        parent="Configuration",
        enabled=True,
        inheritable=False,
        confirm=True,
    )
    payload = response["result"]
    assert payload["status"] == "created"
    assert payload["verified"] is True

    reader = local_state.local_reader
    assert reader is not None
    assert reader.project_path("NewProject").exists()
    assert reader.read_project_meta("NewProject")["parent"] == "Configuration"


async def test_designer_status_is_registered(tools: dict[str, Any]) -> None:
    response = await tools["mcp_designer_status"]()
    payload = response["result"]
    assert payload["status"] in ("running", "not_running")
    assert payload["ui_control_supported"] is False
