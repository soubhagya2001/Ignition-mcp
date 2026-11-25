# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Local project creation with title, description, parent, enabled and inheritable metadata,
  plus dry-run validation and read-back verification.
- `mcp_designer_status` process/window discovery for running Ignition Designer sessions.
  Direct Designer UI editing remains a separate UI-automation integration.
- Versioned Ignition documentation tools: `docs_search`, `docs_get_page`, `docs_system_function`,
  `docs_expression_function`, `docs_component`, `docs_binding`, `docs_script_scope` and
  `docs_refresh`, with an official-host allow-list, Markdown-first cache and HTML fallback.
- Context-aware Ignition script validation: `check_script_indentation` and
  `format_ignition_script`, plus indentation diagnostics for event, transform, property-change,
  message-handler and project-library contexts.
- Perspective embedded-script introspection and targeted writes for bindings, transforms,
  property-change scripts, event scripts and message handlers, with dry-run diffs, snapshots
  and read-back verification.
- Named-query create/update/delete tools and local-resource snapshots.
- `AGENTS.md` (agent/contributor operating manual) and `CONTRIBUTING.md`.
- `mcp_compat.py`: single shim that works with MCP SDK v2 (`MCPServer`) and v1 (`FastMCP`).
- `GatewayClient` with typed `GatewayError`, optional API-token auth, and Gateway web-session
  (cookie) login so no API key is required.
- `ServerState` session audit log and `mcp_audit_log` tool for agent self-auditing.
- Local project access layer: view/script/named-query/style reads, text search, inheritance
  chain, and path-traversal-safe resource access.
- Real implementations for the Gateway, Tags/UDTs, Perspective (reads and local writes),
  Scripting (syntax analysis) and AI Control modules.
- REST-backed implementations for the Alarms, Database, History, Projects, Icons, Security
  and Historians modules with explicit `unsupported` payloads where a gateway edition lacks
  the endpoint.
- CI workflow (ruff, mypy, pytest on Python 3.10–3.13), `.editorconfig`, coverage config.
- Write-safety layer: automatic snapshots before local writes, snapshot list/restore tools,
  dry-run diffs for views, scripts, resources and tags, and read-back verification.
- Capability negotiation from `/openapi.json`, exposed through `mcp_list_capabilities`.
- `ignition-mcp --doctor`: JSON diagnostics for install, gateway, auth and OpenAPI coverage.
- Project analysis tools (`analyze_project`, `project_dependency_map`,
  `generate_project_docs`) and literal/regex `search_project_text`.
- `gateway_trial_status` (read-only, no API token) and expiry-gated `gateway_reset_trial`
  using the Gateway admin session; pre-emptive resets are refused.
- `gateway_backup` streams a full `.gwbk` from `/data/api/v1/backup`; `export_project` now
  saves to a default path, and `create_project_backup` uses the verified project export API
  instead of nonexistent backup routes.

### Changed

- Removed reporting and MES/Sepasoft modules and documentation references; these integrations
  are out of scope for this MCP.
- Project-local path resolution now rejects escaping project names and never falls back to an
  unsafe path.
- Added dry-run previews for project rollback, icon deletion and snapshot restoration.
- Module files are named after their domain (`gateway.py`, `tags.py`, ...) instead of using
  priority-letter prefixes; `IGNITION_MODULES` and registration use the plain module names.

- Authentication is session-first: username/password Gateway login is the default, local
  filesystem access needs no credentials, and `IGNITION_API_TOKEN` is an optional fallback.
- Server bootstrap now uses `mcp.server.mcpserver.MCPServer` (MCP 2.x) through the compat
  shim, and supports `stdio`, `sse` and `streamable-http` transports.
- `config.py` resolves `.env` → environment → `pyproject.toml` → `~/.ignition/mcp.json` with
  typed parsing and safe fallbacks.
- `state.write_guard()` returns the correct envelope (previously referenced an undefined
  name) and records blocked/allowed writes in the audit log.
- Local reader parses `gateway.xml` for HTTP/HTTPS ports instead of assuming 8088, and uses
  `pathlib` joins so it works on Windows and Linux.
- README documents project status, development commands and agent docs.

### Fixed

- `config.py` used `List` without importing it.
- Module registration failed at import time with MCP SDK 2.x (`mcp.server.fastmcp` removed).
- Perspective/script/named-query paths were built with hard-coded backslashes.
- `read_project_resource` allowed `..` traversal outside the project directory.

## [0.1.0] - 2026-11-24

### Added

- Initial scaffold: project layout, 16 module stubs, AutoConnect discovery chain skeleton,
  configuration loader, shared server state, and initial documentation.

[Unreleased]: https://github.com/your-org/ignition-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/your-org/ignition-mcp/releases/tag/v0.1.0
