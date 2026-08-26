# AGENTS.md

## Project

YBM (product name: "YBM") is a configurable local agent-control system. A FastAPI/Python backend accepts
Telegram and local requests, applies policy, schedules work, invokes configured tools
or coding agents, and serves a React admin console at `/admin`. A VS Code extension
provides the editor bridge.

Use these as the durable sources of truth:

- `docs/ARCHITECTURE.md` for current components and message flow.
- `docs/HISTORY.md` for design rationale and completed phases.
- `config/config.example.yaml` for supported configuration.
- `README.md` for operator-facing setup and commands.

Do not describe planned behavior as implemented. Keep documentation, schemas, and
configuration examples aligned with the code that exists.

## Supported Commands

The stable source-checkout entry points are `run.bat` and `run.ps1` on Windows,
`run.command` on macOS, and `run.sh` on Linux. They provide the shared `run`,
`doctor`, `repair`, `docker`, `logs`, and `stop` actions. The default action
installs or updates what is missing, starts the stack, waits for readiness, and
opens the console. It is idempotent.

The release-specific `YBM.bat` wraps `scripts\ybm.ps1 run`; `ybm.sh` is self-contained because the
installed Python CLI lives inside the virtualenv it has to create and so cannot
bootstrap it. Keep the runtime extras in `ybm.sh` in step with
`Get-YbmRuntimeExtraArgs` in `scripts/ybm.ps1`. `scripts/install.sh` and
`scripts/install.ps1` download the complete latest release, including the
prebuilt admin console, verify its checksum, and hand off to those launchers.
`Install-YBM.bat` is the public double-click bootstrap for the PowerShell path.

For development, use `scripts/ybm.ps1` instead of assembling service commands by hand:

```powershell
.\scripts\ybm.ps1 setup
.\scripts\ybm.ps1 doctor
.\scripts\ybm.ps1 start
.\scripts\ybm.ps1 status
.\scripts\ybm.ps1 logs worker -Follow
.\scripts\ybm.ps1 stop
.\scripts\ybm.ps1 test
```

Additional operator workflows:

```powershell
.\scripts\ybm.ps1 e2e --only desktop_inspection
.\scripts\ybm.ps1 trace <task_id>
.\scripts\ybm.ps1 scenario record <name>
.\scripts\ybm.ps1 package-extension
.\scripts\ybm.ps1 tray
.\scripts\ybm.ps1 autostart enable
.\scripts\ybm.ps1 backup
.\scripts\ybm.ps1 check-updates
```

`scenario record` makes a real LLM call. Live E2E requires the setup documented in
`e2e/README.md`; do not run either workflow implicitly.

## Architecture Boundaries

- `backend/src/agent_control/` owns domain models, policy, orchestration, adapters,
  persistence, and API behavior.
- `backend/tests/` contains unit and deterministic scenario coverage.
- `frontend/` is the React admin console (Vite/TypeScript); it talks to the backend
  only through `/admin/api/*` and never embeds domain logic of its own. `ybm ui-build`
  builds it into `backend/src/agent_control/static/admin/`, served at `/admin`.
- `vscode-extension/` is a TypeScript bridge; keep editor-specific behavior out of
  the backend domain layer.
- `scripts/ybm.ps1` is the public lifecycle interface. Keep service scripts behind it.
- `config/config.example.yaml` is safe, committed configuration. Local
  `config/config.yaml` and `.env` are private runtime state.
- `.agent_control/`, `backend/agent_control.db`, logs, screenshots, generated
  workspaces, caches, `.venv`, `node_modules`, and `backend/src/agent_control/static/`
  are generated and must not be committed.

Keep tool execution policy-bound. Preserve approval gates, allowlists, workspace
boundaries, bounded retries, redaction, and the disabled-by-default settings for
terminal, filesystem, browser, desktop control, dependency installation, and Git
pushes. Never log secret values or place them in task output.

Treat prompt, tool-schema, and workspace-layout changes as behavioral changes.
Update or re-record only the affected scenario fixtures, review the generated fixture,
and never hide a degraded fallback path.

## Change Style

- Inspect the relevant code, configuration, and logs before editing.
- Define the expected outcome and make the smallest coherent change.
- Reuse existing registry, policy, adapter, and supervisor patterns.
- Fix a shared root cause instead of adding prompt-specific or example-specific
  branches.
- Preserve unrelated local changes and avoid speculative refactors.
- Distinguish observed facts, inferences, and unverified assumptions.
- Use ASCII punctuation in repository text. Do not use the Unicode em dash
  (`U+2014`); rewrite the sentence or use a spaced ASCII hyphen.

## Verification

Choose checks in proportion to the change:

- Documentation or agent-guidance only: verify referenced paths and commands, then
  run `git diff --check`; application tests are not required.
- Backend behavior: `.\scripts\ybm.ps1 test` and the focused affected test. On a
  checkout where `ybm setup`/`ybm.ps1 setup` has not run (`backend/.venv` doesn't
  exist yet), first run `uv sync --frozen --extra test --extra dev` from
  `backend` - matches what CI does before `uv run --frozen pytest`. Without
  that sync, a bare `uv run --frozen pytest` can silently resolve to a
  system-installed `pytest` outside the project's `.venv` and fail with
  `ModuleNotFoundError` instead of running the suite.
- Python quality: from `backend`, run `uv run --frozen ruff check .` (same
  sync prerequisite as above).
- Admin console: from `frontend`, run `npx tsc -b --noEmit` and `npm run build`.
- VS Code extension: from `vscode-extension`, run `npm run compile`.
- WhatsApp sidecar: from `whatsapp-bridge`, run `npm run check`. Use this, not
  `node --check` - the latter only parses syntax and will pass a file whose imports
  cannot resolve at all (see docs/HISTORY.md Part 6's review pass).
- Full runtime or integration changes: run `doctor`, then the narrowest relevant
  live or E2E flow only when its prerequisites and external effects are understood.

Never claim a check ran unless its output was observed. Report skipped checks and why.

## Git and Handoff

- Keep commits focused and use the configured repository-owner identity.
- Do not add assistant names, co-author trailers, session links, or tool attribution
  to commits, branches, pull requests, or release notes.
- Do not enable external writes, installs, desktop control, or Git pushes merely
  because a task could benefit from them; require explicit scope and existing policy.
- Finish with: what changed, what was verified, what was not verified, and remaining
  risks or next steps.


## Install and run contract

- Keep `run.bat`, `run.ps1`, `run.command`, and `run.sh` as the stable
  user entry points. They must keep the same `run`, `doctor`, `repair`,
  `docker`, `logs`, and `stop` actions where the application supports them.
- Use the `native-app-delivery` Codex skill when changing first-run setup,
  repair, Docker, or launcher behavior. That is an internal workflow name and
  must not appear in product copy or the public README.
- Keep shared install mechanics in `scripts/install-utils.ps1` and
  `scripts/install-utils.sh`. Preserve idempotent reruns, bounded transient
  retries, install locking, disk checks, user state, and `.setup/install.log`.
- Verify launcher changes with PowerShell parsing, `bash -n`, the focused
  delivery audit, and `docker compose config`. Do not run the full application
  test suite unless the change affects application behavior.
