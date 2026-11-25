"""Tests for the gateway HTTP client (offline, via respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ignition_mcp.client.gateway_api import GatewayClient, GatewayError


@respx.mock
async def test_api_token_header_is_sent():
    """The API token is optional but sent when configured."""
    route = respx.get("http://gw:8088/data/status").mock(
        return_value=httpx.Response(200, json={"state": "RUNNING"})
    )

    async with GatewayClient("http://gw:8088", api_token="secret") as client:
        payload = await client.get_json("/data/status")

    assert payload == {"state": "RUNNING"}
    assert route.calls.last.request.headers["X-Ignition-API-Token"] == "secret"


@respx.mock
async def test_session_login_sets_cookie():
    """Username/password logs in to the Gateway web session without any API token."""
    respx.post("http://gw:8088/main/webapp/j_security_check").mock(
        return_value=httpx.Response(200, headers={"set-cookie": "JSESSIONID=abc123; Path=/"})
    )

    async with GatewayClient("http://gw:8088", username="admin", password="pw") as client:
        method = await client.authenticate()

    assert method == "session_login"
    assert client.auth_verified is True
    assert client.api_token is None


@respx.mock
async def test_first_available_skips_404():
    """Endpoint candidates let the client adapt to gateway versions."""
    respx.get("http://gw:8088/data/api/v1/tags/read").mock(return_value=httpx.Response(404))
    respx.get("http://gw:8088/data/tag/read").mock(
        return_value=httpx.Response(200, json={"reads": []})
    )

    async with GatewayClient("http://gw:8088") as client:
        path, payload = await client.first_available(("/tags/read", "/data/tag/read"))

    assert path == "/data/tag/read"
    assert payload == {"reads": []}


@respx.mock
async def test_401_payload_points_at_session_login():
    """Auth failures explain direct-control options instead of demanding a token."""
    respx.get("http://gw:8088/data/api/v1/tags/read").mock(return_value=httpx.Response(401))

    async with GatewayClient("http://gw:8088") as client:
        with pytest.raises(GatewayError) as excinfo:
            await client.get_json("/tags/read")

    payload = excinfo.value.to_payload()
    assert payload["status"] == "gateway_error"
    assert payload["http_status"] == 401
    assert "IGNITION_USERNAME" in payload["hint"]


@respx.mock
async def test_timeout_becomes_gateway_error():
    """Transport timeouts are normalised, not leaked as httpx exceptions."""
    respx.get("http://gw:8088/data/status").mock(side_effect=httpx.ConnectTimeout("slow"))

    async with GatewayClient("http://gw:8088", timeout=1) as client:
        with pytest.raises(GatewayError) as excinfo:
            await client.get_json("/data/status")

    assert "Timed out" in excinfo.value.message
    assert excinfo.value.to_payload()["status"] == "gateway_error"
