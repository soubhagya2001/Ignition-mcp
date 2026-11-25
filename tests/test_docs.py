"""Tests for versioned Ignition documentation access."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from ignition_mcp.config import IgnitionMCPConfig
from ignition_mcp.docs.catalog import DocsPathError, parse_markdown, resolve_path
from ignition_mcp.docs.fetch import fetch_page, refresh_index
from ignition_mcp.docs.store import search
from ignition_mcp.docs.tools import register
from ignition_mcp.state import IgnitionSession, ServerState


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


@pytest.fixture(autouse=True)
def _isolated_docs_cache(tmp_path, monkeypatch):
    """Keep docs cache writes inside the test directory."""
    monkeypatch.setenv("IGNITION_MCP_DOCS_DIR", str(tmp_path / "docs-cache"))


def _state() -> ServerState:
    state = ServerState(config=IgnitionMCPConfig(readonly=True))
    state.session = IgnitionSession(
        is_connected=False,
        readonly=True,
        active_project="TestProject",
        projects=["TestProject"],
    )
    return state


def test_resolve_path_rejects_foreign_hosts():
    """Only the official docs host is accepted."""
    with pytest.raises(DocsPathError):
        resolve_path("https://example.com/docs/8.3/appendix/scripting-functions")


def test_resolve_url_strips_query_and_uses_url_version():
    """Official URLs keep their version and discard query strings."""
    version, path = resolve_path(
        "https://docs.inductiveautomation.com/docs/8.1/appendix/scripting-functions?x=1"
    )

    assert version == "8.1"
    assert path == "appendix/scripting-functions"


def test_parse_markdown_extracts_title_and_sections():
    """Markdown frontmatter and headings become searchable metadata."""
    page = parse_markdown(
        "---\ntitle: system.perspective.sendMessage\n---\n\n## Syntax\n\n```python\nsendMessage(...)\n```\n",
        "8.3",
        "appendix/scripting-functions/system-perspective/system-perspective-sendMessage",
    )

    assert page.title == "system.perspective.sendMessage"
    assert "Syntax" in page.sections
    assert page.url.endswith("system-perspective-sendMessage")


def test_search_known_catalog_finds_send_message():
    """The built-in seed makes common docs lookup work before a network refresh."""
    results = search("8.3", "sendMessage")

    assert results
    assert any("sendMessage" in result["title"] for result in results)


@respx.mock
async def test_fetch_page_caches_markdown():
    """Markdown is fetched once and then served from cache."""
    url = "https://docs.inductiveautomation.com/docs/8.3/appendix/expression-functions/math/ceil.md"
    route = respx.get(url).mock(
        return_value=httpx.Response(
            200,
            text="# ceil\n\n## Syntax\n\nceil(value)\n",
        )
    )

    page = await fetch_page("appendix/expression-functions/math/ceil", "8.3")
    cached = await fetch_page("appendix/expression-functions/math/ceil", "8.3")

    assert page.title == "ceil"
    assert cached.title == "ceil"
    assert route.call_count == 1


@respx.mock
async def test_refresh_index_uses_sitemap():
    """Sitemap URLs become catalog entries for the requested version."""
    route = respx.get("https://docs.inductiveautomation.com/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text=(
                "<urlset><url><loc>https://docs.inductiveautomation.com/docs/8.3/"
                "appendix/scripting-functions</loc></url>"
                "<url><loc>https://docs.inductiveautomation.com/docs/8.3/"
                "appendix/expression-functions/math/ceil</loc></url></urlset>"
            ),
        )
    )

    result = await refresh_index("8.3")

    assert result["pages"] >= 2
    assert route.called


async def test_docs_search_tool_returns_citations():
    """The MCP tool exposes versioned paths and URLs."""
    mcp = RecordingMCP()
    state = _state()
    register(mcp, state)

    response = await mcp.tools["docs_search"]("sendMessage", version="8.3")
    payload = response["result"]

    assert payload["version"] == "8.3"
    assert payload["count"] >= 1
    assert payload["results"][0]["url"].startswith("https://docs.inductiveautomation.com/")
