"""Catalog helpers for the Ignition documentation site.

Only ``docs.inductiveautomation.com`` is allowed.  Paths are normalised and stripped of query
strings and fragments before they are used as cache keys or public URLs.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from ignition_mcp.docs.models import DocsPage

DOCS_HOST = "docs.inductiveautomation.com"
DOCS_BASE = f"https://{DOCS_HOST}"
DEFAULT_VERSION = "8.3"
VALID_VERSIONS = ("8.3", "8.1", "2027")

KNOWN_PAGES: tuple[dict[str, str], ...] = (
    {
        "path": "appendix/scripting-functions",
        "title": "Scripting Functions",
        "kind": "system_function",
    },
    {
        "path": "appendix/expression-functions",
        "title": "Expression Functions",
        "kind": "expression_function",
    },
    {
        "path": "appendix/components/perspective-components",
        "title": "Perspective Components",
        "kind": "component",
    },
    {
        "path": "working-with-perspective-components/bindings-in-perspective",
        "title": "Bindings in Perspective",
        "kind": "binding",
    },
    {
        "path": "working-with-perspective-components/bindings-in-perspective/transforms",
        "title": "Transforms",
        "kind": "binding",
    },
    {
        "path": "working-with-perspective-components/bindings-in-perspective/transforms/script-transform",
        "title": "Script Transform",
        "kind": "binding",
    },
    {
        "path": "working-with-perspective-components/scripting-in-perspective/component-message-handlers",
        "title": "Component Message Handlers",
        "kind": "binding",
    },
    {
        "path": "working-with-perspective-components/scripting-in-perspective/perspective-property-change-scripts",
        "title": "Property Change Scripts",
        "kind": "binding",
    },
    {
        "path": ("appendix/scripting-functions/system-perspective/system-perspective-sendMessage"),
        "title": "system.perspective.sendMessage",
        "kind": "system_function",
    },
    {
        "path": "appendix/expression-functions/math/ceil",
        "title": "ceil",
        "kind": "expression_function",
    },
    {
        "path": (
            "appendix/components/perspective-components/perspective-input-palette/"
            "perspective-button"
        ),
        "title": "Button",
        "kind": "component",
    },
)


class DocsPathError(ValueError):
    """Raised when a docs path or URL is not allowed."""


def normalise_version(version: str) -> str:
    """Validate a docs version.

    Args:
        version: Docs version string.

    Returns:
        The canonical version.

    Raises:
        DocsPathError: If the version is not one of :data:`VALID_VERSIONS`.
    """
    candidate = str(version).strip()
    if candidate not in VALID_VERSIONS:
        raise DocsPathError(
            f"Unsupported docs version '{version}'. Use one of: {', '.join(VALID_VERSIONS)}."
        )
    return candidate


def resolve_path(path_or_url: str, version: str = DEFAULT_VERSION) -> tuple[str, str]:
    """Resolve a path or official URL into ``(version, path)``.

    Args:
        path_or_url: Docs-relative path or an official docs URL.
        version: Fallback docs version.

    Returns:
        The version and normalised docs-relative path.

    Raises:
        DocsPathError: If the host, version or path is not allowed.
    """
    raw = str(path_or_url).strip()
    if not raw:
        raise DocsPathError("A docs path or URL is required.")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if parsed.scheme != "https" or parsed.hostname != DOCS_HOST:
            raise DocsPathError(f"Only https://{DOCS_HOST} URLs are allowed.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "docs":
            raise DocsPathError("Docs URLs must start with /docs/<version>/.")
        url_version = parts[1]
        version = normalise_version(url_version)
        path = "/".join(parts[2:])
    else:
        path = raw.split("?", 1)[0].split("#", 1)[0].strip("/")
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == "docs":
            parts = parts[1:]
            if parts and parts[0] in VALID_VERSIONS:
                version = parts[0]
                parts = parts[1:]
        elif parts and parts[0] in VALID_VERSIONS:
            version = parts[0]
            parts = parts[1:]
        path = "/".join(parts)

    version = normalise_version(version)
    if not path or ".." in path.split("/") or any(part.startswith(".") for part in path.split("/")):
        raise DocsPathError("Docs path is empty or contains an unsafe segment.")
    return version, path


def page_url(version: str, path: str) -> str:
    """Return the canonical public URL for a docs path."""
    version = normalise_version(version)
    return f"{DOCS_BASE}/docs/{version}/{path.lstrip('/')}"


def markdown_url(version: str, path: str) -> str:
    """Return the Markdown URL for a docs path."""
    base = page_url(version, path)
    return base if base.endswith(".md") else f"{base}.md"


def kind_for_path(path: str) -> str:
    """Infer a coarse page kind from its path."""
    lowered = path.lower()
    if "scripting-functions" in lowered:
        return "system_function"
    if "expression-functions" in lowered:
        return "expression_function"
    if "perspective-components" in lowered or "components/" in lowered:
        return "component"
    if "binding" in lowered or "transform" in lowered or "message-handler" in lowered:
        return "binding"
    return "guide"


def system_function_candidates(name: str) -> list[str]:
    """Return likely docs paths for a system function name."""
    cleaned = name.strip().removeprefix("system.").strip(".")
    parts = [part for part in cleaned.split(".") if part]
    if not parts:
        return []
    candidates: list[str] = []
    if len(parts) >= 3:
        candidates.append(
            f"appendix/scripting-functions/{parts[0]}-{parts[1]}/{parts[0]}-{parts[1]}-{parts[2]}"
        )
    if len(parts) == 2:
        candidates.append(f"appendix/scripting-functions/{parts[0]}/{parts[0]}-{parts[1]}")
    candidates.append(f"appendix/scripting-functions/{'-'.join(parts)}")
    return list(dict.fromkeys(candidates))


def expression_function_candidates(name: str) -> list[str]:
    """Return likely docs paths for an expression function."""
    slug = name.strip().lower().replace(" ", "-")
    return list(
        dict.fromkeys(
            [
                f"appendix/expression-functions/math/{slug}",
                f"appendix/expression-functions/{slug}",
            ]
        )
    )


def component_candidates(name: str) -> list[str]:
    """Return likely docs paths for a Perspective component."""
    slug = name.strip().lower().replace(" ", "-").replace("_", "-")
    if slug.startswith("ia."):
        slug = slug.split(".")[-1]
    perspective = slug if slug.startswith("perspective-") else f"perspective-{slug}"
    return list(
        dict.fromkeys(
            [
                (
                    "appendix/components/perspective-components/"
                    f"perspective-input-palette/{perspective}"
                ),
                f"appendix/components/perspective-components/{perspective}",
                f"appendix/components/perspective-components/{slug}",
            ]
        )
    )


def binding_candidates(kind: str) -> list[str]:
    """Return docs paths for a binding-related topic."""
    lowered = kind.strip().lower().replace("_", "-")
    mapping = {
        "tag": ["working-with-perspective-components/bindings-in-perspective"],
        "property": ["working-with-perspective-components/bindings-in-perspective"],
        "expression": ["working-with-perspective-components/bindings-in-perspective"],
        "query": ["working-with-perspective-components/bindings-in-perspective"],
        "transform": ["working-with-perspective-components/bindings-in-perspective/transforms"],
        "script-transform": [
            "working-with-perspective-components/bindings-in-perspective/transforms/"
            "script-transform"
        ],
        "property-change": [
            "working-with-perspective-components/scripting-in-perspective/"
            "perspective-property-change-scripts"
        ],
        "message-handler": [
            "working-with-perspective-components/scripting-in-perspective/"
            "component-message-handlers"
        ],
    }
    return mapping.get(lowered, [f"working-with-perspective-components/{lowered}"])


def parse_markdown(markdown: str, version: str, path: str) -> DocsPage:
    """Parse Markdown/frontmatter into a :class:`DocsPage`."""
    body = markdown.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter: dict[str, str] = {}
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            header = body[3:end].strip()
            body = body[end + 4 :].lstrip("\n")
            for line in header.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    frontmatter[key.strip().lower()] = value.strip().strip("\"'")

    title = frontmatter.get("title", "")
    if not title:
        heading = re.search(r"(?m)^#\s+(.+?)\s*$", body)
        title = heading.group(1).strip() if heading else path.rsplit("/", 1)[-1]
    return DocsPage(
        version=version,
        path=path,
        url=page_url(version, path),
        title=title,
        kind=kind_for_path(path),
        markdown=body,
        text=markdown_to_text(body),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        sections=extract_sections(body),
    )


class _TextExtractor(HTMLParser):
    """Extract readable text from HTML without adding a parser dependency."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "header", "footer"}:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "header", "footer"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)

    def text(self) -> str:
        """Return normalised text."""
        return _collapse_blank_lines(html.unescape("".join(self.parts)))


