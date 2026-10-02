"""A fake `LLMProvider` that replays recorded prompt/response pairs.

This is the deterministic test tier called for in docs/archive/HISTORY.md P2: real DB,
real tool registry, real policy engine, real worker loop, real tools against a
temp filesystem - with this in place of a live LLM. No network, no GPU,
fully reproducible.

Recording key: prompts, not call order. A scenario test that changes the
config context (adds a capability, renames a tool) changes the recorded
prompt text, which changes the key, which means a cache miss - the test fails
loudly instead of silently replaying a now-inaccurate fixture. Re-record with
`RecordingLLMProvider` when that happens; don't hand-edit the fixture around it.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ScriptedLLMError(RuntimeError):
    """Raised when a scenario test hits an LLM call with no recorded fixture."""


class _LiveProvider(Protocol):
    async def generate_text(self, system_prompt: str, user_prompt: str) -> str: ...
    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str: ...
    async def generate_structured(
        self, system_prompt: str, user_prompt: str, output_model: type[T], *, temperature: float | None = None
    ) -> T: ...


# schemas.new_id() mints "<prefix>_<uuid4().hex>", and some tools (e.g.
# code.interpreter's per-task workspace dir) embed that id straight into
# their LLM-facing prompt text. That id is fresh and different on every task
# creation - including two runs of the exact same scenario test - so an
# exact-text fixture key would never match twice. Collapse any run of 6+ hex
# characters before hashing so functionally-identical prompts key the same
# regardless of which random id they happened to embed.
_HEX_RUN = re.compile(r"[0-9a-f]{6,}", re.IGNORECASE)

# Python's tempfile.TemporaryDirectory()/mkdtemp() default naming scheme -
# "tmp" + 8 chars from [a-z0-9], not restricted to hex digits, so _HEX_RUN
# alone doesn't catch it. pytest's own tmp_path fixture uses this scheme too,
# and isolated_settings() chdir's the process into it. When a plan step's
# relative path resolves against that CWD and gets rejected by policy, the
# resulting error text embeds the random dir name - and that text feeds the
# next retry prompt on a replan, producing a fresh, unmatchable fixture key
# every run (real failure hit recording output_delivery.json: the planner's
# first attempt guessed a relative path, the policy-denial error embedded
# the run's random tmp dir, and replay could never match the replan prompt
# recorded under a different random name). Collapse it the same way.
_TEMPDIR_RUN = re.compile(r"\btmp[a-z0-9]{6,}\b", re.IGNORECASE)
_CURRENT_SCENARIO_SCRATCH_ROOT = str(
    Path(tempfile.gettempdir()) / "ybm_scenario_scratch"
)


def _scenario_scratch_pattern(current_root: str) -> str:
    """Match recorded roots plus the host's actual temporary-directory root."""
    roots = {
        re.escape(current_root),
        re.escape(str(Path(current_root).resolve())),
    }
    if current_root.startswith("/var/"):
        roots.add(re.escape(f"/private{current_root}"))
    elif current_root.startswith("/private/var/"):
        roots.add(re.escape(current_root.removeprefix("/private")))

    dynamic_roots = "|".join(sorted(roots, key=len, reverse=True))
    return (
        r"(?:[A-Za-z]:\\(?:[^\\\r\n]+\\)*AppData\\Local\\Temp\\ybm_scenario_scratch|"
        r"/tmp/ybm_scenario_scratch|"
        r"/(?:private/)?var/folders/[^/\s'\"),\]}]+/[^/\s'\"),\]}]+/T/ybm_scenario_scratch|"
        f"{dynamic_roots})"
    )


_SCENARIO_SCRATCH_PATTERN = _scenario_scratch_pattern(
    _CURRENT_SCENARIO_SCRATCH_ROOT
)
_SCENARIO_SCRATCH_ROOT = re.compile(
    _SCENARIO_SCRATCH_PATTERN,
    re.IGNORECASE,
)
_SCENARIO_SCRATCH_PATH = re.compile(
    _SCENARIO_SCRATCH_PATTERN + r"(?:[\\/][^\s'\"),\]}]+)*",
    re.IGNORECASE,
)

