"""Shared helpers used by feature modules.

Keeping these in one place avoids five copies of the same local-reader lookup and keeps
error/unsupported payloads consistent across modules.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)


def resolve_project(
    state: ServerState, project: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the target project, falling back to the session's active project.

    Args:
        state: Shared server state.
        project: Explicit project name or ``None``.

    Returns:
        ``(project_name, error_response)``; exactly one is ``None``.
    """
    resolved = project or state.session.active_project
    if resolved:
        return resolved, None
    return None, state.tool_response(
        {
            "status": "no_project",
            "message": "No project is active and no project was supplied.",
            "hint": "Call mcp_set_project(project=...) or pass the project argument.",
            "available_projects": state.session.projects,
        }
    )


def error_response(state: ServerState, exc: Exception, tool: str = "") -> dict[str, Any]:
    """Convert an exception into a tool response with a normalised error payload.

    Args:
        state: Shared server state.
        exc: The exception to report.
        tool: Optional tool name recorded in the audit log.

    Returns:
        A tool response envelope.
    """
    if isinstance(exc, GatewayError):
        payload = exc.to_payload()
    else:
        payload = {"status": "error", "error": type(exc).__name__, "message": str(exc)}
        logger.exception("Unexpected error%s", f" in {tool}" if tool else "")
    if tool:
        state.record_action(tool, str(payload.get("status", "error")))
    return state.tool_response(payload)


def unsupported(state: ServerState, tool: str, *, hint: str = "", **extra: Any) -> dict[str, Any]:
    """Build a standard payload for behaviour this gateway cannot provide.

    Args:
        state: Shared server state.
        tool: Tool name that is unsupported.
        hint: Actionable next step for the agent.
        **extra: Extra fields merged into the payload.

    Returns:
        A tool response envelope.
    """
    payload: dict[str, Any] = {
        "status": "unsupported",
        "tool": tool,
        "message": (
            "This operation needs a gateway endpoint that is not available here, or a local "
            "Ignition install that could not be found."
        ),
    }
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    state.record_action(tool, "unsupported")
    return state.tool_response(payload)


def ok(state: ServerState, tool: str, payload: dict[str, Any], **extra_ctx: Any) -> dict[str, Any]:
    """Wrap a successful payload and record the action.

    Args:
        state: Shared server state.
        tool: Tool name.
        payload: Tool-specific result.
        **extra_ctx: Extra context keys.

    Returns:
        A tool response envelope.
    """
    state.record_action(tool, "ok")
    return state.tool_response(payload, **extra_ctx)


def iso_now() -> str:
    """Return the current local time as an ISO-8601 string."""
    return datetime.now().isoformat(timespec="seconds")


def walk_components(
    component: dict[str, Any], path: str = ""
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(path, component)`` for a Perspective component tree.

    Paths use ``/`` between components and the component's ``meta.name`` when present,
    otherwise a positional index.

    Args:
        component: Root component dict.
        path: Accumulated path prefix.

    Yields:
        Each component and its location in the tree.
    """
    if not isinstance(component, dict):
        return
    name = str(component.get("meta", {}).get("name") or "")
    node_path = f"{path}/{name}" if path else (name or "root")
    yield node_path, component
    for index, child in enumerate(component.get("children") or []):
        child_path = f"{node_path}/{index}" if not isinstance(child, dict) else node_path
        yield from walk_components(child, child_path)


def find_component(root: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Find a component by its ``meta.name``.

    Args:
        root: Root component dict.
        name: Component name to match (case-sensitive).

    Returns:
        The component dict, or ``None`` when not found.
    """
    for _, component in walk_components(root):
        if str(component.get("meta", {}).get("name", "")) == name:
            return component
    return None
