"""Local cache and search index for Ignition documentation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ignition_mcp.docs.catalog import known_index, normalise_version, search_terms, snippet
from ignition_mcp.docs.models import DocsPage


def docs_root() -> Path:
    """Return the docs cache root, honouring ``IGNITION_MCP_DOCS_DIR``."""
    override = os.environ.get("IGNITION_MCP_DOCS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ignition" / "mcp" / "docs"


def version_dir(version: str) -> Path:
    """Return the cache directory for a docs version."""
    return docs_root() / normalise_version(version)


def _index_path(version: str) -> Path:
    """Return the index path for a docs version."""
    return version_dir(version) / "index.json"


def _page_path(version: str, path: str) -> Path:
    """Return a safe cache path for a docs page."""
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]
    return version_dir(version) / "pages" / f"{digest}.json"


def load_index(version: str) -> list[dict[str, Any]]:
    """Load the cached catalog, falling back to the built-in seed."""
    index_path = _index_path(version)
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except (OSError, ValueError):
            pass
    return known_index(version)


def save_index(version: str, entries: list[dict[str, Any]]) -> Path:
    """Persist a catalog index."""
    path = _index_path(version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def load_page(version: str, path: str) -> DocsPage | None:
    """Load a cached page."""
    cache_path = _page_path(version, path)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return DocsPage.from_dict(data)
    except (OSError, ValueError):
        return None


def save_page(page: DocsPage) -> Path:
    """Persist a page and add it to the catalog index when missing."""
    cache_path = _page_path(page.version, page.path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(page.to_dict(), indent=2), encoding="utf-8")
    index = load_index(page.version)
    if not any(entry.get("path") == page.path for entry in index):
        index.append(
            {
                "version": page.version,
                "path": page.path,
                "title": page.title,
                "kind": page.kind,
                "url": page.url,
            }
        )
        save_index(page.version, index)
    return cache_path


def search(
    version: str, query: str, kind: str | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Search the cached catalog and page bodies.

    Args:
        version: Docs version.
        query: Search terms.
        kind: Optional page kind filter.
        limit: Maximum results.

    Returns:
        Ranked results with title, path, URL, kind and snippet.
    """
    normalise_version(version)
    terms = query.strip()
    if not terms:
        return []
    results: list[dict[str, Any]] = []
    for entry in load_index(version):
        if kind and entry.get("kind") != kind:
            continue
        page = load_page(version, str(entry.get("path", "")))
        title = str(entry.get("title", ""))
        path = str(entry.get("path", ""))
        body = page.text if page else ""
        title_match = search_terms(f"{title} {path}", terms)
        body_match = bool(body) and search_terms(body, terms)
        if not title_match and not body_match:
            continue
        score = (2 if title_match else 0) + (1 if body_match else 0)
        results.append(
            {
                "version": version,
                "path": path,
                "title": title,
                "kind": entry.get("kind", "guide"),
                "url": entry.get("url", ""),
                "snippet": snippet(body, terms) if body_match else "",
                "cached": page is not None,
                "score": score,
            }
        )
    results.sort(key=lambda item: (-int(item["score"]), str(item["title"]).lower()))
    return results[: max(1, limit)]
