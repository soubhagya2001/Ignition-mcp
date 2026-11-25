"""Tests for named-query resource CRUD."""

from __future__ import annotations

from typing import Any

from ignition_mcp.modules import database
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


def _tools(local_state: ServerState) -> dict[str, Any]:
    mcp = RecordingMCP()
    database.register(mcp, local_state)
    return mcp.tools


async def test_named_query_create_update_delete(local_state: ServerState):
    """A named query round-trips through create, update and delete."""
    tools = _tools(local_state)

    created = await tools["create_named_query"](
        "Production/NewQuery",
        "SELECT count FROM production WHERE line = :line",
        [{"name": "line", "type": "String"}],
        confirm=True,
    )
    assert created["result"]["status"] == "created"
    assert created["result"]["verified"] is True

    updated = await tools["write_named_query"](
        "Production/NewQuery",
        "SELECT count FROM production WHERE line = :line AND active = :active",
        [
            {"name": "line", "type": "String"},
            {"name": "active", "type": "Boolean"},
        ],
        confirm=True,
    )
    assert updated["result"]["status"] == "updated"

    reader = local_state.local_reader
    assert reader is not None
    data = reader.read_named_query("TestProject", "Production/NewQuery")
    assert data is not None
    assert "active" in data["query"]
    assert len(data["parameters"]) == 2

    deleted = await tools["delete_named_query"]("Production/NewQuery", confirm=True)
    assert deleted["result"]["status"] == "deleted"
    assert deleted["result"]["verified"] is True
    assert reader.read_named_query("TestProject", "Production/NewQuery") is None


async def test_named_query_dry_run_and_invalid_sql(local_state: ServerState):
    """Dry-run returns a diff and invalid SQL is rejected."""
    tools = _tools(local_state)

    dry_run = await tools["create_named_query"](
        "Production/DryRun",
        "SELECT count FROM production",
        dry_run=True,
    )
    assert dry_run["result"]["status"] == "dry_run"
    assert dry_run["result"]["valid"] is True
    assert dry_run["result"]["diff"]["changed"] is True

    invalid = await tools["create_named_query"](
        "Production/Bad",
        "",
        confirm=True,
    )
    assert invalid["result"]["status"] == "invalid_named_query"
    assert invalid["result"]["issues"]
