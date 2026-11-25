"""Tests for Ignition-aware script indentation validation."""

from __future__ import annotations

from ignition_mcp.modules.scripting import _format_script_code, _validate_script_indentation


def test_event_script_with_leading_tab_is_valid():
    """Perspective event scripts are function bodies and may start with a tab."""
    result = _validate_script_indentation(
        "\tsystem.perspective.closePopup(self.view.params.id)", "perspective_event"
    )

    assert result["valid"] is True
    assert result["indentation_valid"] is True


def test_transform_with_leading_tab_and_return_is_valid():
    """Script transforms require a return and accept the Designer's tab indent."""
    result = _validate_script_indentation("\treturn value * 2", "script_transform")

    assert result["valid"] is True


def test_transform_without_return_is_reported_separately():
    """A missing transform return is not reported as an indentation error."""
    result = _validate_script_indentation("\tvalue * 2", "script_transform")

    assert result["valid"] is False
    assert any(issue["code"] == "missing_transform_return" for issue in result["issues"])


def test_project_library_rejects_leading_indent():
    """Project library modules are parsed as modules, not function bodies."""
    result = _validate_script_indentation("\tdef foo():\n\t\treturn 1\n", "project_library")

    assert result["valid"] is False
    assert any(issue["code"] == "unexpected_indent" for issue in result["issues"])


def test_property_change_and_message_handler_contexts():
    """Property-change and message-handler scripts accept their real argument contexts."""
    property_change = _validate_script_indentation(
        "\tif currentValue.value > 10:\n\t\tself.props.style.color = '#f00'",
        "property_change",
    )
    message_handler = _validate_script_indentation(
        "\tself.getSibling('Repeater').refreshBinding('props.instances')",
        "message_handler",
    )

    assert property_change["valid"] is True
    assert message_handler["valid"] is True


def test_mixed_tabs_and_spaces_are_reported():
    """Mixed whitespace on one line produces a structured diagnostic."""
    result = _validate_script_indentation("\tif True:\n \t   pass\n", "perspective_event")

    assert result["valid"] is False
    assert any(issue["code"] == "mixed_tabs_spaces" for issue in result["issues"])


def test_crlf_is_normalized():
    """Windows line endings do not create false indentation errors."""
    result = _validate_script_indentation("\tif True:\r\n\t\tpass\r\n", "perspective_event")

    assert result["valid"] is True


def test_format_script_preserves_context_indent():
    """Formatting a transform body uses the requested tab unit."""
    formatted = _format_script_code("    return value * 2", "script_transform", "tab")

    assert formatted.startswith("\t")


def test_format_script_preserves_multiline_string_indentation():
    """Formatting does not rewrite indentation inside a triple-quoted string."""
    code = '\tvalue = """\n\tkeep me\n\t"""\n\treturn value'

    formatted = _format_script_code(code, "script_transform", "tab")

    assert "\tkeep me" in formatted
    assert formatted.count("\tkeep me") == 1
