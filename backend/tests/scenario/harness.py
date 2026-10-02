"""Scenario-test harness: the real worker/planner/policy/registry/executor
stack wired against a temp DB and temp filesystem, with a ScriptedLLMProvider
standing in for the LLM. Mirrors agent_control.cli.run_worker()'s production
wiring - see docs/archive/HISTORY.md P2 for why this tier exists (nothing between
mocked unit tests and a live Telegram+LLM+desktop E2E run previously did).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import os
import shutil
import sys
import tempfile

from agent_control.config import (
    AppSettings,
    CapabilityPolicy,
    MCPConfig,
    MCPServerConfig,
    default_capability_policies,
    load_settings,
)
from agent_control.llm.providers import OpenAICompatibleProvider
from agent_control.orchestration.auditor import AuditorService
from agent_control.orchestration.executor import ToolExecutor
from agent_control.orchestration.operator import OperatorLoopService
from agent_control.orchestration.worker import TaskWorker
from agent_control.policy.engine import PolicyEngine
from agent_control.recovery.retry import RetryPolicy
from agent_control.schemas import ApprovalStatus, Capability, RiskLevel, TaskRecord, TaskStatus
from agent_control.storage import AuditLogger, Database, Repositories
from agent_control.testing.scripted_llm import RecordingLLMProvider, ScriptedLLMProvider
from agent_control.tools.registry import build_tool_registry

# Env var read by `ybm scenario record <name>` (docs/archive/HISTORY.md N3). Recording
# is opt-in and never happens by default - a scenario test run with this
# unset always replays fixtures, same as before this existed.
RECORD_ENV_VAR = "YBM_SCENARIO_RECORD"
RECORD_PROFILE_ENV_VAR = "YBM_SCENARIO_RECORD_PROFILE"


FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Anchored to this file's own location, not process cwd - every scenario test
# calls isolated_settings(), which chdir's the process into a throwaway
# tmp_path BEFORE build_scenario() runs, so by the time recording needs the
# REAL config/config.yaml, cwd-relative lookup would already be pointed at
# an empty temp dir regardless of where the test run itself was launched from.
_REPO_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "config.yaml"

# Recorded prompts are keyed on exact text (see testing/scripted_llm.py). Any
# objective that mentions a filesystem path needs that path to be identical
# on every run, so it can't use pytest's tmp_path (randomized per run) -
# use this instead. Stable across repeated runs *on this machine*; not yet
# portable across machines/clone paths (the objective text embeds an
# absolute path) - a real limitation, tracked in docs/archive/HISTORY.md P2, not one
# to design around today.
SCENARIO_SCRATCH_ROOT = Path(tempfile.gettempdir()) / "ybm_scenario_scratch"


def scenario_scratch_dir(name: str) -> Path:
    """A stable, empty directory at the same absolute path every run."""
    path = SCENARIO_SCRATCH_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path

class FakeTelegramClient:
    """Records delivered files instead of calling the real Telegram API.
    Scenario tasks aren't created from a real Telegram update, so
    ``run_task_to_completion`` stamps ``source_chat_id`` into task metadata by
    default (mirroring what the production message handler sets) - without
    it, ``artifact.deliver`` raises "chat_id is required" for any plan that
    delivers a created file, which the planner does routinely (see
    docs/archive/HISTORY.md P2 - discovered while recording the
    code_interpreter_csv_summary fixture)."""

    def __init__(self) -> None:
        self.photos: list[tuple[str | int, str, str | None]] = []
        self.documents: list[tuple[str | int, str, str | None]] = []

    async def send_photo_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict:
        self.photos.append((chat_id, path, caption))
        return {"ok": True, "method": "sendPhoto"}

    async def send_document_file(self, chat_id: str | int, path: str, caption: str | None = None) -> dict:
        self.documents.append((chat_id, path, caption))
        return {"ok": True, "method": "sendDocument"}


SCENARIO_CHAT_ID = "scenario_test_chat"

# MCP stdio handshake budget for the fake-server scenario tests. Was 10s,
# which is genuinely marginal: spawning a Python subprocess on Windows and
# completing the MCP initialize() round trip measured 11.5s on this machine
# while LocalDeploy/Ollama were also running, so the tests failed with an
# opaque anyio WouldBlock/CancelledError rather than a clear timeout. Timed
# directly before picking this number (10s -> fail at 10.3s, 30s -> succeed
# at 11.5s); the headroom is for machine load, not for a slow server.
MCP_HANDSHAKE_TIMEOUT_SECONDS = 30

TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
    TaskStatus.AWAITING_EXTERNAL,
    TaskStatus.CLARIFYING,
    # "Settled, no further autonomous progress without external input" - the
    # same category CLARIFYING is already in. Missing until 2026-07-30: a
    # scenario proving an approval gate correctly fires (never approving it,
    # by design) got stuck retrying process_task() on an unchanging
    # AWAITING_APPROVAL task until run_task_to_completion's own tick budget
    # raised, rather than the harness recognizing the task had genuinely
    # settled.
    TaskStatus.AWAITING_APPROVAL,
}


@dataclass
class Scenario:
    settings: AppSettings
    repositories: Repositories
    audit: AuditLogger
    worker: TaskWorker
    provider: ScriptedLLMProvider | RecordingLLMProvider
    telegram: FakeTelegramClient


def isolated_settings(monkeypatch, tmp_path: Path, **overrides) -> AppSettings:
    """Build AppSettings with zero influence from this repo's real
    config/config.yaml or .env.

    AppSettings(_env_file=None, adapters={...}) is NOT enough on its own:
    pydantic-settings deep-merges a partial ``adapters=`` override with
    whatever config/config.yaml supplies for fields the override doesn't
    mention (confirmed the hard way - a scenario test silently inherited
    this repo's real adapters.computer_use.allowed_roots and passed only by
    accident). chdir'ing to an empty tmp_path first means the relative
    default yaml_file path ("config/config.yaml") resolves to nothing.
    """
    monkeypatch.chdir(tmp_path)
    return AppSettings(_env_file=None, **overrides)


def _recording_provider_from_env() -> OpenAICompatibleProvider:
    """Builds a live provider from THIS machine's real config/config.yaml +
    .env - deliberately the real settings, not the scenario's isolated ones
    (``isolated_settings()`` carries no LLM profiles by design, see its own
    docstring). Profile is ``YBM_SCENARIO_RECORD_PROFILE`` if set, else
    ``llm.default_profile``. Used only when ``YBM_SCENARIO_RECORD`` is set -
    see ``ybm scenario record`` in scripts/ybm.ps1.
    """
    real_settings = load_settings(config_path=_REPO_CONFIG_PATH if _REPO_CONFIG_PATH.exists() else None)
    profile_name = os.environ.get(RECORD_PROFILE_ENV_VAR) or real_settings.llm.default_profile
    profile = real_settings.llm.profiles.get(profile_name)
    if profile is None:
        raise RuntimeError(
            f"{RECORD_PROFILE_ENV_VAR}={profile_name!r} is not a profile in this machine's "
            f"config/config.yaml (llm.profiles: {sorted(real_settings.llm.profiles)})"
        )
    return OpenAICompatibleProvider(profile)


def build_scenario(
    settings: AppSettings,
    *,
    tmp_path: Path,
    fixture_name: str,
    record_with: object | None = None,
    include_auditor: bool = False,
) -> Scenario:
    """Build a scenario. Pass ``record_with=<a live LLMProvider>`` to record a
    fresh fixture instead of replaying one - e.g.
    ``record_with=OpenAICompatibleProvider(settings.llm.profiles["openai_saved"])``.
    Every call the worker makes during that run is persisted to
    ``fixtures/<fixture_name>.json``, ready to commit and replay from after.
    If ``record_with`` is omitted and ``YBM_SCENARIO_RECORD`` is set in the
    environment, a live provider is built automatically from this machine's
    real config (see ``_recording_provider_from_env``) - this is what
    ``ybm scenario record <name>`` uses; a plain scenario/unit test run never
    sets that env var, so this path is never reached unasked.

    Wires the Operator loop (P3 §2.2), mirroring cli.run_worker()'s
    production wiring - the sole execution path since 2026-07-28.

    ``include_auditor=True`` also wires the Auditor (P3 §2.1) - opt-in, not
    the default, because it adds a `generate_text` call the fixture has to
    have recorded; existing fixtures recorded before the Auditor existed
    don't have it and would fail fixture lookup if this defaulted on.
    """
    database = Database(f"sqlite:///{tmp_path / 'agent.db'}")
    database.initialize()
    repositories = Repositories.for_database(database)
    audit = AuditLogger(repositories.audit)

    if record_with is None and os.environ.get(RECORD_ENV_VAR):
        record_with = _recording_provider_from_env()

    fixture_path = FIXTURES_DIR / f"{fixture_name}.json"
    provider: ScriptedLLMProvider | RecordingLLMProvider
    if record_with is not None:
        provider = RecordingLLMProvider(record_with, fixture_path)
    else:
        provider = ScriptedLLMProvider(fixture_path)

    telegram = FakeTelegramClient()
    registry = build_tool_registry(
        settings,
        "http://127.0.0.1:8765",
        provider=provider,
        should_continue=lambda task_id: True,
        artifact_repository=repositories.artifacts,
        task_repository=repositories.tasks,
        repositories=repositories,
        audit_logger=audit,
        telegram_client=telegram,
    )
    policy = PolicyEngine(settings, audit)
    executor = ToolExecutor(
        policy, repositories, audit,
        adapters=registry.adapters,
        tool_definitions=registry.definition_index,
    )
    operator = OperatorLoopService(provider)
    auditor = AuditorService(provider) if include_auditor else None

    worker = TaskWorker(
        repositories,
        audit,
        executor=executor,
        retry_policy=RetryPolicy(settings.limits),
        config_context=registry.context(),
        operator=operator,
        operator_max_steps=settings.operator.max_steps,
        auditor=auditor,
    )

    return Scenario(
        settings=settings, repositories=repositories, audit=audit, worker=worker, provider=provider,
        telegram=telegram,
    )


async def run_task_to_completion(
    scenario: Scenario,
    objective: str,
    *,
    metadata: dict | None = None,
    max_ticks: int = 10,
    auto_approve: bool = False,
) -> TaskRecord:
    """Creates a task and drives worker.process_task() until a terminal
    status. Raises (not hangs) if the task doesn't settle within max_ticks -
    a stuck scenario test should fail fast and loud, not time out silently.

    Stamps ``source_chat_id`` by default, mirroring what the production
    Telegram message handler sets on every real task - without it, any plan
    step that delivers a file (``artifact.deliver``) fails with a synthetic
    "chat_id is required" error that would never happen in production.

    ``auto_approve=True`` simulates a human clicking "approve" the instant a
    task reaches AWAITING_APPROVAL, then keeps ticking - for a test whose
    subject is execution correctness of an operation that is *always*
    gated by design (e.g. code.interpreter's run_python/generate_and_run,
    ``approval_required_operations`` in code_interpreter.py's ToolDefinition
    - "runtime-owned... cannot be bypassed... by design", not a settings
    knob). Without this, AWAITING_APPROVAL is terminal (see
    TERMINAL_STATUSES) and such a test could never observe COMPLETED - which
    is correct default behavior for a test whose actual subject *is* the
    approval gate (it should stay AWAITING_APPROVAL and never call this with
    auto_approve=True).
    """
    task_metadata = {"source_chat_id": SCENARIO_CHAT_ID, **(metadata or {})}
    task = scenario.repositories.tasks.create(objective=objective, metadata=task_metadata)
    for _ in range(max_ticks):
        task = await scenario.worker.process_task(task.id)
        if auto_approve and task.status == TaskStatus.AWAITING_APPROVAL:
            for approval in scenario.repositories.approvals.list_for_task(task.id):
                if approval.status == ApprovalStatus.PENDING:
                    scenario.repositories.approvals.decide_pending(approval.id, ApprovalStatus.APPROVED)
            continue
        if task.status in TERMINAL_STATUSES:
            return task
    raise AssertionError(
        f"task {task.id} did not reach a terminal status within {max_ticks} ticks "
        f"(stuck at {task.status}); either the fixture is missing a replan-cycle "
        f"response or this is a real non-terminating loop"
    )



# A replay miss and a real refusal both leave the task non-COMPLETED, so
# `assert task.status != TaskStatus.COMPLETED` is satisfied either way. Every
# negative scenario test asserted exactly that, and the out-of-roots cases were
# in fact all failing on a missing fixture: their allowed_root is a pytest
# ``tmp_path``, whose ``pytest-<n>`` counter changes every run, so the recorded
# key could never be hit again - not on CI, not on the recording machine. They
# would have passed with the policy check deleted.
_REPLAY_MISS_MARKERS = ("No recorded", "closest fixture")


def assert_completed(task: TaskRecord) -> None:
    """Assert a task completed, and say why it did not when it fails.

    A bare ``assert task.status == TaskStatus.COMPLETED`` reports only that the
    status was FAILED, which on CI - a platform the recording machine cannot
    reproduce locally - leaves nothing to work from. The reason is already in
    the task; this puts it in the assertion message.
    """
    if task.status == TaskStatus.COMPLETED:
        return

    error = str(task.metadata.get("last_worker_error") or "").strip()
    history = task.metadata.get("operator_history")
    if isinstance(history, list):
        steps = "\n".join(
            f"    {index}. {entry.get('tool_name', '?')} ({entry.get('status', '?')})"
            f"{' error=' + str(entry.get('error'))[:200] if entry.get('error') else ''}"
            for index, entry in enumerate(history, start=1)
            if isinstance(entry, dict)
        ) or "    (empty)"
    else:
        steps = "    (no operator history)"

    raise AssertionError(
        f"expected COMPLETED, got {task.status.value}\n"
        f"  last_worker_error: {error or '(none recorded)'}\n"
        f"  operator history:\n{steps}"
    )


def assert_rejected(task: TaskRecord, *, because: str | None = None) -> None:
    """Assert a task was refused by the behaviour under test, not by the harness.

    ``because`` additionally requires a substring in the recorded failure or
    tool history, so the test pins *why* it was refused.
    """
    assert task.status != TaskStatus.COMPLETED, f"expected a refusal, got {task.status}"

    error = str(task.metadata.get("last_worker_error") or "")
    for marker in _REPLAY_MISS_MARKERS:
        assert marker not in error, (
            "task did not complete, but only because the replay fixture did not match:\n"
            f"  {error[:400]}\n"
            "That is a harness miss, not the refusal this test exists to prove. "
            "Re-record this fixture (ybm scenario record <name>)."
        )
    # Distinct from a replay miss, and worth its own message: during a live
    # recording run there is no fixture to miss, and a failed model call would
    # otherwise be reported as a stale fixture and sent someone re-recording.
    assert "operator_decide_failed" not in error, (
        "the operator itself failed, so the task never reached the check under test:\n"
        f"  {error[:400] or '(the exception carried no message)'}\n"
        "On a replay run re-record the fixture; on a live recording run this is "
        "usually a transient model-call failure - retry it."
    )

    if because is None:
        return
    history = task.metadata.get("operator_history")
    haystack = error + json.dumps(history, default=str) if isinstance(history, list) else error
    assert because.casefold() in haystack.casefold(), (
        f"expected the refusal to mention {because!r}; got: {haystack[:400]}"
    )


def filesystem_settings(monkeypatch, tmp_path, allowed_root: str) -> AppSettings:
    """Scenario settings for a filesystem-scoped task: filesystem write on,
    one allowed root.

    Four scenario tests carried a byte-identical private `_settings` doing
    exactly this (docs/archive/HISTORY.md Part 2 §4 item 16).
    """
    caps = default_capability_policies()
    caps[Capability.FILESYSTEM_WRITE] = CapabilityPolicy(
        enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH
    )
    return isolated_settings(
        monkeypatch, tmp_path,
        capabilities=caps,
        adapters={"computer_use": {"enabled": True, "allowed_roots": [allowed_root]}},
    )


def mcp_settings(monkeypatch, tmp_path, server_path, catalog_path) -> AppSettings:
    """Scenario settings for an MCP task: terminal.run on, one stdio server.

    Shared by the two MCP scenario tests, which held identical copies.
    """
    caps = default_capability_policies()
    caps[Capability.TERMINAL_RUN] = CapabilityPolicy(
        enabled=True, requires_approval=False, max_risk_level=RiskLevel.HIGH
    )
    return isolated_settings(
        monkeypatch, tmp_path,
        capabilities=caps,
        mcp=MCPConfig(
            enabled=True,
            catalog_path=str(catalog_path),
            servers={
                "fake": MCPServerConfig(
                    command=sys.executable,
                    args=[str(server_path)],
                    timeout_seconds=MCP_HANDSHAKE_TIMEOUT_SECONDS,
                    # MCPServerConfig.risk_level defaults to HIGH; the fake
                    # echo server's own catalog entry (written separately by
                    # each test via write_mcp_catalog) advertises LOW, which
                    # is what the model reasonably imitates when declaring
                    # risk_level for a call_tool request - a mismatch here
                    # guarantees the request understates the *enforced*
                    # per-server risk (_mcp_required_risk in
                    # tools/mcp_client.py), rejected before it can even
                    # reach the approval gate. Matching them is the fix.
                    risk_level=RiskLevel.LOW,
                )
            },
        ),
    )
