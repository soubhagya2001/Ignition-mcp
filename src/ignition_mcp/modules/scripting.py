"""Ignition scripting.

Project library scripts live at
``data/projects/<project>/ignition/script-python/<module>/code.py`` and are read/written
directly on disk when a local install is available. Static analysis (syntax, functions,
imports, complexity) is pure Python and needs no gateway; only the Script Console tools
require one.

Script scopes carry different context objects, so every generation/validation tool accepts a
``scope`` argument and reports the objects available in that scope.
"""

from __future__ import annotations

import ast
import difflib
import io
import logging
import re
import textwrap
import tokenize
from typing import Any

from ignition_mcp.client.gateway_api import GatewayError
from ignition_mcp.mcp_compat import MCPServer
from ignition_mcp.modules.common import error_response, ok, resolve_project, unsupported
from ignition_mcp.safety import text_diff
from ignition_mcp.state import ServerState

logger = logging.getLogger(__name__)

VALID_SCOPES = (
    "gateway",
    "perspective_session",
    "perspective_component",
    "vision_client",
    "tag_event",
    "project_library",
    "named_query",
)

CONSOLE_CANDIDATES = ("/scripting/console", "/script/console", "/gateway/scripting/console")

#: Ignition scripts are stored in different execution contexts.  ``kind`` controls whether the
#: code is parsed as a module or wrapped in a function body; ``args`` supplies the names that
#: Ignition makes available to that body.
SCRIPT_CONTEXTS: dict[str, dict[str, Any]] = {
    "project_library": {"kind": "module", "args": [], "return_required": False},
    "script_console": {"kind": "module", "args": [], "return_required": False},
    "perspective_event": {
        "kind": "function",
        "args": ["self", "event"],
        "return_required": False,
    },
    "vision_event": {"kind": "function", "args": ["self", "event"], "return_required": False},
    "script_transform": {
        "kind": "function",
        "args": ["self", "value", "quality", "timestamp"],
        "return_required": True,
    },
    "property_change": {
        "kind": "function",
        "args": ["self", "previousValue", "currentValue", "origin", "missedEvents"],
        "return_required": False,
    },
    "message_handler": {
        "kind": "function",
        "args": ["self", "payload"],
        "return_required": False,
    },
    "tag_event": {
        "kind": "function",
        "args": ["initialChange", "currentValue", "previousValue", "tagPath"],
        "return_required": False,
    },
    "gateway_event": {"kind": "function", "args": [], "return_required": False},
    "session_event": {
        "kind": "function",
        "args": ["session", "page", "event"],
        "return_required": False,
    },
}

_INDENTATION_CODES = {
    "unexpected_indent",
    "expected_indent",
    "unindent_mismatch",
    "mixed_tabs_spaces",
    "inconsistent_indent_width",
    "tab_after_space",
    "space_after_tab",
    "indentation_error",
}


