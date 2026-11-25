"""MCP tools for versioned Ignition documentation lookup."""

from __future__ import annotations

from typing import Any

from ignition_mcp.docs.catalog import (
    DocsPathError,
    binding_candidates,
    component_candidates,
    expression_function_candidates,
    normalise_version,
    system_function_candidates,
)
from ignition_mcp.docs.fetch import DocsNotFound, DocsUnavailable, fetch_page, refresh_index
from ignition_mcp.docs.models import DocsPage
from ignition_mcp.docs.store import search
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import ok
from ignition_mcp.modules.scripting import _get_scope_hints
from ignition_mcp.state import ServerState


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the docs tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def docs_search(
        query: str,
        version: str = "8.3",
        kind: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search the versioned Ignition documentation catalog.

        Args:
            query: Search terms.
            version: Docs version (``8.3``, ``8.1`` or ``2027``).
            kind: Optional page kind such as ``system_function`` or ``component``.
            limit: Maximum results.

        Returns:
            Ranked results with paths, URLs and snippets.
        """
        try:
            resolved = normalise_version(version)
        except DocsPathError as exc:
            return state.tool_response({"status": "invalid_version", "message": str(exc)})
        results = search(resolved, query, kind, max(1, min(limit, 50)))
        return ok(
            state,
            "docs_search",
            {
                "version": resolved,
                "query": query,
                "results": results,
                "count": len(results),
                "source": "cache_or_known_catalog",
            },
        )

    @mcp.tool()
    async def docs_get_page(path_or_url: str, version: str = "8.3") -> dict[str, Any]:
        """Read a versioned Ignition documentation page.

        Args:
            path_or_url: Docs path or an official docs URL.
            version: Docs version used for relative paths.

        Returns:
            Markdown/text, citation and extracted sections.
        """
        try:
            page = await fetch_page(path_or_url, version)
        except DocsPathError as exc:
            return state.tool_response({"status": "invalid_url", "message": str(exc)})
        except DocsNotFound as exc:
            return state.tool_response({"status": "not_found", "message": str(exc)})
        except DocsUnavailable as exc:
            return state.tool_response(
                {
                    "status": "unavailable",
                    "message": str(exc),
                    "hint": "Retry when online, or call docs_refresh(confirm=True) first.",
                }
            )
        return ok(state, "docs_get_page", _page_payload(page))

    @mcp.tool()
    async def docs_system_function(name: str, version: str = "8.3") -> dict[str, Any]:
        """Look up a system function in the versioned manual.

        Args:
            name: Function name, e.g. ``system.perspective.sendMessage``.
            version: Docs version.

        Returns:
            The matching page, citation and extracted sections.
        """
        return await _lookup(
            state,
            "docs_system_function",
            system_function_candidates(name),
            version,
            name,
        )

    @mcp.tool()
    async def docs_expression_function(name: str, version: str = "8.3") -> dict[str, Any]:
        """Look up an expression function in the versioned manual.

        Args:
            name: Function name, e.g. ``ceil``.
            version: Docs version.

        Returns:
            The matching page, citation and extracted sections.
        """
        return await _lookup(
            state,
            "docs_expression_function",
            expression_function_candidates(name),
            version,
            name,
        )

    @mcp.tool()
    async def docs_component(name: str, version: str = "8.3") -> dict[str, Any]:
        """Look up a Perspective component in the versioned manual.

        Args:
            name: Component name or type, e.g. ``Button``.
            version: Docs version.

        Returns:
            The matching component page and sections.
        """
        return await _lookup(state, "docs_component", component_candidates(name), version, name)

    @mcp.tool()
    async def docs_binding(kind: str, version: str = "8.3") -> dict[str, Any]:
        """Look up binding, transform or message-handler guidance.

        Args:
            kind: ``tag``, ``property``, ``expression``, ``query``, ``transform``,
                ``script-transform``, ``property-change`` or ``message-handler``.
            version: Docs version.

        Returns:
            The matching page and sections.
        """
        return await _lookup(state, "docs_binding", binding_candidates(kind), version, kind)

    @mcp.tool()
    async def docs_script_scope(scope: str, version: str = "8.3") -> dict[str, Any]:
        """Return scope hints and matching manual pages for an Ignition script scope.

        Args:
            scope: Existing scripting scope such as ``perspective_component``.
            version: Docs version.

        Returns:
            Scope objects plus matching docs search results.
        """
        try:
            resolved = normalise_version(version)
        except DocsPathError as exc:
            return state.tool_response({"status": "invalid_version", "message": str(exc)})
        hints = _get_scope_hints(scope)
        results = search(resolved, scope.replace("_", " "), kind=None, limit=8)
        return ok(
            state,
            "docs_script_scope",
            {
                "version": resolved,
                "scope": scope,
                "scope_hints": hints,
                "results": results,
            },
        )

    @mcp.tool()
    async def docs_refresh(version: str = "8.3", confirm: bool = False) -> dict[str, Any]:
        """Refresh the local docs catalog from the official sitemap.

        Args:
            version: Docs version.
            confirm: Must be ``True`` to perform the network refresh.

        Returns:
            Index summary or a confirmation request.
        """
        if not confirm:
            return state.tool_response(
                {
                    "status": "requires_confirmation",
                    "operation": "docs_refresh",
                    "message": "Call docs_refresh(confirm=True) to fetch the official sitemap.",
                }
            )
        try:
            result = await refresh_index(version)
        except DocsPathError as exc:
            return state.tool_response({"status": "invalid_version", "message": str(exc)})
        except DocsUnavailable as exc:
            return state.tool_response({"status": "unavailable", "message": str(exc)})
        state.record_action("docs_refresh", "ok", version=result["version"])
        return state.tool_response({"status": "refreshed", **result})


async def _lookup(
    state: ServerState,
    tool: str,
    candidates: list[str],
    version: str,
    query: str,
) -> dict[str, Any]:
    """Try candidate paths and return the first available page."""
    try:
        normalise_version(version)
    except DocsPathError as exc:
        return state.tool_response({"status": "invalid_version", "message": str(exc)})
    errors: list[str] = []
    for path in candidates:
        try:
            page = await fetch_page(path, version)
        except DocsNotFound:
            errors.append(path)
            continue
        except DocsPathError as exc:
            return state.tool_response({"status": "invalid_url", "message": str(exc)})
        except DocsUnavailable as exc:
            return state.tool_response(
                {
                    "status": "unavailable",
                    "message": str(exc),
                    "tried": [*errors, path],
                    "hint": "Retry when online or use docs_search with cached pages.",
                }
            )
        return ok(
            state,
            tool,
            {
                "query": query,
                "version": page.version,
                **_page_payload(page),
            },
        )
    return state.tool_response(
        {
            "status": "not_found",
            "query": query,
            "version": version,
            "tried": errors,
            "hint": "Use docs_search to find the exact page path.",
        }
    )


def _page_payload(page: DocsPage) -> dict[str, Any]:
    """Return a citation-rich page payload."""
    return {
        "version": page.version,
        "path": page.path,
        "url": page.url,
        "title": page.title,
        "kind": page.kind,
        "markdown": page.markdown,
        "text": page.text,
        "sections": page.sections,
        "fetched_at": page.fetched_at,
    }
