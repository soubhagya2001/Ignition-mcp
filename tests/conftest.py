"""Shared pytest fixtures.

Every fixture is offline: fake Ignition installs are built in ``tmp_path`` and no test talks
to a real gateway.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ignition_mcp.config import IgnitionMCPConfig
from ignition_mcp.state import IgnitionSession, ServerState


@pytest.fixture(autouse=True)
def _isolated_snapshot_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep write snapshots inside the test's temporary directory."""
    monkeypatch.setenv("IGNITION_MCP_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


@pytest.fixture
def cfg() -> IgnitionMCPConfig:
    """Config with writes enabled for guard tests."""
    return IgnitionMCPConfig(readonly=False)


@pytest.fixture
def state(cfg: IgnitionMCPConfig) -> ServerState:
    """Connected state without a gateway client (local-only)."""
    server_state = ServerState(config=cfg)
    server_state.session = IgnitionSession(
        gateway_url="http://localhost:8088",
        is_connected=True,
        readonly=False,
        active_project="TestProject",
        projects=["TestProject", "Configuration"],
    )
    return server_state


@pytest.fixture
def local_state(cfg: IgnitionMCPConfig, fake_install: Path) -> ServerState:
    """State with local filesystem access to a fake install."""
    server_state = ServerState(config=cfg)
    server_state.session = IgnitionSession(
        gateway_url="http://localhost:9090",
        is_connected=False,
        readonly=False,
        active_project="TestProject",
        projects=["TestProject", "Configuration"],
        local_read_available=True,
        local_install_path=str(fake_install),
    )
    return server_state


@pytest.fixture
def fake_install(tmp_path: Path) -> Path:
    """Create a minimal but realistic Ignition install directory."""
    root = tmp_path / "Ignition"
    data = root / "data"
    project = data / "projects" / "TestProject"
    perspective = project / "com.inductiveautomation.perspective"

    overview = perspective / "views" / "Dashboard" / "Overview"
    overview.mkdir(parents=True)
    view = {
        "custom": {},
        "params": {},
        "props": {},
        "root": {
            "type": "ia.container.flex",
            "meta": {"name": "root"},
            "children": [
                {
                    "type": "ia.display.label",
                    "meta": {"name": "title"},
                    "props": {
                        "text": {
                            "binding": {
                                "type": "tag",
                                "config": {"path": "[default]Line1/Count"},
                            }
                        },
                        "style": {"classes": ["Title"]},
                    },
                    "children": [],
                },
                {
                    "type": "ia.display.view",
                    "meta": {"name": "embed"},
                    "props": {"path": "Equipment/Pump"},
                    "children": [],
                },
            ],
        },
    }
    (overview / "view.json").write_text(json.dumps(view), encoding="utf-8")

    pump = perspective / "views" / "Equipment" / "Pump"
    pump.mkdir(parents=True)
    (pump / "view.json").write_text(
        json.dumps(
            {"root": {"type": "ia.container.flex", "meta": {"name": "root"}, "children": []}}
        ),
        encoding="utf-8",
    )

    page_config = perspective / "page-config"
    page_config.mkdir(parents=True)
    (page_config / "config.json").write_text(
        json.dumps({"pages": [{"path": "/", "view": "Dashboard/Overview"}]}), encoding="utf-8"
    )

    style = perspective / "style-classes" / "Title"
    style.mkdir(parents=True)
    (style / "style-class.json").write_text(
        json.dumps({"name": "Title", "styles": {"color": "#ffffff"}}), encoding="utf-8"
    )

    script = project / "ignition" / "script-python" / "Util"
    script.mkdir(parents=True)
    (script / "code.py").write_text(
        "import system\n\n\ndef total(a, b):\n    return a + b\n", encoding="utf-8"
    )

    query = (
        project / "com.inductiveautomation.sqltables" / "named-query" / "Production" / "GetCounts"
    )
    query.mkdir(parents=True)
    (query / "query.json").write_text(
        json.dumps(
            {
                "query": "SELECT count FROM production WHERE line = :line",
                "parameters": [{"name": "line", "type": "String"}],
                "queryType": "query",
            }
        ),
        encoding="utf-8",
    )

    (project / "project.json").write_text(
        json.dumps({"enabled": True, "parent": "Configuration"}), encoding="utf-8"
    )
    parent = data / "projects" / "Configuration"
    parent.mkdir(parents=True)
    (parent / "project.json").write_text(
        json.dumps({"enabled": True, "parent": None}), encoding="utf-8"
    )

    (data / "gateway.xml").write_text(
        '<gateway><entry key="gateway.port" value="9090"/></gateway>', encoding="utf-8"
    )

    logs = root / "logs"
    logs.mkdir()
    (logs / "wrapper.log").write_text(
        "I [t] startup\nW [t] slow query\nE [t] boom\n", encoding="utf-8"
    )
    return root