def register(mcp: MCPServer, state: ServerState) -> None:
    """Register the module tools.

    Args:
        mcp: MCP server instance.
        state: Shared server state.
    """

    @mcp.tool()
    async def list_project_scripts(project: str | None = None) -> dict[str, Any]:
        """List project library script modules.

        Args:
            project: Project name; defaults to the active project.

        Returns:
            Dotted module paths such as ``MyModule.SubModule``.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is not None:
            scripts = reader.list_scripts(proj or "")
            return ok(
                state,
                "list_project_scripts",
                {
                    "project": proj,
                    "scripts": scripts,
                    "count": len(scripts),
                    "source": "local_filesystem",
                },
            )
        return unsupported(
            state,
            "list_project_scripts",
            hint="Project scripts are read from disk; set IGNITION_INSTALL_PATH.",
        )

    @mcp.tool()
    async def read_project_script(module_path: str, project: str | None = None) -> dict[str, Any]:
        """Read a project library script.

        Args:
            module_path: Dotted module path, e.g. ``MyModule.SubModule``.
            project: Project name; defaults to the active project.

        Returns:
            The script source and a quick syntax check.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "read_project_script",
                hint="Project scripts are read from disk; set IGNITION_INSTALL_PATH.",
            )
        code = reader.read_script(proj or "", module_path)
        if code is None:
            return state.tool_response(
                {"status": "not_found", "module": module_path, "project": proj}
            )
        return ok(
            state,
            "read_project_script",
            {
                "project": proj,
                "module": module_path,
                "code": code,
                "issues": _validate_python_syntax(code),
            },
        )

    @mcp.tool()
    async def write_project_script(
        module_path: str,
        code: str,
        project: str | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or replace a project library script.

        Args:
            module_path: Dotted module path, e.g. ``MyModule.NewModule``.
            code: Python/Jython source.
            project: Project name; defaults to the active project.
            confirm: Must be ``True`` to write.
            dry_run: Validate and preview without writing.

        Returns:
            Write acknowledgement, or validation issues in dry-run mode.
        """
        guard = state.write_guard("write_project_script", confirm, dry_run)
        if guard:
            return guard
        proj, error = resolve_project(state, project)
        if error:
            return error

        reader = state.local_reader
        current = reader.read_script(proj or "", module_path) if reader is not None else None
        issues = _validate_python_syntax(code)
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "project": proj,
                    "module": module_path,
                    "valid": not issues,
                    "issues": issues,
                    "line_count": len(code.splitlines()),
                    "diff": text_diff(current or "", code, module_path),
                }
            )
        if issues:
            return state.tool_response(
                {"status": "invalid_script", "issues": issues, "module": module_path}
            )

        if reader is not None:
            path = reader.write_script(proj or "", module_path, code)
            if path is not None:
                verified = reader.read_script(proj or "", module_path) == code
                state.record_action("write_project_script", "ok", project=proj, module=module_path)
                return state.tool_response(
                    {
                        "status": "written",
                        "project": proj,
                        "module": module_path,
                        "path": str(path),
                        "source": "local_filesystem",
                        "verified": verified,
                    }
                )
        return unsupported(
            state,
            "write_project_script",
            hint=(
                "Script writes need a local install (IGNITION_INSTALL_PATH). The gateway API "
                "does not expose project-library writes on all editions."
            ),
        )

    @mcp.tool()
    async def validate_script_syntax(
        code: str,
        scope: str = "gateway",
        context: str | None = None,
        check_indentation: bool = True,
    ) -> dict[str, Any]:
        """Validate Python/Jython syntax locally.

        Args:
            code: Script source.
            scope: Script scope, used for context hints.
            context: Optional explicit Ignition script context. When omitted, it is inferred
                from ``scope``.
            check_indentation: Include context-aware indentation diagnostics.

        Returns:
            Validity, structured diagnostics and the objects available in the scope.
        """
        resolved_context = context or _context_for_scope(scope)
        result = _validate_script_indentation(code, resolved_context)
        issues = (
            [
                f"{issue['code']} at line {issue['line']}: {issue['message']}"
                for issue in result["issues"]
            ]
            if check_indentation
            else _validate_python_syntax(code)
        )
        return ok(
            state,
            "validate_script_syntax",
            {
                "valid": not issues,
                "issues": issues,
                "diagnostics": result["issues"],
                "context": resolved_context,
                "scope": scope,
                "scope_hints": _get_scope_hints(scope),
                "indentation": result.get("indentation"),
            },
        )

    @mcp.tool()
    async def check_script_indentation(
        code: str,
        context: str = "perspective_event",
        indent_unit: str = "auto",
    ) -> dict[str, Any]:
        """Check Ignition script indentation in its real execution context.

        Args:
            code: Script source, already decoded from ``view.json``.
            context: One of :data:`SCRIPT_CONTEXTS`.
            indent_unit: ``auto``, ``tab``, ``2`` or ``4``.

        Returns:
            Validity, indentation metadata and structured diagnostics.
        """
        result = _validate_script_indentation(code, context, indent_unit)
        return ok(state, "check_script_indentation", result)

    @mcp.tool()
    async def format_ignition_script(
        code: str,
        context: str = "perspective_event",
        indent_unit: str = "auto",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Preview or produce a normalized Ignition script body.

        This tool formats an in-memory string; it does not write a project file.  A confirmed
        result can be passed to a targeted write tool, which performs the snapshot and read-back.

        Args:
            code: Script source.
            context: One of :data:`SCRIPT_CONTEXTS`.
            indent_unit: ``auto``, ``tab``, ``2`` or ``4``.
            dry_run: Return a diff instead of the formatted string.
            confirm: Must be ``True`` to return the formatted string when not dry-running.

        Returns:
            A diff preview, a confirmation request, or the formatted code.
        """
        result = _validate_script_indentation(code, context, indent_unit)
        formatted = _format_script_code(code, context, indent_unit)
        if dry_run:
            return state.tool_response(
                {
                    "status": "dry_run",
                    "context": context,
                    "valid_before": result["valid"],
                    "issues": result["issues"],
                    "diff": text_diff(code, formatted, f"{context}.py"),
                    "formatted": formatted,
                }
            )
        if not confirm:
            return state.tool_response(
                {
                    "status": "requires_confirmation",
                    "operation": "format_ignition_script",
                    "message": "Call again with confirm=True to receive the formatted code.",
                }
            )
        return state.tool_response(
            {
                "status": "formatted",
                "context": context,
                "code": formatted,
                "valid": _validate_script_indentation(formatted, context, indent_unit)["valid"],
            }
        )

    @mcp.tool()
    async def search_script_functions(query: str, project: str | None = None) -> dict[str, Any]:
        """Search for function definitions across project scripts.

        Args:
            query: Function name or fragment (case-insensitive).
            project: Project name; defaults to the active project.

        Returns:
            Matches with module, function name, line and arguments.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "search_script_functions",
                hint="Function search needs local project files (IGNITION_INSTALL_PATH).",
            )

        matches: list[dict[str, Any]] = []
        for module in reader.list_scripts(proj or ""):
            code = reader.read_script(proj or "", module) or ""
            for function in _script_functions(code):
                if query.lower() in function["name"].lower():
                    matches.append({"module": module, **function})
        return ok(
            state,
            "search_script_functions",
            {"query": query, "project": proj, "matches": matches, "count": len(matches)},
        )

    @mcp.tool()
    async def find_script_dependencies(
        module_path: str, project: str | None = None
    ) -> dict[str, Any]:
        """Find imports and system calls used by a script module.

        Args:
            module_path: Dotted module path.
            project: Project name; defaults to the active project.

        Returns:
            Imports, ``system.*`` calls and functions defined in the module.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "find_script_dependencies",
                hint="Dependency analysis needs local project files (IGNITION_INSTALL_PATH).",
            )
        code = reader.read_script(proj or "", module_path)
        if code is None:
            return state.tool_response({"status": "not_found", "module": module_path})
        return ok(
            state,
            "find_script_dependencies",
            {"project": proj, "module": module_path, **_script_dependencies(code)},
        )

    @mcp.tool()
    async def find_script_references(
        function_name: str, project: str | None = None
    ) -> dict[str, Any]:
        """Find where a function or symbol is referenced in a project.

        Args:
            function_name: Symbol to search for.
            project: Project name; defaults to the active project.

        Returns:
            Matching resources and lines across the project and its parents.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "find_script_references",
                hint="Reference search needs local project files (IGNITION_INSTALL_PATH).",
            )
        chain = reader.inheritance_chain(proj or "")
        matches = reader.search_text(proj or "", function_name, projects=chain[1:], max_results=300)
        return ok(
            state,
            "find_script_references",
            {
                "function": function_name,
                "project": proj,
                "matches": matches,
                "count": len(matches),
            },
        )

    @mcp.tool()
    async def analyze_script(module_path: str, project: str | None = None) -> dict[str, Any]:
        """Statically analyse a script: functions, imports, complexity and issues.

        Args:
            module_path: Dotted module path.
            project: Project name; defaults to the active project.

        Returns:
            Analysis results and improvement suggestions.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        reader = state.local_reader
        if reader is None:
            return unsupported(
                state,
                "analyze_script",
                hint="Script analysis needs local project files (IGNITION_INSTALL_PATH).",
            )
        code = reader.read_script(proj or "", module_path)
        if code is None:
            return state.tool_response({"status": "not_found", "module": module_path})
        return ok(
            state,
            "analyze_script",
            {"project": proj, "module": module_path, **_analyze_script(code)},
        )

    @mcp.tool()
    async def generate_ignition_script(
        description: str,
        scope: str = "gateway",
        template: str | None = None,
    ) -> dict[str, Any]:
        """Generate a scope-aware script scaffold.

        Args:
            description: What the script should do, in plain language.
            scope: Script scope (gateway, perspective_component, tag_event, ...).
            template: Optional template hint such as ``tag_change`` or ``timer``.

        Returns:
            A scaffold with the correct context comment and a TODO marker.
        """
        code = _generate_scaffold(description, scope, template)
        return ok(
            state,
            "generate_ignition_script",
            {"scope": scope, "code": code, "scope_hints": _get_scope_hints(scope)},
        )

    @mcp.tool()
    async def compare_script_versions(
        module_path: str,
        code_a: str | None = None,
        code_b: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Diff two versions of a script.

        Args:
            module_path: Module path used to label the diff and load the current version.
            code_a: Original code; defaults to the on-disk version.
            code_b: New code; defaults to an empty string when omitted.
            project: Project name; defaults to the active project.

        Returns:
            A unified diff and a summary of added/removed lines.
        """
        proj, error = resolve_project(state, project)
        if error:
            return error
        if code_a is None:
            reader = state.local_reader
            code_a = reader.read_script(proj or "", module_path) if reader else None
        if code_a is None:
            return state.tool_response(
                {"status": "not_found", "module": module_path, "message": "No base version."}
            )

        diff = list(
            difflib.unified_diff(
                code_a.splitlines(),
                (code_b or "").splitlines(),
                fromfile=f"{module_path} (a)",
                tofile=f"{module_path} (b)",
                lineterm="",
            )
        )
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        return ok(
            state,
            "compare_script_versions",
            {
                "module": module_path,
                "diff": "\n".join(diff),
                "added_lines": added,
                "removed_lines": removed,
                "identical": not diff,
            },
        )

    @mcp.tool()
    async def access_script_console(
        code: str,
        project: str | None = None,
        scope: str = "gateway",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute code in the Gateway Script Console.

        Args:
            code: Jython code to execute.
            project: Project context.
            scope: Execution scope.
            confirm: Must be ``True`` to execute.
            dry_run: Validate the code without executing it.

        Returns:
            Execution output, or a guard/preview payload.
        """
        guard = state.write_guard("access_script_console", confirm, dry_run)
        if guard:
            return guard
        issues = _validate_python_syntax(code)
        if dry_run:
            return state.tool_response(
                {"status": "dry_run", "valid": not issues, "issues": issues, "scope": scope}
            )
        if issues:
            return state.tool_response({"status": "invalid_script", "issues": issues})
        proj, error = resolve_project(state, project)
        if error:
            return error
        try:
            result = await _execute_console(state, code, proj or "", scope)
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "access_script_console")
        state.record_action("access_script_console", "ok", project=proj, scope=scope)
        return state.tool_response({"status": "executed", "result": result})

    @mcp.tool()
    async def test_code_snippet(
        code: str,
        project: str | None = None,
        expected_result: Any | None = None,
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a snippet and compare the result with an expected value.

        Args:
            code: Jython snippet to run.
            project: Project context.
            expected_result: Optional value to compare against the returned result.
            confirm: Must be ``True`` to execute.
            dry_run: Validate without executing.

        Returns:
            The result, whether it matched, and any error text.
        """
        guard = state.write_guard("test_code_snippet", confirm, dry_run)
        if guard:
            return guard
        issues = _validate_python_syntax(code)
        if dry_run:
            return state.tool_response({"status": "dry_run", "valid": not issues, "issues": issues})
        if issues:
            return state.tool_response({"status": "invalid_script", "issues": issues})
        proj, error = resolve_project(state, project)
        if error:
            return error
        try:
            result = await _execute_console(state, code, proj or "", "gateway")
        except GatewayError as exc:
            return _gateway_or_unsupported(state, exc, "test_code_snippet")

        payload: dict[str, Any] = {"status": "executed", "result": result}
        if expected_result is not None:
            payload["expected"] = expected_result
            payload["matched"] = result == expected_result
        return state.tool_response(payload)


# ----------------------------------------------------------------------
# Gateway execution
# ----------------------------------------------------------------------


async def _execute_console(state: ServerState, code: str, project: str, scope: str) -> Any:
    """POST a script to the gateway's Script Console endpoint.

    Raises:
        GatewayError: When the gateway is unavailable or rejects the script.
    """
    if not state.has_gateway:
        raise GatewayError("Script execution requires a gateway connection.")
    _, payload = await state.gateway.first_available(
        CONSOLE_CANDIDATES,
        method="POST",
        json_body={"script": code, "project": project, "scope": scope},
    )
    return payload


# ----------------------------------------------------------------------
# Pure helpers (unit tested)
# ----------------------------------------------------------------------


_JYTHON_EXCEPT_RE = re.compile(
    r"^([ \t]*except\s+.+?)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$",
    re.MULTILINE,
)


def _normalize_jython_for_ast(code: str) -> str:
    """Normalise common Jython 2.7 constructs so Python 3 ``ast.parse`` can inspect them.

    Ignition 8.3 runs Jython 2.7, which permits ``except Type, var:`` and mixed
    tabs/spaces (tabs expanded to 8 spaces). Normalising those lets static analysis
    extract functions, imports, and ``system.*`` calls without false failures.
    """
    normalized = code.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _JYTHON_EXCEPT_RE.sub(r"\1 as \2:\3", normalized)
    return normalized.expandtabs(8)


def _parse_jython_ast(code: str) -> ast.Module:
    """Parse ``code`` with ``ast.parse``, falling back to Jython 2.7 normalisation."""
    try:
        return ast.parse(code)
    except SyntaxError:
        return ast.parse(_normalize_jython_for_ast(code))


def _validate_python_syntax(code: str) -> list[str]:
    """Validate Python / Jython 2.7 syntax with :func:`ast.parse`.

    This is the module-level compatibility helper.  Embedded Ignition scripts should use
    :func:`_validate_script_indentation`, which understands function-body contexts.

    Args:
        code: Source code to check.

    Returns:
        Error strings; empty when the syntax is valid.
    """
    try:
        _parse_jython_ast(code)
    except SyntaxError as exc:
        return [f"SyntaxError at line {exc.lineno}: {exc.msg}"]
    return []


def _context_for_scope(scope: str) -> str:
    """Map the public script scope to an execution context.

    Args:
        scope: Existing ``VALID_SCOPES`` value.

    Returns:
        A key in :data:`SCRIPT_CONTEXTS`.
    """
    return {
        "gateway": "script_console",
        "perspective_session": "session_event",
        "perspective_component": "perspective_event",
        "vision_client": "vision_event",
        "tag_event": "tag_event",
        "project_library": "project_library",
        "named_query": "project_library",
    }.get(scope, "perspective_event")


def _validate_script_indentation(
    code: str, context: str = "perspective_event", indent_unit: str = "auto"
) -> dict[str, Any]:
    """Validate an Ignition script using its real execution context.

    Args:
        code: Decoded script source.
        context: One of :data:`SCRIPT_CONTEXTS`.
        indent_unit: ``auto``, ``tab``, ``2`` or ``4``.

    Returns:
        ``{valid, indentation_valid, context, issues, indentation}``.
    """
    if context not in SCRIPT_CONTEXTS:
        return {
            "valid": False,
            "indentation_valid": False,
            "context": context,
            "issues": [
                _issue(
                    "unknown_context",
                    f"Unknown script context '{context}'.",
                    hint=f"Use one of: {', '.join(sorted(SCRIPT_CONTEXTS))}.",
                )
            ],
            "indentation": {},
        }

    normalized = code.replace("\r\n", "\n").replace("\r", "\n")
    spec = SCRIPT_CONTEXTS[context]
    issues: list[dict[str, Any]] = []
    issues.extend(_indentation_issues(normalized, context))
    issues.extend(_syntax_issues(normalized, context))
    issues = _dedupe_issues(issues)
    if spec.get("return_required") and not _has_return(normalized, context):
        issues.append(
            _issue(
                "missing_transform_return",
                "A script transform must return a value.",
                hint="Add a return statement for the transformed value.",
            )
        )

    indentation_valid = not any(issue["code"] in _INDENTATION_CODES for issue in issues)
    return {
        "valid": not issues,
        "indentation_valid": indentation_valid,
        "context": context,
        "issues": issues,
        "indentation": _indentation_metadata(normalized, indent_unit),
    }


def _indentation_metadata(code: str, indent_unit: str) -> dict[str, Any]:
    """Return detected indentation metadata for a script."""
    prefixes = [_leading_whitespace(line) for line in code.splitlines() if line.strip()]
    non_empty = [prefix for prefix in prefixes if prefix]
    unit = indent_unit
    if unit == "auto":
        if any(prefix.startswith("\t") for prefix in non_empty):
            unit = "tab"
        elif any(len(prefix) >= 2 for prefix in non_empty):
            unit = "4" if any(len(prefix) == 4 for prefix in non_empty) else "2"
        else:
            unit = "tab"
    return {
        "unit": unit,
        "leading_margin": min((len(prefix) for prefix in non_empty), default=0),
        "mixed": any("\t" in prefix and " " in prefix for prefix in non_empty),
    }


def _indentation_issues(code: str, context: str) -> list[dict[str, Any]]:
    """Report mixed or suspicious indentation before parsing."""
    issues: list[dict[str, Any]] = []
    string_lines = _multiline_string_lines(code)
    for number, line in enumerate(code.splitlines(), start=1):
        if not line.strip() or number in string_lines:
            continue
        prefix = _leading_whitespace(line)
        if "\t" in prefix and " " in prefix:
            issues.append(
                _issue(
                    "mixed_tabs_spaces",
                    "This line mixes tabs and spaces for indentation.",
                    line=number,
                    column=1,
                    found=repr(prefix),
                    hint="Use tabs or spaces consistently within a block.",
                )
            )
    if context == "project_library" and code.splitlines():
        first = next((line for line in code.splitlines() if line.strip()), "")
        if first[:1] in (" ", "\t"):
            issues.append(
                _issue(
                    "unexpected_indent",
                    "A project library module must not start with indentation.",
                    line=1,
                    column=1,
                    found=repr(_leading_whitespace(first)),
                    hint="Remove the leading tab/space or use an embedded script context.",
                )
            )
    return issues


def _syntax_issues(code: str, context: str) -> list[dict[str, Any]]:
    """Compile the script in its context and map syntax errors back to source lines."""
    spec = SCRIPT_CONTEXTS[context]
    prepared, offset = _prepare_for_parse(code, context)
    try:
        compile(prepared, "<ignition-script>", "exec")
    except (IndentationError, TabError, SyntaxError) as exc:
        code_name = _syntax_code(exc, context)
        line = max(1, (getattr(exc, "lineno", 1) or 1) - offset)
        column = getattr(exc, "offset", None)
        message = str(getattr(exc, "msg", exc))
        if code_name == "syntax_error" and _looks_like_jython_2(code):
            code_name = "jython_syntax_mismatch"
            message = (
                f"{message}. Ignition 8.x uses Jython 2.7; this may be valid there even if "
                "CPython 3 rejects it."
            )
        return [
            _issue(
                code_name,
                message,
                line=line,
                column=column,
                found=spec["kind"],
                hint=_syntax_hint(code_name),
            )
        ]
    return []


def _prepare_for_parse(code: str, context: str) -> tuple[str, int]:
    """Normalize a script into a compilable module or synthetic function."""
    spec = SCRIPT_CONTEXTS[context]
    if spec["kind"] == "module":
        return code, 0
    body = textwrap.dedent(code)
    args = ", ".join(spec["args"])
    wrapper = f"def __ignition_script__({args}):\n"
    if not body.strip():
        wrapper += "    pass\n"
    else:
        wrapper += textwrap.indent(body, "    ")
        if not wrapper.endswith("\n"):
            wrapper += "\n"
    return wrapper, 1


def _syntax_code(exc: SyntaxError, context: str) -> str:
    """Map a parser exception to a stable diagnostic code."""
    if isinstance(exc, TabError):
        return "mixed_tabs_spaces"
    if isinstance(exc, IndentationError):
        message = str(getattr(exc, "msg", ""))
        if "unexpected indent" in message:
            return "unexpected_indent"
        if "expected an indented block" in message:
            return "expected_indent"
        if "unindent does not match" in message:
            return "unindent_mismatch"
        return "indentation_error"
    if _looks_like_jython_2(getattr(exc, "text", "") or ""):
        return "jython_syntax_mismatch"
    return "syntax_error"


def _syntax_hint(code: str) -> str:
    """Return an actionable hint for a diagnostic code."""
    return {
        "unexpected_indent": "Remove the extra indentation or use the correct script context.",
        "expected_indent": "Indent the block after the colon.",
        "unindent_mismatch": "Align this line with an enclosing block.",
        "mixed_tabs_spaces": "Use tabs or spaces consistently within a block.",
        "jython_syntax_mismatch": "Ignition 8.x runs Jython 2.7, not CPython 3.",
        "syntax_error": "Fix the syntax error before writing the script.",
    }.get(code, "Review the script and try again.")


def _has_return(code: str, context: str) -> bool:
    """Return whether a script contains a return statement."""
    prepared, _ = _prepare_for_parse(code, context)
    try:
        tree = ast.parse(prepared)
    except SyntaxError:
        return False
    return any(isinstance(node, ast.Return) for node in ast.walk(tree))


def _format_script_code(code: str, context: str, indent_unit: str) -> str:
    """Normalize a script's outer margin without touching string literals."""
    normalized = code.replace("\r\n", "\n").replace("\r", "\n")
    body = _dedent_code(normalized)
    spec = SCRIPT_CONTEXTS.get(context, {"kind": "function"})
    if spec["kind"] == "module":
        return body
    unit = indent_unit
    if unit == "auto":
        unit = "tab"
    prefix = "\t" if unit == "tab" else " " * (int(unit) if str(unit).isdigit() else 4)
    return _indent_code(body, prefix)


def _dedent_code(code: str) -> str:
    """Remove a common margin while leaving multi-line string contents alone."""
    lines = code.splitlines(keepends=True)
    protected = _multiline_string_content_lines(code)
    prefixes = [
        _leading_whitespace(line.rstrip("\n"))
        for number, line in enumerate(lines, start=1)
        if line.strip() and number not in protected
    ]
    if not prefixes:
        return code
    common = prefixes[0]
    for prefix in prefixes[1:]:
        while common and not prefix.startswith(common):
            common = common[:-1]
    if not common:
        return code
    return "".join(
        line[len(common) :] if number not in protected and line.startswith(common) else line
        for number, line in enumerate(lines, start=1)
    )


def _indent_code(code: str, prefix: str) -> str:
    """Indent code lines while leaving multi-line string contents untouched."""
    lines = code.splitlines(keepends=True)
    protected = _multiline_string_content_lines(code)
    return "".join(
        line if number in protected or not line.strip() else f"{prefix}{line}"
        for number, line in enumerate(lines, start=1)
    )


def _leading_whitespace(line: str) -> str:
    """Return the leading spaces/tabs of a line."""
    return line[: len(line) - len(line.lstrip(" \t"))]


def _multiline_string_lines(code: str) -> set[int]:
    """Return 1-based line numbers covered by multi-line string tokens."""
    return _multiline_string_content_lines(code, include_start=True)


def _multiline_string_content_lines(code: str, include_start: bool = False) -> set[int]:
    """Return line numbers whose content belongs to a multi-line string.

    The opening line is excluded by default because it also contains normal code before the
    string literal; its indentation can be safely normalized.
    """
    lines: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        for token in tokens:
            if token.type == tokenize.STRING and token.end[0] > token.start[0]:
                start = token.start[0] if include_start else token.start[0] + 1
                lines.update(range(start, token.end[0] + 1))
    except (IndentationError, tokenize.TokenError):
        return lines
    return lines


def _looks_like_jython_2(code: str) -> bool:
    """Detect common Jython 2.7 syntax that CPython 3 rejects."""
    return bool(
        re.search(r"(?m)^\s*print\s+[^()\n]+", code)
        or re.search(r"(?m)^\s*except\s+[^:]+,\s*\w+\s*:", code)
        or re.search(r"(?m)^\s*exec\s+[^()\n]+", code)
    )


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate diagnostics emitted by the pre-check and parser."""
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict[str, Any]] = []
    for issue in issues:
        key = (str(issue.get("code")), int(issue.get("line", 0)), str(issue.get("message")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _issue(
    code: str,
    message: str,
    *,
    line: int = 1,
    column: int | None = None,
    expected: str | None = None,
    found: str | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Build a structured script diagnostic."""
    issue: dict[str, Any] = {
        "code": code,
        "severity": "error",
        "message": message,
        "line": line,
        "column": column,
    }
    if expected:
        issue["expected"] = expected
    if found:
        issue["found"] = found
    if hint:
        issue["hint"] = hint
    return issue


def _get_scope_hints(scope: str) -> dict[str, Any]:
    """Return the objects available in an Ignition script scope.

    Args:
        scope: One of :data:`VALID_SCOPES`.

    Returns:
        A dict with ``available`` names and a usage ``note``.
    """
    hints: dict[str, dict[str, Any]] = {
        "gateway": {
            "available": ["system.*", "shared.*", "project.*"],
            "note": "Full gateway system functions; no UI access.",
        },
        "perspective_session": {
            "available": ["system.perspective.*", "session", "page"],
            "note": "Session scope; read session.custom and page.params.",
        },
        "perspective_component": {
            "available": ["self", "event", "session", "page", "view", "system.*"],
            "note": "Component event scope; 'self' is the component.",
        },
        "vision_client": {
            "available": ["system.gui.*", "system.nav.*", "client.*", "event"],
            "note": "Vision client scope; UI dialogs and navigation are available.",
        },
        "tag_event": {
            "available": [
                "system.*",
                "initialChange",
                "currentValue",
                "previousValue",
                "tagPath",
            ],
            "note": "Tag change scope; values are qualified values, not raw values.",
        },
        "project_library": {
            "available": ["system.*"],
            "note": "Module-level code imported by other scripts; no event context.",
        },
        "named_query": {
            "available": [],
            "note": "Named queries use SQL, not Python. Use SQL parameters instead.",
        },
    }
    if scope not in hints:
        return {"note": f"Unknown scope '{scope}'.", "valid_scopes": list(VALID_SCOPES)}
    return hints[scope]


def _generate_scaffold(description: str, scope: str, template: str | None) -> str:
    """Generate a scope-aware script scaffold.

    Args:
        description: What the script should do.
        scope: Target script scope.
        template: Optional template hint.

    Returns:
        Source code with a context comment and a TODO marker.
    """
    header = f'"""\n{description}\n\nScope: {scope}\n"""\n\n'
    scope_comments = {
        "gateway": "# Gateway scope — system.* functions are available.\n",
        "perspective_session": "# Perspective session scope — session.custom, page.params.\n",
        "perspective_component": "# Component event scope — self, event, session, page.\n",
        "vision_client": "# Vision client scope — system.gui.*, system.nav.*, client.*.\n",
        "tag_event": "# Tag change scope — currentValue/previousValue are qualified values.\n",
        "project_library": "# Project library module — import as 'Module.function'.\n",
        "named_query": "# Named query — write SQL, not Python.\n",
    }
    body = scope_comments.get(scope, f"# Unknown scope '{scope}'.\n")
    if template == "tag_change":
        body += "\nif currentValue.value != previousValue.value:\n    pass  # TODO: implement\n"
    elif template == "timer":
        body += "\ndef run():\n    # TODO: implement\n    pass\n"
    else:
        body += "\n# TODO: implement\n"
    return header + body


def _script_functions(code: str) -> list[dict[str, Any]]:
    """Return function definitions in a module.

    Returns:
        Entries with ``name``, ``line`` and ``args``.
    """
    try:
        tree = _parse_jython_ast(code)
    except SyntaxError:
        return []
    functions: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [arg.arg for arg in node.args.args]
            functions.append({"name": node.name, "line": node.lineno, "args": args})
    return functions


def _script_dependencies(code: str) -> dict[str, Any]:
    """Collect imports, ``system.*`` calls and defined functions.

    Returns:
        ``{"imports": [...], "system_calls": [...], "functions": [...]}``.
    """
    imports: list[str] = []
    system_calls: list[str] = []
    try:
        tree = _parse_jython_ast(code)
    except SyntaxError:
        return {"imports": [], "system_calls": [], "functions": []}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            chain = _attribute_chain(node.func)
            if chain and chain[0] == "system":
                system_calls.append(".".join(chain))
    return {
        "imports": sorted(set(imports)),
        "system_calls": sorted(set(system_calls)),
        "functions": [function["name"] for function in _script_functions(code)],
    }


def _attribute_chain(node: ast.Attribute) -> list[str]:
    """Return the dotted attribute chain for an AST attribute node."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


def _analyze_script(code: str) -> dict[str, Any]:
    """Produce static-analysis metrics and suggestions for a script.

    Returns:
        Functions, imports, branch count, line count and suggestions.
    """
    issues = _validate_python_syntax(code)
    if issues:
        return {"valid": False, "issues": issues, "functions": [], "complexity": 0}

    tree = _parse_jython_ast(code)
    branches = sum(
        isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp, ast.IfExp))
        for node in ast.walk(tree)
    )
    functions = _script_functions(code)
    dependencies = _script_dependencies(code)
    suggestions: list[str] = []
    if branches > 15:
        suggestions.append("High branch count — consider extracting helper functions.")
    if len(code.splitlines()) > 400:
        suggestions.append("Long module — split into smaller project library modules.")
    if re.search(r"\bprint\s*\(", code):
        suggestions.append("Use system.util.getLogger(...).info(...) instead of print().")
    if re.search(r"\bexcept\s*:", code):
        suggestions.append("Bare 'except:' hides errors; catch specific exceptions.")

    return {
        "valid": True,
        "line_count": len(code.splitlines()),
        "function_count": len(functions),
        "functions": functions,
        "complexity": branches,
        "imports": dependencies["imports"],
        "system_calls": dependencies["system_calls"],
        "suggestions": suggestions,
    }


def _gateway_or_unsupported(state: ServerState, exc: GatewayError, tool: str) -> dict[str, Any]:
    """Return an ``unsupported`` payload for 404s and a normal error otherwise."""
    if exc.status == 404:
        return unsupported(
            state,
            tool,
            hint=(
                "The Script Console API is not exposed on this gateway. Static analysis tools "
                "still work offline."
            ),
            tried=exc.payload,
        )
    return error_response(state, exc, tool)
