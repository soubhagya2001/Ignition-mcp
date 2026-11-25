"""Entry point for ``python -m ignition_mcp`` and the ``ignition-mcp`` console script."""

from __future__ import annotations

import argparse
import sys

from ignition_mcp import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="ignition-mcp",
        description="AI-controlled MCP server for Ignition SCADA/HMI",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport mode (default: stdio). Use 'sse' or 'http' for remote agents.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for sse/http transport (default: 8080).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for sse/http transport (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level (default: IGNITION_LOG_LEVEL or INFO).",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print a JSON diagnostic report (install, gateway, auth, OpenAPI) and exit.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main() -> None:
    """CLI entry point."""
    args = build_parser().parse_args()

    if args.doctor:
        from ignition_mcp.config import load_config
        from ignition_mcp.doctor import run_doctor

        cfg = load_config(
            transport=args.transport,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
        sys.exit(run_doctor(cfg))

    # Imported late so ``--help``/``--version`` stay fast and dependency-light.
    from ignition_mcp.server import run_server

    try:
        run_server(
            transport=args.transport,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        sys.exit(0)


if __name__ == "__main__":
    main()
