"""
CredentialStore — read saved Ignition credentials from the OS credential store.

Checks:
  Windows: Windows Credential Manager (via keyring)
  macOS: Keychain
  Linux: libsecret / GNOME Keyring (via keyring)

AI DEVELOPER NOTE:
  Uses the `keyring` library which abstracts all three OS backends.
  The credential is stored under service name "ignition-mcp" or "IgnitionGateway".
  If found, returns a Credential with url, username, password.

  To save a credential manually:
    Windows: Control Panel → Credential Manager → Windows Credentials → Add
    CLI:     keyring set ignition-mcp admin
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Credential store service names to check (in order)
SERVICE_NAMES = [
    "ignition-mcp",
    "IgnitionGateway",
    "Ignition",
    "ignition",
]


@dataclass
class Credential:
    url: str
    username: str
    password: str


async def read_credential() -> Credential | None:
    """
    Read Ignition credentials from OS credential store.
    Returns None if nothing found.
    """
    try:
        import json

        import keyring

        for service in SERVICE_NAMES:
            # keyring stores by service + username
            # We store JSON: {"url": "...", "username": "...", "password": "..."}
            # under username "default"
            raw = keyring.get_password(service, "default")
            if raw:
                try:
                    data = json.loads(raw)
                    return Credential(
                        url=data.get("url", ""),
                        username=data.get("username", "admin"),
                        password=data.get("password", ""),
                    )
                except (json.JSONDecodeError, KeyError):
                    # Maybe stored as plain password with username
                    pass

            # Also try common usernames directly
            for username in ["admin", "administrator"]:
                password = keyring.get_password(service, username)
                if password:
                    logger.debug(
                        "Found credential in OS store: service=%s user=%s",
                        service,
                        username,
                    )
                    return Credential(url="", username=username, password=password)

    except ImportError:
        logger.debug("keyring not installed — skipping OS credential store")
    except Exception as exc:
        logger.debug("OS credential store error: %s", exc)

    return None


async def save_credential(url: str, username: str, password: str) -> bool:
    """
    Save Ignition credentials to the OS credential store.
    Called by the AI after a successful manual auth to cache for future sessions.
    """
    try:
        import json

        import keyring

        data = json.dumps({"url": url, "username": username, "password": password})
        keyring.set_password("ignition-mcp", "default", data)
        logger.info("Saved gateway credential to OS store for %s", url)
        return True
    except ImportError:
        logger.debug("keyring not installed — cannot save credential")
        return False
    except Exception as exc:
        logger.debug("Could not save credential: %s", exc)
        return False
