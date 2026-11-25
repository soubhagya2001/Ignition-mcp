"""Tests for project-wide analysis, dependency maps and generated docs."""

from __future__ import annotations

import json

from ignition_mcp.modules.analysis import _analyze_project, _dependency_map, _project_docs
from ignition_mcp.state import ServerState


def _add_broken_view(state: ServerState) -> None:
    reader = state.local_reader
    assert reader is not None
    view_dir = (
        reader.project_path("TestProject")
        / "com.inductiveautomation.perspective"
        / "views"
        / "Extra"
    )
    view_dir.mkdir(parents=True)
    view = {
        "root": {
            "type": "ia.container.flex",
            "meta": {"name": "root"},
            "children": [
                {
                    "type": "ia.display.label",
                    "meta": {"name": "label"},
                    "props": {
                        "text": {"binding": {"type": "tag", "config": None}},
                        "style": {"classes": ["MissingStyle"]},
                    },
                    "children": [],
                }
            ],
        }
    }
    (view_dir / "view.json").write_text(json.dumps(view), encoding="utf-8")


def test_analysis_detects_broken_bindings_and_styles(local_state: ServerState) -> None:
    _add_broken_view(local_state)
    reader = local_state.local_reader
    assert reader is not None

    report = _analyze_project(reader, "TestProject")
    kinds = {issue["kind"] for issue in report["issues"]}
    assert "empty_binding" in kinds
    assert "missing_style_class" in kinds
    assert "unused_view" in kinds
    assert report["summary"]["views"] == 3
    assert report["summary"]["errors"] == 0
    assert report["valid"] is True


def test_dependency_map_follows_inheritance(local_state: ServerState) -> None:
    reader = local_state.local_reader
    assert reader is not None

    graph = _dependency_map(reader, "TestProject")
    assert [node["project"] for node in graph["nodes"]] == ["TestProject", "Configuration"]
    assert graph["edges"] == [{"from": "TestProject", "to": "Configuration", "type": "inherits"}]
    assert graph["cycle"] == []


def test_generated_docs_cover_project_resources(local_state: ServerState) -> None:
    reader = local_state.local_reader
    assert reader is not None

    markdown = _project_docs(reader, "TestProject")
    assert "# Project: TestProject" in markdown
    assert "Dashboard/Overview" in markdown
    assert "Util" in markdown
    assert "GetCounts" in markdown
    assert "Title" in markdown
