"""Configuration resolution for Ignition MCP.

Precedence (highest wins):

1. CLI arguments passed to :func:`load_config`
2. ``IGNITION_*`` environment variables
3. ``.env`` file in the working directory (loaded on import, never overriding env)
4. ``pyproject.toml`` ``[tool.ignition-mcp]`` section
5. ``~/.ignition/mcp.json``
6. Built-in defaults

Every value has a safe default, so the server starts with no configuration at all. This module
performs no network I/O; discovery happens in :mod:`ignition_mcp.autoconnect.engine`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_TRANSPORTS = ("stdio", "sse", "http")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass
class IgnitionMCPConfig:
    """Resolved configuration for the MCP server.

    Attributes:
        gateway_url: Explicit gateway base URL; ``None`` triggers auto-discovery.
        username: Gateway username used for HTTP Basic auth.
        password: Gateway password used for HTTP Basic auth.
        api_token: Ignition API key sent as ``X-Ignition-API-Token``.
        api_prefix: Prefix for the documented REST namespace.
        verify_ssl: Verify TLS certificates (Ignition often uses self-signed certs).
        readonly: Block every write tool when true (the default).
        trial_default_credentials: Allow factory-default credentials on loopback gateways.
        active_project: Initial project context.
        timeout: HTTP timeout in seconds.
        scan_network: Allow mDNS and port scanning during discovery.
        scan_timeout: Per-probe timeout in seconds during discovery.
        enabled_modules: Optional allow-list of module names (``None`` means all).
        transport: ``stdio``, ``sse`` or ``http``.
        host: Bind host for network transports.
        port: Bind port for network transports.
        log_level: Logging level name.
    """

    # Gateway connection (resolved by AutoConnect when omitted)
    gateway_url: str | None = None
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    api_prefix: str = "/data/api/v1"
    verify_ssl: bool = False

    # Behaviour
    readonly: bool = True
    trial_default_credentials: bool = True
    active_project: str | None = None
    timeout: float = 30.0
    scan_network: bool = True
    scan_timeout: float = 2.0
    enabled_modules: list[str] | None = None

    # Server
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"

    def connection_kwargs(self) -> dict[str, Any]:
        """Return the keyword arguments shared by gateway client construction.

        Returns:
            A dict with base URL, API prefix, credentials and timeouts.
        """
        return {
            "base_url": self.gateway_url or "",
            "api_prefix": self.api_prefix,
            "api_token": self.api_token,
            "username": self.username,
            "password": self.password,
            "timeout": self.timeout,
            "verify": self.verify_ssl,
        }


def load_config(
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
    log_level: str | None = None,
) -> IgnitionMCPConfig:
    """Build the server configuration by layering files, environment and CLI arguments.

    Args:
        transport: CLI transport override (``stdio``/``sse``/``http``).
        host: CLI bind-host override.
        port: CLI bind-port override.
        log_level: CLI log-level override.

    Returns:
        A fully populated :class:`IgnitionMCPConfig`.
    """
    # Load .env optionally; a missing file or python-dotenv dependency is not an error.
    # Done here (not at module level) to avoid polluting test environments on import.
    if not os.environ.get("IGNITION_MCP_NO_DOTENV"):
        try:  # pragma: no cover - depends on the local environment
            from dotenv import load_dotenv

            load_dotenv(override=False)
        except ImportError:  # pragma: no cover
            pass

    cfg = IgnitionMCPConfig()

    # Lowest precedence first: file-based config, then env, then explicit CLI values.
    _apply_pyproject(cfg)
    _apply_user_config(cfg)
    _apply_env(cfg)

    if transport:
        cfg.transport = transport
    if host:
        cfg.host = host
    if port:
        cfg.port = port
    if log_level:
        cfg.log_level = log_level.upper()

    if cfg.transport not in VALID_TRANSPORTS:
        logger.warning("Unknown transport %r, falling back to stdio", cfg.transport)
        cfg.transport = "stdio"
    if cfg.log_level not in VALID_LOG_LEVELS:
        cfg.log_level = "INFO"

    return cfg


def _apply_env(cfg: IgnitionMCPConfig) -> None:
    """Apply ``IGNITION_*`` environment variables to ``cfg``."""
    cfg.gateway_url = os.environ.get("IGNITION_GATEWAY_URL", cfg.gateway_url)
    cfg.username = os.environ.get("IGNITION_USERNAME", cfg.username)
    cfg.password = _env_secret("IGNITION_PASSWORD", "IGNITION_PASSWORD_FILE", cfg.password)
    cfg.api_token = _env_secret("IGNITION_API_TOKEN", "IGNITION_API_TOKEN_FILE", cfg.api_token)
    cfg.api_prefix = os.environ.get("IGNITION_API_PREFIX", cfg.api_prefix)
    cfg.active_project = os.environ.get("IGNITION_PROJECT", cfg.active_project)
    cfg.transport = os.environ.get("IGNITION_TRANSPORT", cfg.transport)
    cfg.host = os.environ.get("IGNITION_HOST", cfg.host)
    cfg.log_level = os.environ.get("IGNITION_LOG_LEVEL", cfg.log_level).upper()

    cfg.timeout = _env_float("IGNITION_TIMEOUT", cfg.timeout)
    cfg.port = _env_int("IGNITION_PORT", cfg.port)
    cfg.scan_timeout = _env_float("IGNITION_SCAN_TIMEOUT", cfg.scan_timeout)
    cfg.readonly = _env_bool("IGNITION_READONLY", cfg.readonly)
    cfg.scan_network = _env_bool("IGNITION_SCAN_NETWORK", cfg.scan_network)
    cfg.verify_ssl = _env_bool("IGNITION_VERIFY_SSL", cfg.verify_ssl)
    cfg.trial_default_credentials = _env_bool(
        "IGNITION_TRIAL_DEFAULT_CREDENTIALS", cfg.trial_default_credentials
    )

    modules_env = os.environ.get("IGNITION_MODULES", "")
    if modules_env:
        cfg.enabled_modules = [m.strip() for m in modules_env.split(",") if m.strip()]


def _apply_pyproject(cfg: IgnitionMCPConfig) -> None:
    """Read ``[tool.ignition-mcp]`` from ``pyproject.toml`` in the working directory."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        try:
            import tomli as tomllib
        except ImportError:
            return

    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.exists():
        return
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
        _apply_dict(cfg, data.get("tool", {}).get("ignition-mcp", {}))
    except (OSError, ValueError) as exc:
        logger.debug("Could not read pyproject.toml [tool.ignition-mcp]: %s", exc)


