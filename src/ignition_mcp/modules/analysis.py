"""Project-wide analysis: binding checks, dependency map and generated documentation.

These tools work entirely on local project files. They are deterministic, need no gateway and
never modify a project. Findings are reported as issues with a severity and a resource path so
an agent can act on them without guessing.
"""

from __future__ import annotations

from typing import Any

from ignition_mcp.autoconnect.local_reader import LocalInstallReader
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import ok, resolve_project, unsupported

# These view-analysis helpers are intentionally imported from perspective.py.
# They are prefixed with _ because they are not MCP tools, but they are shared
# between perspective.py and this analysis module. If perspective.py is refactored,
# move them to a shared `_view_helpers.py` or `common.py` instead.
from ignition_mcp.modules.perspective import (
    _collect_bindings,
    _duplicate_component_names,
    _embedded_view_refs,
    _style_classes,
    _validate_view_json,
    _walk_components,
)
from ignition_mcp.state import ServerState


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def analyze_project(project: str | None = None) -> dict[str, Any]:
        """Analyze a project for broken bindings, missing views, styles and dead resources.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Issues with severity and resource paths, plus a summary and validity flag.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "analyze_project",
                hint="Project analysis needs local project files (IGNITION_INSTALL_PATH).",
            )
        if not reader.project_path(proj or "").exists():
            return state.tool_response(
                {"status": "not_found", "project": proj, "available": reader.list_projects()}
            )
        return ok(state, "analyze_project", _analyze_project(reader, proj or ""))

    @mcp.tool()
    async def project_dependency_map(project: str | None = None) -> dict[str, Any]:
        """Show project inheritance as a graph.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Nodes, inheritance edges and any detected cycle.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "project_dependency_map",
                hint="Dependency analysis needs local project files (IGNITION_INSTALL_PATH).",
            )
        return ok(state, "project_dependency_map", _dependency_map(reader, proj or ""))

    @mcp.tool()
    async def generate_project_docs(project: str | None = None) -> dict[str, Any]:
        """Generate Markdown documentation for a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Markdown covering inheritance, views, scripts, named queries and style classes.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "generate_project_docs",
                hint="Documentation generation needs local project files (IGNITION_INSTALL_PATH).",
            )
        markdown = _project_docs(reader, proj or "")
        return ok(
            state,
            "generate_project_docs",
            {"project": proj, "markdown": markdown, "bytes": len(markdown.encode("utf-8"))},
        )


def _issue(severity: str, kind: str, resource: str, message: str) -> dict[str, str]:
    """Build one analysis issue."""
    return {"severity": severity, "kind": kind, "resource": resource, "message": message}


def _analyze_project(reader: LocalInstallReader, project: str) -> dict[str, Any]:
    """Analyze a project's views, styles and references.

    Args:
        reader: Local install reader.
        project: Project name.

    Returns:
        Summary and issue list. ``valid`` is false only when errors were found.
    """
    chain = reader.inheritance_chain(project)
    view_paths = reader.list_perspective_views(project)
    views = {path: reader.read_perspective_view(project, path) for path in view_paths}
    known_views = set(view_paths)
    style_classes = set(reader.list_style_classes(project))
    for parent in chain[1:]:
        known_views.update(reader.list_perspective_views(parent))
        style_classes.update(reader.list_style_classes(parent))
    issues: list[dict[str, str]] = []
    referenced: set[str] = set()

    page_config = reader.read_page_config(project) or {}
    for page in page_config.get("pages", []) if isinstance(page_config, dict) else []:
        if not isinstance(page, dict):
            continue
        view_ref = page.get("view") or page.get("path")
        if isinstance(view_ref, str):
            referenced.add(view_ref)

    for view_path, view in views.items():
        if not view:
            issues.append(
                _issue("error", "unreadable_view", view_path, "View JSON could not be decoded.")
            )
            continue
        for problem in _validate_view_json(view):
            issues.append(_issue("error", "invalid_view", view_path, problem))
        for name in _duplicate_component_names(view.get("root", {})):
            issues.append(
                _issue("warning", "duplicate_component", view_path, f"Duplicate name '{name}'.")
            )
        for reference in _embedded_view_refs(view):
            referenced.add(reference)
            if reference not in known_views:
                issues.append(
                    _issue(
                        "error",
                        "missing_embedded_view",
                        view_path,
                        f"Embedded view '{reference}' does not exist.",
                    )
                )
        for binding in _collect_bindings(view):
            if not binding.get("config"):
                issues.append(
                    _issue(
                        "warning",
                        "empty_binding",
                        view_path,
                        f"Binding on '{binding['component']}.{binding['property']}' has no config.",
                    )
                )
        for _, component in _walk_components(view.get("root", {})):
            for style_class in _style_classes(component):
                if style_class not in style_classes:
                    issues.append(
                        _issue(
                            "warning",
                            "missing_style_class",
                            view_path,
                            f"Style class '{style_class}' is not defined.",
                        )
                    )

    for view_path in sorted(set(view_paths) - referenced):
        issues.append(
            _issue(
                "info",
                "unused_view",
                view_path,
                "View is not referenced by page-config or another view.",
            )
        )

    for parent in chain[1:]:
        if not reader.project_path(parent).exists():
            issues.append(
                _issue(
                    "error",
                    "missing_parent_project",
                    parent,
                    "Parent project is not present on this machine.",
                )
            )

    summary = {
        "views": len(views),
        "scripts": len(reader.list_scripts(project)),
        "named_queries": len(reader.list_named_queries(project)),
        "style_classes": len(style_classes),
        "issues": len(issues),
        "errors": sum(1 for issue in issues if issue["severity"] == "error"),
        "warnings": sum(1 for issue in issues if issue["severity"] == "warning"),
    }
    return {
        "project": project,
        "inheritance_chain": chain,
        "summary": summary,
        "issues": issues,
        "valid": summary["errors"] == 0,
    }


def _dependency_map(reader: LocalInstallReader, project: str) -> dict[str, Any]:
    """Build an inheritance graph for a project.

    Args:
        reader: Local install reader.
        project: Project name.

    Returns:
        ``nodes``, ``edges`` and a ``cycle`` list (empty when acyclic).
    """
    chain = reader.inheritance_chain(project)
    nodes: list[dict[str, Any]] = []
    for name in chain:
        meta = reader.read_project_meta(name)
        nodes.append(
            {
                "project": name,
                "exists": reader.project_path(name).exists(),
                "parent": meta.get("parent"),
                "views": len(reader.list_perspective_views(name)),
                "scripts": len(reader.list_scripts(name)),
                "named_queries": len(reader.list_named_queries(name)),
            }
        )
    edges = [
        {"from": node["project"], "to": node["parent"], "type": "inherits"}
        for node in nodes
        if node["parent"]
    ]
    return {
        "project": project,
        "nodes": nodes,
        "edges": edges,
        "cycle": _find_cycle(reader, project),
    }


def _find_cycle(reader: LocalInstallReader, project: str) -> list[str]:
    """Return the project cycle starting at ``project``, or an empty list."""
    seen: list[str] = []
    current: str | None = project
    for _ in range(20):
        if current is None:
            return []
        if current in seen:
            return [*seen[seen.index(current) :], current]
        seen.append(current)
        parent = reader.read_project_meta(current).get("parent")
        current = parent if isinstance(parent, str) and parent else None
    return []


def _project_docs(reader: LocalInstallReader, project: str) -> str:
    """Generate Markdown documentation for a project."""
    meta = reader.read_project_meta(project)
    chain = reader.inheritance_chain(project)
    views = reader.list_perspective_views(project)
    scripts = reader.list_scripts(project)
    queries = reader.list_named_queries(project)
    styles = reader.list_style_classes(project)

    lines = [f"# Project: {project}", ""]
    if meta.get("parent"):
        lines.append(f"- **Parent project:** `{meta['parent']}`")
    lines.append(f"- **Inheritance chain:** {' → '.join(f'`{name}`' for name in chain)}")
    lines.append(f"- **Views:** {len(views)}")
    lines.append(f"- **Scripts:** {len(scripts)}")
    lines.append(f"- **Named queries:** {len(queries)}")
    lines.append(f"- **Style classes:** {len(styles)}")
    lines.append("")

    lines.extend(["## Views", "", "| View | Components | Embedded views |", "| --- | --- | --- |"])
    for view_path in views:
        view = reader.read_perspective_view(project, view_path)
        component_count = sum(1 for _ in _walk_components(view.get("root", {}))) if view else 0
        embedded = ", ".join(_embedded_view_refs(view)) if view else ""
        lines.append(f"| `{view_path}` | {component_count} | {embedded or '—'} |")
    if not views:
        lines.append("| _None_ | 0 | — |")
    lines.append("")

    lines.extend(["## Scripts", ""])
    if scripts:
        lines.extend(f"- `{script}`" for script in scripts)
    else:
        lines.append("_None_")
    lines.append("")

    lines.extend(
        [
            "## Named queries",
            "",
            "| Query | Type | Parameters |",
            "| --- | --- | --- |",
        ]
    )
    for query_path in queries:
        query = reader.read_named_query(project, query_path) or {}
        parameters = ", ".join(
            str(parameter.get("name"))
            for parameter in query.get("parameters", [])
            if isinstance(parameter, dict)
        )
        lines.append(
            f"| `{query_path}` | {query.get('queryType', 'query')} | {parameters or '—'} |"
        )
    if not queries:
        lines.append("| _None_ | — | — |")
    lines.append("")

    lines.extend(["## Style classes", ""])
    if styles:
        lines.extend(f"- `{style}`" for style in styles)
    else:
        lines.append("_None_")
    lines.append("")
    return "\n".join(lines)


__all__ = ["register"]
