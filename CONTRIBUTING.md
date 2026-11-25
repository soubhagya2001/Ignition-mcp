# Contributing to Ignition MCP

Thanks for helping. This project is built to be developed *with* AI agents, so the rules are
written down and enforced by tooling. Read [`AGENTS.md`](AGENTS.md) first — it is the source of
truth for architecture and conventions.

## Development setup

```bash
git clone https://github.com/your-org/ignition-mcp
cd ignition-mcp
uv sync                 # creates .venv and installs runtime + dev dependencies
uv run pytest -q        # 100% offline; no gateway required
```

Optional: point the server at a gateway while developing.

```bash
export IGNITION_GATEWAY_URL=http://localhost:8088
export IGNITION_READONLY=false   # enable write tools
uv run ignition-mcp
```

## Definition of done

A change is complete when:

1. `uv run ruff check src tests` passes.
2. `uv run ruff format --check src tests` passes.
3. `uv run mypy src` passes.
4. `uv run pytest` passes.
5. New behaviour has a test that fails without the change.
6. `CHANGELOG.md` has an `Unreleased` entry for anything user-visible.
7. `README.md` and `AGENTS.md` reflect changed behaviour or tool signatures.

## Code standards

- Python 3.10+, full type hints, `from __future__ import annotations`.
- Google-style docstrings; explain *why* in comments, not *what*.
- Tools must return `state.tool_response(...)` and must not leak exceptions.
- Write tools must go through `state.write_guard(...)` and support `dry_run`.
- Prefer local filesystem reads; only use HTTP when local access is unavailable or a live
  change is required.
- Never log or persist credentials. Log to stderr via `logging`, never `print()`.

## Commits and pull requests

- Conventional commit subjects: `feat(tags): add udt instance lookup`, `fix(server): ...`,
  `docs(agents): ...`, `test(...)`, `chore(...)`, `refactor(...)`.
- One logical change per commit; keep diffs reviewable.
- PR description: what changed, why, how it was verified, and which gateway version/edition
  was used if you tested against a real gateway. State clearly when something is unverified.
- Do not commit `.env`, gateway backups (`.gwbk`), exports (`.zip`) or credentials.

## Adding a module

1. Create `src/ignition_mcp/modules/<domain>.py` with a single `register(mcp, state)` function.
2. Add the module name to `MODULES` in `server.py` (registration honours `IGNITION_MODULES`).
3. Document the tools in `README.md` (tool catalog).
4. Add tests under `tests/`.

## Reporting bugs

Include: MCP client + version, Python version, gateway version/edition, `IGNITION_*` variables
set (redact values), the tool called, and the returned payload. Set `IGNITION_LOG_LEVEL=DEBUG`
and include stderr logs.
