"""Tests for gateway trial status and expiry-gated reset."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from ignition_mcp.client.gateway_api import GatewayClient
from ignition_mcp.config import IgnitionMCPConfig
from ignition_mcp.modules import gateway
from ignition_mcp.state import IgnitionSession, ServerState

TRIAL_URL = "http://gw/data/api/v1/trial"

EXPIRED = {
    "licenseMode": "Trial",
    "trialState": "AllInDemo",
    "trialSecondsLeft": 0,
    "expired": True,
    "emergency": False,
    "emergencySecondsLeft": 0,
    "development": False,
    "developmentSecondsLeft": 0,
}
ACTIVE = {**EXPIRED, "trialSecondsLeft": 7200, "expired": False}


class RecordingMCP:
    """Minimal MCP stand-in that records registered tools."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        """Return a decorator that records the tool function."""

        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


def _build(readonly: bool = False) -> tuple[ServerState, dict[str, Any]]:
    state = ServerState(config=IgnitionMCPConfig(readonly=readonly))
    state.session = IgnitionSession(gateway_url="http://gw", is_connected=True, readonly=readonly)
    state.gateway = GatewayClient("http://gw", timeout=2)
    mcp = RecordingMCP()
    gateway.register(mcp, state)
    return state, mcp.tools


@respx.mock
async def test_trial_status_reports_expired() -> None:
    respx.get(TRIAL_URL).mock(return_value=httpx.Response(200, json=EXPIRED))
    _, tools = _build()

    response = await tools["gateway_trial_status"]()
    payload = response["result"]
    assert payload["trial_status"] == "expired"
    assert payload["expired"] is True
    assert payload["seconds_remaining"] == 0
    assert payload["license_mode"] == "Trial"


@respx.mock
async def test_reset_refuses_before_expiry() -> None:
    respx.get(TRIAL_URL).mock(return_value=httpx.Response(200, json=ACTIVE))
    post_route = respx.post(TRIAL_URL).mock(return_value=httpx.Response(200, json={}))
    _, tools = _build()

    response = await tools["gateway_reset_trial"](confirm=True)
    assert response["result"]["status"] == "not_expired"
    assert not post_route.called


@respx.mock
async def test_reset_dry_run_does_not_post() -> None:
    respx.get(TRIAL_URL).mock(return_value=httpx.Response(200, json=EXPIRED))
    post_route = respx.post(TRIAL_URL).mock(return_value=httpx.Response(200, json={}))
    _, tools = _build()

    response = await tools["gateway_reset_trial"](dry_run=True)
    assert response["result"]["status"] == "dry_run"
    assert response["result"]["reset_allowed"] is True
    assert not post_route.called


@respx.mock
async def test_reset_posts_and_verifies() -> None:
    respx.get(TRIAL_URL).mock(
        side_effect=[
            httpx.Response(200, json=EXPIRED),
            httpx.Response(200, json=ACTIVE),
        ]
    )
    post_route = respx.post(TRIAL_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    _, tools = _build()

    response = await tools["gateway_reset_trial"](confirm=True)
    payload = response["result"]
    assert payload["status"] == "reset"
    assert payload["verified"] is True
    assert payload["after"]["seconds_remaining"] == 7200
    assert post_route.called


@respx.mock
async def test_reset_requires_admin_session() -> None:
    respx.get(TRIAL_URL).mock(return_value=httpx.Response(200, json=EXPIRED))
    respx.post(TRIAL_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))
    _, tools = _build()

    response = await tools["gateway_reset_trial"](confirm=True)
    payload = response["result"]
    assert payload["status"] == "authentication_required"
    assert "IGNITION_USERNAME" in payload["hint"]


@respx.mock
async def test_trial_status_without_gateway_is_explicit() -> None:
    state = ServerState(config=IgnitionMCPConfig())
    mcp = RecordingMCP()
    gateway.register(mcp, state)

    response = await mcp.tools["gateway_trial_status"]()
    assert response["result"]["status"] == "unavailable"
    assert "runtime-only" in response["result"]["message"]


@respx.mock
async def test_reset_without_gateway_is_not_connected() -> None:
    state = ServerState(config=IgnitionMCPConfig(readonly=False))
    state.session = IgnitionSession(readonly=False)
    mcp = RecordingMCP()
    gateway.register(mcp, state)

    response = await mcp.tools["gateway_reset_trial"](confirm=True)
    assert response["result"]["status"] == "not_connected"


@respx.mock
async def test_reset_requires_confirmation() -> None:
    _, tools = _build()
    response = await tools["gateway_reset_trial"]()
    assert response["result"]["status"] == "requires_confirmation"


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_reset_auth_errors_are_reported(status: int) -> None:
    respx.get(TRIAL_URL).mock(return_value=httpx.Response(200, json=EXPIRED))
    respx.post(TRIAL_URL).mock(return_value=httpx.Response(status, text="Denied"))
    _, tools = _build()

    response = await tools["gateway_reset_trial"](confirm=True)
    assert response["result"]["status"] == "authentication_required"
    assert response["result"]["http_status"] == status
