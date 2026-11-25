"""Tests for OEE mathematics."""

from __future__ import annotations

import pytest

from ignition_mcp.modules.history import _oee_metrics, _ratio


def test_oee_metrics_full():
    """OEE is availability × performance × quality."""
    metrics = _oee_metrics(
        planned_time_minutes=480,
        run_time_minutes=400,
        total_count=1000,
        good_count=950,
        ideal_cycle_seconds=20,
    )

    assert metrics["availability"] == pytest.approx(400 / 480)
    assert metrics["performance"] == pytest.approx(20 * 1000 / (400 * 60))
    assert metrics["quality"] == pytest.approx(0.95)
    expected = (400 / 480) * (20 * 1000 / (400 * 60)) * 0.95
    assert metrics["oee"] == pytest.approx(expected)


def test_oee_metrics_missing_inputs():
    """Missing inputs produce None rather than invented numbers."""
    metrics = _oee_metrics(None, 400, 1000, 950, 20)

    assert metrics["availability"] is None
    assert metrics["performance"] is not None
    assert metrics["oee"] is None


def test_ratio_handles_zero_denominator():
    """Division by zero returns None."""
    assert _ratio(10, 0) is None
    assert _ratio(None, 10) is None
    assert _ratio(10, 40) == 0.25
