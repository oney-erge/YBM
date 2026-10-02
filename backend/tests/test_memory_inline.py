"""Inline checks for the placeholder-path scrubber used by memory."""
from agent_control.channels.memory import DEFAULT_SUMMARY, _strip_placeholder_paths, memory_context
from agent_control.schemas import MemoryFact, MemorySource


def test_strips_me_path_keeps_basename():
    assert _strip_placeholder_paths(
        r"found and read C:\Users\me\Desktop\resume.pdf today"
    ) == "found and read resume.pdf today"


def test_strips_forward_slash_user_path():
    assert _strip_placeholder_paths(
        "opened C:/Users/user/Documents/report.docx for review"
    ) == "opened report.docx for review"


def test_leaves_real_path_alone():
    text = r"the real path C:\Users\alex\Desktop\foo.pdf"
    assert _strip_placeholder_paths(text) == text


def test_no_path_unchanged():
    assert _strip_placeholder_paths("no path here just text") == "no path here just text"


def test_strips_path_only_no_filename():
    # A path without filename → completely removed (empty replacement).
    assert _strip_placeholder_paths(r"go to C:\Users\me\Desktop now").strip() == "go to  now".strip() or \
           _strip_placeholder_paths(r"go to C:\Users\me\Desktop now").strip() == "go to now"


def test_memory_context_puts_remembered_facts_first_and_untrimmed():
    """docs/archive/UI_UX_AUDIT.md Phase 4: a fact someone deliberately remembered
    must not silently disappear because the rolling summary ran long -
    unlike the summary, the facts block isn't subject to max_chars."""
    long_summary = "x" * 50
    facts = [MemoryFact(category="preference", content="Always use metric units", source=MemorySource.USER_STATED)]

    context = memory_context({"summary": long_summary, "facts": {}}, max_chars=10, remembered_facts=facts)

    assert context.startswith("Remembered facts:\n- [preference] Always use metric units")
    assert len(context) > 10  # would have been truncated to 10 chars without the facts carve-out


def test_memory_context_without_facts_is_unchanged_from_before():
    context = memory_context(None)
    assert context == DEFAULT_SUMMARY

    context_with_record = memory_context({"summary": "existing summary", "facts": {}})
    assert context_with_record == "existing summary"
