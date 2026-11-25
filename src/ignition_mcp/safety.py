"""Write-safety primitives: snapshots, restore and diff previews.

Every local write is preceded by a byte-for-byte snapshot of the file being replaced. The
snapshots live outside the project (``~/.ignition/mcp/snapshots`` by default) so they survive a
bad project change and can be restored without touching the gateway.

The same module provides the diff helpers used by ``dry_run`` responses, so an agent can show a
human exactly what a write would change before asking for confirmation.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_LIMIT = 20
MAX_DIFF_CHARS = 20_000
MAX_JSON_CHANGES = 200


def snapshot_root() -> Path:
    """Return the directory that holds write snapshots.

    ``IGNITION_MCP_SNAPSHOT_DIR`` overrides the default, which is
    ``~/.ignition/mcp/snapshots``.
    """
    override = os.environ.get("IGNITION_MCP_SNAPSHOT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ignition" / "mcp" / "snapshots"


def _snapshot_limit() -> int:
    """Return the retention limit, tolerating a bad environment value."""
    try:
        return max(1, int(os.environ.get("IGNITION_MCP_SNAPSHOT_LIMIT", DEFAULT_SNAPSHOT_LIMIT)))
    except ValueError:
        return DEFAULT_SNAPSHOT_LIMIT


def snapshot_file(path: Path, operation: str, **metadata: Any) -> dict[str, Any]:
    """Copy a file into the snapshot store before it is overwritten.

    Args:
        path: File that is about to change.
        operation: Tool or operation name recorded in the manifest.
        **metadata: Extra non-secret context (project, view, module, ...).

    Returns:
        A manifest dict with ``status`` ``snapshotted`` or ``skipped``.
    """
    path = Path(path)
    if not path.is_file():
        return {"status": "skipped", "reason": "file_not_found", "path": str(path)}

    root = snapshot_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        snapshot_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        snap_dir = root / snapshot_id
        snap_dir.mkdir()
        shutil.copy2(path, snap_dir / "content")
        manifest = {
            "snapshot_id": snapshot_id,
            "operation": operation,
            "path": str(path.resolve()),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "size_bytes": path.stat().st_size,
            "metadata": metadata,
        }
        (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not snapshot %s: %s", path, exc)
        return {"status": "skipped", "reason": str(exc), "path": str(path)}

    _prune(root)
    return {"status": "snapshotted", **manifest}


def _prune(root: Path) -> None:
    """Delete the oldest snapshots beyond the retention limit."""
    try:
        directories = sorted(
            (entry for entry in root.iterdir() if entry.is_dir()), key=lambda entry: entry.name
        )
    except OSError:
        return
    for expired in directories[: max(0, len(directories) - _snapshot_limit())]:
        shutil.rmtree(expired, ignore_errors=True)


def list_snapshots(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent snapshot manifests, newest first.

    Args:
        limit: Maximum number of manifests to return.

    Returns:
        Manifest dicts; unreadable snapshots are skipped.
    """
    root = snapshot_root()
    if not root.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict):
            manifests.append(manifest)
        if len(manifests) >= max(1, limit):
            break
    return manifests


def restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    """Restore a snapshot to its original path.

    Args:
        snapshot_id: Identifier returned by :func:`snapshot_file`.

    Returns:
        ``{"status": "restored", ...}`` or an error payload. Never raises for a bad id.
    """
    if not snapshot_id or snapshot_id in (".", "..") or "/" in snapshot_id or "\\" in snapshot_id:
        return {"status": "invalid_snapshot", "message": "Invalid snapshot id."}

    root = snapshot_root().resolve()
    snap_dir = (root / snapshot_id).resolve()
    if root not in snap_dir.parents:
        return {"status": "invalid_snapshot", "message": "Snapshot path escapes the store."}

    manifest_path = snap_dir / "manifest.json"
    content_path = snap_dir / "content"
    if not manifest_path.is_file() or not content_path.is_file():
        return {"status": "not_found", "snapshot_id": snapshot_id}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = Path(manifest["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(content_path, target)
    except (OSError, KeyError, ValueError) as exc:
        return {"status": "restore_failed", "snapshot_id": snapshot_id, "message": str(exc)}

    return {
        "status": "restored",
        "snapshot_id": snapshot_id,
        "path": str(target),
        "operation": manifest.get("operation"),
        "created_at": manifest.get("created_at"),
    }


def text_diff(before: str, after: str, filename: str = "resource") -> str:
    """Return a unified diff between two text documents.

    Args:
        before: Original text.
        after: Proposed text.
        filename: Label used in the diff header.

    Returns:
        The unified diff, truncated to ``MAX_DIFF_CHARS``.
    """
    diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{filename} (before)",
            tofile=f"{filename} (after)",
            lineterm="",
        )
    )
    return diff[:MAX_DIFF_CHARS]


def json_changes(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Return leaf-level changes between two JSON-compatible values.

    Args:
        before: Original value.
        after: Proposed value.
        path: Accumulated JSON path used in results.

    Returns:
        Up to ``MAX_JSON_CHANGES`` ``{path, before, after}`` entries.
    """
    changes: list[dict[str, Any]] = []
    _collect_json_changes(before, after, path, changes)
    return changes[:MAX_JSON_CHANGES]


def _collect_json_changes(before: Any, after: Any, path: str, out: list[dict[str, Any]]) -> None:
    """Recursively collect leaf changes into ``out``."""
    if len(out) >= MAX_JSON_CHANGES:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}" if path else str(key)
            _collect_json_changes(before.get(key), after.get(key), child, out)
        return
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            _collect_json_changes(
                before[index] if index < len(before) else None,
                after[index] if index < len(after) else None,
                child,
                out,
            )
        return
    if before != after:
        out.append({"path": path or "(root)", "before": before, "after": after})


def preview_json_change(before: Any, after: Any, filename: str = "resource") -> dict[str, Any]:
    """Build a dry-run preview for a JSON document change.

    Args:
        before: Existing document (``None`` for a create).
        after: Proposed document.
        filename: Label used in the diff header.

    Returns:
        ``{changed, change_count, changes, diff}``.
    """
    changes = json_changes(before, after)
    if not changes:
        return {"changed": False, "change_count": 0, "changes": [], "diff": ""}
    before_text = json.dumps(before, indent=2, sort_keys=True, default=str)
    after_text = json.dumps(after, indent=2, sort_keys=True, default=str)
    return {
        "changed": True,
        "change_count": len(changes),
        "changes": changes,
        "diff": text_diff(before_text, after_text, filename),
    }


__all__ = [
    "json_changes",
    "list_snapshots",
    "preview_json_change",
    "restore_snapshot",
    "snapshot_file",
    "snapshot_root",
    "text_diff",
]
