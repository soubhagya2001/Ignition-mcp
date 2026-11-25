"""Data models for the Ignition documentation service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DocsPage:
    """A cached documentation page.

    Attributes:
        version: Docs version such as ``8.3``.
        path: Docs-relative path without a leading slash.
        url: Canonical public URL.
        title: Page title.
        kind: Coarse page kind (``system_function``, ``component``, ...).
        markdown: Markdown body when available; extracted text otherwise.
        text: Plain-text body for search and snippets.
        fetched_at: ISO timestamp of the cached copy.
        sections: Heading-to-text mapping extracted from the Markdown.
    """

    version: str
    path: str
    url: str
    title: str
    kind: str
    markdown: str
    text: str
    fetched_at: str = ""
    sections: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "version": self.version,
            "path": self.path,
            "url": self.url,
            "title": self.title,
            "kind": self.kind,
            "markdown": self.markdown,
            "text": self.text,
            "fetched_at": self.fetched_at,
            "sections": self.sections,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocsPage:
        """Build a page from cached JSON."""
        return cls(
            version=str(data.get("version", "")),
            path=str(data.get("path", "")),
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            kind=str(data.get("kind", "guide")),
            markdown=str(data.get("markdown", "")),
            text=str(data.get("text", "")),
            fetched_at=str(data.get("fetched_at", "")),
            sections=data.get("sections") or {},
        )
