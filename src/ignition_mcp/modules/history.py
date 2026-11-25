"""Tag history and OEE analytics.

History queries need the gateway historian. OEE maths is pure and can run on values the
agent already has, so the calculation tools accept explicit metrics and say exactly what is
missing instead of inventing production data.
"""

from __future__ import annotations

import logging
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, unsupported
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

HISTORY_CANDIDATES = ("/historian/query", "/history/query", "/tags/history")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def query_tag_history(
        paths: list[str],
        start_date: str,
        end_date: str,
        aggregation: str = "Average",
        interval_minutes: int = 60,
    ) -> dict[str, Any]:
        """Query historical tag values.

        Args:
            paths: Tag paths to query.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            aggregation: Aggregation mode (Average, MinMax, LastValue, ...).
            interval_minutes: Aggregation interval in minutes.

        Returns:
            Historian rows and the columns returned.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        body = {
            "paths": paths,
            "startDate": start_date,
            "endDate": end_date,
            "aggregationMode": aggregation,
            "intervalMinutes": interval_minutes,
            "returnSize": -1,
        }
        try:
            _, payload = await state.gateway.first_available(
                HISTORY_CANDIDATES, method="POST", json_body=body
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "query_tag_history")
        if isinstance(payload, list):
            rows = payload
        else:
            rows = payload.get("rows") or payload.get("results") or []
        return ok(
            state,
            "query_tag_history",
            {"paths": paths, "rows": rows, "count": len(rows) if isinstance(rows, list) else None},
        )

    @mcp.tool()
    async def calculate_oee(
        line: str,
        start_date: str,
        end_date: str,
        planned_time_minutes: float | None = None,
        run_time_minutes: float | None = None,
        total_count: float | None = None,
        good_count: float | None = None,
        ideal_cycle_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Calculate OEE = Availability × Performance × Quality.

        Args:
            line: Production line label (echoed in the result).
            start_date: ISO-8601 window start.
            end_date: ISO-8601 window end.
            planned_time_minutes: Scheduled production time.
            run_time_minutes: Actual running time.
            total_count: Total units produced.
            good_count: Units that passed quality checks.
            ideal_cycle_seconds: Ideal seconds per unit.

        Returns:
            Availability, performance, quality and OEE (0–1 and percentages).
        """
        metrics = _oee_metrics(
            planned_time_minutes,
            run_time_minutes,
            total_count,
            good_count,
            ideal_cycle_seconds,
        )
        missing = [name for name, value in metrics.items() if value is None]
        if missing:
            return state.tool_response(
                {
                    "status": "insufficient_data",
                    "line": line,
                    "window": {"start": start_date, "end": end_date},
                    "missing_inputs": missing,
                    "hint": (
                        "OEE needs planned_time_minutes, run_time_minutes, total_count, "
                        "good_count and ideal_cycle_seconds. Query tag history first and pass "
                        "the values in."
                    ),
                    "partial": metrics,
                }
            )
        return ok(
            state,
            "calculate_oee",
            {
                "line": line,
                "window": {"start": start_date, "end": end_date},
                "components": metrics,
                "percentages": {
                    key: round(value * 100, 2)
                    for key, value in metrics.items()
                    if value is not None
                },
            },
        )

    @mcp.tool()
    async def calculate_availability(
        line: str,
        start_date: str,
        end_date: str,
        planned_time_minutes: float | None = None,
        run_time_minutes: float | None = None,
    ) -> dict[str, Any]:
        """Calculate availability (run time ÷ planned time).

        Args:
            line: Production line label.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            planned_time_minutes: Scheduled production time.
            run_time_minutes: Actual running time.

        Returns:
            Availability as a fraction and percentage.
        """
        return _single_component(
            state,
            "calculate_availability",
            line,
            start_date,
            end_date,
            "availability",
            _ratio(run_time_minutes, planned_time_minutes),
        )

    @mcp.tool()
    async def calculate_performance(
        line: str,
        start_date: str,
        end_date: str,
        run_time_minutes: float | None = None,
        total_count: float | None = None,
        ideal_cycle_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Calculate performance ((ideal cycle × count) ÷ run time).

        Args:
            line: Production line label.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            run_time_minutes: Actual running time.
            total_count: Total units produced.
            ideal_cycle_seconds: Ideal seconds per unit.

        Returns:
            Performance as a fraction and percentage.
        """
        value: float | None = None
        if run_time_minutes and total_count is not None and ideal_cycle_seconds is not None:
            cycle: float = ideal_cycle_seconds
            count: float = total_count
            value = (cycle * count) / (run_time_minutes * 60)
        return _single_component(
            state,
            "calculate_performance",
            line,
            start_date,
            end_date,
            "performance",
            value,
        )

    @mcp.tool()
    async def calculate_quality(
        line: str,
        start_date: str,
        end_date: str,
        total_count: float | None = None,
        good_count: float | None = None,
    ) -> dict[str, Any]:
        """Calculate quality (good count ÷ total count).

        Args:
            line: Production line label.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            total_count: Total units produced.
            good_count: Units that passed quality checks.

        Returns:
            Quality as a fraction and percentage.
        """
        return _single_component(
            state,
            "calculate_quality",
            line,
            start_date,
            end_date,
            "quality",
            _ratio(good_count, total_count),
        )

    @mcp.tool()
    async def get_production_counts(
        line: str, start_date: str, end_date: str, count_tag: str | None = None
    ) -> dict[str, Any]:
        """Read production counts for a line from a count tag.

        Args:
            line: Production line label.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            count_tag: Tag path holding the production count.

        Returns:
            Historian rows for the count tag, or a hint when no tag is configured.
        """
        tag_path = count_tag or (line if line.startswith("[") else None)
        if not tag_path:
            return state.tool_response(
                {
                    "status": "insufficient_data",
                    "line": line,
                    "hint": "Pass count_tag (for example '[default]Line1/TotalCount').",
                }
            )
        return await query_tag_history([tag_path], start_date, end_date, "LastValue", 60)

    @mcp.tool()
    async def get_machine_runtime(tag_path: str, start_date: str, end_date: str) -> dict[str, Any]:
        """Read a machine's running-state history.

        Args:
            tag_path: Boolean running tag.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.

        Returns:
            Historian rows for the running tag.
        """
        return await query_tag_history([tag_path], start_date, end_date, "Average", 60)

    @mcp.tool()
    async def get_downtime(
        line: str, start_date: str, end_date: str, state_tag: str | None = None
    ) -> dict[str, Any]:
        """Read downtime events for a line.

        Args:
            line: Production line label.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            state_tag: Tag path holding machine state.

        Returns:
            Historian rows for the state tag, or a hint when no tag is configured.
        """
        tag_path = state_tag or (line if line.startswith("[") else None)
        if not tag_path:
            return state.tool_response(
                {
                    "status": "insufficient_data",
                    "line": line,
                    "hint": "Pass state_tag (for example '[default]Line1/State').",
                }
            )
        return await query_tag_history([tag_path], start_date, end_date, "MinMax", 60)

    @mcp.tool()
    async def compare_shift_production(
        lines: list[str], date: str, count_tag_suffix: str = "TotalCount"
    ) -> dict[str, Any]:
        """Compare production counts across lines for one day.

        Args:
            lines: Line tag prefixes, e.g. ``["[default]Line1"]``.
            date: Day to compare (``YYYY-MM-DD``).
            count_tag_suffix: Tag appended to each line prefix.

        Returns:
            Per-line historian rows for the day.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        start = f"{date}T00:00:00"
        end = f"{date}T23:59:59"
        results: dict[str, Any] = {}
        for line in lines:
            tag_path = f"{line}/{count_tag_suffix}" if not line.endswith(count_tag_suffix) else line
            try:
                _, payload = await state.gateway.first_available(
                    HISTORY_CANDIDATES,
                    method="POST",
                    json_body={
                        "paths": [tag_path],
                        "startDate": start,
                        "endDate": end,
                        "aggregationMode": "LastValue",
                    },
                )
            except GatewayError as exc:
                results[line] = {"status": "error", "message": exc.message}
                continue
            results[line] = payload
        return ok(state, "compare_shift_production", {"date": date, "lines": results})

    @mcp.tool()
    async def generate_production_report(
        lines: list[str],
        start_date: str,
        end_date: str,
        format: str = "json",
    ) -> dict[str, Any]:
        """Generate a production report for several lines.

        Args:
            lines: Line tag prefixes.
            start_date: ISO-8601 start.
            end_date: ISO-8601 end.
            format: ``json`` or ``csv``.

        Returns:
            A report document; CSV is returned as text inside the JSON envelope.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                _, payload = await state.gateway.first_available(
                    HISTORY_CANDIDATES,
                    method="POST",
                    json_body={
                        "paths": [line],
                        "startDate": start_date,
                        "endDate": end_date,
                        "aggregationMode": "Average",
                    },
                )
            except GatewayError as exc:
                rows.append({"line": line, "status": "error", "message": exc.message})
                continue
            rows.append({"line": line, "status": "ok", "data": payload})

        if format.lower() == "csv":
            lines_out = ["line,status,data"]
            for row in rows:
                lines_out.append(
                    f'{row["line"]},{row["status"]},"{row.get("data", row.get("message", ""))}"'
                )
            return ok(
                state,
                "generate_production_report",
                {"format": "csv", "csv": "\n".join(lines_out)},
            )
        return ok(
            state,
            "generate_production_report",
            {
                "format": "json",
                "window": {"start": start_date, "end": end_date},
                "lines": rows,
            },
        )


# ----------------------------------------------------------------------
# OEE maths (pure, unit tested)
# ----------------------------------------------------------------------


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Return ``numerator / denominator`` or ``None`` when inputs are unusable."""
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def _oee_metrics(
    planned_time_minutes: float | None,
    run_time_minutes: float | None,
    total_count: float | None,
    good_count: float | None,
    ideal_cycle_seconds: float | None,
) -> dict[str, float | None]:
    """Compute the OEE components, returning ``None`` for missing inputs."""
    availability = _ratio(run_time_minutes, planned_time_minutes)
    performance: float | None = None
    if run_time_minutes and total_count is not None and ideal_cycle_seconds is not None:
        cycle: float = ideal_cycle_seconds
        count: float = total_count
        performance = (cycle * count) / (run_time_minutes * 60)
    quality = _ratio(good_count, total_count)
    oee = None
    if availability is not None and performance is not None and quality is not None:
        oee = availability * performance * quality
    return {
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee": oee,
    }


def _single_component(
    state: ServerState,
    tool: str,
    line: str,
    start_date: str,
    end_date: str,
    name: str,
    value: float | None,
) -> dict[str, Any]:
    """Return a one-component OEE payload with an explicit missing-input status."""
    if value is None:
        return state.tool_response(
            {
                "status": "insufficient_data",
                "line": line,
                "component": name,
                "window": {"start": start_date, "end": end_date},
                "hint": f"Pass the inputs required to compute {name}.",
            }
        )
    return ok(
        state,
        tool,
        {
            "line": line,
            "window": {"start": start_date, "end": end_date},
            name: value,
            f"{name}_percent": round(value * 100, 2),
        },
    )


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "The historian query API is not present on this gateway. OEE maths still "
                "works when you pass metrics explicitly."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
