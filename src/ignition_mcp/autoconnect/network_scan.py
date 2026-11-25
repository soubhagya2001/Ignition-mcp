"""
NetworkScanner — mDNS and port-scan based gateway discovery.

AI DEVELOPER NOTE:
  Two strategies:
  1. mdns_discover() — uses zeroconf to find _ignition._tcp.local services
  2. port_scan()     — tries TCP connect to known Ignition ports

  Both return lists of gateway URLs for the AutoConnectEngine to trial.
"""

from __future__ import annotations

import asyncio
import logging
import socket

logger = logging.getLogger(__name__)


async def mdns_discover(timeout: float = 3.0) -> list[str]:
    """
    Discover Ignition gateways via mDNS (_ignition._tcp.local).

    AI DEVELOPER NOTE:
      Ignition 8.1+ advertises itself via mDNS when "Gateway Network" discovery
      is enabled in gateway settings (Config → Gateway Network → Gateway Discovery).
      The service type is _ignition._tcp.local.
      zeroconf.ServiceBrowser listens for 'timeout' seconds and collects results.
    """
    urls: list[str] = []

    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

        class IgnitionListener(ServiceListener):
            def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                info = zc.get_service_info(type_, name)
                if info:
                    for addr_bytes in info.addresses:
                        host = socket.inet_ntoa(addr_bytes)
                        port = info.port or 8088
                        scheme = "https" if port == 8043 else "http"
                        url = f"{scheme}://{host}:{port}"
                        logger.info("mDNS found Ignition at: %s", url)
                        urls.append(url)

            def remove_service(self, *_: object) -> None:
                pass

            def update_service(self, *_: object) -> None:
                pass

        zeroconf = Zeroconf()
        listener = IgnitionListener()
        browser = ServiceBrowser(zeroconf, "_ignition._tcp.local.", listener)

        await asyncio.sleep(timeout)
        browser.cancel()
        zeroconf.close()

    except ImportError:
        logger.debug("zeroconf not installed — skipping mDNS discovery")
    except Exception as exc:
        logger.debug("mDNS discovery error: %s", exc)

    return urls


async def port_scan(
    hosts: list[str],
    ports: list[int],
    timeout: float = 2.0,
) -> list[str]:
    """
    Try TCP connections to hosts:ports concurrently.
    Returns list of reachable URLs in format "http://host:port".

    AI DEVELOPER NOTE:
      Uses asyncio.open_connection for async TCP probe.
      Runs all probes concurrently for speed.
      Returns HTTP URL for port 8088/8060/8090, HTTPS for 8043.
    """
    results: list[str] = []

    async def probe(host: str, port: int) -> None:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            scheme = "https" if port == 8043 else "http"
            url = f"{scheme}://{host}:{port}"
            logger.info("Port scan found open port: %s", url)
            results.append(url)
        except (OSError, asyncio.TimeoutError):
            pass

    tasks = [probe(host, port) for host in hosts for port in ports]
    await asyncio.gather(*tasks)
    return results


async def subnet_scan(
    base_ip: str,
    ports: list[int],
    timeout: float = 1.0,
    max_hosts: int = 254,
) -> list[str]:
    """
    Scan an entire /24 subnet for Ignition gateways.

    AI DEVELOPER NOTE:
      Derives subnet from base_ip (e.g. "192.168.1.0" → scan .1 to .254).
      Use this as a last resort — scanning 254 hosts × N ports takes time.
      Runs all probes concurrently with asyncio.gather.
    """
    # Derive subnet base
    parts = base_ip.split(".")
    if len(parts) != 4:
        return []
    subnet_prefix = ".".join(parts[:3])

    hosts = [f"{subnet_prefix}.{i}" for i in range(1, max_hosts + 1)]
    return await port_scan(hosts, ports, timeout)


def get_local_ip() -> str:
    """Get the machine's primary local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
