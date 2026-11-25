"""Tests for shared state, write safety and the audit trail."""

from __future__ import annotations

from ignition_mcp.state import MAX_AUDIT_ENTRIES, ServerState


def test_write_guard_blocked_in_readonly(state: ServerState):
    """Read-only mode blocks every write before confirmation is considered."""
    state.session.readonly = True

    guard = state.write_guard("write_tag", confirm=True, dry_run=False)

    assert guard is not None
    assert guard["result"]["status"] == "blocked"
    assert guard["result"]["reason"] == "readonly_mode"


def test_write_guard_requires_confirmation(state: ServerState):
    """A write without confirm=True returns a preview instruction."""
    guard = state.write_guard("write_tag", confirm=False, dry_run=False)

    assert guard is not None
    assert guard["result"]["status"] == "requires_confirmation"


def test_write_guard_allows_confirmed_write(state: ServerState):
    """A confirmed write in write mode proceeds."""
    assert state.write_guard("write_tag", confirm=True, dry_run=False) is None


def test_write_guard_allows_dry_run_in_readonly(state: ServerState):
    """Dry runs are allowed even in read-only mode."""
    state.session.readonly = True
    assert state.write_guard("write_tag", confirm=False, dry_run=True) is None


def test_require_connection_without_client(state: ServerState):
    """Tools that need HTTP get a structured not_connected payload."""
    payload = state.require_connection()

    assert payload is not None
    assert payload["result"]["status"] == "not_connected"
    assert payload["_ctx"]["local_read_available"] is False


def test_require_connection_with_client(state: ServerState):
    """A configured client satisfies require_connection."""
    state.gateway = object()  # type: ignore[assignment]
    assert state.require_connection() is None


def test_audit_log_is_bounded(cfg):
    """The audit trail keeps the most recent entries only."""
    server_state = ServerState(config=cfg)
    for index in range(MAX_AUDIT_ENTRIES + 25):
        server_state.record_action("tool", "ok", index=index)

    assert len(server_state.audit) == MAX_AUDIT_ENTRIES
    entries = server_state.audit_payload(5)
    assert len(entries) == 5
    assert entries[-1]["detail"]["index"] == MAX_AUDIT_ENTRIES + 24


def test_tool_response_includes_context(state: ServerState):
    """Every response carries the _ctx block."""
    response = state.tool_response({"ok": True})

    assert response["result"] == {"ok": True}
    assert response["_ctx"]["active_project"] == "TestProject"
    assert "readonly_mode" in response["_ctx"]
