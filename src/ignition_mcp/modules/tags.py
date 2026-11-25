"""Tags and UDTs.

Live tag values only exist inside a running gateway, so these tools require a gateway
connection. UDT definitions and tag references can also be inspected from project resources
on disk (bindings, scripts and named queries).
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, resolve_project, unsupported
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

READ_CANDIDATES = ("/tags/read", "/data/tag/read")
BROWSE_CANDIDATES = ("/tags/browse", "/data/tag/browse")
CONFIG_CANDIDATES = ("/tags/configuration", "/tags/config", "/data/tag/getConfiguration")

#: Quality codes that represent a usable value (Ignition uses 192 for "Good").
GOOD_QUALITIES = {192, 200, "192", "Good", "good", "GOOD"}


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def browse_tags(path: str = "[default]", recursive: bool = False) -> dict[str, Any]:
        """Browse tag providers and folders.

        Args:
            path: Tag path to browse, e.g. ``[default]`` or ``[default]Equipment``.
            recursive: Return the full subtree instead of one level.

        Returns:
            Tag nodes with name, path, type and value type when available.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                BROWSE_CANDIDATES,
                method="POST",
                json_body={"path": path, "recursive": recursive},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "browse_tags")
        nodes = _flatten_browse(_items(payload, "tags", "results", "nodes"), path)
        return ok(
            state,
            "browse_tags",
            {"path": path, "recursive": recursive, "tags": nodes, "count": len(nodes)},
        )

    @mcp.tool()
    async def read_tag(paths: list[str]) -> dict[str, Any]:
        """Read current values and quality for one or more tags.

        Args:
            paths: Fully qualified tag paths, e.g. ``["[default]Pump1/Running"]``.

        Returns:
            One entry per tag with value, quality and timestamp.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                READ_CANDIDATES, method="POST", json_body={"paths": paths}
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "read_tag")
        reads = _normalise_reads(payload, paths)
        return ok(state, "read_tag", {"reads": reads, "count": len(reads)})

    @mcp.tool()
    async def write_tag(
        paths: list[str],
        values: list[Any],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Write values to tags.

        Args:
            paths: Fully qualified tag paths.
            values: Values to write; must align with ``paths``.
            confirm: Must be ``True`` to execute the write.
            dry_run: Preview the write without changing anything.

        Returns:
            Write acknowledgement or a guard/preview payload.
        """
        if len(paths) != len(values):
            return state.tool_response(
                {
                    "status": "invalid_request",
                    "message": "paths and values must have the same length.",
                    "paths": len(paths),
                    "values": len(values),
                }
            )
        guard = state.write_guard("write_tag", confirm, dry_run)
        if guard:
            return guard
        if not state.has_gateway:
            if dry_run:
                return state.tool_response(
                    {
                        "status": "dry_run",
                        "would_write": dict(zip(paths, values, strict=False)),
                        "verification": "unavailable_without_gateway",
                    }
                )
            return state.require_connection() or state.tool_response({"status": "not_connected"})

        current: dict[str, Any] = {}
        try:
            current = await _read_values(state, paths)
        except GatewayError as exc:
            logger.debug("Could not read current tag values: %s", exc)

        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "would_write": dict(zip(paths, values, strict=False)),
                    "current": current,
                    "changes": [
                        {"path": path, "before": current.get(path), "after": value}
                        for path, value in zip(paths, values, strict=False)
                        if current.get(path) != value
                    ],
                }
            )

        try:
            _, payload = await state.gateway.first_available(
                ("/tags/write", "/data/tag/write"),
                method="POST",
                json_body={"paths": paths, "values": values},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "write_tag")

        verification: dict[str, Any]
        try:
            after = await _read_values(state, paths)
        except GatewayError as exc:
            verification = {"verified": False, "reason": str(exc)}
        else:
            mismatches = [
                path for path, value in zip(paths, values, strict=False) if after.get(path) != value
            ]
            verification = {
                "verified": not mismatches,
                "mismatches": mismatches,
                "reads": after,
            }

        state.record_action("write_tag", "ok", count=len(paths))
        return state.tool_response(
            {
                "status": "written",
                "count": len(paths),
                "response": payload,
                "verification": verification,
            }
        )

    @mcp.tool()
    async def search_tags(
        query: str,
        provider: str = "default",
        tag_type: str | None = None,
        data_type: str | None = None,
        max_results: int = 100,
    ) -> dict[str, Any]:
        """Search tags by name pattern, type or value type.

        Args:
            query: Name pattern; ``*`` and ``?`` wildcards are supported.
            provider: Tag provider to search.
            tag_type: Optional filter such as ``AtomicTag``, ``UdtInstance`` or ``Folder``.
            data_type: Optional value type such as ``Int4`` or ``Boolean``.
            max_results: Maximum number of matches.

        Returns:
            Matching tag nodes.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                BROWSE_CANDIDATES,
                method="POST",
                json_body={"path": f"[{provider}]", "recursive": True},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "search_tags")

        nodes = _flatten_browse(_items(payload, "tags", "results", "nodes"), f"[{provider}]")
        matches = []
        for node in nodes:
            name = str(node.get("name", ""))
            if not fnmatch.fnmatch(name.lower(), query.lower()):
                continue
            if tag_type and str(node.get("tagType", "")).lower() != tag_type.lower():
                continue
            if data_type and str(node.get("dataType", "")).lower() != data_type.lower():
                continue
            matches.append(node)
            if len(matches) >= max_results:
                break
        return ok(
            state,
            "search_tags",
            {"query": query, "provider": provider, "matches": matches, "count": len(matches)},
        )

    @mcp.tool()
    async def get_tag_configuration(paths: list[str]) -> dict[str, Any]:
        """Get full configuration for one or more tags.

        Args:
            paths: Tag paths to inspect.

        Returns:
            Tag configuration documents, including alarms and history settings.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                CONFIG_CANDIDATES, method="POST", json_body={"paths": paths}
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "get_tag_configuration")
        return ok(state, "get_tag_configuration", {"paths": paths, "configuration": payload})

    @mcp.tool()
    async def list_udts(provider: str = "default") -> dict[str, Any]:
        """List UDT definitions in a tag provider.

        Args:
            provider: Tag provider name.

        Returns:
            UDT type names and member counts when available.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                BROWSE_CANDIDATES,
                method="POST",
                json_body={"path": f"[{provider}]_types_", "recursive": True},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "list_udts")

        nodes = _flatten_browse(_items(payload, "tags", "results", "nodes"), f"[{provider}]_types_")
        udts = [node for node in nodes if _is_udt_type(node)]
        return ok(state, "list_udts", {"provider": provider, "udts": udts, "count": len(udts)})

    @mcp.tool()
    async def get_udt_definition(udt_path: str) -> dict[str, Any]:
        """Get a UDT definition including members and parameters.

        Args:
            udt_path: UDT type path, e.g. ``[default]_types_/Equipment/Motor``.

        Returns:
            The UDT configuration document.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                CONFIG_CANDIDATES, method="POST", json_body={"paths": [udt_path]}
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "get_udt_definition")
        return ok(state, "get_udt_definition", {"udt_path": udt_path, "definition": payload})

    @mcp.tool()
    async def find_udt_instances(udt_type: str, provider: str = "default") -> dict[str, Any]:
        """Find every instance of a UDT in a provider.

        Args:
            udt_type: UDT type name or path.
            provider: Tag provider to search.

        Returns:
            Instance paths whose type matches ``udt_type``.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                BROWSE_CANDIDATES,
                method="POST",
                json_body={"path": f"[{provider}]", "recursive": True},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "find_udt_instances")

        nodes = _flatten_browse(_items(payload, "tags", "results", "nodes"), f"[{provider}]")
        instances = [
            node
            for node in nodes
            if _udt_type_matches(str(node.get("typeId", "")), udt_type)
            or str(node.get("tagType", "")).lower() in ("udtinstance", "udt_inst")
        ]
        return ok(
            state,
            "find_udt_instances",
            {"udt_type": udt_type, "provider": provider, "instances": instances},
        )

    @mcp.tool()
    async def compare_udts(udt_path_a: str, udt_path_b: str) -> dict[str, Any]:
        """Compare two UDT definitions.

        Args:
            udt_path_a: First UDT path.
            udt_path_b: Second UDT path.

        Returns:
            Added, removed and changed member names.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload_a = await state.gateway.first_available(
                CONFIG_CANDIDATES, method="POST", json_body={"paths": [udt_path_a]}
            )
            _, payload_b = await state.gateway.first_available(
                CONFIG_CANDIDATES, method="POST", json_body={"paths": [udt_path_b]}
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "compare_udts")

        members_a = _member_map(payload_a)
        members_b = _member_map(payload_b)
        added = sorted(set(members_b) - set(members_a))
        removed = sorted(set(members_a) - set(members_b))
        changed = sorted(
            name for name in set(members_a) & set(members_b) if members_a[name] != members_b[name]
        )
        return ok(
            state,
            "compare_udts",
            {
                "udt_a": udt_path_a,
                "udt_b": udt_path_b,
                "added": added,
                "removed": removed,
                "changed": changed,
                "identical": not (added or removed or changed),
            },
        )

    @mcp.tool()
    async def find_tag_references(tag_path: str, project: str | None = None) -> dict[str, Any]:
        """Find project resources that reference a tag path.

        Args:
            tag_path: Tag path to search for.
            project: Project to search; defaults to the active project.

        Returns:
            Matching files and lines across the project and its parents.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "find_tag_references",
                hint="Reference search needs a local Ignition install (IGNITION_INSTALL_PATH).",
            )
        chain = reader.inheritance_chain(proj or "")
        matches = reader.search_text(proj or "", tag_path, projects=chain[1:], max_results=300)
        return ok(
            state,
            "find_tag_references",
            {"tag_path": tag_path, "project": proj, "matches": matches, "count": len(matches)},
        )

    @mcp.tool()
    async def find_missing_feedback_tags(
        path: str = "[default]",
        running_suffix: str = "Running",
        cmd_suffix: str = "Start",
    ) -> dict[str, Any]:
        """Find command tags that have no matching feedback tag.

        Args:
            path: Root tag path to scan.
            running_suffix: Feedback tag suffix (e.g. ``Running`` or ``Feedback``).
            cmd_suffix: Command tag suffix (e.g. ``Start`` or ``Cmd``).

        Returns:
            Command tags without a sibling feedback tag.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        try:
            _, payload = await state.gateway.first_available(
                BROWSE_CANDIDATES,
                method="POST",
                json_body={"path": path, "recursive": True},
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "find_missing_feedback_tags")

        nodes = _flatten_browse(_items(payload, "tags", "results", "nodes"), path)
        names = {str(node.get("name", "")) for node in nodes}
        missing = []
        for node in nodes:
            name = str(node.get("name", ""))
            if not name.endswith(cmd_suffix):
                continue
            base = name[: -len(cmd_suffix)]
            if f"{base}{running_suffix}" not in names:
                missing.append(
                    {
                        "command_tag": node.get("path", name),
                        "expected_feedback": f"{base}{running_suffix}",
                    }
                )
        return ok(
            state,
            "find_missing_feedback_tags",
            {"path": path, "missing_feedback": missing, "count": len(missing)},
        )

    @mcp.tool()
    async def validate_tag_paths(paths: list[str], provider: str = "default") -> dict[str, Any]:
        """Check which tag paths exist and return a good quality value.

        Args:
            paths: Tag paths to validate.
            provider: Provider used when a path has no provider prefix.

        Returns:
            Valid and invalid paths with reasons.
        """
        if not state.has_gateway:
            return state.require_connection() or state.tool_response({"status": "not_connected"})
        qualified = [p if p.startswith("[") else f"[{provider}]{p}" for p in paths]
        try:
            _, payload = await state.gateway.first_available(
                READ_CANDIDATES, method="POST", json_body={"paths": qualified}
            )
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "validate_tag_paths")

        valid: list[str] = []
        invalid: list[dict[str, Any]] = []
        for read in _normalise_reads(payload, qualified):
            quality = read.get("quality")
            if quality in GOOD_QUALITIES or str(quality).lower().startswith("good"):
                valid.append(str(read.get("path")))
            else:
                invalid.append({"path": read.get("path"), "quality": quality})
        return ok(
            state,
            "validate_tag_paths",
            {"valid": valid, "invalid": invalid, "valid_count": len(valid)},
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _items(payload: Any, *keys: str) -> list[Any]:
    """Extract a list of items from a payload with a variable wrapper key."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (*keys, "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _flatten_browse(items: list[Any], parent_path: str) -> list[dict[str, Any]]:
    """Flatten a nested browse response into a list of tag nodes with paths.

    Args:
        items: Browse result items, possibly containing ``children``.
        parent_path: Path prefix for the first level.

    Returns:
        Flat list of tag node dicts.
    """
    flat: list[dict[str, Any]] = []

    def visit(entries: list[Any], prefix: str) -> None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("tagName") or "")
            path = str(entry.get("path") or entry.get("tagPath") or "")
            if not path and name:
                path = f"{prefix}/{name}" if prefix else name
            node = {**entry, "name": name, "path": path}
            flat.append(node)
            children = entry.get("children")
            if isinstance(children, list):
                visit(children, path)

    visit(items, parent_path)
    return flat


def _normalise_reads(payload: Any, requested: list[str]) -> list[dict[str, Any]]:
    """Normalise tag read payloads into ``{path, value, quality, timestamp}`` entries."""
    entries = _items(payload, "reads", "results", "values")
    normalised: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            normalised.append(
                {
                    "path": requested[index] if index < len(requested) else None,
                    "value": entry,
                }
            )
            continue
        normalised.append(
            {
                "path": entry.get("path")
                or entry.get("tagPath")
                or (requested[index] if index < len(requested) else None),
                "value": entry.get("value"),
                "quality": entry.get("quality"),
                "timestamp": entry.get("timestamp") or entry.get("time"),
            }
        )
    if not normalised and isinstance(payload, dict):
        normalised = [{"path": requested[0] if requested else None, "value": payload}]
    return normalised


async def _read_values(state: ServerState, paths: list[str]) -> dict[str, Any]:
    """Read current values for tag paths as a ``path -> value`` map."""
    _, payload = await state.gateway.first_available(
        READ_CANDIDATES, method="POST", json_body={"paths": paths}
    )
    return {str(read.get("path")): read.get("value") for read in _normalise_reads(payload, paths)}


def _udt_type_matches(type_id: str, udt_type: str) -> bool:
    """Return whether ``type_id`` exactly matches ``udt_type`` as a path component.

    Prevents substring false-positives (e.g. 'Motor' matching 'ElectricMotor').
    """
    if not type_id:
        return False
    return type_id == udt_type or type_id.rsplit("/", 1)[-1] == udt_type


def _is_udt_type(node: dict[str, Any]) -> bool:
    """Return whether a browse node represents a UDT definition."""
    tag_type = str(node.get("tagType", "")).lower()
    if tag_type in ("udttype", "udt_type", "udtdef", "udtdefinition"):
        return True
    return str(node.get("path", "")).endswith(".xml") or bool(node.get("members"))


def _member_map(payload: Any) -> dict[str, Any]:
    """Map member names to definitions from a UDT configuration payload."""
    members: dict[str, Any] = {}
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates = [payload]
        for key in ("configuration", "results", "tags"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.append(value)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for member in candidate.get("members") or candidate.get("tags") or []:
            if isinstance(member, dict) and member.get("name"):
                members[str(member["name"])] = member
    return members


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "The tag API endpoint was not found. Check /openapi.json on the gateway or "
                "the IGNITION_API_PREFIX setting."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
