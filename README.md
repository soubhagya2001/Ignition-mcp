# Ignition MCP

> A local-first MCP server for **Ignition 8.3** SCADA/HMI.
> Direct filesystem control when Ignition is on the same machine, Gateway web-session login
> when it is remote. No API key required. Read-only by default.

`ignition-mcp` exposes **164 tools across 16 modules** for projects, Perspective views, project
scripts, tags and UDTs, alarms, databases, history/OEE, security, historians, versioned
documentation lookup and diagnostics.
It is built for AI agents first: every response carries a `_ctx` block describing the session,
and every failure is explicit (`unsupported`, `not_connected`, `insufficient_data`) instead of
invented data.

---

## Contents

- [What you get](#what-you-get)
- [Requirements](#requirements)
- [Install and run](#install-and-run)
- [Connect to Ignition](#connect-to-ignition)
- [Client setup](#client-setup)
- [Write safety](#write-safety)
- [Tool catalog](#tool-catalog)
- [Guide for AI agents](#guide-for-ai-agents)
- [Common workflows](#common-workflows)
- [Environment variables](#environment-variables)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Project layout](#project-layout)
- [Limitations](#limitations)
- [License](#license)

---

## What you get

| Area | What it does |
| --- | --- |
| **Local-first control** | Reads and writes project files, views, scripts, named queries and styles directly from an Ignition install — no HTTP, no credentials. |
| **Remote gateway access** | Session-first login with username/password; optional API token; no factory-password spraying. |
| **164 tools** | Gateway diagnostics, trial/license status, tags/UDTs, Perspective, scripting, alarms, database, history/OEE, projects, icons, security, historians, analysis, docs lookup and AI control. |
| **Safe writes** | Read-only by default; `dry_run` diffs, `confirm=True`, read-back verification, automatic local snapshots and restore. |
| **Project analysis** | Broken bindings, missing embedded views/styles, unused views, inheritance maps and generated Markdown docs. |
| **Runtime diagnostics** | `ignition-mcp --doctor` reports install, gateway, auth and OpenAPI coverage as JSON. |

---

## Requirements

- **Python 3.10+**
- **Ignition 8.3** is the primary target. Standard edition is the reference; other editions and
  versions work where their endpoints match, and unsupported endpoints return a clear
  `unsupported` payload with a hint.
- **Local install (optional):** Ignition on the same machine enables direct filesystem access.
- **Remote gateway (optional):** the gateway URL plus a username/password, or an API token.

---

## Install and run

### With `uv` (recommended)

```bash
uvx ignition-mcp
```

### With `pip`

```bash
pip install ignition-mcp
ignition-mcp
```

### From a checkout

```bash
git clone https://github.com/your-org/ignition-mcp
cd ignition-mcp
uv sync
uv run ignition-mcp
```

The default transport is `stdio`, which is what Claude Desktop, Cursor and VS Code expect.

```bash
# Network transports for remote agents
ignition-mcp --transport sse --port 8080
ignition-mcp --transport http --port 8080

# Diagnostics (prints JSON and exits)
ignition-mcp --doctor
```

---

## Connect to Ignition

### Local install (recommended)

If Ignition is installed on the same machine, the server finds it automatically. It reads
`data/gateway.xml`, `data/db/config.idb` and `data/projects/**` directly.

```bash
# Only needed when the install is in a non-standard location
export IGNITION_INSTALL_PATH="/opt/ignition"
```

With local access, project reads and writes need no gateway connection and no credentials.

### Remote gateway

```bash
export IGNITION_GATEWAY_URL="http://192.168.1.50:8088"
export IGNITION_USERNAME="operator"
export IGNITION_PASSWORD="secret"
```

The client logs in to the Gateway web session (`/main/webapp/j_security_check`) and reuses the
session cookie. An API token is optional:

```bash
export IGNITION_API_TOKEN="..."   # optional fallback only
```

### Discovery order

AutoConnect tries, in order:

1. Explicit URL (`IGNITION_GATEWAY_URL` or a config file)
2. Local Ignition install scan
3. OS credential store (Windows Credential Manager / macOS Keychain / Linux keyring)
4. mDNS (`_ignition._tcp.local`)
5. Port scan of `localhost` on 8088, 8043, 8060 and 8090

It stops at the first usable result. If no gateway is found, local filesystem tools still work.

### Verify the connection

```bash
ignition-mcp --doctor
```

The report includes the detected install path, gateway URL, auth method, OpenAPI endpoint count
and a capability matrix. It never prints passwords or tokens.

---

## Client setup

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` on Windows, or
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS:

```json
{
  "mcpServers": {
    "ignition": {
      "command": "uvx",
      "args": ["ignition-mcp"]
    }
  }
}
```

Add an `env` block only when you want to skip discovery:

```json
{
  "mcpServers": {
    "ignition": {
      "command": "uvx",
      "args": ["ignition-mcp"],
      "env": {
        "IGNITION_GATEWAY_URL": "http://192.168.1.50:8088",
        "IGNITION_USERNAME": "operator",
        "IGNITION_PASSWORD": "secret"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "ignition": {
      "command": "uvx",
      "args": ["ignition-mcp"]
    }
  }
}
```

### VS Code

`.vscode/mcp.json`:

```json
{
  "servers": {
    "ignition": {
      "type": "stdio",
      "command": "uvx",
      "args": ["ignition-mcp"]
    }
  }
}
```

### Remote HTTP

```bash
ignition-mcp --transport http --host 0.0.0.0 --port 8080
```

Point the client at `http://your-host:8080/mcp`. Use `sse` instead when the client requires the
older SSE transport.

### Docker and Docker Compose

A non-root `Dockerfile` and `compose.yaml` are included for running Ignition 8.3 alongside
`ignition-mcp` with read-only project access and file-based secrets:

```bash
mkdir -p secrets artifacts
printf 'your-admin-password' > secrets/gateway-admin-password
docker compose up -d --build
docker compose exec ignition-mcp /app/.venv/bin/ignition-mcp --doctor
```

The Compose stack keeps `IGNITION_READONLY=true`, mounts Ignition's `data` volume read-only at
`/ignition/data`, and maps `./artifacts` to `/artifacts` for `export_project` and `gateway_backup`.
`IGNITION_PASSWORD_FILE` and `IGNITION_API_TOKEN_FILE` are also supported outside Docker.

---

## Write safety

Writes are blocked by default.

```bash
export IGNITION_READONLY=false       # enable writes for this process
```

An agent can also enable writes for the current session with `mcp_enable_writes` (and turn them
off again with `mcp_disable_writes`).

Every write follows the same sequence:

1. **Guard** — `write_guard` rejects writes while read-only and requires `confirm=True`.
2. **Preview** — call with `dry_run=True` to receive a unified text diff or a field-level JSON
   diff (`changed`, `change_count`, `changes`, `diff`).
3. **Apply** — call again with `confirm=True`.
4. **Verify** — local writes are read back and compared; tag writes read the values back from
   the gateway and report `verified` plus any `mismatches`.

### Snapshots and restore

Before a local file is replaced, the server copies it to:

```text
~/.ignition/mcp/snapshots/<timestamp>/
```

Each snapshot has a `manifest.json` and the original `content`. The newest 20 are kept.

- `mcp_list_write_snapshots` — list snapshots.
- `mcp_restore_write_snapshot(snapshot_id, confirm=True)` — restore one file.

Restore is itself a write: it needs writes enabled and `confirm=True`. Snapshots cover local
file writes (views, scripts, project resources), not live tag values.

```bash
export IGNITION_MCP_SNAPSHOT_DIR="/custom/snapshot/dir"
export IGNITION_MCP_SNAPSHOT_LIMIT=20
```

---

## Trial and licensing

Ignition's trial is two hours and can be reset an unlimited number of times, but only **after it
has expired**. Pre-emptive resets are against the license agreement, so the MCP refuses them.

- `gateway_trial_status` reads `/data/api/v1/trial`. The read needs no API token and returns
  `license_mode`, `trial_status` (`active`, `expired`, `licensed`), `seconds_remaining`,
  `expired`, `emergency` and `development` flags.
- `gateway_reset_trial(confirm=True)` resets the timer **only when `expired` is true**. It uses
  the Gateway session (`IGNITION_USERNAME`/`IGNITION_PASSWORD`); no API token is required.
  Without an administrator session the gateway returns `401`, and the tool reports
  `authentication_required` with a hint.
- Restarting the gateway does **not** reset the trial. Local project files do not contain the
  trial timer; it is runtime state and must be read from the gateway.

```text
gateway_trial_status
# {"trial_status": "expired", "seconds_remaining": 0, "expired": true, ...}

gateway_reset_trial(dry_run=True)
# {"status": "dry_run", "reset_allowed": true, ...}

gateway_reset_trial(confirm=True)
# {"status": "reset", "verified": true, "after": {"seconds_remaining": 7200, ...}}
```

---

## Project exports and Gateway backups

- `export_project(project, output_path)` downloads a project-only `.zip` from
  `GET /data/api/v1/projects/export/{project}`. It includes project resources, not tags or
  Gateway configuration. Without `output_path`, it writes to
  `exports/<project>_<timestamp>.zip`.
- `create_project_backup(project, output_path)` is the same project export under a
  backup-oriented name; it defaults to `project_backups/<project>_<timestamp>.zip`.
- `gateway_backup(output_path, include_peer_local, confirm=True)` downloads a full `.gwbk` from
  `GET /data/api/v1/backup`. It includes projects, tags, Gateway configuration and credentials.
  Without `output_path`, it writes to
  `backups/<gateway>_gateway_backup_<timestamp>.gwbk`.

All three use the Gateway admin session (`IGNITION_USERNAME`/`IGNITION_PASSWORD`) and do not
require an API token. Without an admin session the gateway returns `401`. `gateway_backup`
requires `confirm=True` because it can be large; use `dry_run=True` first. Downloads are written
to a `.part` file and renamed only after a complete response.

```text
export_project(project="Line1", output_path="/artifacts/Line1.zip")
gateway_backup(output_path="/artifacts/gateway.gwbk", confirm=True)
# {"status": "backup_created", "path": "/artifacts/gateway.gwbk", "verified": true, ...}
```

---

## Tool catalog

The table lists representative tools. Call `mcp_list_capabilities` for the full, live list and
the gateway feature matrix.

| Module | Key tools |
| --- | --- |
| **AI Control** | `mcp_connect`, `mcp_status`, `mcp_set_project`, `mcp_list_capabilities`, `mcp_discover_gateways`, `mcp_set_gateway`, `mcp_audit_log`, `mcp_enable_writes`, `mcp_disable_writes`, `mcp_list_write_snapshots`, `mcp_restore_write_snapshot`, `mcp_designer_status` |
| **Gateway** | `gateway_status`, `gateway_info`, `gateway_modules`, `gateway_projects`, `gateway_logs`, `gateway_diagnostics`, `gateway_performance`, `gateway_backup_status`, `gateway_backup`, `gateway_redundancy_status`, `gateway_restart`, `gateway_trial_status`, `gateway_reset_trial` |
| **Tags & UDTs** | `browse_tags`, `read_tag`, `write_tag`, `search_tags`, `get_tag_configuration`, `list_udts`, `get_udt_definition`, `find_udt_instances`, `compare_udts`, `find_tag_references`, `find_missing_feedback_tags`, `validate_tag_paths` |
| **Perspective** | `list_perspective_views`, `read_perspective_view`, `create_perspective_view`, `update_perspective_view`, `validate_perspective_view`, `list_page_config`, `inspect_component_tree`, `find_view_bindings`, `find_event_scripts`, `list_bindings`, `get_binding`, `list_transforms`, `list_property_change_scripts`, `list_message_handlers`, `get_event_script`, `list_view_scripts`, `validate_view_scripts`, `validate_project_scripts`, `set_binding`, `delete_binding`, `set_transform`, `delete_transform`, `set_property_change_script`, `delete_property_change_script`, `set_message_handler`, `delete_message_handler`, `set_event_script`, `delete_event_script`, `list_styles`, `add_style_class_to_component`, `add_custom_property`, `find_views_by_css`, `check_embedded_views`, `check_view_bindings` |
| **Scripting** | `list_project_scripts`, `read_project_script`, `write_project_script`, `validate_script_syntax`, `check_script_indentation`, `format_ignition_script`, `analyze_script`, `search_script_functions`, `find_script_dependencies`, `compare_script_versions`, `find_script_references`, `generate_ignition_script`, `access_script_console`, `test_code_snippet` |
| **Docs** | `docs_search`, `docs_get_page`, `docs_system_function`, `docs_expression_function`, `docs_component`, `docs_binding`, `docs_script_scope`, `docs_refresh` |
| **Alarms** | `list_alarm_configurations`, `get_active_alarms`, `get_alarm_history`, `get_alarm_pipeline`, `inspect_alarm_properties`, `find_alarm_tag_references`, `analyze_alarm_frequency`, `get_alarm_statistics` |
| **Database** | `list_database_connections`, `create_db_connection`, `test_db_connection`, `list_named_queries`, `read_named_query`, `inspect_named_query_parameters`, `create_named_query`, `write_named_query`, `delete_named_query`, `validate_sql`, `execute_readonly_query`, `test_named_query`, `find_database_references`, `find_queries_by_table` |
| **History/OEE** | `query_tag_history`, `get_production_counts`, `get_machine_runtime`, `get_downtime`, `calculate_availability`, `calculate_performance`, `calculate_quality`, `compare_shift_production`, `generate_production_report` |
| **Projects** | `create_project`, `list_project_resources`, `read_project_resource`, `write_project_resource`, `export_project`, `import_project`, `compare_project_versions`, `validate_project`, `create_project_backup`, `deploy_project`, `rollback_project`, `check_project_inheritance`, `get_parent_project` |
| **Icons** | `list_icons`, `search_icons`, `add_icon`, `edit_icon`, `delete_icon`, `get_icon_usage` |
| **Security** | `list_user_sources`, `get_user_source`, `create_user_source`, `edit_user_source`, `list_identity_providers`, `get_identity_provider`, `create_identity_provider`, `edit_identity_provider`, `list_roles`, `get_audit_profile_data` |
| **Historians** | `list_historians`, `get_historian`, `create_historian`, `edit_historian`, `get_historian_status`, `list_history_providers` |
| **Analysis** | `analyze_project`, `project_dependency_map`, `generate_project_docs`, `search_project_text` |
| **Phase 4 stubs** | `list_devices`, `add_device`, `get_device_status`, `browse_opc_server`, `read_opc_node`, `list_simulators`, `create_simulator`, `create_simulator_tags` — registered but return `unsupported` |

---

## Guide for AI agents

### Start every session

1. Call `mcp_connect`. Read the returned `_ctx` block: gateway URL, version, edition, auth
   method, active project, local read availability and warnings.
2. Call `mcp_list_capabilities`. Inspect `gateway.features`; treat `unverified` and `unknown`
   as "try it and handle `unsupported`", not as "available".
3. Call `mcp_status` before long operations if the session may have expired.

### Choose the right path

- **Project files** (views, scripts, resources, styles, named queries): prefer local filesystem
  reads. They are faster and need no gateway.
- **Live values** (tag reads/writes, alarms, history, gateway health): require a gateway.
- If `state.has_gateway` is false, use local-only tools and explain what cannot be done.

### Write protocol

Never enable writes unless the user explicitly asks for a change.

```text
read current state
  → dry_run=True (inspect diff/changes)
  → confirm=True (apply)
  → check verified / mismatches
  → if needed: mcp_list_write_snapshots → mcp_restore_write_snapshot(confirm=True)
```

- Do not call `mcp_enable_writes` just to "see if it works".
- A write without `confirm=True` returns `requires_confirmation`; that is expected.
- If a write is blocked, report the `reason` and the exact flag the user must change.
- Snapshots cover local file writes only. Live tag writes are verified by read-back, not snapshotted.

### Response envelope

Every tool returns:

```json
{
  "result": { "status": "ok", "...": "..." },
  "_ctx": {
    "gateway_url": "http://localhost:8088",
    "gateway_version": "8.3.0",
    "gateway_edition": "Standard",
    "auth_method": "session_login",
    "active_project": "Line1",
    "local_read_available": true,
    "readonly_mode": true,
    "warnings": [],
    "ai_hints": []
  }
}
```

### Status values to handle

| Status | Meaning | What to do |
| --- | --- | --- |
| `ok` / `written` / `created` / `updated` | Success | Continue; check `verified` when present |
| `dry_run` | Preview only | Inspect `diff` / `changes`, then confirm |
| `requires_confirmation` | `confirm=True` missing | Ask the user, then retry with confirmation |
| `blocked` | Read-only mode | Explain how to enable writes; do not retry blindly |
| `not_connected` | No gateway client | Use local tools or help the user connect |
| `not_expired` | Trial reset requested too early | Wait for expiry; pre-emptive resets are refused |
| `authentication_required` | Trial reset needs an admin Gateway session | Set `IGNITION_USERNAME`/`IGNITION_PASSWORD` |
| `unsupported` | Endpoint/module unavailable | Report the hint; do not invent a result |
| `gateway_error` | HTTP/auth failure | Report status and path; check credentials/version |
| `insufficient_data` | Metrics missing | Ask for the missing values; do not fabricate |

### Analysis before editing

- `analyze_project` finds broken bindings, missing embedded views, missing style classes, unused
  views and missing parent projects.
- `project_dependency_map` shows inheritance and cycles.
- `search_project_text` supports literal and regex search across projects, including parents.
- `generate_project_docs` produces Markdown for views, scripts, queries and styles.

Use these before large edits so you change the right resources.

### Trial reset

- Call `gateway_trial_status` before suggesting a reset. If `trial_status` is `active`, do not
  reset; wait for expiry.
- Only call `gateway_reset_trial(confirm=True)` when `expired` is true and the user asked for
  it. It needs writes enabled and an administrator Gateway session, not an API token.
- If it returns `authentication_required`, ask the user for Gateway credentials; never guess or
  spray passwords.

### Self-audit

`mcp_audit_log` returns this session's tool actions with status and non-secret details. Use it
when the user asks what changed.

### Multiple gateways

Run one server instance per gateway. Within an instance, `mcp_set_gateway(url)` switches the
active session; `mcp_discover_gateways` lists candidates. A shared multi-gateway registry is not
implemented.

---

## Common workflows

### Inspect a project

```text
mcp_connect
mcp_list_capabilities
mcp_set_project(project="Line1")
list_project_resources
list_perspective_views
list_project_scripts
list_named_queries
```

### Read live tags

```text
read_tag(paths=["[default]Line1/Count", "[default]Line1/Running"])
search_tags(query="Pump*", provider="default")
```

### Safely edit a view

```text
read_perspective_view(view_path="Dashboard/Overview")
update_perspective_view(view_path="Dashboard/Overview", view_json={...}, dry_run=True)
# inspect diff
update_perspective_view(view_path="Dashboard/Overview", view_json={...}, confirm=True)
```

### Safely edit a script

```text
read_project_script(module_path="Util")
write_project_script(module_path="Util", code="...", dry_run=True)
# inspect diff
write_project_script(module_path="Util", code="...", confirm=True)
```

### Audit a project

```text
analyze_project(project="Line1")
project_dependency_map(project="Line1")
generate_project_docs(project="Line1")
```

### Diagnose a connection

```bash
ignition-mcp --doctor
```

---

## Environment variables

All variables are optional; safe defaults are built in.

| Variable | Default | Purpose |
| --- | --- | --- |
| `IGNITION_INSTALL_PATH` | auto-detect | Local Ignition install root |
| `IGNITION_GATEWAY_URL` | auto-discover | Gateway base URL, e.g. `http://host:8088` |
| `IGNITION_USERNAME` | — | Gateway session username |
| `IGNITION_PASSWORD` | — | Gateway session password |
| `IGNITION_API_TOKEN` | — | Optional API key (`X-Ignition-API-Token`) |
| `IGNITION_API_PREFIX` | `/data/api/v1` | REST namespace prefix |
| `IGNITION_PROJECT` | first project | Initial active project |
| `IGNITION_READONLY` | `true` | Set `false` to allow writes |
| `IGNITION_MODULES` | all | Comma-separated module allow-list (e.g. `gateway,tags,projects`) |
| `IGNITION_TIMEOUT` | `30` | HTTP timeout in seconds |
| `IGNITION_SCAN_NETWORK` | `true` | Allow mDNS and port discovery |
| `IGNITION_SCAN_TIMEOUT` | `2` | Seconds per discovery probe |
| `IGNITION_VERIFY_SSL` | `false` | Verify TLS certificates |
| `IGNITION_TRANSPORT` | `stdio` | `stdio`, `sse` or `http` |
| `IGNITION_HOST` | `127.0.0.1` | Bind host for network transports |
| `IGNITION_PORT` | `8080` | Bind port for network transports |
| `IGNITION_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `IGNITION_MCP_SNAPSHOT_DIR` | `~/.ignition/mcp/snapshots` | Write-snapshot store |
| `IGNITION_MCP_SNAPSHOT_LIMIT` | `20` | Number of snapshots to keep |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `not_connected` for tag/history tools | No gateway URL or session | Set `IGNITION_GATEWAY_URL`, or run locally with `IGNITION_INSTALL_PATH` |
| `unsupported` for a tool | Endpoint missing on this version/edition | Check `--doctor` and the gateway's `/openapi.json`; the hint names the tried endpoints |
| `401` / `403` | Missing or rejected session login | Set `IGNITION_USERNAME`/`IGNITION_PASSWORD`; API token is optional |
| Writes return `blocked` | Read-only mode | Set `IGNITION_READONLY=false` or call `mcp_enable_writes` |
| Writes return `requires_confirmation` | `confirm=True` missing | Preview with `dry_run=True`, then confirm |
| Local install not found | Non-standard path | Set `IGNITION_INSTALL_PATH` |
| Slow startup | Network discovery on a large subnet | Set `IGNITION_GATEWAY_URL` or `IGNITION_SCAN_NETWORK=false` |
| Gateway changes not visible | Local file write vs. running gateway | Reload/deploy the project in Ignition if your setup requires it |
| Need to undo a local edit | Snapshot available | `mcp_list_write_snapshots` → `mcp_restore_write_snapshot(confirm=True)` |
| Trial expired | Two-hour trial ended | `gateway_trial_status`, then `gateway_reset_trial(confirm=True)` after expiry |
| Trial reset returns `authentication_required` | No admin Gateway session | Set `IGNITION_USERNAME`/`IGNITION_PASSWORD`; no API token needed |

For deeper diagnostics, set `IGNITION_LOG_LEVEL=DEBUG` and inspect stderr. The MCP protocol uses
stdout, so logs always go to stderr.

---

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run ignition-mcp --doctor
```

The test suite is fully offline: HTTP is mocked with `respx`, and local installs are built in
temporary directories. CI runs lint, format, type checks and tests on Python 3.10–3.13.

See [`AGENTS.md`](AGENTS.md) for the architecture, conventions and agent workflow rules.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup and PR expectations.
See [`CHANGELOG.md`](CHANGELOG.md) for release notes.

---

## Project layout

```text
src/ignition_mcp/
├── __main__.py          # CLI, --doctor, transports
├── server.py            # MCP bootstrap and module registration
├── mcp_compat.py        # MCP SDK v1/v2 compatibility
├── config.py            # env → pyproject → ~/.ignition/mcp.json
├── state.py             # session, tool envelope, write guard, audit
├── safety.py            # snapshots, restore, diff previews
├── capabilities.py      # OpenAPI → feature matrix
├── doctor.py            # --doctor diagnostics
├── autoconnect/         # discovery chain + local install reader
├── client/              # Gateway HTTP client
└── modules/             # one domain per file: tags.py, perspective.py, ...
```

Module files expose a single `register(mcp, state)` function. Tool names are the public API and
stay stable.

---

## Limitations

- **REST coverage is best-effort.** Ignition's API surface varies by version and edition. Tools
  use candidate endpoints and return `unsupported` when none match; `/openapi.json` is the
  authority.
- **No live gateway in CI.** HTTP behaviour is tested with mocks. Verify against your own gateway
  with `--doctor` and a few read calls before relying on writes.
- **Phase 4 is stubbed.** Devices/OPC-UA and Simulators tools are registered but return
  `unsupported`.
- **Snapshots are local-file only.** Live tag values are verified by read-back but cannot be
  restored from a snapshot.
- **Local writes are filesystem writes.** Depending on your project setup, Ignition may need a
  project reload or deploy before a running session sees the change.
- **Trial state is runtime-only.** It cannot be read from local project files; use
  `gateway_trial_status` against a running gateway. Reset is refused before expiry.

---

## License

MIT
