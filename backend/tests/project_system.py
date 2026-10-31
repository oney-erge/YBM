"""Isolated real API/worker/tool system; only model decisions are scripted.

Shared by the integration test and browser test server. Never uses the
operator's configuration, accounts, work folders, or live model providers.
"""
from pathlib import Path

from fastapi import FastAPI

from agent_control.admin import create_admin_router
from agent_control.config import AppSettings, CapabilityPolicy
from agent_control.orchestration import TaskWorker, ToolExecutor
from agent_control.policy import PolicyEngine
from agent_control.schemas import Capability, OperatorAction, OperatorDecision, RiskLevel
from agent_control.storage import AuditLogger, Database, Repositories
from agent_control.tools.registry import build_tool_registry
from agent_control.tools.vscode_bridge import VSCodeBridgeStore


class ComparisonOperator:
    def __init__(self, root: Path, *, ask_first: bool = False):
        self.root = root
        self.ask_first = ask_first
        self.contexts: list[str] = []

    async def decide(self, objective, config_context, history, **kwargs):
        self.contexts.append(config_context)
        if self.ask_first:
            self.ask_first = False
            return OperatorDecision(action=OperatorAction.ASK_USER, question="Which criteria should I use?")
        if any(entry.get("status") == "succeeded" for entry in history):
            return OperatorDecision(action=OperatorAction.DONE, final_answer="Evidence comparison created.")
        return OperatorDecision(
            action=OperatorAction.CALL_TOOL, tool_name="document.manage", risk_level=RiskLevel.HIGH,
            tool_input={
                "operation": "compare_documents", "criteria": ["cost", "timeline"],
                "source_paths": [str(self.root / "a.txt"), str(self.root / "b.txt")],
                "output_name": str(self.root / "comparison.xlsx"),
                "scope_target": str(self.root),
            },
        )


def make_system(root: Path, *, ask_first: bool = False):
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_text("Cost: 10 USD.\nTimeline: two weeks.", encoding="utf-8")
    (root / "b.txt").write_text("Cost: 12 USD.\nTimeline: one week.", encoding="utf-8")
    settings = AppSettings(
        _env_file=None,
        storage={"database_url": f"sqlite:///{root / 'test.db'}", "artifact_dir": str(root)},
        channels={"telegram": {"enabled": False}},
        adapters={"workspace": {"root_dir": str(root)}, "computer_use": {"allowed_roots": [str(root)]}},
        llm={"default_profile": "scripted", "profiles": {"scripted": {"provider": "mock", "model": "scripted", "api_key": "test-placeholder"}}},
        capabilities={
            Capability.FILESYSTEM_WRITE: CapabilityPolicy(enabled=True, requires_approval=True, max_risk_level=RiskLevel.HIGH),
        },
    )
    database = Database(settings.storage.database_url)
    database.initialize()
    repositories = Repositories.for_database(database)
    audit = AuditLogger(repositories.audit)
    registry = build_tool_registry(settings, "http://127.0.0.1:4181", artifact_repository=repositories.artifacts)
    executor = ToolExecutor(PolicyEngine(settings, audit), repositories, audit, registry.adapters, registry.definitions)
    operator = ComparisonOperator(root, ask_first=ask_first)
    worker = TaskWorker(repositories, audit, executor=executor, operator=operator)
    app = FastAPI()
    app.include_router(create_admin_router(lambda: settings, lambda: repositories, VSCodeBridgeStore()))
    return app, repositories, audit, worker, operator
