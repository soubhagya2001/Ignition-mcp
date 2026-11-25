"""Tests for configuration resolution and AutoConnect helpers."""

from __future__ import annotations

import pytest

from ignition_mcp.autoconnect.engine import _project_names, credentials_summary
from ignition_mcp.autoconnect.local_reader import LocalInstallReader
from ignition_mcp.config import load_config


def test_config_load_defaults():
    """Config loads sane defaults when no env vars are set."""
    cfg = load_config()
    assert cfg.readonly is True
    assert cfg.timeout == 30.0
    assert cfg.transport == "stdio"
    assert cfg.api_prefix == "/data/api/v1"


def test_config_env_overrides(monkeypatch):
    """Environment variables override defaults and parse types."""
    monkeypatch.setenv("IGNITION_GATEWAY_URL", "http://test-gateway:8088")
    monkeypatch.setenv("IGNITION_READONLY", "false")
    monkeypatch.setenv("IGNITION_TIMEOUT", "12")
    monkeypatch.setenv("IGNITION_PORT", "9000")
    monkeypatch.setenv("IGNITION_MODULES", "gateway, tags")

    cfg = load_config()
    assert cfg.gateway_url == "http://test-gateway:8088"
    assert cfg.readonly is False
    assert cfg.timeout == 12.0
    assert cfg.port == 9000
    assert cfg.enabled_modules == ["gateway", "tags"]


def test_local_reader_finds_views(fake_install):
    """LocalInstallReader lists view paths using POSIX separators."""
    views = LocalInstallReader(fake_install).list_perspective_views("TestProject")
    assert "Dashboard/Overview" in views
    assert "Equipment/Pump" in views


async def test_local_reader_reads_gateway_port(fake_install):
    """gateway.xml port is honoured instead of assuming 8088."""
    info = await LocalInstallReader(fake_install).read()
    assert info.http_port == 9090
    assert info.gateway_url == "http://localhost:9090"


def test_project_names_from_payloads():
    """Project names are extracted from the shapes gateways return."""
    assert _project_names(["A", {"name": "B"}]) == ["A", "B"]
    assert _project_names({"projects": [{"projectName": "C"}]}) == ["C"]
    assert _project_names("nonsense") == []


def test_credentials_summary_never_leaks_values():
    """The summary reports auth methods, never secret values."""
    cfg = load_config()
    cfg.api_token = "secret-token"
    cfg.username = "admin"
    assert credentials_summary(cfg) == ("api_token", "basic")
    assert "secret-token" not in credentials_summary(cfg)


@pytest.mark.parametrize("value,expected", [("false", False), ("0", False), ("true", True)])
def test_readonly_env_parsing(monkeypatch, value, expected):
    """Readonly accepts common boolean spellings."""
    monkeypatch.setenv("IGNITION_READONLY", value)
    assert load_config().readonly is expected


def test_list_projects_ignores_dirs_without_project_json(fake_install):
    """Backup or scratch folders in data/projects without project.json are ignored."""
    backup_dir = fake_install / "data" / "projects" / "Propulse-Old"
    backup_dir.mkdir(parents=True)
    assert "Propulse-Old" not in LocalInstallReader(fake_install).list_projects()
    assert "TestProject" in LocalInstallReader(fake_install).list_projects()


def test_secret_file_env_vars(tmp_path, monkeypatch):
    """IGNITION_PASSWORD_FILE and IGNITION_API_TOKEN_FILE load Docker/mounted secrets."""
    pw_file = tmp_path / "pw"
    tok_file = tmp_path / "tok"
    pw_file.write_text("file-secret\n", encoding="utf-8")
    tok_file.write_text("token-from-file\n", encoding="utf-8")
    monkeypatch.delenv("IGNITION_PASSWORD", raising=False)
    monkeypatch.delenv("IGNITION_API_TOKEN", raising=False)
    monkeypatch.setenv("IGNITION_PASSWORD_FILE", str(pw_file))
    monkeypatch.setenv("IGNITION_API_TOKEN_FILE", str(tok_file))

    cfg = load_config()
    assert cfg.password == "file-secret"
    assert cfg.api_token == "token-from-file"