def _apply_user_config(cfg: IgnitionMCPConfig) -> None:
    """Read ``~/.ignition/mcp.json`` when present."""
    user_config = Path.home() / ".ignition" / "mcp.json"
    if not user_config.exists():
        return
    try:
        with user_config.open(encoding="utf-8") as handle:
            data = json.load(handle)
        _apply_dict(cfg, data)
    except (OSError, ValueError) as exc:
        logger.debug("Could not read ~/.ignition/mcp.json: %s", exc)


def _apply_dict(cfg: IgnitionMCPConfig, data: dict[str, Any]) -> None:
    """Apply a mapping of config keys, ignoring unknown keys and ``None`` values."""
    mapping = {
        "gateway_url": "gateway_url",
        "username": "username",
        "password": "password",
        "api_token": "api_token",
        "api_prefix": "api_prefix",
        "project": "active_project",
        "active_project": "active_project",
        "readonly": "readonly",
        "verify_ssl": "verify_ssl",
        "timeout": "timeout",
        "scan_network": "scan_network",
        "modules": "enabled_modules",
        "transport": "transport",
        "host": "host",
        "port": "port",
        "log_level": "log_level",
    }
    for key, attr in mapping.items():
        if key in data and data[key] is not None:
            setattr(cfg, attr, data[key])


def _env_secret(var_name: str, file_var_name: str, fallback: str | None) -> str | None:
    """Read a secret from ``var_name`` or a mounted secret file ``file_var_name``."""
    direct = os.environ.get(var_name)
    if direct is not None and direct != "":
        return direct
    file_path = os.environ.get(file_var_name, "").strip()
    if file_path:
        try:
            return Path(file_path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Could not read %s (%s): %s", file_var_name, file_path, exc)
    return fallback


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable, accepting common truthy spellings."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable, keeping ``default`` on bad input."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Parse a float environment variable, keeping ``default`` on bad input."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default
