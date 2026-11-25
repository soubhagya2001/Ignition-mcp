"""Tests for literal and regex project text search."""

from __future__ import annotations

from pathlib import Path

from ignition_mcp.autoconnect.local_reader import LocalInstallReader


def test_regex_search_finds_script(fake_install: Path) -> None:
    reader = LocalInstallReader(fake_install)
    matches = reader.search_text("TestProject", r"def\s+total", regex=True)
    assert matches
    assert matches[0]["resource"].endswith("Util/code.py")
    assert matches[0]["line"] == 4


def test_regex_search_is_case_insensitive_by_default(fake_install: Path) -> None:
    reader = LocalInstallReader(fake_install)
    assert reader.search_text("TestProject", r"DEF\s+TOTAL", regex=True)


def test_regex_search_can_be_case_sensitive(fake_install: Path) -> None:
    reader = LocalInstallReader(fake_install)
    assert reader.search_text("TestProject", r"DEF\s+TOTAL", regex=True, case_sensitive=True) == []


def test_literal_search_escapes_regex_characters(fake_install: Path) -> None:
    reader = LocalInstallReader(fake_install)
    assert reader.search_text("TestProject", "def total(", regex=False)
    assert reader.search_text("TestProject", "def total(", regex=True) == []
