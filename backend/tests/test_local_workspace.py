from __future__ import annotations

import json
import os
import signal
import subprocess

import pytest

from agent_control.config import AdapterFactoryConfig, WorkspaceAdapterConfig
from agent_control.schemas import Capability, ToolCallRequest, ToolCallResult, ToolResultStatus
from agent_control.tools.adapter_factory import AdapterFactoryAdapter
from agent_control.tools.local_workspace import LocalWorkspaceAdapter, LocalWorkspaceWebAppAdapter, _verify_local_workspace

@pytest.mark.asyncio
async def test_local_workspace_web_app_creates_files_and_url(tmp_path) -> None:
    adapter = LocalWorkspaceWebAppAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_test",
        tool_name="workspace.web_app",
        capability=Capability.FILESYSTEM_WRITE,
        input={"objective": "Create a hello world web app and launch it"},
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["url"].startswith("http://127.0.0.1:")
    workspace = tmp_path / "workspaces" / "task_test"
    assert (workspace / "index.html").exists()
    assert (workspace / "README.md").exists()

    _stop_process(int(result.output["server_pid"]))

@pytest.mark.asyncio
async def test_local_workspace_prepare_and_write_files(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_files",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "write_files",
            "objective": "Create project files",
            "files": [{"path": "src/app.py", "content": "print('hello')\n"}],
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    workspace = tmp_path / "workspaces" / "task_files"
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["workspace_dir"] == str(workspace)
    assert (workspace / "TASK.md").exists()
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == "print('hello')\n"


@pytest.mark.asyncio
async def test_local_workspace_rejects_a_different_explicit_workspace_instead_of_ignoring_it(tmp_path) -> None:
    managed_root = tmp_path / "workspaces"
    requested = tmp_path / "external-project"
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(managed_root), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_explicit_elsewhere",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "prepare", "workspace_path": str(requested)},
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    assert result.status == ToolResultStatus.FAILED
    assert "does not match this task's managed workspace" in (result.error_message or "")
    assert not requested.exists()
    assert not (managed_root / "task_explicit_elsewhere").exists()

@pytest.mark.asyncio
async def test_local_workspace_materializes_static_app_from_assistant_output(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_materialize",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "materialize_static_app",
            "objective": "Create a modern app about ferrets",
            "source_text": """```html filename=index.html
<!doctype html><html><body><h1>Ferret Studio</h1></body></html>
```
```css filename=styles.css
body { font-family: sans-serif; }
```""",
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    workspace = tmp_path / "workspaces" / "task_materialize"
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["materialized_from"] == "assistant_output"
    assert "Ferret Studio" in (workspace / "index.html").read_text(encoding="utf-8")
    assert (workspace / "styles.css").exists()

@pytest.mark.asyncio
async def test_local_workspace_materialize_creates_missing_local_assets(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_missing_asset",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "materialize_static_app",
            "objective": "Create a modern app about monkeys",
            "source_text": """```html filename=index.html
<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body><h1>Monkeys</h1><script src="script.js"></script></body></html>
```
```css filename=styles.css
body { font-family: sans-serif; }
```""",
            "allow_fallback_template": False,
            "require_index": True,
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    workspace = tmp_path / "workspaces" / "task_missing_asset"
    assert result.status == ToolResultStatus.SUCCEEDED
    assert (workspace / "script.js").exists()
    assert str(workspace / "script.js") in result.output["files"]

@pytest.mark.asyncio
async def test_local_workspace_launch_reports_existing_static_files(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    workspace = tmp_path / "workspaces" / "task_launch_files"
    workspace.mkdir(parents=True)
    (workspace / "index.html").write_text("<!doctype html><html><body>ok</body></html>", encoding="utf-8")
    (workspace / "styles.css").write_text("body { color: teal; }", encoding="utf-8")
    request = ToolCallRequest(
        task_id="task_launch_files",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "launch_static", "objective": "Launch app", "ensure_index": False},
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    assert result.status == ToolResultStatus.SUCCEEDED
    assert str(workspace / "index.html") in result.output["files"]
    assert str(workspace / "styles.css") in result.output["files"]
    _stop_process(int(result.output["server_pid"]))

@pytest.mark.asyncio
async def test_local_workspace_strict_materialize_rejects_missing_app_files(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_strict_materialize",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "materialize_static_app",
            "objective": "Create a modern duck app",
            "source_text": "I could not create files.",
            "allow_fallback_template": False,
            "require_index": True,
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    assert result.status == ToolResultStatus.FAILED
    assert "materializable static app files" in (result.error_message or "")

@pytest.mark.asyncio
async def test_local_workspace_strict_materialize_requires_index(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_missing_index",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "materialize_static_app",
            "objective": "Create a modern duck app",
            "source_text": """```css filename=styles.css
body { color: teal; }
```""",
            "allow_fallback_template": False,
            "require_index": True,
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    assert result.status == ToolResultStatus.FAILED
    assert "index.html" in (result.error_message or "")

@pytest.mark.asyncio
async def test_adapter_factory_scaffolds_cached_adapter(tmp_path) -> None:
    adapter = AdapterFactoryAdapter(AdapterFactoryConfig(root_dir=str(tmp_path / "adapters")))
    request = ToolCallRequest(
        task_id="task_adapter",
        tool_name="adapter.factory",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "scaffold",
            "adapter_name": "browser_bookmarks",
            "objective": "Create an adapter for organizing browser bookmarks",
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)

    adapter_dir = tmp_path / "adapters" / "browser_bookmarks"
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["execution_policy"] == "sandbox_then_hot_register"
    assert (adapter_dir / "manifest.json").exists()
    assert (adapter_dir / "adapter.py").exists()


@pytest.mark.asyncio
async def test_adapter_factory_preserves_natural_scaffold_fields_and_accepts_adapter_id(tmp_path) -> None:
    root = tmp_path / "adapters"
    adapter = AdapterFactoryAdapter(AdapterFactoryConfig(root_dir=str(root)))
    request = ToolCallRequest(
        task_id="task_adapter",
        tool_name="adapter.factory",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "scaffold",
            "name": "stock_quotes",
            "description": "Fetch current quotes from a stock API",
            "api_endpoint": "https://example.test/quotes/{ticker}",
            "capabilities": ["get_quote", "get_daily_change"],
            "auto_load": False,
        },
    )

    result = await adapter.execute(request)
    manifest = json.loads((root / "stock_quotes" / "manifest.json").read_text(encoding="utf-8"))
    sandbox = await adapter.execute(
        ToolCallRequest(
            task_id="task_adapter",
            tool_name="adapter.factory",
            capability=Capability.FILESYSTEM_WRITE,
            input={"operation": "sandbox_execute_once", "adapter_id": "stock_quotes"},
        )
    )

    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.output["adapter_name"] == "stock_quotes"
    assert manifest["objective"] == "Fetch current quotes from a stock API"
    assert manifest["operations"] == ["get_quote", "get_daily_change"]
    assert manifest["api_endpoint"] == "https://example.test/quotes/{ticker}"
    assert manifest["auto_load"] is False
    assert sandbox.status == ToolResultStatus.SUCCEEDED

def _stop_process(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)


# ---- _verify_local_workspace (docs/ROADMAP.md "Finish the Proof": re-read
# the workspace, don't trust the adapter's own claim) ----------------------

@pytest.mark.asyncio
async def test_verify_local_workspace_confirms_written_files(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_files",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "write_files",
            "objective": "Create project files",
            "files": [{"path": "src/app.py", "content": "print('hello')\n"}],
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)
    verification = _verify_local_workspace(request, result)

    assert verification is not None
    assert verification.checked == 1  # changed_paths - just src/app.py, not TASK.md too
    assert verification.verified == 1
    assert verification.missing == []
    assert verification.ok is True


@pytest.mark.asyncio
async def test_verify_local_workspace_flags_a_written_file_missing_after_the_fact(tmp_path) -> None:
    adapter = LocalWorkspaceAdapter(
        WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), web_port_start=8890, open_browser=False)
    )
    request = ToolCallRequest(
        task_id="task_files",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={
            "operation": "write_files",
            "objective": "Create project files",
            "files": [{"path": "src/app.py", "content": "print('hello')\n"}],
        },
        timeout_seconds=30,
    )

    result = await adapter.execute(request)
    (tmp_path / "workspaces" / "task_files" / "src" / "app.py").unlink()
    verification = _verify_local_workspace(request, result)

    assert verification is not None
    assert verification.verified == 0
    assert "file not found" in verification.missing[0]
    assert "app.py" in verification.missing[0]


@pytest.mark.asyncio
async def test_verify_local_workspace_falls_back_to_files_when_changed_paths_is_absent(tmp_path) -> None:
    """`prepare` has no `changed_paths` key at all - falls back to the
    full `files` listing (just TASK.md here) instead of returning None."""
    adapter = LocalWorkspaceAdapter(WorkspaceAdapterConfig(root_dir=str(tmp_path / "workspaces"), open_browser=False))
    request = ToolCallRequest(
        task_id="task_prepare",
        tool_name="workspace.manage",
        capability=Capability.FILESYSTEM_WRITE,
        input={"operation": "prepare", "objective": "Set up the workspace"},
        timeout_seconds=30,
    )

    result = await adapter.execute(request)
    assert "changed_paths" not in result.output
    verification = _verify_local_workspace(request, result)

    assert verification is not None
    assert verification.checked == 1
    assert verification.verified == 1


def test_verify_local_workspace_returns_none_without_any_file_list() -> None:
    request = ToolCallRequest(
        task_id="task_x", tool_name="workspace.manage", capability=Capability.FILESYSTEM_WRITE, input={"operation": "prepare"}
    )
    result = ToolCallResult(request_id="toolres_ws", status=ToolResultStatus.SUCCEEDED, output={})

    assert _verify_local_workspace(request, result) is None
