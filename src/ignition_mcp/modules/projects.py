"""Project development and deployment.

Project resources are plain files under ``data/projects/<project>``, so reads, writes,
inheritance and validation work directly on disk. Export/import/deploy need a gateway and are
implemented as best-effort calls with explicit ``unsupported`` results.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, resolve_project, unsupported
from ignition_mcp.modules.scripting import _validate_python_syntax
from ignition_mcp.safety import preview_json_change, text_diff
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

PROJECT_EXPORT_PATH = "/data/api/v1/projects/export"
IMPORT_CANDIDATES = ("/project/import", "/projects/import")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def create_project(
        name: str,
        title: str | None = None,
        description: str | None = None,
        parent: str | None = None,
        enabled: bool = True,
        inheritable: bool = False,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a blank Ignition project directly in a local installation.

        Database connections are Gateway-wide resources, not project metadata. Use
        ``create_db_connection`` to create them, then create named queries for the project.

        Args:
            name: Project directory name.
            title: Human-readable title; defaults to the name.
            description: Optional project description.
            parent: Existing local parent project.
            enabled: Whether the project is enabled.
            inheritable: Whether child projects may inherit from this project.
            confirm: Must be ``True`` to create.
            dry_run: Validate and preview without creating.

        Returns:
            Creation acknowledgement or a preview payload.
        """
        guard = state.write_guard("create_project", confirm, dry_run)
        if guard:
            return guard

        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "create_project",
                hint="Project creation needs a local Ignition install (IGNITION_INSTALL_PATH).",
            )

        invalid_name = (
            not name or name.strip() != name or "/" in name or "\\" in name or name in (".", "..")
        )
        issues: dict[str, str] = {}
        if invalid_name:
            issues["name"] = "Project name must be a single valid directory name."
        if reader.project_path(name).exists():
            issues["name"] = "A project with this name already exists."
        if parent:
            parent_path = reader.project_path(parent)
            if not parent_path.exists():
                issues["parent"] = f"Parent project '{parent}' was not found locally."

        proposed = {
            "title": title or name,
            "enabled": enabled,
            "inheritable": inheritable,
            "parent": parent,
        }
        if description:
            proposed["description"] = description

        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "valid": not issues,
                    "issues": issues,
                    "would_create": name,
                    "metadata": proposed,
                }
            )
        if issues:
            return state.tool_response(
                {"status": "invalid_project", "issues": issues, "project": name}
            )

        project_dir, create_issues = reader.create_project(
            name,
            title=title,
            description=description,
            parent=parent,
            enabled=enabled,
            inheritable=inheritable,
        )
        if project_dir is None:
            return state.tool_response(
                {"status": "create_failed", "project": name, "issues": create_issues}
            )

        state.session.projects = sorted(set(state.session.projects) | {name})
        state.session.active_project = name
        state.session.parent_project = parent
        state.record_action(
            "create_project",
            "ok",
            project=name,
            parent=parent,
            enabled=enabled,
        )
        return state.tool_response(
            {
                "status": "created",
                "project": name,
                "path": str(project_dir),
                "metadata": proposed,
                "verified": reader.read_project_meta(name) == proposed,
                "hint": (
                    "Create Gateway-wide database connections with create_db_connection, "
                    "then add named queries or binding resources to this project."
                ),
            }
        )

    @mcp.tool()
    async def list_project_resources(
        project: str | None = None, resource_type: str | None = None
    ) -> dict[str, Any]:
        """List project resources.

        Args:
            project: Project name; defaults to the active project.
            resource_type: Optional path fragment filter (``perspective``, ``script-python``).

        Returns:
            Project-relative resource paths.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "list_project_resources",
                hint="Project resources are read from disk; set IGNITION_INSTALL_PATH.",
            )
        if not reader.project_path(proj or "").exists():
            return state.tool_response(
                {"status": "not_found", "project": proj, "available": reader.list_projects()}
            )
        resources = reader.list_resources(proj or "")
        if resource_type:
            resources = [path for path in resources if resource_type in path]
        return ok(
            state,
            "list_project_resources",
            {"project": proj, "resources": resources, "count": len(resources)},
        )

    @mcp.tool()
    async def read_project_resource(
        resource_path: str, project: str | None = None
    ) -> dict[str, Any]:
        """Read any project resource.

        Args:
            resource_path: Project-relative path, e.g. ``project.json``.
            project: Project name; defaults to the active project.

        Returns:
            Decoded JSON for ``.json`` resources, text otherwise.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "read_project_resource",
                hint="Project resources are read from disk; set IGNITION_INSTALL_PATH.",
            )
        data = reader.read_resource(proj or "", resource_path)
        if data is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "resource_path": resource_path}
            )
        return ok(
            state,
            "read_project_resource",
            {"project": proj, "resource_path": resource_path, "data": data},
        )

    @mcp.tool()
    async def write_project_resource(
        resource_path: str,
        data: Any,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Write a project resource.

        Args:
            resource_path: Project-relative path.
            data: JSON-serialisable payload or raw text.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Preview without writing.

        Returns:
            Write acknowledgement or a preview payload.
        """
        guard = state.write_guard("write_project_resource", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "write_project_resource",
                hint="Resource writes need a local install (IGNITION_INSTALL_PATH).",
            )
        current = reader.read_resource(proj or "", resource_path)
        proposed_text = data if isinstance(data, str) else json.dumps(data, indent=2)
        current_text = current if isinstance(current, str) else json.dumps(current, indent=2)
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "project": proj,
                    "resource_path": resource_path,
                    "would_write_bytes": len(proposed_text.encode("utf-8")),
                    "diff": text_diff(current_text, proposed_text, resource_path),
                }
            )
        path = reader.write_resource(proj or "", resource_path, data)
        if path is None:
            return state.tool_response(
                {
                    "status": "invalid_path",
                    "message": "Resource path escapes the project directory.",
                    "resource_path": resource_path,
                }
            )
        written = reader.read_resource(proj or "", resource_path)
        state.record_action("write_project_resource", "ok", project=proj, path=resource_path)
        return state.tool_response(
            {
                "status": "written",
                "project": proj,
                "path": str(path),
                "verified": written == data,
            }
        )

    @mcp.tool()
    async def search_project_text(
        query: str,
        project: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        include_parents: bool = True,
        max_results: int = 200,
    ) -> dict[str, Any]:
        """Search project files with a literal string or regular expression.

        Args:
            query: Literal text or regex pattern.
            project: Project name; defaults to the active project.
            regex: Treat ``query`` as a regular expression.
            case_sensitive: Match case exactly (default is case-insensitive).
            include_parents: Also search inherited parent projects.
            max_results: Maximum number of matches (capped at 1000).

        Returns:
            Matching files and lines with project-relative resource paths.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "search_project_text",
                hint="Text search needs a local Ignition install (IGNITION_INSTALL_PATH).",
            )
        chain = reader.inheritance_chain(proj or "")
        matches = reader.search_text(
            proj or "",
            query,
            regex=regex,
            case_sensitive=case_sensitive,
            projects=chain[1:] if include_parents else [],
            max_results=max(1, min(max_results, 1000)),
        )
        return ok(
            state,
            "search_project_text",
            {
                "project": proj,
                "query": query,
                "regex": regex,
                "matches": matches,
                "count": len(matches),
            },
        )

    @mcp.tool()
    async def check_project_inheritance(project: str | None = None) -> dict[str, Any]:
        """Show the full project inheritance chain.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            The chain from the project up to its root parent.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "check_project_inheritance",
                hint="Inheritance is read from project.json on disk.",
            )
        chain = reader.inheritance_chain(proj or "")
        return ok(
            state,
            "check_project_inheritance",
            {
                "project": proj,
                "inheritance_chain": chain,
                "parent": chain[1] if len(chain) > 1 else None,
            },
        )

    @mcp.tool()
    async def get_parent_project(project: str | None = None) -> dict[str, Any]:
        """Get the direct parent of a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            The parent project name, or ``None``.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(state, "get_parent_project", hint="project.json is read from disk.")
        chain = reader.inheritance_chain(proj or "")
        return ok(
            state,
            "get_parent_project",
            {"project": proj, "parent": chain[1] if len(chain) > 1 else None},
        )

    @mcp.tool()
    async def validate_project(project: str | None = None) -> dict[str, Any]:
        """Validate a project's views, scripts and embedded view references.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Issues grouped by resource, plus a validity flag.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "validate_project",
                hint="Project validation needs local project files (IGNITION_INSTALL_PATH).",
            )
        if not reader.project_path(proj or "").exists():
            return state.tool_response(
                {"status": "not_found", "project": proj, "available": reader.list_projects()}
            )

        from ignition_mcp.modules.perspective import (
            _embedded_view_refs,
            _validate_view_json,
        )
        from ignition_mcp.modules.scripting import _validate_python_syntax

        issues: list[dict[str, str]] = []
        views = reader.list_perspective_views(proj or "")
        known = set(views)
        for view_path in views:
            view = reader.read_perspective_view(proj or "", view_path)
            if view is None:
                issues.append({"resource": view_path, "message": "View JSON could not be decoded"})
                continue
            for message in _validate_view_json(view):
                issues.append({"resource": view_path, "message": message})
            for reference in _embedded_view_refs(view):
                if reference not in known:
                    issues.append(
                        {
                            "resource": view_path,
                            "message": f"Embedded view '{reference}' is missing",
                        }
                    )

        for module in reader.list_scripts(proj or ""):
            code = reader.read_script(proj or "", module) or ""
            for message in _validate_python_syntax(code):
                issues.append({"resource": f"script:{module}", "message": message})

        return ok(
            state,
            "validate_project",
            {
                "project": proj,
                "views_checked": len(views),
                "scripts_checked": len(reader.list_scripts(proj or "")),
                "queries_checked": len(reader.list_named_queries(proj or "")),
                "issues": issues,
                "valid": not issues,
            },
        )

    @mcp.tool()
    async def export_project(
        project: str | None = None, output_path: str | None = None
    ) -> dict[str, Any]:
        """Export a project archive through the gateway.

        Args:
            project: Project name; defaults to the active project.
            output_path: Destination ``.zip``; defaults to
                ``exports/<project>_<timestamp>.zip``.

        Returns:
            Export acknowledgement with the saved path, size and ZIP validity.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        return await _save_project_export(
            state, proj or "", output_path, "exports", "export_project", "exported"
        )

    @mcp.tool()
    async def import_project(
        zip_path: str, confirm: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Import a project archive through the gateway.

        Args:
            zip_path: Local path to the ``.zip`` archive.
            confirm: Must be ``True`` to import.
            dry_run: Validate the archive without uploading it.

        Returns:
            Import acknowledgement or a preview payload.
        """
        guard = state.write_guard("import_project", confirm, dry_run)
        if guard:
            return guard
        archive = Path(zip_path)
        try:
            payload = await anyio.to_thread.run_sync(archive.read_bytes)
        except FileNotFoundError:
            return state.tool_response(
                {"status": "not_found", "message": f"Archive '{zip_path}' does not exist."}
            )
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "archive": str(archive),
                    "bytes": len(payload),
                    "valid_zip": zipfile.is_zipfile(archive),
                }
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, result = await state.gateway.first_available(
                IMPORT_CANDIDATES, method="POST", content=payload
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "import_project")
        state.record_action("import_project", "ok", archive=str(archive))
        return state.tool_response(
            {"status": "imported", "archive": str(archive), "response": result}
        )

    @mcp.tool()
    async def create_project_backup(
        project: str | None = None, output_path: str | None = None
    ) -> dict[str, Any]:
        """Download a project backup (a project export archive).

        Ignition calls this a project export; it contains project resources only, not tags or
        Gateway configuration. Use ``gateway_backup`` for a full ``.gwbk``.

        Args:
            project: Project name; defaults to the active project.
            output_path: Destination ``.zip``; defaults to
                ``project_backups/<project>_<timestamp>.zip``.

        Returns:
            Backup acknowledgement with the saved path, size and ZIP validity.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        return await _save_project_export(
            state,
            proj or "",
            output_path,
            "project_backups",
            "create_project_backup",
            "backup_created",
        )

    @mcp.tool()
    async def deploy_project(
        project: str | None = None, confirm: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Deploy a project to the gateway.

        Args:
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to deploy.
            dry_run: Preview without deploying.

        Returns:
            Deployment acknowledgement or a preview payload.
        """
        guard = state.write_guard("deploy_project", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        if dry_run:
            return state.tool_response(
                {"status": "dry_run", "would_deploy": proj, "gateway": state.session.gateway_url}
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                (f"/project/{proj}/deploy", "/project/deploy"),
                method="POST",
                json_body={"project": proj},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "deploy_project")
        state.record_action("deploy_project", "ok", project=proj)
        return state.tool_response({"status": "deployed", "project": proj, "response": payload})

    @mcp.tool()
    async def rollback_project(
        project: str | None = None,
        backup_id: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Roll a project back to a previous backup.

        Args:
            project: Project name; defaults to the active project.
            backup_id: Backup identifier to restore.
            confirm: Must be ``True`` to roll back.
            dry_run: Preview the rollback without changing the gateway.

        Returns:
            Rollback acknowledgement.
        """
        guard = state.write_guard("rollback_project", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "would_roll_back": proj,
                    "backup_id": backup_id,
                    "gateway": state.session.gateway_url,
                }
            )
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                ("/project/rollback", "/projects/rollback"),
                method="POST",
                json_body={"project": proj, "backupId": backup_id},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "rollback_project")
        state.record_action("rollback_project", "ok", project=proj)
        return state.tool_response({"status": "rolled_back", "project": proj, "response": payload})

    @mcp.tool()
    async def compare_project_versions(zip_a: str, zip_b: str) -> dict[str, Any]:
        """Diff two project archives by resource path.

        Args:
            zip_a: Path to the first archive.
            zip_b: Path to the second archive.

        Returns:
            Added, removed and changed resources.
        """
        path_a, path_b = Path(zip_a), Path(zip_b)
        try:
            with zipfile.ZipFile(path_a) as archive_a, zipfile.ZipFile(path_b) as archive_b:
                names_a = set(archive_a.namelist())
                names_b = set(archive_b.namelist())
                common = names_a & names_b
                changed = [name for name in common if archive_a.read(name) != archive_b.read(name)]
        except (OSError, zipfile.BadZipFile):
            return state.tool_response({"status": "invalid_archive", "paths": [zip_a, zip_b]})
        return ok(
            state,
            "compare_project_versions",
            {
                "added": sorted(names_b - names_a),
                "removed": sorted(names_a - names_b),
                "changed": sorted(changed),
                "identical": names_a == names_b and not changed,
            },
        )

    @mcp.tool()
    async def list_webdev_endpoints(project: str | None = None) -> dict[str, Any]:
        """List WebDev HTTP endpoints configured in a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Sorted WebDev endpoint paths under ``com.inductiveautomation.webdev/resources``.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "list_webdev_endpoints",
                hint="WebDev endpoints are read from disk; set IGNITION_INSTALL_PATH.",
            )
        endpoints = reader.list_webdev_endpoints(proj or "")
        return ok(
            state,
            "list_webdev_endpoints",
            {"project": proj, "endpoints": endpoints, "count": len(endpoints)},
        )

    @mcp.tool()
    async def read_webdev_endpoint(
        endpoint_path: str, project: str | None = None
    ) -> dict[str, Any]:
        """Read a WebDev endpoint's ``config.json``, handler scripts, and ``resource.json``.

        Args:
            endpoint_path: Endpoint path, e.g. ``api/v1/health``.
            project: Project name; defaults to the active project.

        Returns:
            Endpoint configuration, handler scripts, and resource metadata.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "read_webdev_endpoint",
                hint="WebDev endpoints are read from disk; set IGNITION_INSTALL_PATH.",
            )
        data = reader.read_webdev_endpoint(proj or "", endpoint_path)
        if data is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "endpoint_path": endpoint_path}
            )
        return ok(
            state,
            "read_webdev_endpoint",
            {"project": proj, "endpoint_path": endpoint_path, **data},
        )

    @mcp.tool()
    async def create_webdev_endpoint(
        endpoint_path: str,
        methods: list[str] | None = None,
        handlers: dict[str, str] | None = None,
        require_auth: bool = False,
        require_https: bool = False,
        required_roles: str = "",
        user_source: str = "",
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or update a WebDev HTTP endpoint in a local Ignition project.

        Args:
            endpoint_path: URL path relative to WebDev root, e.g. ``api/health``.
            methods: HTTP handler names to enable (e.g. ``["doGet", "doPost"]``). Defaults to
                ``list(handlers)`` or ``["doGet"]``.
            handlers: Mapping of handler name (e.g. ``"doGet"``) to Python/Jython source code.
            require_auth: Require Ignition authentication on enabled handlers.
            require_https: Require HTTPS on enabled handlers.
            required_roles: Comma-separated role names when auth is required.
            user_source: User source name when auth is required.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Validate handler scripts and preview changes without writing.

        Returns:
            Creation acknowledgement or a dry-run/validation payload.
        """
        guard = state.write_guard("create_webdev_endpoint", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "create_webdev_endpoint",
                hint="WebDev endpoint creation needs a local install (IGNITION_INSTALL_PATH).",
            )

        all_handlers = (
            "doGet",
            "doPost",
            "doPut",
            "doDelete",
            "doHead",
            "doOptions",
            "doTrace",
            "doPatch",
        )
        enabled = set(methods or (list(handlers) if handlers else ["doGet"]))
        unknown = sorted((enabled | set(handlers or {})) - set(all_handlers))
        issues: list[str] = []
        if unknown:
            issues.append(f"Unknown WebDev handler(s): {', '.join(unknown)}.")

        scripts: dict[str, str] = {}
        for name in sorted(enabled):
            code = (handlers or {}).get(name)
            if code is None:
                code = f"def {name}(request, session):\n\treturn {{'json': {{'status': 'ok'}}}}\n"
            for err in _validate_python_syntax(code):
                issues.append(f"{name}.py: {err}")
            scripts[name] = code

        config: dict[str, Any] = {"resource-type": "python-resource"}
        for name in all_handlers:
            config[name] = {
                "enabled": name in enabled,
                "max-retry-attempts": 3,
                "require-auth": require_auth,
                "require-https": require_https,
                "required-roles": required_roles,
                "user-source": user_source,
            }

        existing = reader.read_webdev_endpoint(proj or "", endpoint_path)
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "project": proj,
                    "endpoint_path": endpoint_path,
                    "valid": not issues,
                    "validation_issues": issues,
                    "enabled_handlers": sorted(enabled),
                    "config_diff": preview_json_change(
                        existing.get("config") if existing else None,
                        config,
                        f"{endpoint_path}/config.json",
                    ),
                }
            )
        if issues:
            return state.tool_response(
                {"status": "invalid_endpoint", "issues": issues, "project": proj}
            )

        written = reader.write_webdev_endpoint(proj or "", endpoint_path, config, scripts)
        if written is None:
            return state.tool_response({"status": "write_failed", "endpoint_path": endpoint_path})
        verified = reader.read_webdev_endpoint(proj or "", endpoint_path) is not None
        state.record_action("create_webdev_endpoint", "ok", project=proj, endpoint=endpoint_path)
        return state.tool_response(
            {
                "status": "written",
                "project": proj,
                "endpoint_path": endpoint_path,
                "path": str(written),
                "enabled_handlers": sorted(enabled),
                "verified": verified,
            }
        )


async def _save_project_export(
    state: ServerState,
    project: str,
    output_path: str | None,
    default_dir: str,
    tool: str,
    status: str,
) -> dict[str, Any]:
    """Download a project export and save it to disk."""
    destination = (
        Path(output_path) if output_path else _default_archive_path(default_dir, project, ".zip")
    )
    try:
        payload = await _binary_get(state, f"{PROJECT_EXPORT_PATH}/{quote(project, safe='')}")
    except GatewayError as exc:
        return _gateway_or_unsupported(state, exc, tool)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        await anyio.to_thread.run_sync(destination.write_bytes, payload)
    except OSError as exc:
        return state.tool_response(
            {"status": "write_failed", "path": str(destination), "message": str(exc)}
        )
    state.record_action(tool, "ok", project=project, path=str(destination))
    return state.tool_response(
        {
            "status": status,
            "project": project,
            "path": str(destination),
            "bytes": len(payload),
            "valid_zip": _is_zip(payload),
        }
    )


def _default_archive_path(directory: str, project: str, suffix: str) -> Path:
    """Build a filesystem-safe default archive path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "_", project).strip("._") or "project"
    return Path(directory) / f"{safe_project}_{stamp}{suffix}"


def _is_zip(payload: bytes) -> bool:
    """Return whether ``payload`` is a readable ZIP archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


async def _binary_get(state: ServerState, path: str) -> bytes:
    """GET a binary body, retrying once with a Gateway session if required."""
    try:
        response = await state.gateway.request("GET", path)
    except GatewayError as exc:
        if exc.status not in (401, 403) or not (state.config.username and state.config.password):
            raise
        if await state.gateway.authenticate() != "session_login":
            raise
        response = await state.gateway.request("GET", path)
    return response.content


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "Project deployment endpoints are not exposed on this gateway. Direct file "
                "editing under data/projects works without the API."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