def html_to_markdown(page_html: str, version: str, path: str) -> DocsPage:
    """Extract a readable page from HTML when no Markdown route exists."""
    parser = _TextExtractor()
    parser.feed(page_html)
    text = parser.text()
    title = parser.title.strip() or path.rsplit("/", 1)[-1]
    return DocsPage(
        version=version,
        path=path,
        url=page_url(version, path),
        title=title,
        kind=kind_for_path(path),
        markdown=text,
        text=text,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        sections={},
    )


def markdown_to_text(markdown: str) -> str:
    """Strip common Markdown syntax for search and snippets."""
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"[*_>|-]+", " ", text)
    return _collapse_blank_lines(text)


def extract_sections(markdown: str) -> dict[str, str]:
    """Extract ``##`` sections from Markdown."""
    sections: dict[str, str] = {}
    current = ""
    buffer: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).strip()
            buffer = []
        elif current:
            buffer.append(line)
    if current:
        sections[current] = "\n".join(buffer).strip()
    return sections


def search_terms(text: str, query: str) -> bool:
    """Return whether all query terms appear in text, case-insensitively."""
    haystack = text.lower()
    return all(term in haystack for term in query.lower().split())


def snippet(text: str, query: str, length: int = 240) -> str:
    """Return a short snippet around the first query term."""
    lowered = text.lower()
    index = min(
        (lowered.find(term) for term in query.lower().split() if term in lowered),
        default=0,
    )
    start = max(0, index - length // 3)
    return text[start : start + length].replace("\n", " ").strip()


def known_index(version: str) -> list[dict[str, Any]]:
    """Return the built-in catalog entries for a version."""
    normalise_version(version)
    return [
        {
            "version": version,
            "path": entry["path"],
            "title": entry["title"],
            "kind": entry["kind"],
            "url": page_url(version, entry["path"]),
        }
        for entry in KNOWN_PAGES
    ]


def _collapse_blank_lines(text: str) -> str:
    """Collapse excessive whitespace while preserving paragraph breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line:
            collapsed.append(line)
        elif collapsed and collapsed[-1] != "":
            collapsed.append("")
    return "\n".join(collapsed).strip()
