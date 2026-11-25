"""Gateway capability negotiation.

Ignition 8.3 Standard is the primary target, but the same server should degrade gracefully on
other editions and versions. This module turns the gateway's ``/openapi.json`` document into a
small feature matrix so tools and agents can tell the difference between "the endpoint exists
but returned no data" and "this edition does not expose the endpoint at all".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ignition_mcp.state import ServerState

#: Feature name -> endpoint prefixes that would prove it is available.
FEATURE_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "tag_read": ("/tags/read", "/data/tag/read"),
    "tag_write": ("/tags/write", "/data/tag/write"),
    "tag_browse": ("/tags/browse", "/data/tag/browse"),
    "perspective": ("/perspective/projects", "/perspective/project"),
    "history": ("/history/query", "/historian/query"),
    "alarms": ("/alarms", "/alarm"),
    "projects": ("/project/", "/projects/"),
    "security": ("/security/", "/users"),
    "gateway": ("/gateway/", "/system/gateway"),
}


def feature_matrix(paths: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Map OpenAPI paths to a per-feature availability matrix.

    Args:
        paths: Paths from the gateway's OpenAPI document.

    Returns:
        ``{feature: {status, matched, candidates}}`` where status is ``available`` or
        ``unverified``.
    """
    path_set = {str(path) for path in paths}
    matrix: dict[str, dict[str, Any]] = {}
    for feature, candidates in FEATURE_ENDPOINTS.items():
        matched = sorted(
            candidate
            for candidate in candidates
            if any(
                path == candidate or path.startswith(candidate.rstrip("/") + "/")
                for path in path_set
            )
        )
        matrix[feature] = {
            "status": "available" if matched else "unverified",
            "matched": matched,
            "candidates": list(candidates),
        }
    return matrix


async def gateway_capabilities(state: ServerState) -> dict[str, Any]:
    """Build a capability report for the current session.

    Args:
        state: Shared server state.

    Returns:
        A JSON-serialisable report with target version, edition, OpenAPI summary and feature
        matrix. Local-only sessions report features as ``unknown`` rather than guessing.
    """
    session = state.session
    report: dict[str, Any] = {
        "target": {
            "ignition": "8.3",
            "edition": "Standard (primary); all editions supported with graceful degradation",
        },
        "gateway_connected": state.has_gateway,
        "version": session.gateway_version or "unknown",
        "edition": session.gateway_edition or "unknown",
        "local_filesystem": session.local_read_available,
        "local_install_path": session.local_install_path,
        "writes_enabled": not session.readonly,
        "openapi": {"available": False, "path_count": 0, "version": None},
        "features": {},
        "notes": [
            "Features marked 'unverified' may still work on editions that do not publish "
            "OpenAPI metadata; the tool will return an explicit unsupported payload if not.",
        ],
    }

    if not state.has_gateway:
        report["features"] = {
            feature: {
                "status": "unknown",
                "reason": "no_gateway_connection",
                "matched": [],
                "candidates": list(candidates),
            }
            for feature, candidates in FEATURE_ENDPOINTS.items()
        }
        return report

    spec = await state.gateway.openapi()
    paths = [str(path) for path in spec.get("paths", [])]
    report["openapi"] = {
        "available": bool(spec.get("available")),
        "path_count": len(paths),
        "version": spec.get("version"),
    }
    if spec.get("available"):
        report["features"] = feature_matrix(paths)
    else:
        reason = "openapi_unavailable"
        report["features"] = {
            feature: {
                "status": "unknown",
                "reason": reason,
                "matched": [],
                "candidates": list(candidates),
            }
            for feature, candidates in FEATURE_ENDPOINTS.items()
        }
    return report


__all__ = ["FEATURE_ENDPOINTS", "feature_matrix", "gateway_capabilities"]
