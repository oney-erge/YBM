# AGENTS.md

## Project

YBM (product name: "YBM") is a configurable local agent-control system. A FastAPI/Python backend accepts
Telegram and local requests, applies policy, schedules work, invokes configured tools
or coding agents, and serves a React admin console at `/admin`. A VS Code extension
provides the editor bridge.

Use these as the durable sources of truth:

- `docs/ARCHITECTURE.md` for current components and message flow.
- `docs/STATE_MACHINES.md` for task-control ownership and recovery boundaries.
- `docs/CAPABILITIES.md` for the tool catalog and the access each tool needs.
- `docs/PLAN.md` for where it stands and what is left, in order; `docs/ROADMAP.md` for what is built, the backlog and the known limits.
- `config/config.example.yaml` for supported configuration.
- `docs/INSTALL.md` and `docs/USING.md` for operator-facing setup, commands, and the console tour.
  `README.md` is a short front door; keep detail in the docs and link to it rather than repeating it.
- `docs/archive/` (design rationale, finished plans, review snapshots) is a historical record, never
  evidence that behavior exists. Code comments may cite it for the reasoning behind a decision.

Do not describe planned behavior as implemented. Keep documentation, schemas, and
configuration examples aligned with the code that exists.

## What YBM is for

The owner's own assistant, on one PC, reached through Telegram, running on a local model, meant to stay while models
change. This is the intent every change is judged against; what is built is in the docs above, and the program to get
there is [docs/PLAN.md](docs/PLAN.md). Planned behavior is not implemented behavior.

- It understands instead of asking. It reads what is meant and how big it is from the words, typed or dictated ("take your
  time", "all the daycares around me", "overnight"), states its assumptions in its first reply, and goes. Asking the owner
  is the last resort, for something no route can produce. Autonomy has three levels; at the top one it never asks.
- It remembers, across every chat, by default (a setting, on): facts about the owner, his family and home, preferences,
  and projects with their decisions and reasons. Facts that change are updated with their history kept. A project is never
  just open or closed; it can be picked up weeks later.
- Long and exhaustive work runs by itself as a loop that judges its own result against a measured standard and does more
  until it is good enough or reaches its ceiling, with a time estimate up front and updates along the way.
- It works a big build with the owner: steps, a bill of materials, what to buy, a schedule, check-ins.
- It uses the computer and the web itself: signs in, creates accounts when needed (choosing the username and password,
  storing them, writing a skill for the site, and telling the owner). It does not solve checks built to stop automated
  sign-ups; it takes another route.
- It never buys anything or uses the owner's cards on its own, at any autonomy level. It prepares the purchase up to the
  payment page and asks once, with the exact items, seller, total, card (last four digits) and address; a yes covers that one
  order. This rule lives in the purchase skill and in the worker's approval gate. Everything else at the top level is
  autonomous.
- When nothing fits it writes code, tests it read-only on real data, runs it, and keeps it; recurring work becomes a monitor.
- When it truly cannot, it ends with a blocker report: what was tried, why it failed, the routes left with exact steps, its
  pick, and the one thing it needs. Never a bare "I could not".
- It proves what it did (receipts, sources, observed results), keeps all data in open formats, and does not depend on one model.
- Fixes are holistic: one mechanism per need, reusing existing objects (goal, project, plan, job, memory, skill). No
  per-example patches.

## How a change is made

Where each job lives is in [docs/DESIGN.md](docs/DESIGN.md). Every change to `backend/src/agent_control/` is one of two kinds, and says which in its pull request
(the `design-rule` check reads it):

- **Minimal**: it fits a home in DESIGN.md and adds no module, table, config key, tool or tool operation, prompt file or pattern that matches wording, in at most
  150 lines, with the failing case it fixes and a test.
- **Design**: anything else. It adds an entry to DESIGN.md, deletes what it replaces in the same series, re-records the fixtures it changes, and compares the same
  live cases before and after.

A fix for one wording, one tool habit or one example is neither: it is refused, and the home is fixed instead. Two counts may only go down
(`docs/design_baseline.json`): the patterns that match wording and the lines of `call_rewrites.py`.

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
  `backend` - matches what CI does before `uv run --frozen pytest -n auto --dist loadfile`
  (the whole suite in parallel, about 35 seconds on 16 cores; while editing, add `--testmon` to run only
  the tests a change can affect). Without
  that sync, a bare `uv run --frozen pytest` can silently resolve to a
  system-installed `pytest` outside the project's `.venv` and fail with
  `ModuleNotFoundError` instead of running the suite.
- Python quality: from `backend`, run `uv run --frozen ruff check .` (same
  sync prerequisite as above).
- Admin console: from `frontend`, run `npx tsc -b --noEmit` and `npm run build`.
- VS Code extension: from `vscode-extension`, run `npm run compile`.
- WhatsApp sidecar: from `whatsapp-bridge`, run `npm run check`. Use this, not
  `node --check` - the latter only parses syntax and will pass a file whose imports
  cannot resolve at all (see docs/archive/HISTORY.md Part 6's review pass).
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
