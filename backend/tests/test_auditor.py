from __future__ import annotations

import pytest

from agent_control.orchestration.auditor import AuditorService


class QueueProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[tuple[str, str]] = []

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append((system_prompt, user_prompt))
        return self.responses.pop(0)

    async def generate_multimodal_text(self, system_prompt, user_prompt, image_paths) -> str:
        raise NotImplementedError

    async def generate_structured(self, system_prompt, user_prompt, output_model, **_ignored_kwargs):
        raise NotImplementedError


class RaisingProvider:
    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("provider down")

    async def generate_multimodal_text(self, system_prompt, user_prompt, image_paths) -> str:
        raise NotImplementedError

    async def generate_structured(self, system_prompt, user_prompt, output_model, **_ignored_kwargs):
        raise NotImplementedError


def test_is_content_tool_matches_the_old_synthesizer_gate() -> None:
    assert AuditorService.is_content_tool("browser.open")
    assert AuditorService.is_content_tool("filesystem.manage")
    assert not AuditorService.is_content_tool("task.status")
    assert not AuditorService.is_content_tool("schedule.manage")
    assert not AuditorService.is_content_tool(None)


@pytest.mark.asyncio
async def test_audit_returns_grounded_answer_when_sufficient() -> None:
    provider = QueueProvider(["The invoice total is $250.00."])
    auditor = AuditorService(provider)

    result = await auditor.audit("what is the invoice total?", "Invoice #4471 - $250.00")

    assert result.sufficient is True
    assert result.answer == "The invoice total is $250.00."
    assert "what is the invoice total?" in provider.prompts[0][1]


@pytest.mark.asyncio
async def test_audit_returns_gap_reason_when_insufficient() -> None:
    provider = QueueProvider(["INSUFFICIENT: only 2 of 5 requested episodes present in raw output"])
    auditor = AuditorService(provider)

    result = await auditor.audit("list the first 5 episodes", "1. Pilot\n2. Second episode")

    assert result.sufficient is False
    assert result.answer is None
    assert "2 of 5" in (result.reason or "")


@pytest.mark.asyncio
async def test_audit_empty_raw_output_is_insufficient_without_calling_provider() -> None:
    provider = QueueProvider([])
    auditor = AuditorService(provider)

    result = await auditor.audit("what happened?", "   ")

    assert result.sufficient is False
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_audit_fails_open_on_provider_error() -> None:
    auditor = AuditorService(RaisingProvider())

    result = await auditor.audit("what happened?", "some raw content")

    assert result.sufficient is True
    assert result.answer is None
    assert result.reason == "auditor_error"


@pytest.mark.asyncio
async def test_audit_passes_original_message_for_language_and_terminology() -> None:
    provider = QueueProvider(["ok"])
    auditor = AuditorService(provider)

    await auditor.audit("summarize the page", "raw html", original_message="Résume la page en français")

    assert "Résume la page en français" in provider.prompts[0][1]


@pytest.mark.asyncio
async def test_audit_passes_remembered_response_requirements() -> None:
    provider = QueueProvider(["- One\n- Two\n- Three\nConfidence: high"])
    auditor = AuditorService(provider)

    await auditor.audit(
        "summarize the report",
        "raw report",
        response_context="Remembered facts: exactly three bullets, then Confidence:",
    )

    assert "exactly three bullets" in provider.prompts[0][1]


# ---- an essay about the check is not an answer ------------------------------------------------------------------
# Live: a small model wrote "The objective is to calculate... 1. **Sufficiency Check**: ..." and the user was sent that.

COMMENTARY = (
    "The objective is to calculate the total order value from a specific Excel file.\n\n1.  **Sufficiency Check**:\n"
    "    *   **Data Presence**: The raw output successfully extracted text from `orders.xlsx` (Step 2)."
)


@pytest.mark.asyncio
async def test_commentary_about_the_audit_does_not_replace_the_operators_answer() -> None:
    provider = QueueProvider([COMMENTARY, COMMENTARY])
    result = await AuditorService(provider).audit("total the orders", "A2=keyboard B2=3 C2=40.0")

    assert len(provider.prompts) == 2  # it was asked once more for the verdict alone
    assert result.sufficient is True and result.answer is None  # the caller keeps the Operator's own answer
    assert result.reason == "auditor_wrote_commentary_not_an_answer"


@pytest.mark.asyncio
async def test_an_essay_followed_by_a_verdict_uses_the_verdict() -> None:
    insufficient = AuditorService(QueueProvider([COMMENTARY, "INSUFFICIENT: the screenshot was never sent"]))
    result = await insufficient.audit("open the page and send me a screenshot", "page opened")
    assert result.sufficient is False and "screenshot was never sent" in result.reason

    answered = AuditorService(QueueProvider([COMMENTARY, "The page is titled Agent Control E2E Site."]))
    result = await answered.audit("what page opened", "title: Agent Control E2E Site")
    assert result.sufficient is True and result.answer == "The page is titled Agent Control E2E Site."


@pytest.mark.asyncio
@pytest.mark.parametrize("essay", [
    "The objective requires three things:\n1.  **Describe the page content**: the output has it.\n2.  **Send a screenshot**: not present.",
    "1.  **Count check**: five items needed.\n2.  **Topic alignment**: matches.",
    "The user's request asks for a file to be sent; the raw output does not show that.",
])
async def test_the_ways_an_auditor_writes_up_its_check_are_recognised(essay) -> None:
    from agent_control.orchestration.auditor import looks_like_audit_commentary

    assert looks_like_audit_commentary(essay) is True


@pytest.mark.parametrize(
    "text",
    [
        "The invoice total is $250.00.",
        "Revenue grew twelve percent and support tickets fell by a third.",
        "The objective was met: the file is at C:/notes/a.txt.",  # mentions the word but is an answer
        "Steps to reproduce are in the README.",
    ],
)
@pytest.mark.asyncio
async def test_a_real_answer_is_still_used(text) -> None:
    result = await AuditorService(QueueProvider([text])).audit("what is it", "raw")
    assert result.sufficient is True and result.answer == text
