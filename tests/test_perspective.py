"""Tests for the Perspective helpers and local reads."""

from __future__ import annotations

import json
from pathlib import Path

from ignition_mcp.autoconnect.local_reader import LocalInstallReader
from ignition_mcp.modules.perspective import (
    _collect_bindings,
    _collect_event_scripts,
    _duplicate_component_names,
    _embedded_view_refs,
    _extract_component_tree,
    _validate_view_json,
)


def test_list_perspective_views_local(fake_install):
    """Views are listed from the local filesystem."""
    views = LocalInstallReader(fake_install).list_perspective_views("TestProject")

    assert "Dashboard/Overview" in views
    assert "Equipment/Pump" in views


def test_validate_view_json_missing_root():
    """A view without root is invalid."""
    issues = _validate_view_json({})
    assert any("root" in issue for issue in issues)


def test_validate_view_json_valid():
    """A minimal valid view has no issues."""
    issues = _validate_view_json({"root": {"type": "ia.container.flex"}})
    assert issues == []


def test_extract_component_tree():
    """The component tree reports type, name and child counts."""
    component = {
        "type": "ia.container.flex",
        "meta": {"name": "root"},
        "children": [
            {"type": "ia.display.label", "meta": {"name": "label1"}, "children": []},
        ],
    }

    tree = _extract_component_tree(component, max_depth=5)

    assert tree["type"] == "ia.container.flex"
    assert tree["children_count"] == 1
    assert tree["children"][0]["name"] == "label1"


def test_extract_component_tree_respects_depth():
    """Depth limiting marks truncated branches."""
    component = {
        "type": "ia.container.flex",
        "meta": {"name": "root"},
        "children": [{"type": "ia.display.label", "meta": {"name": "a"}, "children": []}],
    }

    tree = _extract_component_tree(component, max_depth=0)
    assert "children" not in tree


def test_collect_bindings(fake_install):
    """Tag bindings are discovered with their component and property path."""
    view = LocalInstallReader(fake_install).read_perspective_view(
        "TestProject", "Dashboard/Overview"
    )
    assert view is not None

    bindings = _collect_bindings(view)

    assert len(bindings) == 1
    assert bindings[0]["component"] == "title"
    assert bindings[0]["type"] == "tag"
    assert bindings[0]["config"]["path"] == "[default]Line1/Count"


def test_embedded_view_refs(fake_install):
    """Embedded view components expose their target path."""
    view = LocalInstallReader(fake_install).read_perspective_view(
        "TestProject", "Dashboard/Overview"
    )
    assert view is not None
    assert _embedded_view_refs(view) == ["Equipment/Pump"]


def test_collect_event_scripts():
    """Event handler scripts are collected per component."""
    view = {
        "root": {
            "type": "ia.container.flex",
            "meta": {"name": "root"},
            "events": {"onClick": {"script": "system.perspective.print('hi')"}},
            "children": [],
        }
    }

    scripts = _collect_event_scripts(view)

    assert scripts[0]["component"] == "root"
    assert scripts[0]["event"] == "onClick"
    assert "print" in scripts[0]["script"]


def test_duplicate_component_names():
    """Duplicate component names are detected."""
    view = {
        "type": "ia.container.flex",
        "meta": {"name": "root"},
        "children": [
            {"type": "ia.display.label", "meta": {"name": "dup"}, "children": []},
            {"type": "ia.display.label", "meta": {"name": "dup"}, "children": []},
        ],
    }

    assert _duplicate_component_names(view) == ["dup"]


def test_local_write_roundtrip(tmp_path: Path):
    """A view written through the reader is valid JSON on disk."""
    reader = LocalInstallReader(tmp_path)
    view = {"root": {"type": "ia.container.flex", "meta": {"name": "root"}, "children": []}}

    path = reader.write_perspective_view("P", "A/B", view)

    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8")) == view


def test_collect_prop_config_bindings():
    """Bindings stored under Perspective propConfig are collected on view and components."""
    view = {
        "propConfig": {
            "custom.total": {
                "binding": {"type": "expr", "config": {"expression": "1 + 1"}},
            }
        },
        "root": {
            "type": "ia.container.flex",
            "meta": {"name": "root"},
            "children": [
                {
                    "type": "ia.display.label",
                    "meta": {"name": "Label"},
                    "propConfig": {
                        "position.display": {
                            "binding": {
                                "type": "expr",
                                "config": {"expression": "!{view.params.hide}"},
                            }
                        }
                    },
                    "children": [],
                }
            ],
        },
    }

    bindings = _collect_bindings(view)
    assert len(bindings) == 2
    assert bindings[0]["component"] == "view"
    assert bindings[0]["property"] == "custom.total"
    assert bindings[1]["component"] == "Label"
    assert bindings[1]["property"] == "position.display"
