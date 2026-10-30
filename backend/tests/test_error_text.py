"""An error a human reads must never be empty.

`operator_decide_failed: ` with nothing after the colon is what three fixture
recordings reported while failing reproducibly; it read as a flake and was
retried instead of investigated.
"""
from __future__ import annotations

from agent_control.error_text import describe_exception, explain_tool_input_error


class SilentError(Exception):
    """Raised bare, so str() is empty - the case that started this."""


def test_message_is_kept_with_its_type() -> None:
    assert describe_exception(ValueError("bad input")) == "ValueError: bad input"


def test_empty_message_falls_back_to_the_type_name() -> None:
    assert describe_exception(SilentError()) == "SilentError"


def test_never_returns_an_empty_string() -> None:
    for exc in (SilentError(), ValueError(""), RuntimeError(), TimeoutError()):
        assert describe_exception(exc).strip(), type(exc).__name__


def test_empty_message_borrows_the_cause() -> None:
    """`raise X from Y` leaves Y reachable; when X says nothing, Y usually does."""
    try:
        try:
            raise ConnectionResetError("peer closed the connection")
        except ConnectionResetError as inner:
            raise SilentError from inner
    except SilentError as exc:
        described = describe_exception(exc)

    assert "SilentError" in described
    assert "peer closed the connection" in described


def test_long_message_is_truncated_but_keeps_the_type() -> None:
    described = describe_exception(ValueError("x" * 5000), limit=100)

    assert len(described) == 100
    assert described.startswith("ValueError: ")


_MISSING_QUERY = (
    "invalid input for filesystem.manage: 1 validation error for FilesystemSearchInput\n"
    "query\n"
    "  Field required [type=missing, input_value={'operation': 'search', 'root': 'desktop'}, input_type=dict]\n"
    "    For further information visit https://errors.pydantic.dev/2.14/v/missing"
)


def test_a_missing_tool_argument_reads_as_a_sentence_without_the_report() -> None:
    said = explain_tool_input_error(_MISSING_QUERY)

    assert said == "The filesystem.manage step was missing required input (query)."
    assert "pydantic" not in said and "type=missing" not in said and "input_value" not in said


def test_missing_and_unusable_arguments_are_both_named() -> None:
    report = (
        "invalid input for filesystem.manage: 2 validation errors for FilesystemReadInput\n"
        "path\n"
        "  Field required [type=missing, input_value={}, input_type=dict]\n"
        "    For further information visit https://errors.pydantic.dev/2.14/v/missing\n"
        "max_bytes\n"
        "  Input should be a valid integer [type=int_parsing, input_value='lots', input_type=str]\n"
        "    For further information visit https://errors.pydantic.dev/2.14/v/int_parsing"
    )

    assert explain_tool_input_error(report) == (
        "The filesystem.manage step was missing required input (path) and was given an unusable value for max_bytes."
    )


def test_other_errors_pass_through_unchanged() -> None:
    for text in ("path is outside allowed roots: C:/Windows", "", "no such column: revoked"):
        assert explain_tool_input_error(text) == text