# pytest's own ``tmp_path`` fixture: ``.../pytest-of-<user>/pytest-<n>/<test><n>``.
# The ``pytest-<n>`` counter increments on every single run, so any prompt
# carrying a tmp_path keyed differently each time and could never replay - not
# on another machine, and not even on the recording machine a minute later.
# The scenario tests that pass an out-of-roots directory do exactly this, so
# they failed on a missing fixture rather than on the policy denial they exist
# to prove, while still satisfying `status != COMPLETED`. Collapsing the
# volatile prefix leaves the stable per-test directory name intact, and also
# stops every re-record from appending a fresh set of dead keys.
_PYTEST_TMP_ROOT = re.compile(
    r"(?:[A-Za-z]:\\|/)(?:[^\\/\r\n]+?[\\/])*?pytest-of-[^\\/\r\n]+?[\\/]pytest-\d+",
    re.IGNORECASE,
)
_PYTEST_TMP_PATH = re.compile(
    _PYTEST_TMP_ROOT.pattern + r"(?:[\\/][^\s'\"),\]}]+)*",
    re.IGNORECASE,
)

# A pydantic validation message's rendered value, up to the closing brace. Kept
# non-greedy and newline-free so one span cannot swallow the rest of a prompt.
_INPUT_VALUE_SPAN = re.compile(r"input_value=\{[^}\n]*\}")

# `C:\Users\<name>` / `C:/Users/<name>`, capturing the separator style so the
# replacement does not itself change one.
_WINDOWS_USER_DIR = re.compile(r"([A-Za-z]:[\\/]Users[\\/])[^\\/\s'\"),\]}]+", re.IGNORECASE)

# The same, but tolerating the doubled separators that appear when a path is
# embedded in generated source (`open('C:\\Users\\name\\...')`).
_WINDOWS_USER_DIR_ANY_SEP = re.compile(
    r"([A-Za-z]:[\\/]{1,2}Users[\\/]{1,2})[^\\/\s'\"),\]}]+", re.IGNORECASE
)
SCRATCH_PLACEHOLDER = "<scenario_scratch_root>"

# A stored placeholder path, with whatever suffix followed the scratch root.
_PLACEHOLDER_PATH = re.compile(re.escape(SCRATCH_PLACEHOLDER) + r"(?:/[^\s'\"),\]}]+)*")


def _normalize(text: str) -> str:
    # Tool output uses the host platform's native line endings. Recordings
    # made on Windows therefore contain CRLF while Linux CI produces LF for
    # the same successful command. Treat those prompts as identical.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Prompt history renders dict values with escaped Windows separators
    # (``C:\\Users``), while objectives and tool output contain ordinary
    # separators (``C:\Users``). Collapse the rendered form before replacing
    # scenario roots so recordings replay across both users and platforms.
    text = text.replace("\\\\", "\\")

    def replace_path(match: re.Match[str]) -> str:
        matched_path = match.group(0)
        root_match = _SCENARIO_SCRATCH_ROOT.match(matched_path)
        suffix = matched_path[root_match.end():] if root_match else ""
        normalized_suffix = suffix.replace("\\", "/")
        return f"{SCRATCH_PLACEHOLDER}{normalized_suffix}"

    text = _SCENARIO_SCRATCH_PATH.sub(replace_path, text)

    def replace_pytest_tmp(match: re.Match[str]) -> str:
        matched_path = match.group(0)
        root_match = _PYTEST_TMP_ROOT.match(matched_path)
        suffix = matched_path[root_match.end():] if root_match else ""
        return f"<pytest_tmp>{suffix.replace(chr(92), '/')}"

    text = _PYTEST_TMP_PATH.sub(replace_pytest_tmp, text)

    # Pydantic renders the offending value into its validation message as
    # `input_value={...}` and truncates that repr at a fixed length. Two things
    # then differ per platform, and normalizing only the first is not enough:
    #
    #   1. the path separator in whatever survives, and
    #   2. *where* the truncation falls - the underlying path lengths differ
    #      (`C:\Users\...\AppData\Local\Temp\...` against `/tmp/...`), so the
    #      surviving tail is cut at a different character.
    #
    # The whole span is replaced. The field name and error type either side of
    # it still carry the meaning; the rendered value never did, and it is only
    # ever hashed and diffed, never executed.
    text = _INPUT_VALUE_SPAN.sub("input_value={<omitted>}", text)

    # Any remaining Windows user directory: the recording machine's account
    # name is not part of the behaviour under test, and committing it to a
    # public repository is a disclosure with no upside.
    text = _WINDOWS_USER_DIR.sub(r"\1<user>", text)

    return _HEX_RUN.sub("<id>", _TEMPDIR_RUN.sub("<tmpdir>", text))


