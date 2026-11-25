"""Alarms and events.

Alarm state and history live in the gateway (or its alarm journal database), so these tools
require a gateway connection. Aggregations are computed locally from the returned events so
the agent receives ready-to-reason-about statistics.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, iso_now, ok, resolve_project, unsupported
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

ACTIVE_CANDIDATES = ("/alarms/active", "/alarm/status")
HISTORY_CANDIDATES = ("/alarms/history", "/alarm/history")
CONFIG_CANDIDATES = ("/alarms/configuration", "/alarm/configuration")
PIPELINE_CANDIDATES = ("/alarm-notification/pipelines", "/alarm/pipelines")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_alarm_configurations(project: str | None = None) -> dict[str, Any]:
        """List configured alarms.

        Args:
            project: Optional project filter.

        Returns:
            Alarm configuration entries.
        """
        return await _query(
            state,
            "list_alarm_configurations",
            CONFIG_CANDIDATES,
            method="GET",
            params={"project": project} if project else None,
        )

    @mcp.tool()
    async def get_active_alarms(
        source_filter: str = "*",
        state_filter: str = "*",
        priority_filter: str | None = None,
        max_results: int = 1000,
    ) -> dict[str, Any]:
        """Get currently active alarms.

        Args:
            source_filter: Tag/source filter (``*`` for all).
            state_filter: Alarm state filter (``*`` for all).
            priority_filter: Optional priority filter.
            max_results: Maximum alarms to return.

        Returns:
            Active alarm entries and their count.
        """
        body = {
            "source": source_filter,
            "state": state_filter,
            "maxResults": max_results,
        }
        if priority_filter:
            body["priority"] = priority_filter
        return await _query(
            state, "get_active_alarms", ACTIVE_CANDIDATES, method="POST", json_body=body
        )

    @mcp.tool()
    async def get_alarm_history(
        start_date: str,
        end_date: str,
        source_filter: str = "*",
        max_results: int = 1000,
    ) -> dict[str, Any]:
        """Get alarm journal entries for a time range.

        Args:
            start_date: ISO-8601 start timestamp.
            end_date: ISO-8601 end timestamp.
            source_filter: Tag/source filter.
            max_results: Maximum events to return.

        Returns:
            Historical alarm events.
        """
        return await _query(
            state,
            "get_alarm_history",
            HISTORY_CANDIDATES,
            method="POST",
            json_body={
                "startDate": start_date,
                "endDate": end_date,
                "source": source_filter,
                "maxResults": max_results,
            },
        )

    @mcp.tool()
    async def get_alarm_pipeline(pipeline_name: str | None = None) -> dict[str, Any]:
        """Read alarm notification pipeline configuration.

        Args:
            pipeline_name: Optional pipeline to select.

        Returns:
            Pipeline configuration entries.
        """
        payload = await _query(state, "get_alarm_pipeline", PIPELINE_CANDIDATES, method="GET")
        if pipeline_name and payload["result"].get("items"):
            items = [
                item
                for item in payload["result"]["items"]
                if str(item.get("name", "")).lower() == pipeline_name.lower()
            ]
            payload["result"]["items"] = items
        return payload

    @mcp.tool()
    async def inspect_alarm_properties(tag_path: str) -> dict[str, Any]:
        """Read the alarm definitions attached to a tag.

        Args:
            tag_path: Tag path to inspect.

        Returns:
            Alarm definitions from the tag configuration.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ("/tags/configuration", "/tags/config"),
                method="POST",
                json_body={"paths": [tag_path]},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "inspect_alarm_properties")

        alarms = _find_alarms(payload)
        return ok(
            state,
            "inspect_alarm_properties",
            {"tag_path": tag_path, "alarms": alarms, "count": len(alarms)},
        )

    @mcp.tool()
    async def find_alarm_tag_references(pipeline_name: str) -> dict[str, Any]:
        """Find project resources that reference an alarm pipeline.

        Args:
            pipeline_name: Pipeline name to search for.

        Returns:
            Matching files and lines.
        """
        proj, error = resolve_project(state, None)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "find_alarm_tag_references",
                hint="Reference search needs local project files (IGNITION_INSTALL_PATH).",
            )
        matches = reader.search_text(proj or "", pipeline_name, max_results=300)
        return ok(
            state,
            "find_alarm_tag_references",
            {
                "pipeline": pipeline_name,
                "project": proj,
                "matches": matches,
                "count": len(matches),
            },
        )

    @mcp.tool()
    async def analyze_alarm_frequency(days: int = 7, top_n: int = 10) -> dict[str, Any]:
        """Rank the most frequent alarms over the last N days.

        Args:
            days: Look-back window in days.
            top_n: Number of alarms to return.

        Returns:
            Ranked sources with occurrence counts.
        """
        end = datetime.now()
        start = end - timedelta(days=days)
        payload = await _query(
            state,
            "analyze_alarm_frequency",
            HISTORY_CANDIDATES,
            method="POST",
            json_body={
                "startDate": start.isoformat(timespec="seconds"),
                "endDate": end.isoformat(timespec="seconds"),
                "source": "*",
                "maxResults": 10000,
            },
        )
        events = payload["result"].get("items", [])
        counts: dict[str, int] = {}
        for event in events:
            source = str(event.get("source") or event.get("sourcePath") or "unknown")
            counts[source] = counts.get(source, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:top_n]
        return state.tool_response(
            {
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "total_events": len(events),
                "top_alarms": [{"source": source, "count": count} for source, count in ranked],
            }
        )

    @mcp.tool()
    async def get_alarm_statistics(
        start_date: str | None = None, end_date: str | None = None
    ) -> dict[str, Any]:
        """Compute alarm statistics for a time range.

        Args:
            start_date: ISO-8601 start; defaults to 7 days ago.
            end_date: ISO-8601 end; defaults to now.

        Returns:
            Event count, acknowledgement rate and most active sources.
        """
        end = end_date or iso_now()
        start = start_date or (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
        payload = await _query(
            state,
            "get_alarm_statistics",
            HISTORY_CANDIDATES,
            method="POST",
            json_body={
                "startDate": start,
                "endDate": end,
                "source": "*",
                "maxResults": 10000,
            },
        )
        events = payload["result"].get("items", [])
        acked = sum(1 for event in events if _is_acked(event))
        counts: dict[str, int] = {}
        for event in events:
            source = str(event.get("source") or event.get("sourcePath") or "unknown")
            counts[source] = counts.get(source, 0) + 1
        top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:5]
        return state.tool_response(
            {
                "window": {"start": start, "end": end},
                "total_events": len(events),
                "acknowledged": acked,
                "ack_rate": round(acked / len(events), 3) if events else None,
                "top_sources": [{"source": s, "count": c} for s, c in top],
            }
        )


