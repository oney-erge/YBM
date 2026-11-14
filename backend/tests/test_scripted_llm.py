from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent_control.testing.scripted_llm import (
    RecordingLLMProvider,
    ScriptedLLMError,
    ScriptedLLMProvider,
    fixture_key,
)


class _Answer(BaseModel):
    text: str
    confidence: float


class _CodeAnswer(BaseModel):
    code: str
    summary: str


def _write_fixture(path, entries: dict) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


@pytest.mark.asyncio
async def test_generate_text_replays_recorded_response(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    key = fixture_key("generate_text", "sys", "hello")
    _write_fixture(fixture, {key: {"method": "generate_text", "response": "hi there"}})

    provider = ScriptedLLMProvider(fixture)
    result = await provider.generate_text("sys", "hello")

    assert result == "hi there"


@pytest.mark.asyncio
async def test_generate_structured_replays_and_validates(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    key = fixture_key("generate_structured", "sys", "user")
    _write_fixture(fixture, {
        key: {"method": "generate_structured", "response": {"text": "answer", "confidence": 0.9}},
    })

    provider = ScriptedLLMProvider(fixture)
    result = await provider.generate_structured("sys", "user", _Answer)

    assert isinstance(result, _Answer)
    assert result.text == "answer"
    assert result.confidence == 0.9


@pytest.mark.asyncio
async def test_unrecorded_prompt_raises_loudly(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    _write_fixture(fixture, {})

    provider = ScriptedLLMProvider(fixture)

    with pytest.raises(ScriptedLLMError, match="No recorded"):
        await provider.generate_text("sys", "unrecorded prompt")


@pytest.mark.asyncio
async def test_missing_fixture_file_raises_on_any_call(tmp_path) -> None:
    provider = ScriptedLLMProvider(tmp_path / "does_not_exist.json")

    with pytest.raises(ScriptedLLMError):
        await provider.generate_text("sys", "anything")


def test_fixture_key_normalizes_embedded_random_ids() -> None:
    # schemas.new_id() mints "<prefix>_<uuid4().hex>" and some tools (e.g.
    # code.interpreter's per-task workspace dir) embed that id straight into
    # their prompt text - a fresh id every task creation. Prompts that are
    # otherwise identical must key the same regardless of which id they embed,
    # or no fixture could ever replay twice (this was a real bug, caught by
    # running the same scenario test setup twice with different random ids).
    prompt_a = "Workspace: C:\\scratch\\task_7f19962bf3054ddcb306a5c3cec28e44_b7de3ab1"
    prompt_b = "Workspace: C:\\scratch\\task_0011223344556677889900aabbccdd_12ab34cd"

    assert fixture_key("generate_text", "sys", prompt_a) == fixture_key("generate_text", "sys", prompt_b)


def test_fixture_key_normalizes_embedded_tempdir_names() -> None:
    # tempfile.TemporaryDirectory()/pytest's tmp_path fixture name dirs "tmp"
    # + 8 chars from [a-z0-9] - not restricted to hex, so _HEX_RUN alone
    # misses names like "tmpor3q4148". A relative plan-step path that
    # resolves against that randomized CWD leaks the name into the
    # policy-denial error text, which then feeds the next replan prompt -
    # different every run, so no replan fixture could ever replay twice
    # (real failure hit recording output_delivery.json).
    prompt_a = "path is outside allowed roots: C:\\Users\\x\\AppData\\Local\\Temp\\tmpor3q4148\\out.txt"
    prompt_b = "path is outside allowed roots: C:\\Users\\x\\AppData\\Local\\Temp\\tmpzz9k2b7c\\out.txt"

    assert fixture_key("generate_structured", "sys", prompt_a) == fixture_key(
        "generate_structured", "sys", prompt_b
    )


def test_fixture_key_normalizes_cross_platform_line_endings() -> None:
    windows = "Stdout:\r\n6765\r\n\r\nAvailable tools:"
    linux = "Stdout:\n6765\n\nAvailable tools:"

    assert fixture_key("generate_structured", "sys", windows) == fixture_key(
        "generate_structured", "sys", linux
    )


def test_fixture_key_normalizes_scenario_scratch_root_across_platforms() -> None:
    windows = (
        r"search C:\Users\recording-user\AppData\Local\Temp"
        r"\ybm_scenario_scratch\file_search"
    )
    linux = "search /tmp/ybm_scenario_scratch/file_search"

    assert fixture_key("generate_text", "sys", windows) == fixture_key(
        "generate_text", "sys", linux
    )


def test_fixture_key_normalizes_pytest_tmp_path_across_runs_and_platforms() -> None:
    """pytest's tmp_path carries a run counter that increments every single
    run, so a prompt containing one keyed differently each time and could never
    replay - not on CI, and not on the recording machine a minute later.

    The scenario tests that pass an out-of-roots directory all do this, and all
    were failing on a missing fixture while still satisfying their
    `status != COMPLETED` assertion, so none of them ever exercised the policy
    refusal they exist to prove.
    """
    first_run = (
        r"roots: C:\Users\recording-user\AppData\Local\Temp\pytest-of-recording-user"
        r"\pytest-2292\test_filesystem_search_rejects0\somewhere_else"
    )
    later_run = (
        r"roots: C:\Users\recording-user\AppData\Local\Temp\pytest-of-recording-user"
        r"\pytest-9999\test_filesystem_search_rejects0\somewhere_else"
    )
    on_linux = (
        "roots: /tmp/pytest-of-runner/pytest-7"
        "/test_filesystem_search_rejects0/somewhere_else"
    )

    key = fixture_key("generate_structured", "sys", first_run)
    assert key == fixture_key("generate_structured", "sys", later_run)
    assert key == fixture_key("generate_structured", "sys", on_linux)


def test_fixture_key_still_separates_different_pytest_test_directories() -> None:
    """Only the volatile prefix is collapsed; the per-test directory name is
    stable and must keep two different tests keyed apart."""
    one = r"C:\Temp\pytest-of-u\pytest-1\test_alpha0\somewhere_else"
    two = r"C:\Temp\pytest-of-u\pytest-1\test_beta0\somewhere_else"

    assert fixture_key("generate_structured", "sys", one) != fixture_key(
        "generate_structured", "sys", two
    )


def test_fixture_key_normalizes_a_truncated_pydantic_input_value() -> None:
    """Pydantic renders the offending value into its message as
    `input_value={...}` and truncates the middle with a literal "...", which
    discards the scenario-scratch prefix every other rule here matches on. What
    survives is a tail still carrying the platform separator, so a fixture
    recorded on Windows could not replay on Linux - the third distinct
    host-dependency found this way, after the pytest tmp_path counter and
    sort_keys reordering the recorded payload.
    """
    windows = (
        "1 validation error for FilesystemSearchInput\nquery\n  Field required "
        r"[type=missing, input_value={'operation': 'search', '...ch\file_find_and_read'}, input_type=dict]"
    )
    posix = (
        "1 validation error for FilesystemSearchInput\nquery\n  Field required "
        "[type=missing, input_value={'operation': 'search', '...ch/file_find_and_read'}, input_type=dict]"
    )
    # Truncation position differs too: the underlying paths are different
    # lengths, so pydantic cuts the repr at a different character. Normalizing
    # only the separator left this case still failing on CI.
    posix_cut_elsewhere = (
        "1 validation error for FilesystemSearchInput\nquery\n  Field required "
        "[type=missing, input_value={'operation': 'search', '...atch/file_find_and_read'}, input_type=dict]"
    )

    key = fixture_key("generate_structured", "sys", windows)
    assert key == fixture_key("generate_structured", "sys", posix)
    assert key == fixture_key("generate_structured", "sys", posix_cut_elsewhere)

    # The parts that carry meaning survive - only the rendered value is dropped.
    from agent_control.testing.scripted_llm import _normalize

    assert "FilesystemSearchInput" in _normalize(windows)
    assert "Field required" in _normalize(windows)


def test_fixture_key_is_independent_of_the_recording_account() -> None:
    """A recorded prompt keeps the recording machine's absolute paths, which is
    how one Windows account name reached thirteen committed fixtures."""
    mine = r"reading C:\Users\alice\Documents\notes.txt"
    yours = r"reading C:\Users\bob\Documents\notes.txt"

    assert fixture_key("generate_text", "sys", mine) == fixture_key("generate_text", "sys", yours)


def test_recorded_response_stores_a_placeholder_and_replays_as_a_real_path() -> None:
    """A response echoes back the path it was given - a tool_input root, a final
    answer naming the folder - so storing it verbatim committed the recording
    machine's account name. It is stored as a placeholder and expanded again on
    replay, so behaviour is unchanged and the fixture names nobody.
    """
    from agent_control.testing.scripted_llm import (
        _CURRENT_SCENARIO_SCRATCH_ROOT,
        _placeholder_scenario_paths,
        _rebase_scenario_paths,
    )

    recorded_on_windows = {
        "tool_input": {"root": r"C:\Users\somebody\AppData\Local\Temp\ybm_scenario_scratch\demo"},
        "code": r"open(r'C:\Users\somebody\AppData\Local\Temp\ybm_scenario_scratch\demo\x.py')",
    }

    stored = _placeholder_scenario_paths(recorded_on_windows)

    assert stored["tool_input"]["root"] == "<scenario_scratch_root>/demo"
    assert "somebody" not in stored["tool_input"]["root"]
    # Source code keeps its path shape - rewriting it can send platforms down
    # different recorded control flow - but not the account name, which has no
    # bearing on replay. See the dedicated test below.
    assert "<scenario_scratch_root>" not in stored["code"]
    assert r"AppData\Local\Temp\ybm_scenario_scratch\demo\x.py" in stored["code"]
    assert "somebody" not in stored["code"]

    replayed = _rebase_scenario_paths(stored)

    assert replayed["tool_input"]["root"] == str(
        Path(_CURRENT_SCENARIO_SCRATCH_ROOT, "demo")
    )


def test_recorded_source_code_keeps_its_shape_but_not_the_account_name() -> None:
    """Paths inside generated code stay a carve-out - rewriting them can change
    parse or runtime behaviour. The account name in them is a different matter:
    the path already resolves nowhere but the recording machine, so scrubbing
    just that segment costs nothing and keeps the fixture publishable.
    """
    from agent_control.testing.scripted_llm import _placeholder_scenario_paths

    recorded = {
        "code": r"open('C:\\Users\\somebody\\AppData\\Local\\Temp\\ybm_scenario_scratch\\r.txt', 'w')",
    }

    stored = _placeholder_scenario_paths(recorded)

    assert "somebody" not in stored["code"]
    assert "<user>" in stored["code"]
    # Shape preserved: still the same absolute path, still doubly-escaped, and
    # not turned into a placeholder the way a returned path would be.
    assert r"AppData\\Local\\Temp\\ybm_scenario_scratch\\r.txt" in stored["code"]
    assert "<scenario_scratch_root>" not in stored["code"]


def test_normalize_is_idempotent() -> None:
    """The recorder stores normalized prompts now, and the closest-fixture hint
    normalizes again before comparing - so a second pass must be a no-op."""
    from agent_control.testing.scripted_llm import _normalize

    raw = (
        r"root C:\Users\carol\AppData\Local\Temp\pytest-of-carol\pytest-31\test_x0\somewhere "
        r"and input_value={'path': 'C:\Users\carol\a\b.txt'} and tmpab12cd34"
    )
    once = _normalize(raw)

    assert _normalize(once) == once
    assert "carol" not in once


def test_fixture_key_normalizes_macos_scenario_scratch_aliases() -> None:
    windows = (
        r"search C:\Users\recording-user\AppData\Local\Temp"
        r"\ybm_scenario_scratch\file_search"
    )
    macos = "search /var/folders/ab/random-hash/T/ybm_scenario_scratch/file_search"
    resolved_macos = (
        "search /private/var/folders/ab/random-hash/T/"
        "ybm_scenario_scratch/file_search"
    )

    expected = fixture_key("generate_text", "sys", windows)
    assert fixture_key("generate_text", "sys", macos) == expected
    assert fixture_key("generate_text", "sys", resolved_macos) == expected


def test_fixture_key_normalizes_escaped_scenario_paths_in_history() -> None:
    recorded = (
        r"input: {'root': 'C:\\Users\\recording-user\\AppData\\Local\\Temp"
        r"\\ybm_scenario_scratch\\file_search'}"
    )
    linux = "input: {'root': '/tmp/ybm_scenario_scratch/file_search'}"

    assert fixture_key("generate_structured", "sys", recorded) == fixture_key(
        "generate_structured", "sys", linux
    )


@pytest.mark.asyncio
async def test_replay_rebases_recorded_scenario_response_path(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    recorded_root = (
        r"C:\Users\recording-user\AppData\Local\Temp"
        r"\ybm_scenario_scratch\file_search"
    )
    current_root = Path(tempfile.gettempdir()) / "ybm_scenario_scratch" / "file_search"
    _write_fixture(
        fixture,
        {
            "legacy-key": {
                "method": "generate_text",
                "system_prompt": "sys",
                "user_prompt": f"search {recorded_root}",
                "response": f"read {recorded_root}\\resume.txt",
            }
        },
    )

    provider = ScriptedLLMProvider(fixture)
    result = await provider.generate_text("sys", f"search {current_root}")

    assert result == f"read {current_root / 'resume.txt'}"


@pytest.mark.asyncio
async def test_replay_does_not_rewrite_recorded_source_code(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    recorded_root = (
        r"C:\Users\recording-user\AppData\Local\Temp"
        r"\ybm_scenario_scratch\code_interpreter"
    )
    current_root = Path(tempfile.gettempdir()) / "ybm_scenario_scratch" / "code_interpreter"
    recorded_code = f"print({recorded_root!r})"
    _write_fixture(
        fixture,
        {
            "legacy-key": {
                "method": "generate_structured",
                "system_prompt": "sys",
                "user_prompt": f"write under {recorded_root}",
                "response": {
                    "code": recorded_code,
                    "summary": f"write under {recorded_root}",
                },
            }
        },
    )

    provider = ScriptedLLMProvider(fixture)
    result = await provider.generate_structured(
        "sys", f"write under {current_root}", _CodeAnswer
    )

    assert result.code == recorded_code
    assert result.summary == f"write under {current_root}"


def test_fixture_key_still_distinguishes_genuinely_different_prompts() -> None:
    assert fixture_key("generate_text", "sys", "search for a resume") != fixture_key(
        "generate_text", "sys", "search for an invoice"
    )


@pytest.mark.asyncio
async def test_prompt_change_is_a_cache_miss_not_a_stale_hit(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    key = fixture_key("generate_text", "sys", "original prompt")
    _write_fixture(fixture, {key: {"method": "generate_text", "response": "stale answer"}})

    provider = ScriptedLLMProvider(fixture)

    with pytest.raises(ScriptedLLMError):
        await provider.generate_text("sys", "changed prompt")


@pytest.mark.asyncio
async def test_calls_are_tracked_for_assertions(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    key = fixture_key("generate_text", "sys", "hello")
    _write_fixture(fixture, {key: {"method": "generate_text", "response": "hi"}})

    provider = ScriptedLLMProvider(fixture)
    await provider.generate_text("sys", "hello")

    assert len(provider.calls) == 1
    assert provider.calls[0]["method"] == "generate_text"


class _FakeLiveProvider:
    def __init__(self) -> None:
        self.generate_text_calls = 0

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.generate_text_calls += 1
        return f"live response to: {user_prompt}"

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        return "live multimodal response"

    async def generate_structured(self, system_prompt, user_prompt, output_model, *, temperature=None):
        return output_model.model_validate({"text": "live structured", "confidence": 1.0})


@pytest.mark.asyncio
async def test_recording_provider_persists_to_fixture_file(tmp_path) -> None:
    fixture = tmp_path / "recorded.json"
    live = _FakeLiveProvider()
    recorder = RecordingLLMProvider(live, fixture)

    result = await recorder.generate_text("sys", "record me")

    assert result == "live response to: record me"
    assert fixture.exists()
    replay = ScriptedLLMProvider(fixture)
    replayed = await replay.generate_text("sys", "record me")
    assert replayed == result


@pytest.mark.asyncio
async def test_recording_provider_persists_structured_response(tmp_path) -> None:
    fixture = tmp_path / "recorded.json"
    live = _FakeLiveProvider()
    recorder = RecordingLLMProvider(live, fixture)

    result = await recorder.generate_structured("sys", "user", _Answer)

    assert result.text == "live structured"
    replay = ScriptedLLMProvider(fixture)
    replayed = await replay.generate_structured("sys", "user", _Answer)
    assert replayed.text == "live structured"
    assert replayed.confidence == 1.0


@pytest.mark.asyncio
async def test_recording_provider_appends_without_clobbering_existing_entries(tmp_path) -> None:
    fixture = tmp_path / "recorded.json"
    live = _FakeLiveProvider()
    recorder = RecordingLLMProvider(live, fixture)
    await recorder.generate_text("sys", "first")
    await recorder.generate_text("sys", "second")

    replay = ScriptedLLMProvider(fixture)
    assert await replay.generate_text("sys", "first") == "live response to: first"
    assert await replay.generate_text("sys", "second") == "live response to: second"


# --- ybm scenario rekey: a prompt changed, the recorded response is kept under the new key --------------------------------------------


def _long_prompt(extra_line: str = "") -> str:
    lines = [f"Tool {n}: does thing number {n} on the user's computer and says so in one sentence." for n in range(60)]
    if extra_line:
        lines.insert(30, extra_line)
    return "\n".join(lines)


def _recorded(tmp_path, *, system: str, user: str, response: str, name: str = "scenario_x") -> Path:
    from agent_control.testing.scripted_llm import _normalize

    fixture = tmp_path / f"{name}.json"
    key = fixture_key("generate_text", system, user)
    _write_fixture(fixture, {key: {"method": "generate_text", "system_prompt": _normalize(system), "user_prompt": _normalize(user), "response": response}})
    return fixture


@pytest.mark.asyncio
async def test_a_changed_prompt_misses_without_rekey(tmp_path) -> None:
    fixture = _recorded(tmp_path, system="sys", user=_long_prompt(), response="the recorded answer")
    with pytest.raises(ScriptedLLMError):
        await ScriptedLLMProvider(fixture).generate_text("sys", _long_prompt("Tool 99: a new tool."))


@pytest.mark.asyncio
async def test_rekey_keeps_the_response_under_the_new_key_and_logs_what_changed(tmp_path, monkeypatch) -> None:
    import json

    fixture = _recorded(tmp_path, system="sys", user=_long_prompt(), response="the recorded answer")
    log = tmp_path / "rekey.log"
    monkeypatch.setenv("YBM_SCENARIO_REKEY", "scenario_x")
    monkeypatch.setenv("YBM_SCENARIO_REKEY_LOG", str(log))

    changed = _long_prompt("Tool 99: a new tool.")
    assert await ScriptedLLMProvider(fixture).generate_text("sys", changed) == "the recorded answer"

    monkeypatch.delenv("YBM_SCENARIO_REKEY")
    # saved: a plain replay with the new prompt now hits
    assert await ScriptedLLMProvider(fixture).generate_text("sys", changed) == "the recorded answer"
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["fixture"] == "scenario_x.json" and any("Tool 99: a new tool." in line and line.startswith("+") for line in entry["diff"])


@pytest.mark.asyncio
async def test_rekey_only_touches_the_fixture_it_was_asked_for(tmp_path, monkeypatch) -> None:
    fixture = _recorded(tmp_path, system="sys", user=_long_prompt(), response="r", name="other_fixture")
    monkeypatch.setenv("YBM_SCENARIO_REKEY", "scenario_x")
    with pytest.raises(ScriptedLLMError):
        await ScriptedLLMProvider(fixture).generate_text("sys", _long_prompt("Tool 99: a new tool."))


@pytest.mark.asyncio
async def test_rekey_refuses_a_prompt_that_is_not_alike_and_never_reuses_a_response_twice(tmp_path, monkeypatch) -> None:
    fixture = _recorded(tmp_path, system="sys", user=_long_prompt(), response="the recorded answer")
    monkeypatch.setenv("YBM_SCENARIO_REKEY", "scenario_x")
    provider = ScriptedLLMProvider(fixture)
    with pytest.raises(ScriptedLLMError):
        await provider.generate_text("sys", "something else entirely " * 40)
    await provider.generate_text("sys", _long_prompt("Tool 98: one."))
    with pytest.raises(ScriptedLLMError):  # the one recorded response already answered a call; a second call needs its own
        await provider.generate_text("sys", _long_prompt("Tool 97: two."))


@pytest.mark.asyncio
async def test_a_replay_lists_the_keys_it_used_for_pruning(tmp_path, monkeypatch) -> None:
    import json

    fixture = _recorded(tmp_path, system="sys", user=_long_prompt(), response="r")
    track = tmp_path / "used.log"
    monkeypatch.setenv("YBM_SCENARIO_TRACK", str(track))
    await ScriptedLLMProvider(fixture).generate_text("sys", _long_prompt())
    row = json.loads(track.read_text(encoding="utf-8").splitlines()[0])
    assert row == {"fixture": "scenario_x.json", "key": fixture_key("generate_text", "sys", _long_prompt())}


def _step_prompt(history_steps: int, extra_line: str = "") -> str:
    """The prompt of one step of a task: the same text each time, plus the history so far."""
    header = [f"Tool {n}: does thing number {n} on the user's computer and says so in one sentence." for n in range(60)]
    if extra_line:
        header.insert(30, extra_line)
    history = [f"{n}. tool call number {n} returned a long result about the files it found there, in detail." for n in range(1, history_steps + 1)]
    return "\n".join([*header, "History:", *history])


@pytest.mark.asyncio
async def test_each_step_of_a_task_gets_its_own_recorded_answer_when_the_prompt_changed(tmp_path, monkeypatch) -> None:
    """Steps of one task differ only by the history they carry, so a similarity match alone could hand step 1 the answer recorded for step 2."""
    from agent_control.testing.scripted_llm import _normalize

    fixture = tmp_path / "scenario_x.json"
    entries = {}
    for step in (1, 2, 3):
        prompt = _step_prompt(step)
        entries[fixture_key("generate_text", "sys", prompt)] = {
            "method": "generate_text", "system_prompt": _normalize("sys"), "user_prompt": _normalize(prompt), "response": f"answer for step {step}",
        }
    _write_fixture(fixture, entries)
    monkeypatch.setenv("YBM_SCENARIO_REKEY", "scenario_x")
    provider = ScriptedLLMProvider(fixture)
    changed = "Tool 99: a new tool."
    assert [await provider.generate_text("sys", _step_prompt(step, changed)) for step in (1, 2, 3)] == [
        "answer for step 1", "answer for step 2", "answer for step 3",
    ]
    monkeypatch.delenv("YBM_SCENARIO_REKEY")
    replay = ScriptedLLMProvider(fixture)
    assert [await replay.generate_text("sys", _step_prompt(step, changed)) for step in (1, 2, 3)] == ["answer for step 1", "answer for step 2", "answer for step 3"]


@pytest.mark.asyncio
async def test_a_match_another_is_nearly_as_good_as_is_refused(tmp_path, monkeypatch) -> None:
    from agent_control.testing.scripted_llm import _normalize

    fixture = tmp_path / "scenario_x.json"
    entries = {}
    for name, tail in (("a", "ends this way."), ("b", "ends that way.")):  # two recorded calls the new prompt is equally near to
        prompt = _step_prompt(1) + "\n" + tail
        entries[fixture_key("generate_text", "sys", prompt)] = {
            "method": "generate_text", "system_prompt": _normalize("sys"), "user_prompt": _normalize(prompt), "response": name,
        }
    _write_fixture(fixture, entries)
    monkeypatch.setenv("YBM_SCENARIO_REKEY", "scenario_x")
    with pytest.raises(ScriptedLLMError):
        await ScriptedLLMProvider(fixture).generate_text("sys", _step_prompt(1, "Tool 99: a new tool.") + "\nends some other way.")


@pytest.mark.asyncio
async def test_a_changed_system_prompt_is_matched_by_the_unchanged_user_prompt_even_when_it_varies_by_step(tmp_path, monkeypatch) -> None:
    from agent_control.testing.scripted_llm import _normalize

    fixture = tmp_path / "scenario_x.json"
    entries = {}
    for step in (1, 2):
        system = "You are the operator.\n" + "\n".join(f"Rule {n}: do it carefully." for n in range(60)) + f"\nThis is step {step} of 12."
        prompt = _step_prompt(step)
        entries[fixture_key("generate_text", system, prompt)] = {
            "method": "generate_text", "system_prompt": _normalize(system), "user_prompt": _normalize(prompt), "response": f"answer for step {step}",
        }
    _write_fixture(fixture, entries)
    monkeypatch.setenv("YBM_SCENARIO_REKEY", "scenario_x")
    provider = ScriptedLLMProvider(fixture)
    new_system = "You are the operator.\n" + "\n".join(f"Rule {n}: do it carefully." for n in range(60)) + "\nA new rule.\nThis is step {} of 12."
    assert [await provider.generate_text(new_system.format(step), _step_prompt(step)) for step in (2, 1)] == [
        "answer for step 2", "answer for step 1",
    ]
