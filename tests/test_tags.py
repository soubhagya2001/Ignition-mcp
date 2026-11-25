"""Tests for the Tags helpers and write safety."""

from __future__ import annotations

from ignition_mcp.modules.tags import _flatten_browse, _is_udt_type, _normalise_reads


def test_write_guard_blocked_in_readonly(cfg):
    """write_tag is blocked in read-only mode."""
    from ignition_mcp.state import IgnitionSession, ServerState

    state = ServerState(config=cfg)
    state.session = IgnitionSession(is_connected=True, readonly=True)

    guard = state.write_guard("write_tag", confirm=False, dry_run=False)

    assert guard is not None
    assert guard["result"]["status"] == "blocked"


def test_write_guard_requires_confirm(state):
    """A write without confirm returns requires_confirmation."""
    guard = state.write_guard("write_tag", confirm=False, dry_run=False)

    assert guard is not None
    assert guard["result"]["status"] == "requires_confirmation"


def test_write_guard_proceeds_with_confirm(state):
    """A confirmed write proceeds."""
    assert state.write_guard("write_tag", confirm=True, dry_run=False) is None


def test_flatten_browse_nests_children():
    """Nested browse results are flattened with full paths."""
    payload = [
        {
            "name": "Equipment",
            "children": [
                {"name": "Pump1", "children": [{"name": "Running", "tagType": "AtomicTag"}]}
            ],
        }
    ]

    nodes = _flatten_browse(payload, "[default]")

    paths = [node["path"] for node in nodes]
    assert "[default]/Equipment" in paths
    assert "[default]/Equipment/Pump1/Running" in paths


def test_normalise_reads():
    """Tag reads are normalised to path/value/quality entries."""
    payload = {"reads": [{"path": "[default]A", "value": 1, "quality": "Good"}]}

    reads = _normalise_reads(payload, ["[default]A"])

    assert reads == [{"path": "[default]A", "value": 1, "quality": "Good", "timestamp": None}]


def test_normalise_reads_aligns_requested_paths():
    """Entries without a path fall back to the requested path order."""
    reads = _normalise_reads({"reads": [{"value": 5}]}, ["[default]B"])
    assert reads[0]["path"] == "[default]B"


def test_is_udt_type():
    """UDT definitions are recognised by tag type or members."""
    assert _is_udt_type({"tagType": "UdtType"}) is True
    assert _is_udt_type({"members": [{"name": "a"}]}) is True
    assert _is_udt_type({"tagType": "AtomicTag"}) is False