async def _query(
    state: ServerState,
    tool: str,
    candidates: tuple[str, ...],
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> dict[str, Any]:
    """Run an alarm API query and normalise the result into ``items``."""
    if not state.has_gateway:
        return state.require_connection() or state.tool_response({"status": "not_connected"})
    try:
        _, payload = await state.gateway.first_available(
            candidates, method=method, params=params, json_body=json_body
        )
    except GatewayError as exc:
        return _gateway_or_unsupported(state, exc, tool)
    items = _items(payload)
    return ok(state, tool, {"items": items, "count": len(items), "raw": payload})


def _items(payload: Any) -> list[dict[str, Any]]:
    """Extract a list of dicts from an alarm payload."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "alarms", "events", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _find_alarms(payload: Any) -> list[dict[str, Any]]:
    """Extract alarm definitions from a tag-configuration payload."""
    alarms: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("alarms"), list):
                alarms.extend(a for a in node["alarms"] if isinstance(a, dict))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return alarms


def _is_acked(event: dict[str, Any]) -> bool:
    """Return whether an alarm event was acknowledged."""
    for key in ("acknowledged", "acked", "isAcked"):
        if key in event:
            return bool(event[key])
    return bool(event.get("ackTime") or event.get("acknowledgedTime"))


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "The Alarm API is not present on this gateway. Check /openapi.json, or query "
                "the alarm journal database directly."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