def fixture_key(method: str, system_prompt: str, user_prompt: str) -> str:
    normalized = f"{method}\n{_normalize(system_prompt)}\n{_normalize(user_prompt)}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:16]


def _load(fixture_path: Path) -> dict[str, dict[str, Any]]:
    if not fixture_path.exists():
        return {}
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _reindex(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Recompute keys when a stored fixture includes its original prompts."""
    indexed = {}
    for stored_key, entry in entries.items():
        method = entry.get("method")
        system_prompt = entry.get("system_prompt")
        user_prompt = entry.get("user_prompt")
        key = (
            fixture_key(method, system_prompt, user_prompt)
            if all(isinstance(value, str) for value in (method, system_prompt, user_prompt))
            else stored_key
        )
        indexed[key] = entry
    return indexed


def _placeholder_scenario_paths(value: Any) -> Any:
    """Replace the recording machine's scratch root with a placeholder.

    Prompts are normalized before being stored, but a response echoes the path
    back - a tool_input root, a final answer naming the folder - and that is
    how a Windows "Users/<account>" path reached thirteen committed fixtures. The
    placeholder is expanded again by `_rebase_scenario_paths` at replay, so the
    behaviour is unchanged and the fixture no longer names anyone.
    """
    if isinstance(value, str):
        def replace_path(match: re.Match[str]) -> str:
            matched_path = match.group(0)
            root_match = _SCENARIO_SCRATCH_ROOT.match(matched_path)
            suffix = matched_path[root_match.end():] if root_match else ""
            return f"{SCRATCH_PLACEHOLDER}{suffix.replace(chr(92), '/')}"

        return _SCENARIO_SCRATCH_PATH.sub(replace_path, value)
    if isinstance(value, list):
        return [_placeholder_scenario_paths(item) for item in value]
    if isinstance(value, dict):
        # Source code stays a carve-out for *paths* - rewriting it can change
        # parse/runtime behaviour, which is why _rebase_scenario_paths skips it
        # too. But the model routinely bakes the absolute workspace path into
        # the code it writes, and the account name in it has no bearing on
        # replay: the path already does not resolve anywhere but the recording
        # machine. Scrub just the user segment, leaving the path's shape and
        # every other character untouched.
        return {
            key: _scrub_user_segment(item) if key == "code" else _placeholder_scenario_paths(item)
            for key, item in value.items()
        }
    return value


def _scrub_user_segment(value: Any) -> Any:
    """Replace the account name in a Windows user directory, nothing else."""
    if isinstance(value, str):
        return _WINDOWS_USER_DIR_ANY_SEP.sub(r"\1<user>", value)
    if isinstance(value, list):
        return [_scrub_user_segment(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_user_segment(item) for key, item in value.items()}
    return value


def _rebase_scenario_paths(value: Any) -> Any:
    if isinstance(value, str):
        def replace_path(match: re.Match[str]) -> str:
            matched_path = match.group(0)
            root_match = _SCENARIO_SCRATCH_ROOT.match(matched_path)
            suffix = matched_path[root_match.end():] if root_match else ""
            parts = [part for part in re.split(r"[\\/]", suffix) if part]
            return str(Path(_CURRENT_SCENARIO_SCRATCH_ROOT, *parts))

        def expand_placeholder(match: re.Match[str]) -> str:
            suffix = match.group(0)[len(SCRATCH_PLACEHOLDER):]
            parts = [part for part in suffix.split("/") if part]
            return str(Path(_CURRENT_SCENARIO_SCRATCH_ROOT, *parts))

        # Placeholder first; fixtures recorded before it still hold a real
        # absolute path, which the substitution after this one handles.
        value = _PLACEHOLDER_PATH.sub(expand_placeholder, value)
        return _SCENARIO_SCRATCH_PATH.sub(replace_path, value)
    if isinstance(value, list):
        return [_rebase_scenario_paths(item) for item in value]
    if isinstance(value, dict):
        # Source code is replay input, not a returned path. Rewriting it can
        # change parse/runtime behavior and send different platforms down
        # different recorded control-flow paths.
        return {
            key: item if key == "code" else _rebase_scenario_paths(item)
            for key, item in value.items()
        }
    return value


def _closest_prompt_difference(
    entries: dict[str, dict[str, Any]],
    method: str,
    user_prompt: str,
) -> str:
    actual = _normalize(user_prompt)
    candidates = [
        (key, _normalize(str(entry["user_prompt"])))
        for key, entry in entries.items()
        if entry.get("method") == method
        and isinstance(entry.get("user_prompt"), str)
    ]
    if not candidates:
        return "no stored prompts use this method"

    closest_key, expected = max(
        candidates,
        key=lambda item: SequenceMatcher(None, item[1], actual).ratio(),
    )
    matcher = SequenceMatcher(None, expected, actual)
    difference = next(
        (opcode for opcode in matcher.get_opcodes() if opcode[0] != "equal"),
        None,
    )
    if difference is None:
        return f"closest fixture {closest_key} has identical normalized user prompt"

    _, expected_start, expected_end, actual_start, actual_end = difference
    expected_excerpt = expected[max(0, expected_start - 80) : expected_end + 80]
    actual_excerpt = actual[max(0, actual_start - 80) : actual_end + 80]
    return (
        f"closest fixture {closest_key} first difference: "
        f"expected={expected_excerpt!r}; actual={actual_excerpt!r}"
    )


def _save(fixture_path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Sort the fixture keys, but never the recorded payload underneath them.

    ``sort_keys=True`` reordered the recorded response too, so replay handed the
    tool a differently-ordered dict than recording did. Pydantic renders the
    offending dict into its validation message (``input_value={'operation':
    ...}``), that message lands in the operator history, and the next step's
    prompt therefore differed from the recorded one - a guaranteed key miss on
    any scenario whose second call follows a rejected first call. Sorting only
    the outer mapping keeps diffs stable without touching replay fidelity.
    """
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: entries[key] for key in sorted(entries)}
    fixture_path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")


