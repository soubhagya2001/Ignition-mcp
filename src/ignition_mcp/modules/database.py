"""Database connections and named queries.

Named queries are project resources and are read from disk when available. Connection
status and query execution require the gateway (or a direct JDBC connection, which the MCP
deliberately does not open).
"""

from __future__ import annotations

import logging
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, resolve_project, unsupported
from ignition_mcp.safety import preview_json_change
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

CONNECTION_CANDIDATES = ("/connections", "/databases/connections")
EXEC_CANDIDATES = ("/query/execute", "/db/execQuery", "/named-query/execute")
NAMED_QUERY_EXEC_CANDIDATES = (
    "/named-queries/execute",
    "/query/named/execute",
    "/named-query/execute",
)

READONLY_PREFIXES = ("select", "with", "show", "explain", "describe")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_database_connections() -> dict[str, Any]:
        """List database connections and their status.

        Returns:
            Connection names from the local config database or the gateway API.
        """
        reader = state.local_reader
        if reader is not None:
            info = await reader.read()
            if info.db_connections:
                return ok(
                    state,
                    "list_database_connections",
                    {"source": "local_config_db", "connections": info.db_connections},
                )
        if not state.has_gateway:
            return unsupported(
                state,
                "list_database_connections",
                hint="Set IGNITION_INSTALL_PATH to read connections from config.idb.",
            )
        try:
            _, payload = await state.gateway.first_available(CONNECTION_CANDIDATES)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "list_database_connections")
        return ok(
            state,
            "list_database_connections",
            {"source": "gateway_api", "connections": _items(payload)},
        )

    @mcp.tool()
    async def test_db_connection(connection_name: str) -> dict[str, Any]:
        """Test a database connection.

        Args:
            connection_name: Name of the configured connection.

        Returns:
            Connection status and latency when reported by the gateway.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                (f"/connections/{connection_name}/test", "/connections/test"),
                method="POST",
                json_body={"name": connection_name},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "test_db_connection")
        return ok(
            state,
            "test_db_connection",
            {"connection": connection_name, "result": payload},
        )

    @mcp.tool()
    async def create_db_connection(
        name: str,
        driver: str,
        connect_url: str,
        username: str,
        password: str,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a database connection.

        Args:
            name: Connection name.
            driver: JDBC driver class.
            connect_url: JDBC connection URL.
            username: Database username.
            password: Database password (never logged).
            confirm: Must be ``True`` to create.
            dry_run: Preview without creating.

        Returns:
            Creation acknowledgement or a guard/preview payload.
        """
        guard = state.write_guard("create_db_connection", confirm, dry_run)
        if guard:
            return guard
        body = {
            "name": name,
            "driver": driver,
            "connectUrl": connect_url,
            "username": username,
            "password": password,
        }
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "would_create": {
                        key: value for key, value in body.items() if key != "password"
                    },
                }
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                CONNECTION_CANDIDATES, method="POST", json_body=body
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "create_db_connection")
        state.record_action("create_db_connection", "ok", connection=name)
        return state.tool_response({"status": "created", "connection": name, "response": payload})

    @mcp.tool()
    async def list_named_queries(project: str | None = None) -> dict[str, Any]:
        """List named queries in a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Named query paths.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is not None:
            queries = reader.list_named_queries(proj or "")
            return ok(
                state,
                "list_named_queries",
                {
                    "project": proj,
                    "named_queries": queries,
                    "count": len(queries),
                    "source": "local_filesystem",
                },
            )
        return unsupported(
            state,
            "list_named_queries",
            hint="Named queries are project resources; set IGNITION_INSTALL_PATH.",
        )

    @mcp.tool()
    async def read_named_query(query_path: str, project: str | None = None) -> dict[str, Any]:
        """Read a named query's SQL and parameters.

        Args:
            query_path: Named query path, e.g. ``Production/GetCounts``.
            project: Project name; defaults to the active project.

        Returns:
            The decoded ``query.json`` resource.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "read_named_query",
                hint="Named queries are project resources; set IGNITION_INSTALL_PATH.",
            )
        data = reader.read_named_query(proj or "", query_path)
        if data is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "query_path": query_path}
            )
        return ok(
            state,
            "read_named_query",
            {"project": proj, "query_path": query_path, "query": data},
        )

    @mcp.tool()
    async def create_named_query(
        path: str,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        query_type: str = "query",
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a named query resource.

        Args:
            path: Named query path, e.g. ``Production/GetCounts``.
            query: SQL text with named parameters such as ``:line``.
            parameters: Parameter definitions with ``name`` and ``type``.
            query_type: Ignition query type, normally ``query``.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Preview the change without writing.

        Returns:
            Write acknowledgement or a dry-run diff.
        """
        guard = state.write_guard("create_named_query", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "create_named_query",
                hint="Named queries are project resources; set IGNITION_INSTALL_PATH.",
            )
        data = _named_query_payload(query, parameters, query_type)
        issues = _named_query_issues(data)
        existing = reader.read_named_query(proj or "", path)
        if existing is not None:
            issues.append(f"Named query '{path}' already exists; use write_named_query.")
        return await _finish_named_query_write(
            state, reader, proj or "", path, existing, data, issues, dry_run, "create_named_query"
        )

    @mcp.tool()
    async def write_named_query(
        path: str,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        query_type: str = "query",
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or replace a named query resource.

        Args:
            path: Named query path.
            query: SQL text with named parameters.
            parameters: Parameter definitions with ``name`` and ``type``.
            query_type: Ignition query type, normally ``query``.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Preview the change without writing.

        Returns:
            Write acknowledgement or a dry-run diff.
        """
        guard = state.write_guard("write_named_query", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "write_named_query",
                hint="Named queries are project resources; set IGNITION_INSTALL_PATH.",
            )
        data = _named_query_payload(query, parameters, query_type)
        issues = _named_query_issues(data)
        existing = reader.read_named_query(proj or "", path)
        return await _finish_named_query_write(
            state, reader, proj or "", path, existing, data, issues, dry_run, "write_named_query"
        )

    @mcp.tool()
    async def delete_named_query(
        path: str,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete a named query resource.

        Args:
            path: Named query path.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to delete.
            dry_run: Preview the deletion without writing.

        Returns:
            Deletion acknowledgement or a dry-run preview.
        """
        guard = state.write_guard("delete_named_query", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "delete_named_query",
                hint="Named queries are project resources; set IGNITION_INSTALL_PATH.",
            )
        existing = reader.read_named_query(proj or "", path)
        if existing is None:
            return state.tool_response({"status": "not_found", "project": proj, "query_path": path})
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "project": proj,
                    "query_path": path,
                    "valid": True,
                    "would_delete": existing,
                    "diff": preview_json_change(existing, None, path),
                }
            )
        deleted = reader.delete_named_query(proj or "", path)
        if deleted is None:
            return state.tool_response({"status": "delete_failed", "query_path": path})
        verified = reader.read_named_query(proj or "", path) is None
        state.record_action("delete_named_query", "ok", project=proj, query=path)
        return state.tool_response(
            {
                "status": "deleted",
                "project": proj,
                "query_path": path,
                "path": str(deleted),
                "verified": verified,
            }
        )

    @mcp.tool()
    async def inspect_named_query_parameters(
        query_path: str, project: str | None = None
    ) -> dict[str, Any]:
        """Inspect a named query's parameter definitions.

        Args:
            query_path: Named query path.
            project: Project name; defaults to the active project.

        Returns:
            Parameter definitions with type and default value.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "inspect_named_query_parameters",
                hint="Named queries are project resources; set IGNITION_INSTALL_PATH.",
            )
        data = reader.read_named_query(proj or "", query_path)
        if data is None:
            return state.tool_response({"status": "not_found", "query_path": query_path})
        parameters = data.get("parameters") or data.get("params") or []
        return ok(
            state,
            "inspect_named_query_parameters",
            {"query_path": query_path, "parameters": parameters},
        )

    @mcp.tool()
    async def validate_sql(sql: str, dialect: str = "generic") -> dict[str, Any]:
        """Validate SQL syntax without touching a database.

        Args:
            sql: SQL text.
            dialect: Dialect hint (informational; no parser is dialect-specific here).

        Returns:
            Validity and any structural issues found.
        """
        issues = _validate_sql(sql)
        return ok(
            state,
            "validate_sql",
            {
                "valid": not issues,
                "issues": issues,
                "dialect": dialect,
                "statement_type": _statement_type(sql),
            },
        )

    @mcp.tool()
    async def execute_readonly_query(
        datasource: str,
        query: str,
        params: list[Any] | None = None,
        max_rows: int = 100,
    ) -> dict[str, Any]:
        """Execute a read-only SQL query.

        Args:
            datasource: Database connection name.
            query: SELECT/WITH statement.
            params: Positional parameters.
            max_rows: Maximum rows to request.

        Returns:
            Query rows or a rejection payload for non-read statements.
        """
        if _statement_type(query) not in READONLY_PREFIXES:
            return state.tool_response(
                {
                    "status": "rejected",
                    "message": "Only SELECT/WITH/SHOW/EXPLAIN statements are allowed.",
                    "statement_type": _statement_type(query),
                }
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                EXEC_CANDIDATES,
                method="POST",
                json_body={
                    "datasource": datasource,
                    "query": query,
                    "params": params or [],
                    "maxRows": max_rows,
                },
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "execute_readonly_query")
        rows = _rows(payload)
        return ok(
            state,
            "execute_readonly_query",
            {"datasource": datasource, "rows": rows, "count": len(rows), "raw": payload},
        )

    @mcp.tool()
    async def test_named_query(
        query_path: str,
        params: dict[str, Any] | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Execute a named query with test parameters.

        Args:
            query_path: Named query path.
            params: Named parameters.
            project: Project name; defaults to the active project.

        Returns:
            Query rows or an error payload.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                NAMED_QUERY_EXEC_CANDIDATES,
                method="POST",
                json_body={"project": proj, "path": query_path, "parameters": params or {}},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "test_named_query")
        rows = _rows(payload)
        return ok(
            state,
            "test_named_query",
            {"query_path": query_path, "rows": rows, "count": len(rows), "raw": payload},
        )

    @mcp.tool()
    async def find_queries_by_table(
        table_name: str,
        column_name: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Find named queries that reference a table or column.

        Args:
            table_name: Table to search for (case-insensitive).
            column_name: Optional column to require as well.
            project: Project name; defaults to the active project.

        Returns:
            Matching query paths.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "find_queries_by_table",
                hint="Named query search needs local project files (IGNITION_INSTALL_PATH).",
            )
        matches: list[dict[str, str]] = []
        for query_path in reader.list_named_queries(proj or ""):
            data = reader.read_named_query(proj or "", query_path) or {}
            sql = str(data.get("query", ""))
            if table_name.lower() not in sql.lower():
                continue
            if column_name and column_name.lower() not in sql.lower():
                continue
            matches.append({"query_path": query_path, "query_type": str(data.get("queryType", ""))})
        return ok(
            state,
            "find_queries_by_table",
            {
                "table": table_name,
                "column": column_name,
                "matches": matches,
                "count": len(matches),
            },
        )

    @mcp.tool()
    async def find_database_references(
        connection_name: str, project: str | None = None
    ) -> dict[str, Any]:
        """Find project resources that reference a database connection.

        Args:
            connection_name: Connection name to search for.
            project: Project name; defaults to the active project.

        Returns:
            Matching resources and lines.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "find_database_references",
                hint="Reference search needs local project files (IGNITION_INSTALL_PATH).",
            )
        chain = reader.inheritance_chain(proj or "")
        matches = reader.search_text(
            proj or "", connection_name, projects=chain[1:], max_results=300
        )
        return ok(
            state,
            "find_database_references",
            {
                "connection": connection_name,
                "project": proj,
                "matches": matches,
                "count": len(matches),
            },
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _named_query_payload(
    query: str, parameters: list[dict[str, Any]] | None, query_type: str
) -> dict[str, Any]:
    """Build the verified ``query.json`` shape."""
    return {
        "query": query,
        "parameters": parameters or [],
        "queryType": query_type,
    }


def _named_query_issues(data: dict[str, Any]) -> list[str]:
    """Validate a named query payload before writing."""
    issues = _validate_sql(str(data.get("query", "")))
    if not data.get("queryType"):
        issues.append("queryType must not be empty.")
    parameters = data.get("parameters")
    if not isinstance(parameters, list):
        issues.append("parameters must be a list.")
        return issues
    for index, parameter in enumerate(parameters):
        if not isinstance(parameter, dict):
            issues.append(f"Parameter {index} must be an object.")
            continue
        if not parameter.get("name"):
            issues.append(f"Parameter {index} is missing 'name'.")
        if not parameter.get("type"):
            issues.append(f"Parameter {index} is missing 'type'.")
    return issues


async def _finish_named_query_write(
    state: ServerState,
    reader: Any,
    project: str,
    path: str,
    existing: dict[str, Any] | None,
    data: dict[str, Any],
    issues: list[str],
    dry_run: bool,
    tool: str,
) -> dict[str, Any]:
    """Preview or persist a named query and verify the result."""
    if dry_run:
        return state.tool_response(
            {
                "status": "dry_run",
                "project": project,
                "query_path": path,
                "valid": not issues,
                "issues": issues,
                "diff": preview_json_change(existing, data, path),
            }
        )
    if issues:
        return state.tool_response(
            {"status": "invalid_named_query", "issues": issues, "query_path": path}
        )
    written = reader.write_named_query(project, path, data)
    if written is None:
        return state.tool_response({"status": "write_failed", "query_path": path})
    verified = reader.read_named_query(project, path) == data
    state.record_action(tool, "ok", project=project, query=path)
    return state.tool_response(
        {
            "status": "created" if existing is None else "updated",
            "project": project,
            "query_path": path,
            "path": str(written),
            "verified": verified,
        }
    )


def _validate_sql(sql: str) -> list[str]:
    """Perform structural SQL checks without a dialect-specific parser.

    Args:
        sql: SQL text.

    Returns:
        Issue strings; empty when the statement looks valid.
    """
    stripped = sql.strip()
    issues: list[str] = []
    if not stripped:
        return ["SQL is empty"]
    if stripped.count("'") % 2:
        issues.append("Unbalanced single quotes")
    if stripped.count("(") != stripped.count(")"):
        issues.append("Unbalanced parentheses")
    if _statement_type(stripped) not in (
        "select",
        "with",
        "insert",
        "update",
        "delete",
        "show",
        "explain",
        "describe",
        "create",
        "alter",
        "drop",
    ):
        issues.append(f"Unrecognised statement start: '{stripped.split()[0]}'")
    return issues


def _statement_type(sql: str) -> str:
    """Return the lowercase leading keyword of a statement."""
    stripped = sql.strip().lstrip("(")
    return stripped.split()[0].lower() if stripped.split() else ""


def _items(payload: Any) -> list[Any]:
    """Extract a list of connection entries."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("connections", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _rows(payload: Any) -> list[Any]:
    """Extract result rows from a query response."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("rows", "results", "data", "values"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "The database/query API is not present on this gateway. Named query "
                "definitions are still readable from disk."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
