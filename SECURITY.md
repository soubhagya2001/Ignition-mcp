# Security Policy

## Supported Versions

Only the latest release on the `main` branch is actively supported with security updates.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately to the maintainers via GitHub Security Advisories
(the **Security → Advisories → Report a vulnerability** button on the repository) or by
contacting the repository maintainers directly. Include:

- A description of the vulnerability and potential impact
- Steps to reproduce or a minimal proof of concept
- Any Ignition version / MCP client details if relevant

## Security Model Notes for Users

- **Read-only by default:** `IGNITION_READONLY` defaults to `true`. Write tools require
  explicit opt-in (`IGNITION_READONLY=false` or `mcp_enable_writes`) plus `confirm=True`.
- **Sensitive artifacts:** Gateway backups (`.gwbk`) and project exports (`.zip`) contain
  configuration and credentials. Never commit them or store them inside a public workspace.
- **Credentials:** Passwords, session cookies, and API tokens stay in memory and are never
  written to logs or tool response `_ctx` blocks. Prefer `IGNITION_PASSWORD_FILE` /
  OS keyring storage over plain-text environment variables where possible.