class ScriptedLLMProvider:
    """Replays recorded (prompt -> response) pairs from a JSON fixture file."""

    def __init__(self, fixture_path: str | Path) -> None:
        self.fixture_path = Path(fixture_path)
        self._entries = _reindex(_load(self.fixture_path))
        self.calls: list[dict[str, str]] = []  # for test assertions on call order/count
        # No real API call happens on replay, so there is no real cost to
        # report - always None, never a fabricated zero. Present so callers
        # can use getattr(provider, "last_usage", None) uniformly across
        # every provider type without an isinstance check.
        self.last_usage: dict | None = None

    def _lookup(self, method: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        key = fixture_key(method, system_prompt, user_prompt)
        entry = self._entries.get(key)
        if entry is None:
            difference = _closest_prompt_difference(
                self._entries, method, user_prompt
            )
            raise ScriptedLLMError(
                f"{difference}\n"
                f"No recorded '{method}' fixture (key={key}) in {self.fixture_path}.\n"
                f"system_prompt[:200]={system_prompt[:200]!r}\n"
                f"user_prompt[:200]={user_prompt[:200]!r}\n"
                "Record it with RecordingLLMProvider against a live LLM, or the prompt "
                "text changed and every fixture using it needs re-recording."
            )
        self.calls.append({"method": method, "key": key})
        return _rebase_scenario_paths(entry)

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        return str(self._lookup("generate_text", system_prompt, user_prompt)["response"])

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        return str(self._lookup("generate_multimodal_text", system_prompt, user_prompt)["response"])

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[T],
        *,
        temperature: float | None = None,
    ) -> T:
        entry = self._lookup("generate_structured", system_prompt, user_prompt)
        return output_model.model_validate(entry["response"])


