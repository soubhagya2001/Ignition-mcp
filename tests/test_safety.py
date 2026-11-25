"""Tests for write snapshots, restore and diff previews."""

from __future__ import annotations

import json
from pathlib import Path

from ignition_mcp import safety


def test_snapshot_and_restore_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IGNITION_MCP_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    target = tmp_path / "project" / "view.json"
    target.parent.mkdir()
    target.write_text('{"version": 1}', encoding="utf-8")

    manifest = safety.snapshot_file(target, "update_view", project="Test")
    assert manifest["status"] == "snapshotted"

    target.write_text('{"version": 2}', encoding="utf-8")
    listed = safety.list_snapshots()
    assert len(listed) == 1
    assert listed[0]["snapshot_id"] == manifest["snapshot_id"]

    restored = safety.restore_snapshot(manifest["snapshot_id"])
    assert restored["status"] == "restored"
    assert target.read_text(encoding="utf-8") == '{"version": 1}'


def test_snapshot_skips_missing_file(tmp_path: Path) -> None:
    result = safety.snapshot_file(tmp_path / "missing.json", "write")
    assert result["status"] == "skipped"
    assert result["reason"] == "file_not_found"


def test_restore_rejects_path_traversal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IGNITION_MCP_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    result = safety.restore_snapshot("../outside")
    assert result["status"] == "invalid_snapshot"


def test_retention_keeps_only_newest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IGNITION_MCP_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("IGNITION_MCP_SNAPSHOT_LIMIT", "2")
    target = tmp_path / "code.py"
    target.write_text("a = 1\n", encoding="utf-8")
    for value in range(4):
        target.write_text(f"a = {value}\n", encoding="utf-8")
        safety.snapshot_file(target, "write_script")
    assert len(safety.list_snapshots(limit=10)) == 2


def test_text_diff_shows_change() -> None:
    diff = safety.text_diff("a = 1\n", "a = 2\n", "code.py")
    assert "-a = 1" in diff
    assert "+a = 2" in diff


def test_preview_json_change_reports_leaf_changes() -> None:
    preview = safety.preview_json_change(
        {"root": {"props": {"text": "old"}}, "params": {}},
        {"root": {"props": {"text": "new"}}, "params": {"line": "A"}},
        "view.json",
    )
    assert preview["changed"] is True
    paths = {change["path"] for change in preview["changes"]}
    assert "root.props.text" in paths
    assert "params.line" in paths
    assert "diff" in preview


def test_preview_json_change_noop() -> None:
    preview = safety.preview_json_change({"a": 1}, {"a": 1})
    assert preview["changed"] is False
    assert preview["change_count"] == 0
    assert preview["diff"] == ""


def test_snapshot_manifest_is_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IGNITION_MCP_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    target = tmp_path / "resource.json"
    target.write_text("{}", encoding="utf-8")
    manifest = safety.snapshot_file(target, "write", project="P")
    manifest_path = tmp_path / "snapshots" / manifest["snapshot_id"] / "manifest.json"
    decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert decoded["metadata"]["project"] == "P"
