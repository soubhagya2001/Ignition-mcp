"""Versioned Ignition documentation access.

The docs service is deliberately small: it discovers pages from the official sitemap, caches
Markdown or extracted HTML under ``~/.ignition/mcp/docs`` and exposes read-only MCP tools.  It
never fetches an arbitrary host and never sends credentials.
"""

from ignition_mcp.docs.models import DocsPage

__all__ = ["DocsPage"]