class RecordingLLMProvider:
    """Wraps a live LLMProvider and persists every call into a fixture file.

    Point this at a real profile (e.g. LocalDeploy), run the scenario test
    once with it in place of ScriptedLLMProvider, and the fixture file is
    written ready to commit and replay from thereafter.
    """

    def __init__(self, live_provider: _LiveProvider, fixture_path: str | Path) -> None:
        self.live_provider = live_provider
        self.fixture_path = Path(fixture_path)
        self._entries = _reindex(_load(self.fixture_path))
        # Same interface as ScriptedLLMProvider.calls - scenario tests that
        # assert on call count/order (e.g. "this objective needs zero LLM
        # calls") should work identically whether replaying or recording.
        self.calls: list[dict[str, str]] = []
        # Unlike ScriptedLLMProvider, this DOES make real calls - forward the
        # live provider's real usage so a recording session shows actual cost.
        self.last_usage: dict | None = None

    def _store(self, method: str, system_prompt: str, user_prompt: str, response: Any) -> None:
        key = fixture_key(method, system_prompt, user_prompt)
        # The *normalized* prompts are stored, not the raw ones. Raw prompts
        # carry the recording machine's absolute paths - which is how the
        # recording user's Windows account name ended up committed in thirteen
        # fixtures. Normalizing first keeps them host-independent, and loses
        # nothing: the only consumer is _closest_fixture_hint, which normalizes
        # before comparing anyway. _normalize is idempotent, so normalizing an
        # already-normalized prompt is a no-op.
        self._entries[key] = {
            "method": method,
            "system_prompt": _normalize(system_prompt),
            "user_prompt": _normalize(user_prompt),
            "response": _placeholder_scenario_paths(response),
        }
        _save(self.fixture_path, self._entries)
        self.calls.append({"method": method, "key": key})

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        response = await self.live_provider.generate_text(system_prompt, user_prompt)
        self.last_usage = getattr(self.live_provider, "last_usage", None)
        self._store("generate_text", system_prompt, user_prompt, response)
        return response

    async def generate_multimodal_text(self, system_prompt: str, user_prompt: str, image_paths: list[str]) -> str:
        response = await self.live_provider.generate_multimodal_text(system_prompt, user_prompt, image_paths)
        self.last_usage = getattr(self.live_provider, "last_usage", None)
        self._store("generate_multimodal_text", system_prompt, user_prompt, response)
        return response

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[T],
        *,
        temperature: float | None = None,
    ) -> T:
        result = await self.live_provider.generate_structured(
            system_prompt, user_prompt, output_model, temperature=temperature
        )
        self.last_usage = getattr(self.live_provider, "last_usage", None)
        self._store("generate_structured", system_prompt, user_prompt, result.model_dump(mode="json"))
        return result
