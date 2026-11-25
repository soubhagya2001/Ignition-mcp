"""Tests for Perspective embedded script inspection and targeted writes."""

from __future__ import annotations

from typing import Any

from ignition_mcp.modules import perspective
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
    perspective.register(mcp, local_state)
    return mcp.tools


def _seed_embedded_scripts(local_state: ServerState) -> None:
    reader = local_state.local_reader
    assert reader is not None
    view = reader.read_perspective_view("TestProject", "Dashboard/Overview")
    assert view is not None
    title = view["root"]["children"][0]
    title["props"]["text"]["binding"]["transforms"] = [
        {"type": "script", "code": "\treturn value * 2"}
    ]
    title["propConfig"] = {
        "props.value": {
            "onChange": {
                "enabled": True,
                "script": "\tif currentValue.value > 10:\n\t\tpass",
            }
        }
    }
    title["events"] = {
        "onActionPerformed": {
            "type": "script",
            "scope": "G",
            "config": {"script": "\tpass"},
        }
    }
    title["scripts"] = {
        "messageHandlers": [
            {
                "messageType": "Refresh",
                "pageScope": True,
                "sessionScope": False,
                "viewScope": False,
                "script": "\tpass",
            }
        ]
    }
    reader.write_perspective_view("TestProject", "Dashboard/Overview", view)


async def test_embedded_scripts_are_listed_and_validated(local_state: ServerState):
    """Bindings, transforms, property changes, events and handlers are discoverable."""
    _seed_embedded_scripts(local_state)
    tools = _tools(local_state)

    bindings = (await tools["list_bindings"]("Dashboard/Overview"))["result"]["bindings"]
    scripts = (await tools["list_view_scripts"]("Dashboard/Overview"))["result"]
    validation = (await tools["validate_view_scripts"]("Dashboard/Overview"))["result"]

    assert bindings[0]["transforms"][0]["code"].startswith("\treturn")
    assert {script["kind"] for script in scripts["scripts"]} == {
        "event",
        "transform",
        "property_change",
        "message_handler",
    }
    assert validation["valid"] is True
    assert validation["issue_count"] == 0


async def test_targeted_script_writes_change_only_the_target(local_state: ServerState):
    """Targeted writes update one script and preserve the rest of the view."""
    _seed_embedded_scripts(local_state)
    tools = _tools(local_state)

    response = await tools["set_event_script"](
        "Dashboard/Overview",
        "title",
        "onActionPerformed",
        "\tsystem.perspective.print('clicked')",
        confirm=True,
    )
    assert response["result"]["status"] == "updated"

    reader = local_state.local_reader
    assert reader is not None
    view = reader.read_perspective_view("TestProject", "Dashboard/Overview")
    assert view is not None
    title = view["root"]["children"][0]
    assert "clicked" in title["events"]["onActionPerformed"]["config"]["script"]
    assert title["propConfig"]["props.value"]["onChange"]["script"].startswith("\tif")
    assert title["scripts"]["messageHandlers"][0]["messageType"] == "Refresh"


async def test_transform_and_property_change_writes(local_state: ServerState):
    """Transform and property-change writes round-trip through disk."""
    _seed_embedded_scripts(local_state)
    tools = _tools(local_state)

    transform_response = await tools["set_transform"](
        "Dashboard/Overview",
        "title",
        "props.text",
        {"type": "script", "code": "\treturn value + 1"},
        index=0,
        confirm=True,
    )
    property_response = await tools["set_property_change_script"](
        "Dashboard/Overview",
        "title",
        "props.value",
        "\tif currentValue.value > 5:\n\t\tpass",
        confirm=True,
    )

    assert transform_response["result"]["status"] == "updated"
    assert property_response["result"]["status"] == "updated"

    reader = local_state.local_reader
    assert reader is not None
    view = reader.read_perspective_view("TestProject", "Dashboard/Overview")
    assert view is not None
    title = view["root"]["children"][0]
    assert title["props"]["text"]["binding"]["transforms"][0]["code"] == "\treturn value + 1"
    assert "> 5" in title["propConfig"]["props.value"]["onChange"]["script"]


async def test_message_handler_write_and_delete(local_state: ServerState):
    """Message handlers can be replaced and deleted by message type."""
    _seed_embedded_scripts(local_state)
    tools = _tools(local_state)

    set_response = await tools["set_message_handler"](
        "Dashboard/Overview",
        "title",
        "Refresh",
        "\tself.refreshBinding('props.text')",
        session_scope=True,
        confirm=True,
    )
    delete_response = await tools["delete_message_handler"](
        "Dashboard/Overview", "title", "Refresh", confirm=True
    )

    assert set_response["result"]["status"] == "updated"
    assert delete_response["result"]["status"] == "updated"

    reader = local_state.local_reader
    assert reader is not None
    view = reader.read_perspective_view("TestProject", "Dashboard/Overview")
    assert view is not None
    assert view["root"]["children"][0]["scripts"]["messageHandlers"] == []


async def test_invalid_script_is_rejected_before_write(local_state: ServerState):
    """A transform without a return is rejected."""
    _seed_embedded_scripts(local_state)
    tools = _tools(local_state)

    response = await tools["set_transform"](
        "Dashboard/Overview",
        "title",
        "props.text",
        {"type": "script", "code": "\tvalue + 1"},
        index=0,
        confirm=True,
    )

    assert response["result"]["status"] == "invalid_view"
    assert any("missing_transform_return" in issue for issue in response["result"]["issues"])
