"""AutoConnect engine — zero-configuration gateway discovery.

The engine walks a priority chain and stops at the first usable result:

1. Explicit URL (``IGNITION_GATEWAY_URL`` or config file)
2. Local Ignition install (ports from ``gateway.xml``, projects from disk)
3. OS credential store (URL and/or Basic-auth credentials)
4. mDNS service discovery (``_ignition._tcp.local``)
5. Port scan on loopback for common Ignition ports

Local filesystem access is enabled independently of HTTP: a machine with Ignition installed
gets read-only project tools even when the gateway is unreachable.

Authentication note: the engine prefers **direct control** — local filesystem access needs no
authentication at all, and remote gateways are reached with a Gateway web-session login
(username/password). An API key is optional and never required. The engine never sprays
factory-default passwords at a gateway.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import anyio

from ignition_mcp.autoconnect.local_reader import LocalInstallInfo, LocalInstallReader
from ignition_mcp.client.gateway_api import GatewayClient, GatewayError
from ignition_mcp.config import IgnitionMCPConfig
from ignition_mcp.state import IgnitionSession, ServerState

logger = logging.getLogger(__name__)

IGNITION_PORTS = [8088, 8043, 8060, 8090]
LOOPBACK_PORTS = [8088, 8043]

WINDOWS_INSTALL_ROOTS = [
    r"C:\Program Files\Inductive Automation\Ignition",
    r"C:\Program Files (x86)\Inductive Automation\Ignition",
    r"D:\Inductive Automation\Ignition",
    r"D:\Ignition",
]
LINUX_INSTALL_ROOTS = [
    "/usr/local/bin/ignition",
    "/opt/ignition",
    "/ignition",
]


class AutoConnectEngine:
    """Discover and connect to an Ignition gateway without user input.

    Args:
        config: Resolved server configuration.
    """

    def __init__(self, config: IgnitionMCPConfig) -> None:
        self.config = config

    async def connect(self, state: ServerState) -> None:
        """Run the discovery chain and populate ``state.session``.

        Never raises for expected failures: on total failure the session is marked
        disconnected and carries diagnostics the agent can reason about.

        Args:
            state: Shared server state to populate.
        """
        logger.info("AutoConnect: starting gateway discovery")
        state.session.readonly = self.config.readonly

        # 1. Explicit URL — fastest path, skips all scanning.
        if self.config.gateway_url:
            logger.info("AutoConnect: using configured gateway %s", self.config.gateway_url)
            if await self._try_url(state, self.config.gateway_url, "env_var"):
                return

        # 2. Local install: enable disk reads and derive the gateway URL.
        local_info = await self._step_local_install(state)
        if local_info and state.session.is_connected:
            return

        # 3. OS credential store.
        if await self._step_credential_store(state, local_info):
            return

        # 4/5. Network discovery (opt-in because scanning is slow).
        if self.config.scan_network:
            if await self._step_mdns(state):
                return
            if await self._step_port_scan(state):
                return

        if local_info:
            logger.info("AutoConnect: local install available, gateway HTTP unreachable")
            self._apply_local_info(state, local_info, "local_install_only")
            return

        logger.warning("AutoConnect: no gateway found. Set IGNITION_GATEWAY_URL to skip discovery.")
        state.session.is_connected = False
        state.session.discovery_method = "failed"

    # ------------------------------------------------------------------
    # Discovery steps
    # ------------------------------------------------------------------

    async def _step_local_install(self, state: ServerState) -> LocalInstallInfo | None:
        """Scan known install roots, enable local reads and try the derived URL."""
        candidates = await anyio.to_thread.run_sync(_existing_install_paths, self._install_roots())
        for path in candidates:
            logger.info("AutoConnect: found local Ignition install at %s", path)
            reader = LocalInstallReader(path)
            info = await reader.read()
            state.session.local_install_path = str(path)
            state.session.local_read_available = True

            if info.gateway_url and await self._try_url(state, info.gateway_url, "local_install"):
                self._apply_local_info(state, info, state.session.discovery_method)
                return info
            return info
        return None

    async def _step_credential_store(
        self, state: ServerState, local_info: LocalInstallInfo | None
    ) -> bool:
        """Try credentials saved in the OS keyring, with or without a stored URL."""
        from ignition_mcp.autoconnect.cred_store import read_credential

        credential = await read_credential()
        if not credential:
            return False

        url = credential.url or (local_info.gateway_url if local_info else "")
        if not url:
            return False

        logger.info("AutoConnect: trying credentials from the OS store for %s", url)
        return bool(
            await self._try_url(
                state, url, "os_credential_store", credential.username, credential.password
            )
        )

    async def _step_mdns(self, state: ServerState) -> bool:
        """Discover gateways advertised over mDNS."""
        from ignition_mcp.autoconnect.network_scan import mdns_discover

        for url in await mdns_discover(timeout=self.config.scan_timeout):
            if await self._try_url(state, url, "mdns"):
                return True
        return False

    async def _step_port_scan(self, state: ServerState) -> bool:
        """Probe loopback for open Ignition ports."""
        from ignition_mcp.autoconnect.network_scan import port_scan

        urls = await port_scan(
            hosts=["localhost", "127.0.0.1"],
            ports=LOOPBACK_PORTS,
            timeout=self.config.scan_timeout,
        )
        for url in urls:
            if await self._try_url(state, url, "port_scan"):
                return True
        return False

    async def connect_to(self, state: ServerState, url: str) -> bool:
        """Connect to a specific gateway URL (used by ``mcp_set_gateway``).

        Args:
            state: Shared server state.
            url: Gateway base URL.

        Returns:
            ``True`` when the gateway answered and the session was replaced.
        """
        return await self._try_url(state, url, "manual")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def _try_url(
        self,
        state: ServerState,
        url: str,
        discovery: str,
        username: str | None = None,
        password: str | None = None,
    ) -> bool:
        """Probe a gateway URL and, on success, install the session.

        Args:
            state: Shared server state.
            url: Gateway base URL.
            discovery: Discovery step name recorded on the session.
            username: Override username (e.g. from the keyring).
            password: Override password.

        Returns:
            ``True`` when the gateway answered and the session is usable.
        """
        url = url.rstrip("/")
        user = username or self.config.username
        secret = password if username else (self.config.password if user else None)
        token = self.config.api_token

        client = GatewayClient(
            url,
            api_prefix=self.config.api_prefix,
            api_token=token,
            username=user,
            password=secret,
            timeout=min(self.config.timeout, 10.0),
            verify=self.config.verify_ssl,
        )

        # Prefer a Gateway web session (no API key needed); the token is only a fallback.
        auth_method = await client.authenticate() if (user and secret) else client.auth_method
        try:
            await client.get_json("/data/status")
        except GatewayError as exc:
            if exc.status in (401, 403):
                logger.warning("AutoConnect: %s requires authentication (%s)", url, exc.status)
                await client.aclose()
                return False
            if exc.status == 404:
                # Older/newer gateways may not expose /data/status; /openapi.json will do.
                try:
                    await client.get_json("/openapi.json")
                except GatewayError:
                    await client.aclose()
                    return False
            else:
                logger.debug("AutoConnect: probe failed for %s: %s", url, exc.message)
                await client.aclose()
                return False

        await self._install_session(
            state, url, client, discovery, auth_method, user, token, client.auth_verified
        )
        return True

    async def _install_session(
        self,
        state: ServerState,
        url: str,
        client: GatewayClient,
        discovery: str,
        auth_method: str,
        username: str | None,
        token: str | None,
        auth_verified: bool = True,
    ) -> None:
        """Replace the session and gateway client, closing any previous client."""
        previous = state._gateway
        state.gateway = client
        if previous is not None:
            await previous.aclose()

        state.session = IgnitionSession(
            gateway_url=url,
            username=username or ("api-token" if token else "anonymous"),
            api_token=token,
            discovery_method=discovery,
            auth_method=auth_method,
            auth_verified=auth_verified,
            local_install_path=state.session.local_install_path,
            local_read_available=state.session.local_read_available,
            is_connected=True,
            readonly=self.config.readonly,
        )
        await self._populate_session_info(state)
        logger.info("AutoConnect: connected to %s via %s (%s auth)", url, discovery, auth_method)

    async def _populate_session_info(self, state: ServerState) -> None:
        """Fill in version, project list, module list and active project."""
        session = state.session

        reader = state.local_reader
        if reader is not None:
            info = await reader.read()
            self._apply_local_info(state, info, session.discovery_method)

        try:
            status = await state.gateway.get_json("/data/status")
        except GatewayError:
            status = {}
        except RuntimeError:
            status = {}

        if isinstance(status, dict):
            session.gateway_version = str(
                status.get("version") or status.get("platformVersion") or session.gateway_version
            )
            session.gateway_edition = str(
                status.get("edition") or status.get("license") or session.gateway_edition
            )

        if not session.projects:
            try:
                payload = await state.gateway.get_json("/perspective/projects")
                session.projects = _project_names(payload)
            except (GatewayError, RuntimeError):
                pass

        self._select_active_project(state)

    def _apply_local_info(self, state: ServerState, info: LocalInstallInfo, discovery: str) -> None:
        """Copy locally discovered metadata onto the session."""
        session = state.session
        session.local_install_path = info.install_path
        session.local_read_available = True
        if discovery != "local_install_only":
            session.discovery_method = discovery
        if info.projects:
            session.projects = sorted(set(session.projects) | set(info.projects))
        if info.installed_modules:
            session.installed_ignition_modules = info.installed_modules
        if info.gateway_version:
            session.gateway_version = info.gateway_version
        if discovery == "local_install_only":
            session.is_connected = False
            session.auth_method = "none (local filesystem)"
            session.gateway_url = info.gateway_url or session.gateway_url

    def _select_active_project(self, state: ServerState) -> None:
        """Pick the configured project, else the first available one."""
        session = state.session
        if self.config.active_project and self.config.active_project in session.projects:
            session.active_project = self.config.active_project
        elif session.projects and not session.active_project:
            session.active_project = session.projects[0]

        reader = state.local_reader
        if reader is not None and session.active_project:
            chain = reader.inheritance_chain(session.active_project)
            session.parent_project = chain[1] if len(chain) > 1 else None

    def _install_roots(self) -> list[str]:
        """Return candidate install roots for the current platform plus any override."""
        env_root = _env_install_root()
        if env_root:
            return [env_root]
        if sys.platform == "win32":
            return WINDOWS_INSTALL_ROOTS
        return LINUX_INSTALL_ROOTS


def _existing_install_paths(roots: list[str]) -> list[Path]:
    """Return install roots that exist on disk.

    Args:
        roots: Candidate install directories.

    Returns:
        Existing paths in the original order.
    """
    return [Path(root) for root in roots if Path(root).exists()]


def _env_install_root() -> str | None:
    """Read ``IGNITION_INSTALL_PATH`` when set."""
    import os

    value = os.environ.get("IGNITION_INSTALL_PATH", "").strip()
    return value or None


def _project_names(payload: object) -> list[str]:
    """Extract project names from a gateway project payload."""
    names: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("projectName")
                if isinstance(name, str):
                    names.append(name)
    elif isinstance(payload, dict):
        for key in ("projects", "results", "items"):
            if key in payload:
                return _project_names(payload[key])
    return sorted(set(names))


def credentials_summary(config: IgnitionMCPConfig) -> tuple[str, ...]:
    """Return the auth methods configured, without exposing secret values."""
    methods = []
    if config.api_token:
        methods.append("api_token")
    if config.username:
        methods.append("basic")
    return tuple(methods)
