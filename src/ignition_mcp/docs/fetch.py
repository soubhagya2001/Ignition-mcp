"""Network access for the Ignition documentation service."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from ignition_mcp.docs.catalog import (
    DOCS_BASE,
    DocsPathError,
    html_to_markdown,
    known_index,
    markdown_url,
    normalise_version,
    page_url,
    parse_markdown,
    resolve_path,
)
from ignition_mcp.docs.models import DocsPage
from ignition_mcp.docs.store import load_index, load_page, save_index, save_page


class DocsUnavailable(RuntimeError):
    """Raised when the docs site cannot be reached and no cache exists."""


class DocsNotFound(LookupError):
    """Raised when a requested docs page does not exist."""


async def fetch_page(path_or_url: str, version: str = "8.3", refresh: bool = False) -> DocsPage:
    """Fetch a page from cache or the official docs site.

    Args:
        path_or_url: Docs path or official URL.
        version: Fallback docs version.
        refresh: Ignore a cached copy.

    Returns:
        The parsed page.

    Raises:
        DocsPathError: If the path or URL is not allowed.
        DocsNotFound: If neither Markdown nor HTML exists.
        DocsUnavailable: If the site cannot be reached and no cache exists.
    """
    resolved_version, path = resolve_path(path_or_url, version)
    cached = load_page(resolved_version, path)
    if cached is not None and not refresh:
        return cached

    timeout = httpx.Timeout(20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            markdown_response = await client.get(markdown_url(resolved_version, path))
            if markdown_response.status_code == 200:
                page = parse_markdown(markdown_response.text, resolved_version, path)
                save_page(page)
                return page
            if markdown_response.status_code not in (404, 405):
                raise DocsUnavailable(
                    f"Docs request failed with HTTP {markdown_response.status_code}."
                )

            html_response = await client.get(page_url(resolved_version, path))
            if html_response.status_code == 404:
                raise DocsNotFound(f"Docs page '{path}' was not found for {resolved_version}.")
            if html_response.status_code != 200:
                raise DocsUnavailable(f"Docs request failed with HTTP {html_response.status_code}.")
            page = html_to_markdown(html_response.text, resolved_version, path)
            save_page(page)
            return page
    except httpx.HTTPError as exc:
        raise DocsUnavailable(f"Docs site is unreachable: {exc}") from exc


async def refresh_index(version: str = "8.3") -> dict[str, Any]:
    """Refresh the catalog from the official sitemap.

    Args:
        version: Docs version to index.

    Returns:
        A summary with the number of discovered pages and the cache path.

    Raises:
        DocsUnavailable: If the sitemap cannot be fetched.
    """
    version = normalise_version(version)
    timeout = httpx.Timeout(20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(f"{DOCS_BASE}/sitemap.xml")
    except httpx.HTTPError as exc:
        raise DocsUnavailable(f"Docs sitemap is unreachable: {exc}") from exc
    if response.status_code != 200:
        raise DocsUnavailable(f"Sitemap request failed with HTTP {response.status_code}.")

    entries: dict[str, dict[str, Any]] = {entry["path"]: entry for entry in known_index(version)}
    try:
        root = ET.fromstring(response.text)
        locations = [element.text or "" for element in root.iter() if element.tag.endswith("loc")]
    except ET.ParseError:
        locations = re.findall(r"<loc>(.*?)</loc>", response.text)
    prefix = f"/docs/{version}/"
    for location in locations:
        if prefix not in location:
            continue
        path = location.split(prefix, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
        if not path or path.endswith(".md"):
            continue
        try:
            resolved_version, normalised = resolve_path(path, version)
        except DocsPathError:
            continue
        if resolved_version != version:
            continue
        entries.setdefault(
            normalised,
            {
                "version": version,
                "path": normalised,
                "title": normalised.rsplit("/", 1)[-1].replace("-", " ").title(),
                "kind": _kind_for(normalised),
                "url": page_url(version, normalised),
            },
        )
    index = sorted(entries.values(), key=lambda item: str(item["path"]))
    index_path = save_index(version, index)
    return {"version": version, "pages": len(index), "index_path": str(index_path)}


def cached_index(version: str) -> list[dict[str, Any]]:
    """Return the current index, seeded from known pages when empty."""
    return load_index(normalise_version(version))


def _kind_for(path: str) -> str:
    """Infer a page kind for a sitemap entry."""
    lowered = path.lower()
    if "scripting-functions" in lowered:
        return "system_function"
    if "expression-functions" in lowered:
        return "expression_function"
    if "perspective-components" in lowered:
        return "component"
    if "binding" in lowered or "transform" in lowered or "message-handler" in lowered:
        return "binding"
    return "guide"
