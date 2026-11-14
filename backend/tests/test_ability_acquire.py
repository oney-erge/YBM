"""Getting a capability from a recipe: what is fetched is checked, nothing is switched on until it has been, and the request carries on."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_control.abilities.acquire import AcquireError, Acquirer, deep_merge
from agent_control.abilities.recipes import ASK_ALWAYS_MB, RECIPES, Download, Recipe, recipe_abilities, recipe_by_id
from agent_control.abilities.model import AbilityState
from agent_control.config import AppSettings
from agent_control.schemas import ToolCallRequest, ToolResultStatus
from agent_control.tools.ability_acquire import AbilityAcquireAdapter, approval_sentence, needs_approval, risk_of
from agent_control.tools.registry import build_tool_registry
from agent_control.schemas import RiskLevel
from helpers import make_repos

PAYLOAD = b"model bytes" * 100
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _recipe(**overrides) -> Recipe:
    base = dict(
        id="demo.voice", title="a demo voice", summary="speak", provides=("speak",), confirm="Does the user want it spoken?", size_mb=1,
        source="example.com", after="It is set up.", ready=lambda settings: False,
        downloads=(Download("https://example.com/voice.bin", "voice.bin", SHA, len(PAYLOAD)),),
        config=lambda folder: {"adapters": {"tts": {"enabled": True, "model_path": str(folder / "voice.bin")}}},
    )
    return Recipe(**{**base, **overrides})


class _Disk:
    """The config file as the acquirer sees it, and what a fetch and a command did."""

    def __init__(self, payload: bytes = PAYLOAD, command_result: tuple[int, str] = (0, "ok"), service: bool = True) -> None:
        self.config: dict = {"adapters": {"tts": {"enabled": False, "voice": "x"}}, "other": 1}
        self.writes = 0
        self.fetched: list[str] = []
        self.commands: list[tuple[str, ...]] = []
        self.payload, self.command_result, self.service = payload, command_result, service

    def read(self) -> dict:
        return self.config

    def write(self, value: dict) -> None:
        self.config = value
        self.writes += 1

    async def fetch(self, url: str, destination: Path) -> None:
        self.fetched.append(url)
        destination.write_bytes(self.payload)

    async def run(self, command: tuple[str, ...], timeout: int) -> tuple[int, str]:
        self.commands.append(command)
        return self.command_result

    async def start(self, name: str) -> bool:
        return self.service

    def acquirer(self, tmp_path: Path) -> Acquirer:
        return Acquirer(tmp_path / "models", read_config=self.read, write_config=self.write, fetch=self.fetch, run=self.run, start=self.start)


# --- the recipes are data worth trusting ------------------------------------------------------------------------------------------------


def test_every_recipe_says_where_its_files_come_from_and_how_to_check_them() -> None:
    assert len({recipe.id for recipe in RECIPES}) == len(RECIPES)
    for recipe in RECIPES:
        assert recipe.summary and recipe.confirm.endswith("?") and recipe.source and recipe.after and recipe.provides
        for download in recipe.downloads:
            assert download.url.startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", download.sha256) and download.size_bytes > 0
        assert recipe.downloads or recipe.command or recipe.config  # it does something
        assert recipe.folder(Path("models")).name == recipe.id.replace(".", "-")


def test_what_is_big_or_changes_the_computer_is_always_asked_about() -> None:
    assert recipe_by_id("voice.kokoro").must_ask is False  # 120 MB in this assistant's own folder: the model's judgment
    assert recipe_by_id("video.ffmpeg").must_ask is True  # a program installed on the computer
    assert recipe_by_id("vision.qwen3vl").must_ask is True and recipe_by_id("vision.qwen3vl").size_mb >= ASK_ALWAYS_MB
    assert recipe_by_id("nothing.here") is None


# --- asking is the model's judgment, with floors ----------------------------------------------------------------------------------------


def test_the_model_may_ask_and_may_not_skip_what_must_be_asked() -> None:
    assert needs_approval({"recipe": "voice.kokoro"}) is False
    assert needs_approval({"recipe": "voice.kokoro", "ask_first": True}) is True  # its own judgment
    assert needs_approval({"recipe": "video.ffmpeg", "ask_first": False}) is True  # the floor
    assert needs_approval({"recipe": "made.up"}) is True
    assert risk_of({"recipe": "voice.kokoro"}) == RiskLevel.LOW and risk_of({"recipe": "video.ffmpeg"}) == RiskLevel.HIGH


def test_the_approval_says_what_is_fetched_how_big_and_from_where() -> None:
    text = approval_sentence({"recipe": "voice.kokoro", "reason": "read the answer aloud"})
    assert "a local voice" in text and "120 MB" in text and "github.com" in text and "read the answer aloud" in text and "own folder" in text
    assert "installs a program on this computer" in approval_sentence({"recipe": "video.ffmpeg"})


# --- carrying a recipe out --------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_are_fetched_checked_kept_and_only_then_the_config_is_switched_on(tmp_path) -> None:
    disk = _Disk()
    done = await disk.acquirer(tmp_path).install(_recipe())
    kept = tmp_path / "models" / "demo-voice" / "voice.bin"
    assert kept.read_bytes() == PAYLOAD and not list(kept.parent.glob("*.part")) and done.files == [str(kept.resolve())]
    assert disk.config["adapters"]["tts"] == {"enabled": True, "voice": "x", "model_path": str(kept.resolve())}
    assert disk.config["other"] == 1 and disk.writes == 1  # merged, not replaced


@pytest.mark.asyncio
async def test_a_file_that_is_not_the_one_written_for_is_not_kept_and_nothing_is_switched_on(tmp_path) -> None:
    disk = _Disk(payload=b"something else entirely")
    with pytest.raises(AcquireError, match="did not match"):
        await disk.acquirer(tmp_path).install(_recipe())
    assert disk.writes == 0 and not list((tmp_path / "models").rglob("voice.bin*"))


@pytest.mark.asyncio
async def test_a_file_already_there_with_the_right_hash_is_not_fetched_again(tmp_path) -> None:
    disk = _Disk()
    await disk.acquirer(tmp_path).install(_recipe())
    disk.fetched.clear()
    await disk.acquirer(tmp_path).install(_recipe())
    assert disk.fetched == []


@pytest.mark.asyncio
async def test_a_command_that_fails_stops_before_the_config_is_touched(tmp_path) -> None:
    disk = _Disk(command_result=(1, "no space left"))
    with pytest.raises(AcquireError, match="no space left"):
        await disk.acquirer(tmp_path).install(_recipe(command=("winget", "install", "x")))
    assert disk.writes == 0 and disk.commands == [("winget", "install", "x")]


@pytest.mark.asyncio
async def test_a_service_the_command_needs_is_started_or_the_install_says_so(tmp_path) -> None:
    down = _Disk(service=False)
    with pytest.raises(AcquireError, match="ollama is not running"):
        await down.acquirer(tmp_path).install(_recipe(command=("ollama", "pull", "m"), needs_service="ollama", downloads=(), config=None))
    assert down.commands == []
    up = _Disk(service=True)
    await up.acquirer(tmp_path).install(_recipe(command=("ollama", "pull", "m"), needs_service="ollama", downloads=(), config=None))
    assert up.commands == [("ollama", "pull", "m")]


def test_a_config_is_merged_deeply_and_leaves_what_it_does_not_name() -> None:
    assert deep_merge({"a": {"b": 1, "c": 2}, "d": 3}, {"a": {"b": 9}, "e": 4}) == {"a": {"b": 9, "c": 2}, "d": 3, "e": 4}


# --- the tool --------------------------------------------------------------------------------------------------------------------------


def _request(task_id: str, **input_) -> ToolCallRequest:
    return ToolCallRequest(task_id=task_id, tool_name="ability.acquire", capability="dependencies.install", risk_level="medium", input={"operation": "install", **input_})


def _adapter(tmp_path, repos, disk: _Disk, recipe: Recipe, monkeypatch) -> AbilityAcquireAdapter:
    import agent_control.tools.ability_acquire as module

    monkeypatch.setattr(module, "recipe_by_id", lambda recipe_id: recipe if recipe_id == recipe.id else None)
    settings = AppSettings(_env_file=None, abilities={"enabled": True, "acquire": True, "models_dir": str(tmp_path / "models")})
    deps = SimpleNamespace(settings=settings, repositories=repos)
    return AbilityAcquireAdapter(deps, disk.acquirer(tmp_path))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_install_sets_it_up_and_starts_the_request_that_needed_it_again_once(tmp_path, monkeypatch) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("Read that to me out loud.", conversation_id=None, metadata={
        "source_channel": "telegram", "source_chat_id": "42", "original_message_text": "Read that to me out loud.", "ability_notes": "x", "fact_checks": {},
    })
    disk = _Disk()
    adapter = _adapter(tmp_path, repos, disk, _recipe(), monkeypatch)
    result = await adapter.execute(_request(task.id, recipe="demo.voice", reason="read it aloud"))
    assert result.status == ToolResultStatus.SUCCEEDED and result.output["installed"] is True and result.output["retry_task_id"]
    retry = repos.tasks.get(result.output["retry_task_id"])
    assert retry.objective == task.objective and retry.metadata["source_chat_id"] == "42" and retry.metadata["retry_of"] == task.id
    assert "ability_notes" not in retry.metadata and "fact_checks" not in retry.metadata  # the retry looks at the world afresh
    # a retry that installs something else does not start a third task
    again = await _adapter(tmp_path, repos, _Disk(), _recipe(id="demo.other"), monkeypatch).execute(_request(retry.id, recipe="demo.other"))
    assert again.output["retry_task_id"] is None


@pytest.mark.asyncio
async def test_a_capability_that_is_already_there_is_said_not_fetched_and_plan_only_describes(tmp_path, monkeypatch) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("x")
    disk = _Disk()
    ready = await _adapter(tmp_path, repos, disk, _recipe(ready=lambda settings: True), monkeypatch).execute(_request(task.id, recipe="demo.voice"))
    assert "already set up" in ready.output["summary"] and disk.fetched == [] and ready.output["installed"] is False
    plan = await _adapter(tmp_path, repos, disk, _recipe(), monkeypatch).execute(_request(task.id, recipe="demo.voice", operation="plan"))
    assert "fetches about 1 MB from example.com" in plan.output["summary"] and disk.fetched == []


@pytest.mark.asyncio
async def test_an_unknown_recipe_and_a_failed_install_are_failures_in_words(tmp_path, monkeypatch) -> None:
    repos, _ = make_repos(tmp_path)
    task = repos.tasks.create("x")
    unknown = await _adapter(tmp_path, repos, _Disk(), _recipe(), monkeypatch).execute(_request(task.id, recipe="who.knows"))
    assert unknown.status == ToolResultStatus.FAILED and "voice.kokoro" in (unknown.error_message or "")
    broken = await _adapter(tmp_path, repos, _Disk(payload=b"nope"), _recipe(), monkeypatch).execute(_request(task.id, recipe="demo.voice"))
    assert broken.status == ToolResultStatus.FAILED and "did not match" in (broken.error_message or "")


# --- the index and the registry ---------------------------------------------------------------------------------------------------------


def test_what_is_not_set_up_is_an_installable_record_the_index_finds() -> None:
    settings = AppSettings(_env_file=None, abilities={"enabled": True, "acquire": True})
    found = {ability.id: ability for ability in recipe_abilities(settings)}
    voice = found["model:voice.kokoro"]
    assert voice.state == AbilityState.INSTALLABLE and voice.call["tool"] == "ability.acquire" and voice.call["input"]["recipe"] == "voice.kokoro"
    assert voice.confirm and "ask_first" in voice.guidance
    assert "winget" not in voice.guidance and "120 MB" in voice.guidance
    ready = AppSettings(_env_file=None, adapters={"stt": {"enabled": True}})
    assert "model:speech.whisper" not in {a.id for a in recipe_abilities(ready)}  # already on: not a thing to set up


def test_the_tool_is_absent_until_it_is_switched_on_so_the_prompt_is_unchanged() -> None:
    off = build_tool_registry(AppSettings(_env_file=None), "http://127.0.0.1:8765")
    assert "ability.acquire" not in {d.name for d in off.definitions if d.enabled}
    assert "ability.acquire" not in off.context()  # not even named as disabled
    on = build_tool_registry(
        AppSettings(_env_file=None, abilities={"enabled": True, "acquire": True},
                    capabilities={"dependencies.install": {"enabled": True, "requires_approval": False, "max_risk_level": "high"}}),
        "http://127.0.0.1:8765",
    )
    definition = next(d for d in on.definitions if d.name == "ability.acquire")
    assert definition.enabled and "voice.kokoro" in definition.description
    assert definition.requires_approval({"operation": "install", "recipe": "video.ffmpeg"})
    assert not definition.requires_approval({"operation": "install", "recipe": "voice.kokoro"})
    assert not definition.requires_approval({"operation": "plan", "recipe": "video.ffmpeg"})
