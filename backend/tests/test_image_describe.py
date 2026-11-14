"""Looking at a picture with a local vision model, with the user's approval (tools/image_describe.py)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent_control.config import AppSettings, CapabilityPolicy, LLMProfileConfig
from agent_control.schemas import Capability, RiskLevel, ToolCallRequest, ToolResultStatus
from agent_control.tools.image_describe import ImageDescribeAdapter, _size_in_billions, list_vision_profiles, localdeploy_root
from agent_control.tools.registry import build_tool_registry


def _settings(tmp_path: Path, **vision) -> AppSettings:
    return AppSettings(
        _env_file=None,
        llm={"default_profile": "main", "profiles": {"main": {"model": "qwen35_9b_llamacpp", "base_url": "http://127.0.0.1:8000/v1"}}},
        capabilities={Capability.FILESYSTEM_READ: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)},
        adapters={"computer_use": {"allowed_roots": [str(tmp_path)]}},
        vision={"enabled": True, **vision},
    )


class _Provider:
    def __init__(self, profile: LLMProfileConfig, answers: list[object]) -> None:
        self.profile, self.answers, self.calls = profile, answers, []

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        self.calls.append((system_prompt, user_prompt, image_paths))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return str(answer)


def _adapter(tmp_path, *, answers=("INVOICE 1234 TOTAL 56.78 USD",), profiles=None, **vision):
    seen = {}

    async def listing(root: str):
        seen["root"] = root
        if isinstance(profiles, Exception):
            raise profiles
        if callable(profiles):
            return profiles()
        return [{"profile": "qwen3vl_4b_ollama", "model": "qwen3-vl:4b-instruct", "backend": "ollama", "installed": True}] if profiles is None else profiles

    def factory(profile: LLMProfileConfig):
        seen["provider"] = _Provider(profile, list(answers))
        return seen["provider"]

    settings = _settings(tmp_path, **vision)
    guard = lambda value: _guard(tmp_path, value)  # noqa: E731
    return ImageDescribeAdapter(settings, guard, listing=listing, provider_factory=factory), seen


def _guard(root: Path, value: str) -> Path:
    path = Path(value).resolve()
    if root.resolve() not in path.parents:
        raise ValueError(f"path is outside allowed roots: {path}")
    return path


def _request(path: Path | str, **payload) -> ToolCallRequest:
    return ToolCallRequest(task_id="task_x", tool_name="image.describe", capability="filesystem.read", input={"path": str(path), **payload}, origin="operator")


def _picture(tmp_path: Path, name: str = "invoice.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return path


@pytest.mark.asyncio
async def test_a_picture_is_described_by_the_smallest_vision_model_localdeploy_has(tmp_path):
    adapter, seen = _adapter(tmp_path)
    result = await adapter.execute(_request(_picture(tmp_path), question="What is the total?"))
    assert result.status == ToolResultStatus.SUCCEEDED and result.output["description"] == "INVOICE 1234 TOTAL 56.78 USD"
    assert result.output["model"] == "qwen3vl_4b_ollama" and result.output["file"].endswith("invoice.png")
    assert seen["root"] == "http://127.0.0.1:8000"  # the default model's address, without /v1
    provider = seen["provider"]
    assert provider.profile.model == "qwen3vl_4b_ollama" and provider.profile.base_url == "http://127.0.0.1:8000/v1"
    system, question, images = provider.calls[0]
    assert "quote any text exactly" in system and question == "What is the total?" and images[0].endswith("invoice.png")
    plain, seen_plain = _adapter(tmp_path)
    await plain.execute(_request(_picture(tmp_path)))  # no question: describe it and quote its text
    assert seen_plain["provider"].calls[0][1] == "Describe this picture and quote any text that is in it."


@pytest.mark.asyncio
async def test_a_listed_profile_whose_model_is_not_installed_is_passed_over_for_one_that_is(tmp_path):
    profiles = [
        {"profile": "qwen25vl_3b_ollama", "model": "qwen2.5vl:3b", "backend": "ollama", "installed": False},
        {"profile": "qwen3vl_4b_ollama", "model": "qwen3-vl:4b-instruct", "backend": "ollama", "installed": True},
    ]
    adapter, seen = _adapter(tmp_path, profiles=profiles)
    assert (await adapter.execute(_request(_picture(tmp_path)))).output["model"] == "qwen3vl_4b_ollama"


@pytest.mark.asyncio
async def test_when_ollama_cannot_be_asked_it_is_started_and_the_models_are_listed_again(tmp_path, monkeypatch):
    started = []

    async def fake_start() -> bool:
        started.append(True)
        return True

    monkeypatch.setattr("agent_control.tools.image_describe._start_ollama", fake_start)
    answers = iter([
        [{"profile": "qwen25vl_3b_ollama", "model": "qwen2.5vl:3b", "backend": "ollama", "installed": None}],
        [{"profile": "qwen3vl_4b_ollama", "model": "qwen3-vl:4b-instruct", "backend": "ollama", "installed": True}],
    ])
    adapter, _ = _adapter(tmp_path, profiles=lambda: next(answers))
    result = await adapter.execute(_request(_picture(tmp_path)))
    assert result.output["model"] == "qwen3vl_4b_ollama" and started == [True]


@pytest.mark.asyncio
async def test_the_profile_the_user_chose_is_used_without_asking_localdeploy(tmp_path):
    adapter, seen = _adapter(tmp_path, profiles=RuntimeError("must not be asked"), profile="gemma3_12b_ollama_safe")
    result = await adapter.execute(_request(_picture(tmp_path)))
    assert result.status == ToolResultStatus.SUCCEEDED and seen["provider"].profile.model == "gemma3_12b_ollama_safe"


@pytest.mark.asyncio
async def test_what_cannot_be_looked_at_is_refused_in_words(tmp_path):
    adapter, _ = _adapter(tmp_path)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    big = tmp_path / "big.png"
    big.write_bytes(b"0" * (10 * 1024 * 1024 + 1))
    elsewhere = tmp_path.parent / "elsewhere.png"
    elsewhere.write_bytes(b"0")
    for path, words in (
        (tmp_path / "notes.txt", "not a picture file"), (tmp_path / "missing.png", "not a picture file"), (big, "larger than 10 MB"),
        (elsewhere, "outside allowed roots"),
    ):
        result = await adapter.execute(_request(path))
        assert result.status == ToolResultStatus.FAILED and words in result.error_message, (path, result.error_message)


@pytest.mark.asyncio
async def test_failures_say_what_is_wrong_not_a_stack_trace(tmp_path):
    none, _ = _adapter(tmp_path, profiles=[])
    assert "no vision model set up" in (await none.execute(_request(_picture(tmp_path)))).error_message
    absent = [{"profile": "qwen25vl_3b_ollama", "model": "qwen2.5vl:3b", "backend": "ollama", "installed": False}]
    missing, _ = _adapter(tmp_path, profiles=absent)
    message = (await missing.execute(_request(_picture(tmp_path)))).error_message
    assert "none of the vision models LocalDeploy lists is installed" in message and "ollama pull qwen3-vl:4b-instruct" in message
    down, _ = _adapter(tmp_path, profiles=httpx.ConnectError("refused"))
    assert "Is it running?" in (await down.execute(_request(_picture(tmp_path)))).error_message
    broken, _ = _adapter(tmp_path, answers=[RuntimeError("HTTP 500 out of memory")], start_ollama=False)
    message = (await broken.execute(_request(_picture(tmp_path)))).error_message
    assert "qwen3vl_4b_ollama" in message and "out of memory" in message
    blank, _ = _adapter(tmp_path, answers=["   "])
    assert "returned nothing" in (await blank.execute(_request(_picture(tmp_path)))).error_message


@pytest.mark.asyncio
async def test_a_model_server_that_is_not_running_is_started_once_and_the_picture_is_looked_at(tmp_path, monkeypatch):
    started = []

    async def fake_start() -> bool:
        started.append(True)
        return True

    monkeypatch.setattr("agent_control.tools.image_describe._start_ollama", fake_start)
    adapter, seen = _adapter(tmp_path, answers=[RuntimeError("Ollama is not running or is unreachable at http://localhost:11434"), "a cat"])
    result = await adapter.execute(_request(_picture(tmp_path)))
    assert result.status == ToolResultStatus.SUCCEEDED and result.output["description"] == "a cat" and started == [True]
    assert len(seen["provider"].calls) == 2
    off, _ = _adapter(tmp_path, answers=[RuntimeError("Ollama is not running")], start_ollama=False)
    refused = await off.execute(_request(_picture(tmp_path)))
    assert refused.status == ToolResultStatus.FAILED and "Ollama is not running" in refused.error_message


def test_models_are_listed_smallest_first_and_only_those_that_can_see():
    assert [_size_in_billions(m) for m in ("qwen3-vl:4b-instruct", "gemma3:12b", "llava", "qwen2.5vl:3b")] == [4.0, 12.0, 99.0, 3.0]
    assert localdeploy_root(AppSettings(_env_file=None, llm={"default_profile": "m", "profiles": {"m": {"model": "x", "base_url": "http://127.0.0.1:8000/v1/"}}})) == "http://127.0.0.1:8000"
    assert localdeploy_root(AppSettings(_env_file=None)) is None


@pytest.mark.asyncio
async def test_localdeploys_model_list_is_read_for_vision_profiles(monkeypatch):
    listing = {"data": [
        {"id": "gemma3_12b_ollama_safe", "localdeploy": {"supports_vision": True, "model_id": "gemma3:12b", "kind": "chat", "backend": "ollama"}},
        {"id": "qwen3vl_4b_ollama", "localdeploy": {"supports_vision": True, "model_id": "qwen3-vl:4b-instruct", "kind": "chat", "backend": "ollama"}},
        {"id": "qwen25vl_3b_ollama", "localdeploy": {"supports_vision": True, "model_id": "qwen2.5vl:3b", "kind": "chat", "backend": "ollama"}},
        {"id": "qwen35_9b_llamacpp", "localdeploy": {"supports_vision": False, "model_id": "q9", "kind": "chat", "backend": "llamacpp"}},
        {"id": "d3_lite_decision", "localdeploy": {"supports_vision": True, "model_id": "d3", "kind": "decision", "backend": "decision"}},
    ]}
    installed = {"ollama": {"models": ["gemma3:12b", "qwen3-vl:4b-instruct"], "error": None}}
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=installed if request.url.path == "/models" else listing)

    def client(*args, **kwargs):
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("agent_control.tools.image_describe.httpx.AsyncClient", client)
    found = await list_vision_profiles("http://x")
    assert [(p["profile"], p["installed"]) for p in found] == [
        ("qwen25vl_3b_ollama", False), ("qwen3vl_4b_ollama", True), ("gemma3_12b_ollama_safe", True),
    ]
    installed["ollama"] = {"models": [], "error": "Ollama is not running"}
    assert {p["installed"] for p in await list_vision_profiles("http://x")} == {None}  # could not be asked: unknown, not "no"


# --- registration -----------------------------------------------------------------------------------------------------------------------------


def test_the_tool_needs_vision_switched_on_and_the_file_tool_and_asks_before_each_use(tmp_path):
    registry = build_tool_registry(_settings(tmp_path), "http://127.0.0.1:8765")
    definition = registry.definition_index["image.describe"]
    assert definition.enabled and "image.describe" in registry.context() and "image.describe" in registry.adapters
    assert definition.requires_approval({"operation": "describe", "path": "C:/x/invoice.png"})
    reason = definition.approval_reason({"operation": "describe", "path": "C:/x/invoice.png"})
    assert "invoice.png" in reason and "stays on this computer" in reason and "smallest vision model LocalDeploy has" in reason
    assert definition.group == "vision" and "photo" in definition.provides
    named = build_tool_registry(_settings(tmp_path, profile="qwen3vl_4b_ollama"), "http://127.0.0.1:8765")
    assert "qwen3vl_4b_ollama" in named.definition_index["image.describe"].approval_reason({"operation": "describe", "path": "a.png"})


def test_it_is_off_and_unnamed_by_default_or_without_the_file_tool_so_recorded_prompts_do_not_change(tmp_path):
    plain = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765")
    assert "image.describe" not in plain.context() and "image.describe" not in plain.adapters
    no_files = AppSettings(_env_file=None, vision={"enabled": True})
    assert not build_tool_registry(no_files, "http://127.0.0.1:8765").definition_index["image.describe"].enabled
    off = AppSettings(_env_file=None, capabilities={Capability.FILESYSTEM_READ: CapabilityPolicy(enabled=True, requires_approval=False, max_risk_level=RiskLevel.LOW)})
    assert not build_tool_registry(off, "http://127.0.0.1:8765").definition_index["image.describe"].enabled
