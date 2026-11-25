"""Tests for the local install reader (direct, credential-free control)."""

from __future__ import annotations

import json

from ignition_mcp.autoconnect.local_reader import LocalInstallReader


async def test_read_install_metadata(fake_install):
    """Projects, modules and ports are discovered from disk."""
    reader = LocalInstallReader(fake_install)
    info = await reader.read()

    assert info.gateway_url == "http://localhost:9090"
    assert "TestProject" in info.projects
    assert info.install_path == str(fake_install)


def test_safe_path_rejects_traversal(fake_install):
    """Paths that escape the base directory are refused."""
    reader = LocalInstallReader(fake_install)
    assert reader.safe_path(reader.projects_dir, "..", "..", "secrets") is None
    assert reader.safe_path(reader.projects_dir, "TestProject") is not None


def test_project_path_rejects_traversal(fake_install):
    """Unknown or escaping project names resolve inside an invalid placeholder path."""
    reader = LocalInstallReader(fake_install)
    escaped = reader.project_path("..\\outside")
    assert escaped.is_relative_to(reader.projects_dir)
    assert escaped.name == "__invalid_project__"


def test_read_resource_refuses_traversal(fake_install):
    """Resource reads cannot escape the project directory."""
    reader = LocalInstallReader(fake_install)
    assert reader.read_resource("TestProject", "../../../gateway.xml") is None


def test_write_and_read_view(fake_install):
    """A view can be written and read back directly from disk."""
    reader = LocalInstallReader(fake_install)
    view = {"root": {"type": "ia.container.flex", "meta": {"name": "root"}, "children": []}}

    path = reader.write_perspective_view("TestProject", "Created/New", view)

    assert path is not None and path.exists()
    assert reader.read_perspective_view("TestProject", "Created/New") == view
    assert "Created/New" in reader.list_perspective_views("TestProject")


def test_scripts_are_listed_and_written(fake_install):
    """Project library scripts round-trip through code.py."""
    reader = LocalInstallReader(fake_install)

    assert "Util" in reader.list_scripts("TestProject")
    assert "def total" in (reader.read_script("TestProject", "Util") or "")

    reader.write_script("TestProject", "New.Module", "def hello():\n    return 1\n")
    assert reader.read_script("TestProject", "New.Module") == "def hello():\n    return 1\n"
    assert "New.Module" in reader.list_scripts("TestProject")


def test_named_queries_and_parameters(fake_install):
    """Named query resources are decoded from query.json."""
    reader = LocalInstallReader(fake_install)

    assert reader.list_named_queries("TestProject") == ["Production/GetCounts"]
    data = reader.read_named_query("TestProject", "Production/GetCounts")
    assert data is not None
    assert "production" in data["query"]
    assert data["parameters"][0]["name"] == "line"


def test_inheritance_chain(fake_install):
    """The parent chain comes from project.json."""
    reader = LocalInstallReader(fake_install)
    assert reader.inheritance_chain("TestProject") == ["TestProject", "Configuration"]
    assert reader.inheritance_chain("Configuration") == ["Configuration"]


def test_style_classes_and_page_config(fake_install):
    """Style classes and page configuration are readable."""
    reader = LocalInstallReader(fake_install)

    assert reader.list_style_classes("TestProject") == ["Title"]
    assert "Title" in reader.read_style_classes("TestProject")
    assert reader.read_page_config("TestProject") is not None


def test_search_text_and_logs(fake_install):
    """Text search and log tailing work without a gateway."""
    reader = LocalInstallReader(fake_install)

    matches = reader.search_text("TestProject", "[default]Line1/Count")
    assert matches and matches[0]["resource"].endswith("view.json")

    lines = reader.read_log_lines()
    assert any("boom" in line for line in lines)


def test_write_resource_json_and_text(fake_install):
    """Generic resource writes handle JSON and plain text."""
    reader = LocalInstallReader(fake_install)

    reader.write_resource("TestProject", "notes/readme.txt", "hello")
    assert reader.read_resource("TestProject", "notes/readme.txt") == "hello"

    reader.write_resource("TestProject", "meta/custom.json", {"a": 1})
    assert reader.read_resource("TestProject", "meta/custom.json") == {"a": 1}


def test_missing_project_returns_empty(fake_install):
    """Unknown projects yield empty results rather than exceptions."""
    reader = LocalInstallReader(fake_install)
    assert reader.list_perspective_views("Missing") == []
    assert reader.read_perspective_view("Missing", "A/B") is None
    assert json.loads(json.dumps(reader.read_project_meta("Missing"))) == {}
