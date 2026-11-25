"""Gateway HTTP client for the Ignition REST API.

Authentication is **session-first** so no API token is required:

1. If a username/password is configured, the client logs in to the Gateway web session
   (Spring Security form login) and reuses the resulting ``JSESSIONID`` cookie.
2. HTTP Basic auth is sent alongside, because several gateway endpoints accept it.
3. ``X-Ignition-API-Token`` is used only when an API key is explicitly configured.

Most Ignition installations are also controlled *directly* through the local filesystem
(see :mod:`ignition_mcp.autoconnect.local_reader`), which needs no authentication at all.

The live schema for a specific gateway is always authoritative: ``GET /openapi.json`` on the
gateway. Modules therefore try a short list of candidate paths and treat a 404 as "not on this
version" rather than a hard failure.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio
import httpx

logger = logging.getLogger(__name__)

# Paths that must not be prefixed with the API namespace.
_ABSOLUTE_PREFIXES = ("/data/", "/system/", "/openapi", "/main/")

# Gateway web-session login endpoints, newest first. Ignition's Gateway UI uses Spring
# Security form login; the 8.x path is /main/webapp/j_security_check.
LOGIN_PATHS = (
    "/main/webapp/j_security_check",
    "/data/security/login",
    "/system/login",
)


class GatewayError(Exception):
    """Structured error raised for gateway communication failures.

    Args:
        message: Human readable description.
        status: HTTP status code when the failure came from a response.
        path: Request path that failed.
        payload: Optional response body (already decoded when JSON).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        path: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.path = path
        self.payload = payload

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serialisable description of the error for a tool response."""
        payload: dict[str, Any] = {"status": "gateway_error", "message": self.message}
        if self.status is not None:
            payload["http_status"] = self.status
        if self.path:
            payload["path"] = self.path
        if self.payload is not None:
            payload["detail"] = self.payload
        if self.status in (401, 403):
            payload["hint"] = (
                "Set IGNITION_USERNAME/IGNITION_PASSWORD for Gateway session login, or point "
                "IGNITION_INSTALL_PATH at a local Ignition install for direct filesystem "
                "control. An API key (IGNITION_API_TOKEN) is optional."
            )
        elif self.status == 404:
            payload["hint"] = (
                "Endpoint not available on this gateway version. Check /openapi.json on the "
                "gateway, or set IGNITION_API_PREFIX to the correct namespace."
            )
        return payload


class GatewayClient:
    """Async client for a single Ignition gateway.

    Args:
        base_url: Gateway base URL, e.g. ``http://localhost:8088``.
        api_prefix: REST namespace prefix, default ``/data/api/v1``.
        api_token: Ignition API key, sent as ``X-Ignition-API-Token``.
        username: Username for HTTP Basic auth.
        password: Password for HTTP Basic auth.
        timeout: Request timeout in seconds.
        verify: Verify TLS certificates.

    Example:
        >>> async with GatewayClient("http://localhost:8088", api_token="...") as gw:
        ...     status = await gw.get_json("/gateway/status")
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_prefix: str = "/data/api/v1",
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        verify: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_prefix = "/" + api_prefix.strip("/") if api_prefix else ""
        self.api_token = api_token
        self.username = username
        self.password = password
        self.timeout = timeout
        self.auth_method = "api_token" if api_token else ("basic" if username else "none")
        self.auth_verified = bool(api_token)
        self._openapi_cache: dict[str, Any] | None = None
        auth = httpx.BasicAuth(username, password) if username and password else None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth,
            timeout=httpx.Timeout(timeout),
            verify=verify,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "ignition-mcp"},
        )

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> GatewayClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # -- authentication ----------------------------------------------------

    async def authenticate(self) -> str:
        """Establish the strongest available authentication without an API token.

        A Gateway web-session login is attempted first (cookie auth works for the Gateway
        web UI and for session-protected ``/data`` endpoints). If that fails, the client
        keeps HTTP Basic auth configured and reports it as unverified.

        Returns:
            ``"session_login"``, ``"basic"``, ``"api_token"`` or ``"none"``.
        """
        if self.api_token:
            return "api_token"
        if not (self.username and self.password):
            return "none"

        if await self.login_session():
            self.auth_method = "session_login"
            self.auth_verified = True
            return self.auth_method

        # Basic auth cannot be confirmed without a protected probe endpoint; keep it and
        # let the first real request decide.
        self.auth_method = "basic"
        self.auth_verified = False
        return self.auth_method

    async def login_session(self) -> bool:
        """Log in to the Gateway web session and keep the resulting cookies.

        Ignition's Gateway UI uses Spring Security form login. The exact path has changed
        between releases, so the documented variants are tried in order.

        Returns:
            ``True`` when a session cookie was issued.
        """
        form = {
            "j_username": self.username or "",
            "j_password": self.password or "",
            "j_rememberMe": "false",
        }
        for path in LOGIN_PATHS:
            try:
                response = await self._client.post(
                    path,
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                logger.debug("Session login failed at %s: %s", path, exc)
                continue
            if response.status_code < 400 and self._has_session_cookie():
                logger.info("Authenticated with the Gateway web session")
                return True
        logger.debug("No Gateway session cookie obtained for %s", self.base_url)
        return False

    def _has_session_cookie(self) -> bool:
        """Return whether the cookie jar holds a gateway session cookie."""
        for cookie in self._client.cookies.jar:
            name = cookie.name.upper()
            if "JSESSIONID" in name or "IGNITION" in name or "SESSION" in name:
                return True
        return False

    # -- request plumbing --------------------------------------------------

    def _resolve_path(self, path: str) -> str:
        """Prefix API-relative paths; leave gateway-absolute paths untouched."""
        if path.startswith(_ABSOLUTE_PREFIXES):
            return path
        return f"{self.api_prefix}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        """Build per-request headers, including the API token when configured."""
        headers: dict[str, str] = {}
        if self.api_token:
            headers["X-Ignition-API-Token"] = self.api_token
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        content: Any = None,
    ) -> httpx.Response:
        """Perform an HTTP request and raise :class:`GatewayError` on failure.

        Args:
            method: HTTP verb.
            path: API-relative path (``/tags/read``) or absolute gateway path.
            params: Optional query parameters.
            json_body: Optional JSON body.
            data: Optional form/multipart body.
            content: Optional raw body (used for uploads).

        Returns:
            The successful response.

        Raises:
            GatewayError: On transport errors or non-2xx responses.
        """
        url_path = self._resolve_path(path)
        try:
            response = await self._client.request(
                method,
                url_path,
                params=params,
                json=json_body,
                data=data,
                content=content,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise GatewayError(
                f"Timed out after {self.timeout:g}s calling {url_path}", path=url_path
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(f"Connection failed: {exc}", path=url_path) from exc

        if response.status_code >= 400:
            raise GatewayError(
                f"HTTP {response.status_code} from {url_path}",
                status=response.status_code,
                path=url_path,
                payload=_decode_body(response),
            )
        return response

    async def download(
        self,
        path: str,
        destination: Path,
        *,
        params: Mapping[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> int:
        """Stream a binary response to ``destination``.

        Args:
            path: API-relative or absolute path.
            destination: File to write. The parent directory is created when missing.
            params: Optional query parameters.
            request_timeout: Per-request timeout; ``None`` uses the client default.

        Returns:
            Number of bytes written.

        Raises:
            GatewayError: On transport errors or non-2xx responses.
        """
        url_path = self._resolve_path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".part")
        headers = self._headers()
        headers["Accept"] = "*/*"
        total = 0
        try:
            async with self._client.stream(
                "GET",
                url_path,
                params=params,
                headers=headers,
                timeout=request_timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise GatewayError(
                        f"HTTP {response.status_code} from {url_path}",
                        status=response.status_code,
                        path=url_path,
                        payload=_decode_body(response),
                    )
                async with await anyio.open_file(temp, "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        await handle.write(chunk)
                        total += len(chunk)
            temp.replace(destination)
            return total
        except httpx.TimeoutException as exc:
            temp.unlink(missing_ok=True)
            raise GatewayError(
                f"Timed out after {request_timeout or self.timeout:g}s calling {url_path}",
                path=url_path,
            ) from exc
        except httpx.HTTPError as exc:
            temp.unlink(missing_ok=True)
            raise GatewayError(f"Connection failed: {exc}", path=url_path) from exc
        except Exception:
            temp.unlink(missing_ok=True)
            raise

    async def json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform a request and decode the JSON response body.

        Args:
            method: HTTP verb.
            path: API-relative or absolute path.
            **kwargs: Forwarded to :meth:`request`.

        Returns:
            Decoded JSON, or an empty dict for an empty body.
        """
        response = await self.request(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayError(
                "Response was not valid JSON",
                status=response.status_code,
                path=str(response.request.url),
                payload=response.text[:500],
            ) from exc

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        """GET a path and return decoded JSON."""
        return await self.json("GET", path, **kwargs)

    async def post_json(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        """POST a JSON body and return decoded JSON."""
        return await self.json("POST", path, json_body=body, **kwargs)

    async def put_json(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        """PUT a JSON body and return decoded JSON."""
        return await self.json("PUT", path, json_body=body, **kwargs)

    async def delete_json(self, path: str, **kwargs: Any) -> Any:
        """DELETE a path and return decoded JSON."""
        return await self.json("DELETE", path, **kwargs)

    async def openapi(self) -> dict[str, Any]:
        """Return a cached summary of the gateway's OpenAPI document.

        Returns:
            ``{available, paths, path_count, version}``; unavailable gateways return
            ``available: False`` plus an error payload instead of raising.
        """
        if self._openapi_cache is not None:
            return self._openapi_cache
        try:
            payload = await self.get_json("/openapi.json")
        except GatewayError as exc:
            self._openapi_cache = {
                "available": False,
                "paths": [],
                "path_count": 0,
                "version": None,
                "error": exc.to_payload(),
            }
            return self._openapi_cache

        paths = (
            sorted(str(path) for path in payload.get("paths", {}))
            if isinstance(payload, dict)
            else []
        )
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        self._openapi_cache = {
            "available": bool(paths),
            "paths": paths,
            "path_count": len(paths),
            "version": info.get("version") if isinstance(info, dict) else None,
        }
        return self._openapi_cache

    async def first_available(
        self,
        candidates: Sequence[str],
        *,
        method: str = "GET",
        **kwargs: Any,
    ) -> tuple[str, Any]:
        """Try candidate paths in order and return the first non-404 response.

        Ignition's REST surface moved between 8.0, 8.1 and later releases, so tools describe
        alternatives and let the gateway pick the one it supports.

        Args:
            candidates: Ordered path candidates.
            method: HTTP verb used for every candidate.
            **kwargs: Forwarded to :meth:`json`.

        Returns:
            A ``(path, payload)`` tuple for the first candidate that responds.

        Raises:
            GatewayError: If every candidate returns 404, or one fails for another reason.
        """
        not_found: list[GatewayError] = []
        for path in candidates:
            try:
                return path, await self.json(method, path, **kwargs)
            except GatewayError as exc:
                if exc.status == 404:
                    not_found.append(exc)
                    continue
                raise
        detail = ", ".join(error.path or "?" for error in not_found)
        raise GatewayError(
            f"None of the endpoint candidates responded: {detail}",
            status=404,
            path=not_found[-1].path if not_found else None,
            payload={"tried": list(candidates)},
        )


def normalize_error(exc: Exception) -> dict[str, Any]:
    """Convert any exception into a structured error dict for tool responses.

    Args:
        exc: The exception to normalise.

    Returns:
        A JSON-serialisable error payload.
    """
    if isinstance(exc, GatewayError):
        return exc.to_payload()
    if isinstance(exc, httpx.HTTPStatusError):
        return {
            "status": "gateway_error",
            "error": f"HTTP {exc.response.status_code}",
            "url": str(exc.request.url),
            "message": exc.response.text[:500],
        }
    if isinstance(exc, httpx.ConnectError):
        return {"status": "gateway_error", "message": f"connection_failed: {exc}"}
    if isinstance(exc, httpx.TimeoutException):
        return {"status": "gateway_error", "message": f"timeout: {exc}"}
    return {"status": "error", "error": type(exc).__name__, "message": str(exc)}


def _decode_body(response: httpx.Response) -> Any:
    """Best-effort decode of an error body for diagnostics."""
    try:
        return response.json()
    except ValueError:
        return response.text[:1000] if response.text else None


def summarize_payload(payload: Any, limit: int = 5) -> Any:
    """Return a small preview of a payload, useful in dry-run responses.

    Args:
        payload: Arbitrary decoded JSON.
        limit: Maximum number of list items to keep.

    Returns:
        A truncated copy of ``payload`` when it is a list, otherwise the payload itself.
    """
    if isinstance(payload, list):
        return payload[:limit]
    if isinstance(payload, dict):
        return {key: payload[key] for key in list(payload)[:limit]}
    return payload


__all__ = [
    "GatewayClient",
    "GatewayError",
    "Iterable",
    "normalize_error",
    "summarize_payload",
]
