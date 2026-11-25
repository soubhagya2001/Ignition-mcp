"""Tests for the ``--doctor`` diagnostic report."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ignition_mcp import doctor
from ignition_mcp.config import IgnitionMCPConfig


@pytest.fixture
def report(monkeypatch: pytest.MonkeyPatch, fake_install: Path):
    """Build a doctor report against a fake local install."""

    class FakeEngine:
        def __init__(self, cfg: IgnitionMCPConfig) -> None:
            self.cfg = cfg

        async def connect(self, state) -> None:
            state.session.local_read_available = True
            state.session.local_install_path = str(fake_install)
            state.session.discovery_method = "local_install_only"
            state.session.auth_method = "none (local filesystem)"

    monkeypatch.setattr(doctor, "AutoConnectEngine", FakeEngine)
    return asyncio.run(doctor.build_report(IgnitionMCPConfig()))


def test_doctor_reports_local_install(report, fake_install: Path) -> None:
    assert report["status"] == "ok"
    assert report["install"]["path"] == str(fake_install)
    assert report["checks"][0]["name"] == "local_install"
    assert report["checks"][0]["status"] == "pass"
    assert report["capabilities"]["gateway_connected"] is False


def test_doctor_redacts_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoopEngine:
        def __init__(self, cfg: IgnitionMCPConfig) -> None:
            self.cfg = cfg

        async def connect(self, state) -> None:
            return None

    monkeypatch.setattr(doctor, "AutoConnectEngine", NoopEngine)
    cfg = IgnitionMCPConfig(username="operator", password="secret", api_token="token")
    result = asyncio.run(doctor.build_report(cfg))

    assert "password" not in result["config"]
    assert "api_token" not in result["config"]
    assert "username" not in result["config"]
