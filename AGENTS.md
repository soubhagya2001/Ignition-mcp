# AGENTS.md — Working on Ignition MCP

This file is the operating manual for AI coding agents (and humans) contributing to
`ignition-mcp`. Read it before changing code. It holds the architecture, conventions and
workflow rules that keep the codebase consistent.

## What this project is

A Python MCP (Model Context Protocol) server that gives an AI agent first-class access to an
Ignition SCADA/HMI gateway: tags and UDTs, Perspective views, project scripts, alarms,
databases, history, projects, security and diagnostics.

Two access paths exist and both must keep working:

| Path | When | Implementation |
| --- | --- | --- |
| **Local filesystem** | Ignition is installed on the same machine | `autoconnect/local_reader.py` — reads `data/projects/**`, `wrapper.log`, `config.idb` |
| **Gateway HTTP** | Remote gateway, or writes that must be applied live | `client/gateway_api.py` — `GatewayClient` with cookie session login over `/data/**` |

Local reads are the differentiator: prefer them for every read-only tool, and fall back to HTTP.

## Golden rules

1. **Zero-config first.** The server must start and answer tools with no `.env`, no URL and no
   credentials. Discovery lives in `autoconnect/`; never require user input to boot. Prefer
   **direct control**: local filesystem first, Gateway web-session login
   (`IGNITION_USERNAME`/`IGNITION_PASSWORD`) second. An API token is optional, never required.
2. **Read-only by default.** `IGNITION_READONLY` defaults to `true`. Every write tool must call
   `state.write_guard(operation, confirm, dry_run)` before doing anything and support
   `dry_run=True` previews.
3. **Every tool returns the envelope.** Return `state.tool_response(payload, **extra_ctx)`, never
   a bare dict. The `_ctx` block is how the agent knows its environment.
4. **Never fake a result.** If a gateway endpoint is unverified or a module is unavailable,
   return an explicit `{"status": "unsupported" | "gateway_error"}` payload with a `hint`. Do not
   return invented data and do not swallow exceptions silently.
5. **No secrets on disk.** Session tokens and passwords stay in memory. Never log credentials.
6. **stdio is a wire protocol.** Log to stderr only (`logging`); `print()` corrupts MCP framing.
7. **Stable tool names.** Tool names are the public API. Add tools, don't rename existing ones.
8. **Small, typed, documented.** Type hints everywhere, Google-style docstrings, comments that
   explain *why* (not what the next line does).

## Layout

```
src/ignition_mcp/
├── __main__.py          # CLI parsing, calls server.run_server
├── server.py            # MCPServer bootstrap, module registration, transport
├── mcp_compat.py        # MCPServer (mcp>=2) / FastMCP (mcp 1.x) shim
├── config.py            # env → pyproject → ~/.ignition/mcp.json resolution
├── state.py             # ServerState, IgnitionSession, tool envelope, write guard, audit log
├── safety.py            # write snapshots, restore, diff previews
├── capabilities.py      # OpenAPI → feature matrix (8.3 Standard first)
├── doctor.py            # `ignition-mcp --doctor` diagnostics
├── autoconnect/
│   ├── engine.py        # discovery chain orchestrator
│   ├── local_reader.py  # local install + project resource access
│   ├── network_scan.py  # mDNS + port/subnet scan
│   └── cred_store.py    # OS keyring bridge
├── client/
│   └── gateway_api.py   # GatewayClient: cookie session login, error normalisation
└── modules/
    ├── common.py        # shared helpers (project resolution, payloads, tree walking)
    ├── ai_control.py    # session/context/capabilities/audit/snapshots
    ├── analysis.py      # project analysis, dependency map, generated docs
    ├── gateway.py ... simulators.py
```

Module files are named after their domain (`tags.py`, `perspective.py`, `history.py`, ...) and
each exposes exactly one public function:
`register(mcp: MCPServer, state: ServerState) -> None`.

## Commands

```bash
uv sync                      # install runtime + dev deps
uv run ignition-mcp          # run the server (stdio)
uv run pytest                # full test suite
uv run pytest tests/test_tags.py -q
uv run ruff check src tests  # lint
uv run ruff format src tests # format
uv run mypy src              # type check
uv run ignition-mcp --doctor # install/gateway/OpenAPI diagnostics (JSON)
```

Run `ruff check`, `mypy src` and `pytest` before declaring work finished. CI
(`.github/workflows/ci.yml`) runs the same three checks on Python 3.10–3.13.

## Conventions

- Python **3.10+**, `from __future__ import annotations` at the top of every module.
- Line length **100**; ruff rules `E,F,I,UP,B,SIM,ASYNC`; imports sorted stdlib → third-party →
  first-party.
- `snake_case` functions/modules, `PascalCase` classes, `UPPER_SNAKE` constants.
- Public functions get Google-style docstrings with `Args:` / `Returns:` / `Raises:` as needed.
- Raise `GatewayError` for gateway problems; tools convert it with
  `common.error_response(state, exc)`. Never raise raw `httpx` errors out of a tool.
- Guard filesystem paths with `LocalInstallReader.safe_path()` — never join untrusted strings.
- Use `pathlib.Path` and `.as_posix()` for Ignition resource paths; do not hard-code `\`.

## Adding a tool

1. Pick the module file (create `<domain>.py` and add it to `MODULES` in `server.py` if new).
2. Implement inside `register()` using `state`, `state.local_reader`, or `state.gateway`.
3. Read path: local filesystem first, HTTP second. Write path: `write_guard` → `dry_run`
   preview → `confirm` → apply → verify.
4. Wrap gateway calls in `try/except GatewayError` and return `common.error_response`.
5. Add a focused test (helpers can be module-level functions prefixed `_` so they are testable).
6. Update `CHANGELOG.md` under `## [Unreleased]`.

## Testing rules

- `pytest` with `asyncio_mode = "auto"`; no `pytest.mark.asyncio` needed.
- Mock HTTP with `respx`; never hit a real gateway in tests.
- Build fake installs with `tmp_path` (see `tests/conftest.py::fake_install`).
- Tests must not depend on the developer's machine, network or Ignition install.

## Phase status

Keep this table current when a phase lands.

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Foundation, AutoConnect, AI Control, Gateway, Tags, Perspective, Scripting (read + local) | implemented |
| 2 | Scripting console, Alarms, Database, Project deployment, Perspective writes | implemented (REST best-effort) |
| 3 | History/OEE, Icons, Security, Historians | implemented (REST best-effort) |
| 4 | OPC-UA, Simulators | stubs |

## Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and SemVer.
Add user-visible changes to `## [Unreleased]` under `Added` / `Changed` / `Fixed` / `Removed`.
