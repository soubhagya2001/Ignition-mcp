"""Perspective development.

Perspective views are JSON documents stored under
``data/projects/<project>/com.inductiveautomation.perspective/views/<path>/view.json``. When a
local install is available, reads and writes go straight to disk (no credentials, no HTTP);
otherwise the gateway API is used.

View document shape (abridged)::

    {
      "custom": {}, "params": {}, "props": {},
      "root": {"type": "ia.container.flex", "meta": {"name": "root"}, "children": []}
    }
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterable
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, resolve_project, unsupported
from ignition_mcp.modules.scripting import _validate_script_indentation
from ignition_mcp.safety import preview_json_change
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

EMBEDDED_VIEW_TYPES = ("ia.display.view", "embeddedview", "ia.perspective.client.embeddedview")


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_perspective_views(project: str | None = None) -> dict[str, Any]:
        """List Perspective views in a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Sorted view paths such as ``Dashboard/Overview``.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error

        reader = state.local_reader
        if reader is not None:
            views = reader.list_perspective_views(proj or "")
            return ok(
                state,
                "list_perspective_views",
                {
                    "project": proj,
                    "views": views,
                    "count": len(views),
                    "source": "local_filesystem",
                },
            )

        if not state.has_gateway:
            return unsupported(
                state,
                "list_perspective_views",
                hint="Set IGNITION_INSTALL_PATH for local reads or connect a gateway.",
            )
        try:
            _, payload = await state.gateway.first_available(_view_list_candidates(proj or ""))
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "list_perspective_views")
        views = _normalise_view_list(payload)
        return ok(
            state,
            "list_perspective_views",
            {"project": proj, "views": views, "count": len(views), "source": "gateway_api"},
        )

    @mcp.tool()
    async def read_perspective_view(view_path: str, project: str | None = None) -> dict[str, Any]:
        """Read a view's full JSON definition.

        Args:
            view_path: View path such as ``Dashboard/Overview``.
            project: Project name; defaults to the active project.

        Returns:
            The decoded view JSON.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        try:
            view, source = await _read_view(state, proj or "", view_path)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "read_perspective_view")
        if view is None:
            return state.tool_response(
                {
                    "status": "not_found",
                    "project": proj,
                    "view_path": view_path,
                    "hint": "Check the path with list_perspective_views.",
                }
            )
        return ok(
            state,
            "read_perspective_view",
            {"project": proj, "view_path": view_path, "view": view, "source": source},
        )

    @mcp.tool()
    async def create_perspective_view(
        view_path: str,
        view_json: dict[str, Any],
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a new Perspective view.

        Args:
            view_path: Destination path, e.g. ``Dashboard/NewView``.
            view_json: Full view document.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Validate and preview without writing.

        Returns:
            Write acknowledgement, or validation issues in dry-run mode.
        """
        guard = state.write_guard("create_perspective_view", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error

        issues = _validate_view_json(view_json)
        existing, _ = await _read_view(state, proj or "", view_path)
        if existing is not None:
            issues.append(f"View '{view_path}' already exists; use update_perspective_view.")
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "project": proj,
                    "view_path": view_path,
                    "valid": not issues,
                    "validation_issues": issues,
                }
            )
        if issues:
            return state.tool_response(
                {"status": "invalid_view", "issues": issues, "project": proj}
            )

        try:
            result = await _write_view(state, proj or "", view_path, view_json)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "create_perspective_view")
        state.record_action("create_perspective_view", "ok", project=proj, view=view_path)
        return state.tool_response(
            {"status": "created", "project": proj, "view_path": view_path, **result}
        )

    @mcp.tool()
    async def update_perspective_view(
        view_path: str,
        view_json: dict[str, Any],
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Replace an existing Perspective view.

        Args:
            view_path: View to update.
            view_json: New full view document.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Validate and summarise the change without writing.

        Returns:
            Write acknowledgement, or a change summary in dry-run mode.
        """
        guard = state.write_guard("update_perspective_view", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error

        current, _ = await _read_view(state, proj or "", view_path)
        issues = _validate_view_json(view_json)
        if current is None:
            issues.append(f"View '{view_path}' does not exist; use create_perspective_view.")
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "project": proj,
                    "view_path": view_path,
                    "valid": not issues,
                    "validation_issues": issues,
                    "changed": current != view_json,
                    "diff": preview_json_change(current, view_json, view_path),
                }
            )
        if issues:
            return state.tool_response(
                {"status": "invalid_view", "issues": issues, "project": proj}
            )

        try:
            result = await _write_view(state, proj or "", view_path, view_json)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "update_perspective_view")
        state.record_action("update_perspective_view", "ok", project=proj, view=view_path)
        return state.tool_response(
            {"status": "updated", "project": proj, "view_path": view_path, **result}
        )

    @mcp.tool()
    async def validate_perspective_view(
        view_path: str, project: str | None = None
    ) -> dict[str, Any]:
        """Validate structure, embedded view references and bindings of a view.

        Args:
            view_path: View to validate.
            project: Project name; defaults to the active project.

        Returns:
            Issues grouped by kind plus a validity flag.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, source = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )

        issues = _validate_view_json(view)
        duplicates = _duplicate_component_names(view.get("root", {}))
        issues.extend(f"Duplicate component name '{name}'" for name in duplicates)

        embedded = _embedded_view_refs(view)
        if embedded:
            known = await _known_views(state, proj or "")
            missing = sorted({ref for ref in embedded if ref not in known})
            issues.extend(f"Embedded view '{ref}' not found" for ref in missing)

        empty_bindings = [
            binding for binding in _collect_bindings(view) if not binding.get("config")
        ]
        issues.extend(
            f"Binding on '{binding['component']}.{binding['property']}' has no config"
            for binding in empty_bindings
        )

        return ok(
            state,
            "validate_perspective_view",
            {
                "project": proj,
                "view_path": view_path,
                "valid": not issues,
                "issues": issues,
                "source": source,
            },
        )

    @mcp.tool()
    async def inspect_component_tree(
        view_path: str, project: str | None = None, depth: int = 999
    ) -> dict[str, Any]:
        """Return the component hierarchy of a view.

        Args:
            view_path: View to inspect.
            project: Project name; defaults to the active project.
            depth: Maximum nesting depth to return.

        Returns:
            A nested tree of component type, name and child count.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )
        tree = _extract_component_tree(view.get("root", {}), max_depth=depth)
        return ok(state, "inspect_component_tree", {"view_path": view_path, "tree": tree})

    @mcp.tool()
    async def find_view_bindings(
        view_path: str,
        project: str | None = None,
        binding_type: str | None = None,
    ) -> dict[str, Any]:
        """Find all property bindings in a view.

        Args:
            view_path: View to scan.
            project: Project name; defaults to the active project.
            binding_type: Optional filter such as ``tag`` or ``expr``.

        Returns:
            Bindings with component, property, type and config.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )

        bindings = _collect_bindings(view)
        if binding_type:
            wanted = binding_type.lower()
            bindings = [
                binding for binding in bindings if str(binding.get("type", "")).lower() == wanted
            ]
        return ok(
            state,
            "find_view_bindings",
            {
                "view_path": view_path,
                "bindings": bindings,
                "count": len(bindings),
                "by_type": _count_by(bindings, "type"),
            },
        )

    @mcp.tool()
    async def find_event_scripts(view_path: str, project: str | None = None) -> dict[str, Any]:
        """Find component event scripts in a view.

        Args:
            view_path: View to scan.
            project: Project name; defaults to the active project.

        Returns:
            Event handlers with their component and source code.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )
        scripts = _collect_event_scripts(view)
        return ok(
            state,
            "find_event_scripts",
            {"view_path": view_path, "scripts": scripts, "count": len(scripts)},
        )

    @mcp.tool()
    async def list_bindings(
        view_path: str,
        project: str | None = None,
        include_transforms: bool = True,
    ) -> dict[str, Any]:
        """List bindings, including their ordered transforms.

        Args:
            view_path: View to scan.
            project: Project name; defaults to the active project.
            include_transforms: Include each binding's transform list.

        Returns:
            Bindings with component, property, type, config and transforms.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )
        bindings = _collect_bindings(view)
        if not include_transforms:
            bindings = [
                {key: value for key, value in item.items() if key != "transforms"}
                for item in bindings
            ]
        return ok(
            state,
            "list_bindings",
            {"view_path": view_path, "bindings": bindings, "count": len(bindings)},
        )

    @mcp.tool()
    async def get_binding(
        view_path: str, component: str, property: str, project: str | None = None
    ) -> dict[str, Any]:
        """Read one component property binding.

        Args:
            view_path: View containing the component.
            component: Component ``meta.name``.
            property: Property path, with or without the ``props.`` prefix.
            project: Project name; defaults to the active project.

        Returns:
            The binding object, including transforms.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        node = _binding_node(found, property)
        binding = node.get("binding") if node is not None else None
        if not isinstance(binding, dict):
            return state.tool_response(
                {"status": "binding_not_found", "component": component, "property": property}
            )
        return ok(
            state,
            "get_binding",
            {
                "view_path": view_path,
                "component": component,
                "property": property,
                "binding": binding,
            },
        )

    @mcp.tool()
    async def list_transforms(
        view_path: str, component: str, property: str, project: str | None = None
    ) -> dict[str, Any]:
        """List ordered transforms for one binding.

        Args:
            view_path: View containing the binding.
            component: Component ``meta.name``.
            property: Property path.
            project: Project name; defaults to the active project.

        Returns:
            Ordered transform entries.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        node = _binding_node(found, property)
        binding = node.get("binding") if node is not None else None
        transforms = binding.get("transforms", []) if isinstance(binding, dict) else []
        return ok(
            state,
            "list_transforms",
            {
                "view_path": view_path,
                "component": component,
                "property": property,
                "transforms": transforms if isinstance(transforms, list) else [],
            },
        )

    @mcp.tool()
    async def list_property_change_scripts(
        view_path: str, project: str | None = None
    ) -> dict[str, Any]:
        """List property-change scripts in a view.

        Args:
            view_path: View to scan.
            project: Project name; defaults to the active project.

        Returns:
            Property, component, enabled state and script.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        scripts = _collect_property_change_scripts(view)
        return ok(
            state,
            "list_property_change_scripts",
            {"view_path": view_path, "scripts": scripts, "count": len(scripts)},
        )

    @mcp.tool()
    async def list_message_handlers(view_path: str, project: str | None = None) -> dict[str, Any]:
        """List component message handlers in a view.

        Args:
            view_path: View to scan.
            project: Project name; defaults to the active project.

        Returns:
            Message type, scope flags, component and script.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        handlers = _collect_message_handlers(view)
        return ok(
            state,
            "list_message_handlers",
            {"view_path": view_path, "handlers": handlers, "count": len(handlers)},
        )

    @mcp.tool()
    async def get_event_script(
        view_path: str, component: str, event: str, project: str | None = None
    ) -> dict[str, Any]:
        """Read one component event handler.

        Args:
            view_path: View containing the component.
            component: Component ``meta.name``.
            event: Event key, such as ``onActionPerformed``.
            project: Project name; defaults to the active project.

        Returns:
            The event handler object.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        handler = (found.get("events") or {}).get(event)
        if not isinstance(handler, dict):
            return state.tool_response(
                {"status": "event_not_found", "component": component, "event": event}
            )
        return ok(
            state,
            "get_event_script",
            {"view_path": view_path, "component": component, "event": event, "handler": handler},
        )

    @mcp.tool()
    async def list_view_scripts(view_path: str, project: str | None = None) -> dict[str, Any]:
        """List every embedded script in a view with its Ignition context.

        Args:
            view_path: View to scan.
            project: Project name; defaults to the active project.

        Returns:
            Events, transforms, property-change scripts and message handlers.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        scripts = _collect_all_scripts(view)
        return ok(
            state,
            "list_view_scripts",
            {"view_path": view_path, "scripts": scripts, "count": len(scripts)},
        )

    @mcp.tool()
    async def validate_view_scripts(view_path: str, project: str | None = None) -> dict[str, Any]:
        """Validate every embedded script using its real Ignition context.

        Args:
            view_path: View to validate.
            project: Project name; defaults to the active project.

        Returns:
            Per-script diagnostics and a validity flag.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        results = _validate_view_scripts(view)
        invalid = [result for result in results if not result["valid"]]
        return ok(
            state,
            "validate_view_scripts",
            {
                "view_path": view_path,
                "valid": not invalid,
                "scripts": results,
                "issue_count": sum(len(result["issues"]) for result in results),
            },
        )

    @mcp.tool()
    async def set_binding(
        view_path: str,
        component: str,
        property: str,
        binding: dict[str, Any],
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or replace one component property binding.

        Args:
            view_path: View containing the component.
            component: Component ``meta.name``.
            property: Property path, with or without the ``props.`` prefix.
            binding: Binding object, e.g. ``{"type": "tag", "config": {...}}``.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Preview the change without writing.

        Returns:
            Write acknowledgement or a dry-run diff.
        """
        guard = state.write_guard("set_binding", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        issues = _validate_binding_shape(binding)
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        node = _binding_node(found, property, create=True)
        if node is None:
            issues.append(f"Invalid property path '{property}'.")
        else:
            node["binding"] = binding
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "set_binding", dry_run, issues
        )

    @mcp.tool()
    async def delete_binding(
        view_path: str,
        component: str,
        property: str,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one component property binding."""
        guard = state.write_guard("delete_binding", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        node = _binding_node(found, property)
        if node is None or "binding" not in node:
            return state.tool_response(
                {"status": "binding_not_found", "component": component, "property": property}
            )
        del node["binding"]
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "delete_binding", dry_run, []
        )

    @mcp.tool()
    async def set_transform(
        view_path: str,
        component: str,
        property: str,
        transform: dict[str, Any],
        index: int | None = None,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Append or replace one binding transform."""
        guard = state.write_guard("set_transform", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        issues = _validate_transform_shape(transform)
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        node = _binding_node(found, property)
        binding = node.get("binding") if node is not None else None
        if not isinstance(binding, dict):
            return state.tool_response(
                {"status": "binding_not_found", "component": component, "property": property}
            )
        transforms = binding.setdefault("transforms", [])
        if not isinstance(transforms, list):
            issues.append("Binding transforms must be a list.")
        elif index is None:
            transforms.append(transform)
        elif 0 <= index < len(transforms):
            transforms[index] = transform
        else:
            issues.append(f"Transform index {index} is out of range.")
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "set_transform", dry_run, issues
        )

    @mcp.tool()
    async def delete_transform(
        view_path: str,
        component: str,
        property: str,
        index: int,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete one binding transform by index."""
        guard = state.write_guard("delete_transform", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        node = _binding_node(found, property)
        binding = node.get("binding") if node is not None else None
        transforms = binding.get("transforms") if isinstance(binding, dict) else None
        if not isinstance(transforms, list) or not (0 <= index < len(transforms)):
            return state.tool_response(
                {"status": "transform_not_found", "component": component, "index": index}
            )
        transforms.pop(index)
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "delete_transform", dry_run, []
        )

    @mcp.tool()
    async def set_property_change_script(
        view_path: str,
        component: str,
        property: str,
        script: str,
        enabled: bool = True,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or replace a property-change script."""
        guard = state.write_guard("set_property_change_script", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        issues = _script_issues(script, "property_change")
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        prop_config = found.setdefault("propConfig", {})
        if not isinstance(prop_config, dict):
            issues.append("propConfig must be an object.")
        else:
            entry = prop_config.setdefault(property, {})
            if not isinstance(entry, dict):
                issues.append(f"propConfig entry for '{property}' is not an object.")
            else:
                entry["onChange"] = {"enabled": enabled, "script": script}
        return await _finish_view_change(
            state,
            proj or "",
            view_path,
            before,
            view,
            "set_property_change_script",
            dry_run,
            issues,
        )

    @mcp.tool()
    async def delete_property_change_script(
        view_path: str,
        component: str,
        property: str,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete a property-change script."""
        guard = state.write_guard("delete_property_change_script", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        entry = (found.get("propConfig") or {}).get(property)
        if not isinstance(entry, dict) or not isinstance(entry.get("onChange"), dict):
            return state.tool_response(
                {"status": "script_not_found", "component": component, "property": property}
            )
        entry["onChange"].pop("script", None)
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "delete_property_change_script", dry_run, []
        )

    @mcp.tool()
    async def set_message_handler(
        view_path: str,
        component: str,
        message_type: str,
        script: str,
        page_scope: bool = False,
        session_scope: bool = False,
        view_scope: bool = False,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or replace a component message handler."""
        guard = state.write_guard("set_message_handler", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        issues = _script_issues(script, "message_handler")
        if not message_type.strip():
            issues.append("message_type must not be empty.")
        if not any((page_scope, session_scope, view_scope)):
            issues.append("At least one message-handler scope must be enabled.")
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        scripts = found.setdefault("scripts", {})
        if not isinstance(scripts, dict):
            issues.append("scripts must be an object.")
            handlers = None
        else:
            handlers = scripts.setdefault("messageHandlers", [])
        if not isinstance(handlers, list):
            issues.append("messageHandlers must be a list.")
        else:
            handler = {
                "messageType": message_type,
                "pageScope": page_scope,
                "sessionScope": session_scope,
                "viewScope": view_scope,
                "script": script,
            }
            existing = next(
                (item for item in handlers if item.get("messageType") == message_type), None
            )
            if existing is None:
                handlers.append(handler)
            else:
                existing.update(handler)
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "set_message_handler", dry_run, issues
        )

    @mcp.tool()
    async def delete_message_handler(
        view_path: str,
        component: str,
        message_type: str,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete a component message handler by message type."""
        guard = state.write_guard("delete_message_handler", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        scripts = found.get("scripts")
        if not isinstance(scripts, dict):
            return state.tool_response(
                {"status": "handler_not_found", "message_type": message_type}
            )
        handlers = scripts.get("messageHandlers")
        if not isinstance(handlers, list):
            return state.tool_response(
                {"status": "handler_not_found", "message_type": message_type}
            )
        remaining = [item for item in handlers if item.get("messageType") != message_type]
        if len(remaining) == len(handlers):
            return state.tool_response(
                {"status": "handler_not_found", "message_type": message_type}
            )
        scripts["messageHandlers"] = remaining
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "delete_message_handler", dry_run, []
        )

    @mcp.tool()
    async def set_event_script(
        view_path: str,
        component: str,
        event: str,
        script: str,
        scope: str = "G",
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or replace a component event script."""
        guard = state.write_guard("set_event_script", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        issues = _script_issues(script, "perspective_event")
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        events = found.setdefault("events", {})
        if not isinstance(events, dict):
            issues.append("events must be an object.")
        else:
            existing = events.get(event)
            handler = existing if isinstance(existing, dict) else {}
            handler["type"] = "script"
            handler["scope"] = scope
            config = handler.get("config")
            if not isinstance(config, dict):
                config = {}
            config["script"] = script
            handler["config"] = config
            events[event] = handler
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "set_event_script", dry_run, issues
        )

    @mcp.tool()
    async def delete_event_script(
        view_path: str,
        component: str,
        event: str,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete a component event script."""
        guard = state.write_guard("delete_event_script", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response({"status": "not_found", "view_path": view_path})
        before = copy.deepcopy(view)
        found = _find_component(view.get("root", {}), component)
        if found is None:
            return _component_not_found(state, view, component)
        events = found.get("events")
        if not isinstance(events, dict) or event not in events:
            return state.tool_response(
                {"status": "event_not_found", "component": component, "event": event}
            )
        del events[event]
        return await _finish_view_change(
            state, proj or "", view_path, before, view, "delete_event_script", dry_run, []
        )

    @mcp.tool()
    async def validate_project_scripts(project: str | None = None) -> dict[str, Any]:
        """Validate embedded scripts across every view in a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Per-view script diagnostics and a project-level validity flag.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "validate_project_scripts",
                hint="Project script validation needs local project files (IGNITION_INSTALL_PATH).",
            )
        views: list[dict[str, Any]] = []
        for view_path in reader.list_perspective_views(proj or ""):
            view = reader.read_perspective_view(proj or "", view_path)
            if view is None:
                continue
            results = _validate_view_scripts(view)
            invalid = [result for result in results if not result["valid"]]
            if results:
                views.append(
                    {
                        "view_path": view_path,
                        "valid": not invalid,
                        "scripts": results,
                        "issue_count": sum(len(result["issues"]) for result in results),
                    }
                )
        return ok(
            state,
            "validate_project_scripts",
            {
                "project": proj,
                "valid": all(view["valid"] for view in views),
                "views": views,
                "script_count": sum(len(view["scripts"]) for view in views),
            },
        )

    @mcp.tool()
    async def list_styles(project: str | None = None) -> dict[str, Any]:
        """List style classes and theme data for a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Style class names and their definitions.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is not None:
            styles = reader.read_style_classes(proj or "")
            return ok(
                state,
                "list_styles",
                {
                    "project": proj,
                    "style_classes": sorted(reader.list_style_classes(proj or "")),
                    "definitions": styles,
                    "source": "local_filesystem",
                },
            )
        return unsupported(
            state,
            "list_styles",
            hint="Style classes are read from project files; set IGNITION_INSTALL_PATH.",
        )

    @mcp.tool()
    async def list_page_config(project: str | None = None) -> dict[str, Any]:
        """Read Perspective page configuration for a project.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Page configuration JSON.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is not None:
            page_config = reader.read_page_config(proj or "")
            if page_config is not None:
                return ok(
                    state,
                    "list_page_config",
                    {
                        "project": proj,
                        "page_config": page_config,
                        "source": "local_filesystem",
                    },
                )
        return unsupported(
            state,
            "list_page_config",
            hint="Page configuration is read from page-config/config.json on disk.",
        )

    @mcp.tool()
    async def find_views_by_css(css_class: str, project: str | None = None) -> dict[str, Any]:
        """Find views and components that use a style class.

        Args:
            css_class: Style class name to search for.
            project: Project name; defaults to the active project.

        Returns:
            Matching views with the component names that use the class.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "find_views_by_css",
                hint="CSS usage search needs local project files (IGNITION_INSTALL_PATH).",
            )

        matches: list[dict[str, Any]] = []
        for view_path in reader.list_perspective_views(proj or ""):
            view = reader.read_perspective_view(proj or "", view_path)
            if not view:
                continue
            components = [
                name
                for name, component in _walk_components(view.get("root", {}))
                if css_class in _style_classes(component)
            ]
            if components:
                matches.append({"view_path": view_path, "components": components})
        return ok(
            state,
            "find_views_by_css",
            {"css_class": css_class, "matches": matches, "count": len(matches)},
        )

    @mcp.tool()
    async def check_embedded_views(project: str | None = None) -> dict[str, Any]:
        """Verify every embedded view reference in a project resolves.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Valid and broken embedded view references.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "check_embedded_views",
                hint="Embedded-view checks need local project files (IGNITION_INSTALL_PATH).",
            )

        known = set(reader.list_perspective_views(proj or ""))
        broken: list[dict[str, str]] = []
        checked = 0
        for view_path in known:
            view = reader.read_perspective_view(proj or "", view_path)
            if not view:
                continue
            for reference in _embedded_view_refs(view):
                checked += 1
                if reference not in known:
                    broken.append({"view_path": view_path, "missing_view": reference})
        return ok(
            state,
            "check_embedded_views",
            {
                "project": proj,
                "references_checked": checked,
                "broken": broken,
                "valid": not broken,
            },
        )

    @mcp.tool()
    async def add_style_class_to_component(
        view_path: str,
        component_name: str,
        style_class: str,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add a style class to a component.

        Args:
            view_path: View containing the component.
            component_name: Component ``meta.name``.
            style_class: Style class to add.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Preview the change without writing.

        Returns:
            The updated component or a preview payload.
        """
        guard = state.write_guard("add_style_class_to_component", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )

        component = _find_component(view.get("root", {}), component_name)
        if component is None:
            return state.tool_response(
                {
                    "status": "component_not_found",
                    "component": component_name,
                    "available": [name for name, _ in _walk_components(view.get("root", {}))],
                }
            )

        classes = _style_classes(component)
        if style_class in classes:
            return state.tool_response(
                {"status": "no_change", "message": f"'{style_class}' is already applied."}
            )
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "component": component_name,
                    "would_add": style_class,
                    "current_classes": classes,
                }
            )

        classes.append(style_class)
        component.setdefault("props", {}).setdefault("style", {})["classes"] = classes
        try:
            result = await _write_view(state, proj or "", view_path, view)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "add_style_class_to_component")
        state.record_action(
            "add_style_class_to_component", "ok", view=view_path, component=component_name
        )
        return state.tool_response(
            {
                "status": "updated",
                "component": component_name,
                "style_classes": classes,
                **result,
            }
        )

    @mcp.tool()
    async def add_custom_property(
        view_path: str,
        component_name: str,
        property_name: str,
        property_type: str,
        default_value: Any = None,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add a custom property to a component.

        Args:
            view_path: View containing the component.
            component_name: Component ``meta.name``.
            property_name: New property name.
            property_type: Perspective type, e.g. ``String``, ``Int4`` or ``Boolean``.
            default_value: Initial value.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Preview the change without writing.

        Returns:
            The updated custom-property map or a preview payload.
        """
        guard = state.write_guard("add_custom_property", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )
        component = _find_component(view.get("root", {}), component_name)
        if component is None:
            return state.tool_response(
                {"status": "component_not_found", "component": component_name}
            )
        custom = component.setdefault("custom", {})
        if property_name in custom:
            return state.tool_response({"status": "already_exists", "property": property_name})
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "component": component_name,
                    "would_add": {
                        "name": property_name,
                        "type": property_type,
                        "value": default_value,
                    },
                }
            )

        custom[property_name] = {"type": property_type, "value": default_value}
        try:
            result = await _write_view(state, proj or "", view_path, view)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "add_custom_property")
        state.record_action("add_custom_property", "ok", view=view_path, component=component_name)
        return state.tool_response(
            {
                "status": "updated",
                "component": component_name,
                "custom_properties": sorted(custom),
                **result,
            }
        )

    @mcp.tool()
    async def check_view_bindings(view_path: str, project: str | None = None) -> dict[str, Any]:
        """Validate tag bindings in a view against live tag quality.

        Args:
            view_path: View to check.
            project: Project name; defaults to the active project.

        Returns:
            Valid and broken bindings; ``verified`` is false without a gateway.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        view, _ = await _read_view(state, proj or "", view_path)
        if view is None:
            return state.tool_response(
                {"status": "not_found", "project": proj, "view_path": view_path}
            )

        tag_bindings = [
            binding
            for binding in _collect_bindings(view)
            if str(binding.get("type", "")).lower() in ("tag", "tagprop")
        ]
        paths = sorted(
            {
                str(binding["config"].get("path"))
                for binding in tag_bindings
                if isinstance(binding.get("config"), dict) and binding["config"].get("path")
            }
        )
        if not state.has_gateway or not paths:
            return ok(
                state,
                "check_view_bindings",
                {
                    "view_path": view_path,
                    "bindings": tag_bindings,
                    "tag_paths": paths,
                    "verified": False,
                    "reason": "No gateway connection or no tag bindings to verify.",
                },
            )

        try:
            _, payload = await state.gateway.first_available(
                ("/tags/read", "/data/tag/read"),
                method="POST",
                json_body={"paths": paths},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "check_view_bindings")

        from ignition_mcp.modules.tags import _normalise_reads

        reads = _normalise_reads(payload, paths)
        broken = [
            read
            for read in reads
            if not str(read.get("quality", "")).lower().startswith("good")
            and read.get("quality") not in (192, 200, "192")
        ]
        return ok(
            state,
            "check_view_bindings",
            {
                "view_path": view_path,
                "bindings": tag_bindings,
                "tag_paths": paths,
                "broken": broken,
                "verified": True,
                "valid": not broken,
            },
        )


# ----------------------------------------------------------------------
# Read / write plumbing
# ----------------------------------------------------------------------


async def _read_view(
    state: ServerState, project: str, view_path: str
) -> tuple[dict[str, Any] | None, str]:
    """Read a view from disk when possible, otherwise from the gateway.

    Returns:
        ``(view_json, source)``; ``view_json`` is ``None`` when unavailable.
    """
    reader = state.local_reader
    if reader is not None:
        view = reader.read_perspective_view(project, view_path)
        if view is not None:
            return view, "local_filesystem"

    if not state.has_gateway:
        return None, "unavailable"
    _, payload = await state.gateway.first_available(_view_read_candidates(project, view_path))
    if isinstance(payload, dict) and isinstance(payload.get("view"), dict):
        payload = payload["view"]
    return payload if isinstance(payload, dict) else None, "gateway_api"


async def _write_view(
    state: ServerState, project: str, view_path: str, view: dict[str, Any]
) -> dict[str, Any]:
    """Persist a view to disk when possible, otherwise through the gateway.

    Returns:
        A dict describing where the view was written.

    Raises:
        GatewayError: If no write path is available or the gateway rejects the write.
    """
    reader = state.local_reader
    if reader is not None:
        path = reader.write_perspective_view(project, view_path, view)
        if path is not None:
            verified = reader.read_perspective_view(project, view_path) == view
            return {"source": "local_filesystem", "path": str(path), "verified": verified}

    if not state.has_gateway:
        raise GatewayError("No write path available: no local install and no gateway connection.")
    _, payload = await state.gateway.first_available(
        _view_write_candidates(project, view_path), method="PUT", json_body=view
    )
    return {"source": "gateway_api", "response": payload}


def _view_read_candidates(project: str, view_path: str) -> tuple[str, ...]:
    """Return candidate GET paths for a view."""
    return (
        f"/perspective/projects/{project}/views/{view_path}",
        f"/perspective/project/{project}/view/{view_path}",
        f"/data/perspective/project/{project}/view/{view_path}",
    )


def _view_write_candidates(project: str, view_path: str) -> tuple[str, ...]:
    """Return candidate PUT paths for a view."""
    return (
        f"/perspective/projects/{project}/views/{view_path}",
        f"/perspective/project/{project}/view/{view_path}",
        f"/data/perspective/project/{project}/view/{view_path}",
    )


def _view_list_candidates(project: str) -> tuple[str, ...]:
    """Return candidate GET paths for the view list."""
    return (
        f"/perspective/projects/{project}/views",
        f"/perspective/project/{project}/views",
        f"/data/perspective/projects/{project}",
    )


def _normalise_view_list(payload: Any) -> list[str]:
    """Normalise a view-list payload into a list of view paths."""
    items: Iterable[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("views")
            or payload.get("results")
            or payload.get("paths")
            or payload.get("items")
            or []
        )
    else:
        items = []
    paths: list[str] = []
    for item in items:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            value = item.get("path") or item.get("viewPath") or item.get("name")
            if value:
                paths.append(str(value))
    return sorted(set(paths))


async def _known_views(state: ServerState, project: str) -> set[str]:
    """Return the set of known view paths for a project."""
    reader = state.local_reader
    if reader is not None:
        return set(reader.list_perspective_views(project))
    if not state.has_gateway:
        return set()
    try:
        _, payload = await state.gateway.first_available(_view_list_candidates(project))
    except GatewayError:
        return set()
    return set(_normalise_view_list(payload))


# ----------------------------------------------------------------------
# Pure helpers (unit tested)
# ----------------------------------------------------------------------


def _validate_view_json(view_json: dict[str, Any]) -> list[str]:
    """Validate the basic structure of a Perspective view document.

    Args:
        view_json: View document to validate.

    Returns:
        A list of human-readable issues; empty when valid.
    """
    issues: list[str] = []
    if not isinstance(view_json, dict):
        return ["View JSON must be an object"]
    if "root" not in view_json:
        issues.append("Missing 'root' component")
    elif not isinstance(view_json["root"], dict):
        issues.append("'root' must be an object")
    elif "type" not in view_json["root"]:
        issues.append("Root component missing 'type'")
    return issues


def _extract_component_tree(
    component: dict[str, Any], max_depth: int, current_depth: int = 0
) -> dict[str, Any]:
    """Recursively describe a component tree.

    Args:
        component: Component dict.
        max_depth: Maximum depth to descend.
        current_depth: Current recursion depth.

    Returns:
        A tree of ``type``, ``name``, ``children_count`` and nested ``children``.
    """
    if current_depth > max_depth:
        return {"truncated": True}
    children = component.get("children") or []
    result: dict[str, Any] = {
        "type": component.get("type", "unknown"),
        "name": component.get("meta", {}).get("name", ""),
        "children_count": len(children),
    }
    if children and current_depth < max_depth:
        result["children"] = [
            _extract_component_tree(child, max_depth, current_depth + 1)
            for child in children
            if isinstance(child, dict)
        ]
    return result


def _walk_components(
    component: dict[str, Any], path: str = ""
) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(name, component)`` for every component in a tree."""
    if not isinstance(component, dict):
        return
    name = str(component.get("meta", {}).get("name") or path or "root")
    yield name, component
    for child in component.get("children") or []:
        if isinstance(child, dict):
            yield from _walk_components(child, name)


def _find_component(root: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Find a component by ``meta.name``."""
    for component_name, component in _walk_components(root):
        if component_name == name:
            return component
    return None


def _collect_prop_config_bindings(
    prop_config: Any, component: str, out: list[dict[str, Any]]
) -> None:
    """Collect bindings from a Perspective ``propConfig`` mapping."""
    if not isinstance(prop_config, dict):
        return
    for property_name, entry in prop_config.items():
        if not isinstance(entry, dict):
            continue
        binding = entry.get("binding")
        if not isinstance(binding, dict):
            continue
        out.append(
            {
                "component": component,
                "property": str(property_name),
                "type": binding.get("type"),
                "config": binding.get("config"),
                "transforms": binding.get("transforms", []),
            }
        )


def _collect_bindings(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every property binding in a view.

    Reads both modern Perspective ``propConfig`` bindings (on the view and on each
    component) and inline ``props.*.binding`` trees.

    Returns:
        Bindings as ``{component, property, type, config}``.
    """
    bindings: list[dict[str, Any]] = []
    _collect_prop_config_bindings(view.get("propConfig"), "view", bindings)
    for component_name, component in _walk_components(view.get("root", {})):
        _collect_prop_config_bindings(component.get("propConfig"), component_name, bindings)
        _walk_bindings(component.get("props", {}), component_name, "", bindings)
    return bindings


def _walk_bindings(node: Any, component: str, path: str, out: list[dict[str, Any]]) -> None:
    """Recursively collect ``binding`` objects from a props tree."""
    if isinstance(node, dict):
        binding = node.get("binding")
        if isinstance(binding, dict):
            out.append(
                {
                    "component": component,
                    "property": path or "(root)",
                    "type": binding.get("type"),
                    "config": binding.get("config"),
                    "transforms": binding.get("transforms", []),
                }
            )
        for key, value in node.items():
            if key == "binding":
                continue
            _walk_bindings(value, component, f"{path}.{key}" if path else key, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_bindings(value, component, f"{path}[{index}]", out)


def _collect_event_scripts(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect event handlers that contain script code."""
    scripts: list[dict[str, Any]] = []
    for component_name, component in _walk_components(view.get("root", {})):
        events = component.get("events")
        if not isinstance(events, dict):
            continue
        for event_name, handler in events.items():
            if not isinstance(handler, dict):
                continue
            script = handler.get("script") or handler.get("code")
            if not script and isinstance(handler.get("config"), dict):
                script = handler["config"].get("script")
            if script:
                scripts.append(
                    {
                        "component": component_name,
                        "event": event_name,
                        "script": script,
                    }
                )
    return scripts


def _embedded_view_refs(view: dict[str, Any]) -> list[str]:
    """Return embedded view paths referenced by a view."""
    refs: list[str] = []
    for _, component in _walk_components(view.get("root", {})):
        component_type = str(component.get("type", "")).lower()
        if not any(marker in component_type for marker in EMBEDDED_VIEW_TYPES):
            continue
        props = component.get("props") or {}
        for key in ("path", "viewPath", "view"):
            value = props.get(key)
            if isinstance(value, str) and value:
                refs.append(value)
                break
    return refs


def _style_classes(component: dict[str, Any]) -> list[str]:
    """Return the mutable style-class list for a component."""
    style = (component.get("props") or {}).get("style")
    if not isinstance(style, dict):
        return []
    classes = style.get("classes")
    if isinstance(classes, list):
        return classes
    return []


def _duplicate_component_names(root: dict[str, Any]) -> list[str]:
    """Return component names that appear more than once."""
    seen: dict[str, int] = {}
    for name, _ in _walk_components(root):
        seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)


def _normalise_property_path(property_path: str) -> list[str]:
    """Split a property path, accepting an optional ``props.`` prefix."""
    path = property_path.strip().strip(".")
    if path.startswith("props."):
        path = path[len("props.") :]
    return [part for part in path.split(".") if part]


def _binding_node(
    component: dict[str, Any], property_path: str, create: bool = False
) -> dict[str, Any] | None:
    """Return the props node that owns a binding."""
    parts = _normalise_property_path(property_path)
    if not parts:
        return None
    node: Any = component.setdefault("props", {}) if create else component.get("props")
    if not isinstance(node, dict):
        return None
    for part in parts:
        if not isinstance(node, dict):
            return None
        if part not in node:
            if not create:
                return None
            node[part] = {}
        node = node[part]
    return node if isinstance(node, dict) else None


def _collect_transforms(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every binding transform in a view."""
    transforms: list[dict[str, Any]] = []
    for binding in _collect_bindings(view):
        entries = binding.get("transforms")
        if not isinstance(entries, list):
            continue
        for index, transform in enumerate(entries):
            if isinstance(transform, dict):
                transforms.append(
                    {
                        "component": binding["component"],
                        "property": binding["property"],
                        "index": index,
                        "type": transform.get("type"),
                        "transform": transform,
                    }
                )
    return transforms


def _collect_property_change_scripts(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect property-change scripts from component ``propConfig`` entries."""
    scripts: list[dict[str, Any]] = []
    for component_name, component in _walk_components(view.get("root", {})):
        prop_config = component.get("propConfig")
        if not isinstance(prop_config, dict):
            continue
        for property_name, entry in prop_config.items():
            if not isinstance(entry, dict):
                continue
            on_change = entry.get("onChange")
            if not isinstance(on_change, dict):
                continue
            script = on_change.get("script")
            if script:
                scripts.append(
                    {
                        "component": component_name,
                        "property": property_name,
                        "enabled": on_change.get("enabled"),
                        "script": script,
                    }
                )
    return scripts


def _collect_message_handlers(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect component message handlers."""
    handlers: list[dict[str, Any]] = []
    for component_name, component in _walk_components(view.get("root", {})):
        scripts = component.get("scripts")
        if not isinstance(scripts, dict):
            continue
        entries = scripts.get("messageHandlers")
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            handlers.append(
                {
                    "component": component_name,
                    "index": index,
                    "messageType": entry.get("messageType"),
                    "pageScope": entry.get("pageScope", False),
                    "sessionScope": entry.get("sessionScope", False),
                    "viewScope": entry.get("viewScope", False),
                    "script": entry.get("script", ""),
                }
            )
    return handlers


def _collect_all_scripts(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every embedded script with its Ignition context."""
    scripts: list[dict[str, Any]] = []
    for event in _collect_event_scripts(view):
        scripts.append({**event, "kind": "event", "context": "perspective_event"})
    for transform in _collect_transforms(view):
        scripts.append({**transform, "kind": "transform", "context": "script_transform"})
    for change in _collect_property_change_scripts(view):
        scripts.append({**change, "kind": "property_change", "context": "property_change"})
    for handler in _collect_message_handlers(view):
        scripts.append({**handler, "kind": "message_handler", "context": "message_handler"})
    return scripts


def _validate_view_scripts(view: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate every embedded script in a view."""
    results: list[dict[str, Any]] = []
    for entry in _collect_all_scripts(view):
        script = entry.get("script")
        if not isinstance(script, str):
            continue
        result = _validate_script_indentation(script, str(entry["context"]))
        results.append({**entry, **result})
    return results


def _script_issues(script: str, context: str) -> list[str]:
    """Return human-readable script issues for a targeted write."""
    result = _validate_script_indentation(script, context)
    return [
        f"{issue['code']} at line {issue['line']}: {issue['message']}" for issue in result["issues"]
    ]


def _validate_binding_shape(binding: dict[str, Any]) -> list[str]:
    """Validate the minimum shape of a Perspective binding."""
    if not isinstance(binding, dict):
        return ["Binding must be an object."]
    issues: list[str] = []
    if not binding.get("type"):
        issues.append("Binding is missing 'type'.")
    if not isinstance(binding.get("config", {}), dict):
        issues.append("Binding 'config' must be an object.")
    transforms = binding.get("transforms")
    if transforms is not None and not isinstance(transforms, list):
        issues.append("Binding 'transforms' must be a list.")
    return issues


def _validate_transform_shape(transform: dict[str, Any]) -> list[str]:
    """Validate a transform entry and its script context."""
    if not isinstance(transform, dict):
        return ["Transform must be an object."]
    issues: list[str] = []
    if not transform.get("type"):
        issues.append("Transform is missing 'type'.")
    if str(transform.get("type", "")).lower() == "script":
        code = transform.get("code")
        if not isinstance(code, str):
            issues.append("Script transform is missing 'code'.")
        else:
            issues.extend(_script_issues(code, "script_transform"))
    return issues


def _component_not_found(state: ServerState, view: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a component-not-found response with available names."""
    return state.tool_response(
        {
            "status": "component_not_found",
            "component": name,
            "available": [
                component_name for component_name, _ in _walk_components(view.get("root", {}))
            ],
        }
    )


async def _finish_view_change(
    state: ServerState,
    project: str,
    view_path: str,
    before: dict[str, Any],
    after: dict[str, Any],
    tool: str,
    dry_run: bool,
    issues: list[str],
) -> dict[str, Any]:
    """Preview or persist a targeted view change with read-back verification."""
    issues = [*_validate_view_json(after), *issues]
    if dry_run:
        return state.tool_response(
            {
                "status": "dry_run",
                "project": project,
                "view_path": view_path,
                "valid": not issues,
                "issues": issues,
                "diff": preview_json_change(before, after, view_path),
            }
        )
    if issues:
        return state.tool_response({"status": "invalid_view", "issues": issues, "project": project})
    try:
        result = await _write_view(state, project, view_path, after)
    except GatewayError as exc:
        return _gateway_or_unsupported(state, exc, tool)
    state.record_action(tool, "ok", project=project, view=view_path)
    return state.tool_response(
        {"status": "updated", "project": project, "view_path": view_path, **result}
    )


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    """Count items grouped by a key."""
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "The Perspective API is not present on this gateway. Local filesystem access "
                "(IGNITION_INSTALL_PATH) supports full view read/write without HTTP."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)


def _json_dumps(value: Any) -> str:
    """Serialise a value compactly (used for diff previews)."""
    return json.dumps(value, sort_keys=True, default=str)
