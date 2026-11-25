"""Tests for the Scripting helpers."""

from __future__ import annotations

from ignition_mcp.modules.scripting import (
    _analyze_script,
    _generate_scaffold,
    _get_scope_hints,
    _script_dependencies,
    _script_functions,
    _validate_python_syntax,
)


def test_validate_python_syntax_valid():
    """Valid Python passes the syntax check."""
    assert _validate_python_syntax("def foo():\n    return 42\n") == []


def test_validate_python_syntax_invalid():
    """Invalid Python reports a SyntaxError with a line number."""
    issues = _validate_python_syntax("def foo(\n    return 42\n")

    assert issues
    assert "SyntaxError" in issues[0]


def test_get_scope_hints_gateway():
    """Gateway scope advertises system functions."""
    hints = _get_scope_hints("gateway")
    assert "system.*" in hints["available"]


def test_get_scope_hints_unknown():
    """Unknown scopes list the valid ones."""
    hints = _get_scope_hints("not_a_scope")
    assert "valid_scopes" in hints


def test_generate_scaffold():
    """Generated scaffolds carry the scope and a TODO marker."""
    code = _generate_scaffold("Run a timer", "gateway", None)

    assert "gateway" in code.lower()
    assert "TODO" in code


def test_generate_scaffold_tag_change_template():
    """Template hints add the matching skeleton."""
    code = _generate_scaffold("On tag change", "tag_event", "tag_change")
    assert "currentValue" in code


def test_script_functions():
    """Function definitions are extracted with arguments."""
    code = "def a(x, y):\n    return x\n\nasync def b():\n    return 1\n"

    functions = _script_functions(code)

    assert [function["name"] for function in functions] == ["a", "b"]
    assert functions[0]["args"] == ["x", "y"]


def test_script_dependencies():
    """Imports and system calls are detected."""
    code = "import system\nfrom java.util import Date\n\nsystem.tag.readBlocking(['[default]A'])\n"

    dependencies = _script_dependencies(code)

    assert "system" in dependencies["imports"]
    assert "java.util" in dependencies["imports"]
    assert dependencies["system_calls"] == ["system.tag.readBlocking"]


def test_analyze_script_reports_suggestions():
    """Analysis reports metrics and style suggestions."""
    code = "def f():\n    print('x')\n"

    analysis = _analyze_script(code)

    assert analysis["valid"] is True
    assert analysis["function_count"] == 1
    assert any("print" in suggestion for suggestion in analysis["suggestions"])


def test_analyze_invalid_script():
    """Analysis of invalid code reports the syntax issue."""
    analysis = _analyze_script("def broken(")
    assert analysis["valid"] is False
    assert analysis["issues"]


def test_jython_except_and_mixed_tabs_supported():
    """Ignition 8.3 Jython 2.7 except syntax and mixed tabs/spaces are analysed cleanly."""
    code = "def run():\n\ttry:\n\t    return 1\n\texcept Exception, err:\n\t    return str(err)\n"
    assert _validate_python_syntax(code) == []
    assert [fn["name"] for fn in _script_functions(code)] == ["run"]
    analysis = _analyze_script(code)
    assert analysis["valid"] is True
    assert analysis["function_count"] == 1
