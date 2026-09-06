# Changelog

Notable changes to YBM. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `frontend`: resolved the `fast-uri` (high) and `qs` (moderate) advisories
  flagged by the scheduled dependency audit (#32) via `npm audit fix`.
- The version a released archive reports through `ybm check-updates` now
  always matches the git tag it was built from. `scripts/package_release.py`
  previously copied `backend/pyproject.toml` into the payload unchanged, so
  the installed package's own metadata - what `check-updates` actually reads
  - stayed on whatever version was last committed there rather than the tag
  the release was named after; a release could ship in a way that reported
  itself as an *older* version than the download it came from. The packaging
  step now stamps the staged copy with the release version being built.
  Also caught up the committed value itself, which had drifted a full
  release behind (`0.1.2` while `v0.1.3` was already public).

### Added

- `frontend`: a vitest + Testing Library unit-test setup, seeded with
  coverage for access-mode preset computation, task-status action gating,
  and the chat/task API response schemas - the console had TypeScript and a
  production build as its only automated gates until now.
- `frontend`: Playwright coverage for approving/denying a pending action,
  an expired approval disabling every decision button, and a failed task's
  trace highlighting the step that failed.
- Task receipts now report `execution` (calls attempted/succeeded/failed)
  and `verification` (items mechanically re-checked/verified/missing),
  surfaced in the chat receipt card and the plain-text export. Backed by a
  new `ToolDefinition.verify` hook (currently implemented for
  `filesystem.manage`'s `apply_manifest`, which re-reads the destination
  and source paths on disk after every move/copy/rename instead of trusting
  the adapter's own success claim) and a `ToolCallResult.verification`
  field `ToolExecutor` attaches automatically on a succeeded call.
- `ToolDefinition` gained a declarative `operation_egress` field:
  `ToolExecutor` now records an operation's outbound host itself by reading
  the call's own reported URL, so a tool needs no manual
  `egress.record_egress()` call site to be covered. `http.request` moved
  onto this from its old manual call; `browser.open` and `browser.control`
  are now covered for the first time - previously only `http.request`
  contacted egress, so browser traffic was invisible to receipts.
- `ToolDefinition.operation_content_trust` labels which operations return
  content this machine does not control - a web page, an HTTP response, an
  MCP server's own reply, a document someone else authored
  (`browser.open`, `browser.control`'s page-reading operations,
  `http.request`, `web.search`, `mcp.client`'s `call_tool`,
  `document.manage`). `ToolExecutor` stamps it onto
  `ToolCallResult.content_trust`, and it now shows as an "untrusted
  content" badge on the matching step in a task's trace. Does not yet
  reach the Operator prompt itself - see `docs/GAPS.md`.
- An automatically-retried TIMEOUT or transient failure on a risky write
  (filesystem/terminal/desktop/browser-control/VS Code, or high/critical
  risk) now records an explicit warning in the retry's history entry - it
  may have already taken effect before the connection was lost, so verify
  before repeating it - the same "silently retrying can do a thing twice"
  reasoning `reconcile_orphaned_tasks` already applies to a crashed worker,
  now reaching the ordinary in-process retry path the next `decide()` call
  actually reads. A clean rejection (rate-limited, quota exhausted) still
  gets no such warning - nothing ran yet in that case.
- Per-role LLM models: `concierge_profile`, `operator_profile`, and
  `auditor_profile` let Concierge, Operator, and Auditor use different
  profiles instead of always sharing `default_profile` - configurable from
  Settings' new "Per-role models" card (advanced mode) or directly in
  `config.yaml`. A new `fallback_chain` (ordered list of profile names)
  replaces single-fallback `fallback_profile` when set, and each entry gets
  its own cooldown after failing so a subsequent call skips straight to the
  next one instead of re-paying that entry's timeout. `_is_unavailability`
  now also treats HTTP 429/401/403 as failover-worthy, alongside the
  existing 5xx/timeout/connection-error handling (still not 400 - a request
  bug fails the same way against any profile). A receipt now says when a
  fallback model answered somewhere in the task.
- Scoped, revocable "Allow for this task" grants: a grant now inherits the
  exact path/target scope of the call it was approved from (so "allow this
  move in Downloads" no longer silently covers a later move anywhere else),
  carries a 200-operation cap so it's never unbounded for the rest of the
  task's TTL, and can be revoked early. The Access page's new "Active
  grants" card lists every currently-usable grant across every task -
  scope, usage, time remaining - with a one-click revoke, closing the gap
  `docs/UI_UX_AUDIT.md` named: "there is still no way to see or revoke a
  live one."
- MCP servers can now be added, edited, tested, and removed from Settings
  instead of by hand-editing `config.yaml` - three new endpoints
  (`POST`/`DELETE /api/config/mcp/servers[/…]`, `POST …/test`, the last
  running a real stdio MCP handshake against the server). Env values are
  write-only: the response never echoes them back, and leaving the env
  field blank on an edit keeps the existing values instead of wiping them.
- A pending `adapter.factory` `promote_after_approval` approval now shows
  the actual generated adapter source and a real sandbox test result
  (`GET /api/adapters/review`), not just `adapter_dir`/`approved=true` -
  approving one used to be a decision made from the tool name alone.
- A new Insights page (`GET /api/dashboard`) reports cross-task reliability
  over a 7- or 30-day window: completion and *verified*-completion rate
  (the same mechanical `ToolVerification` data a task receipt already
  claims, not the model's word that it finished), failure/retry counts,
  token spend, a per-tool failure-rate table with a least-reliable-tool
  callout, and per-model/per-task-type breakdowns. Entirely computed on
  read from existing task/tool-invocation data - nothing new persisted.
- A completed task can now be replayed (`POST /api/tasks/{id}/replay`, a
  new "Replay" button on its trace page): a new task is created whose
  operator loop deterministically reissues that task's own succeeded tool
  calls in order, through the exact same approval, retry, and verification
  pipeline a live LLM-driven task uses - authority does not carry over, so
  a step that needed approval the first time needs it again. Delegated and
  parallel-batch calls are excluded from the replay plan for now, since
  reissuing those needs different handling than a single call; a step that
  fails or is denied on replay blocks the same way it would live, and a
  timeout or rate-limit is reissued up to three times before giving up
  rather than looping forever. Built as a thin scripted-decision layer over
  the existing operator loop rather than a second execution engine, so it
  needed no changes to `ToolExecutor`, `PolicyEngine`, or verification.
- A task receipt and its trace steps now say plainly when nothing was
  mechanically re-checked, instead of quietly showing nothing. Only one
  tool (`filesystem.manage`'s `apply_manifest`) has a `verify()` hook
  today, so a receipt whose calls all succeeded through some other tool
  previously rendered identically to one where every check came back
  clean - "checked, all good" and "never checked" looked the same because
  the verification line only appeared when `checked > 0`. The receipt's
  `verification` now also reports `not_checked` (succeeded calls whose
  tool had no hook) and `unverified_tools`, rendered as a neutral "Not
  verified - no automatic check yet for ..." line rather than the
  destructive/success styling used for an actual check result. Each trace
  step carries the same distinction (a new `verification` field, joined
  onto `operator_history` the same way `content_trust` already is), shown
  as a "not verified" badge next to a succeeded step whose tool has no
  hook. The reliability dashboard gained matching fields -
  `checked_completed`/`checked_completed_pct`/`verification_coverage_pct`
  and a per-tool `not_checked` count - so `verified_completed_pct` no
  longer has to be read without knowing whether its denominator was even
  checkable.
- Replaying a task no longer blindly reissues a timed-out consequential
  write. A `RATE_LIMITED` result is a clean pre-execution rejection -
  nothing ran yet, so reissuing is always safe - but a `TIMEOUT` on a
  filesystem/terminal/desktop/browser-control/VS Code write (the same
  "consequential" test `reconcile_orphaned_tasks` already applies to a
  crashed worker's in-flight call) means the call may have reached the far
  end before the connection was lost, and replay had no equivalent of the
  warning a live task's history entry gets in that situation. It now stops
  and asks instead of guessing: establishing that a reissue is safe would
  mean asking the tool's own `verify()` hook, but every hook today
  (`filesystem.manage`'s `apply_manifest` included) reads the completed
  call's reported output to decide what to re-check, and a timeout never
  produced one - so a consequential write's timeout now blocks the replay
  on the first occurrence, naming the step and why, rather than retrying a
  few times first. A tool no longer in the registry is treated the same
  fail-safe way. Reads and other low-risk calls are unaffected - a timeout
  there still reissues, bounded the same as before.
- Three more tools mechanically re-check their own effect on success
  instead of leaving `ToolCallResult.verification` as None
  (`filesystem.manage`'s `apply_manifest` was previously the only one):
  `code.interpreter` re-reads the workspace after `run_python`/
  `generate_and_run`/`solve_once`/`build_temp_helper`/`repair_script` to
  confirm every file the adapter's own before/after snapshot diff claimed
  as created or modified actually exists, and every file it claimed
  deleted does not; `document.manage` re-opens a `create_presentation`/
  `update_presentation`'s output `.pptx` (a zip) and counts its real
  `ppt/slides/slideN.xml` entries to confirm the claimed `slide_count`
  matches what is actually in the file, not just that a file with that
  name exists; `workspace.manage` re-reads the workspace after
  `prepare`/`write_files`/`materialize_static_app`/`launch_static`/
  `web_app_preview` the same way `code.interpreter` does. Chosen as the
  next-highest-frequency previously-unverified tools after the receipt's
  new `not_checked`/`unverified_tools` fields made that gap visible per
  call instead of only in the aggregate.
- A completion obligation can now be declared instead of only inferred.
  `MessageClassification` gained an optional `expected_postconditions`
  field the Concierge can populate at intake, before any tool has run;
  `orchestration/fulfillment.py`'s `expected_postconditions()` prefers a
  non-empty declaration over its own keyword-matched guess at the
  objective/original message text. Purely additive: the field is
  transmitted through the structured-output JSON schema, not embedded in
  prompt text, so it changed no `system_prompt`/`user_prompt` string and
  needed no scenario fixture re-record - every recorded fixture and every
  live classifier today simply omits it, which the fallback treats
  identically to "nothing declared". A hallucinated or unrecognized value
  is dropped rather than failing the whole classification, at both the
  schema boundary and the fulfillment lookup. Keyword inference is
  unchanged and remains the operative path until a classifier is actually
  updated to populate the field.
- Untrusted content is now fenced in the Operator prompt itself, not just
  labeled on the trace/evidence views. `ToolDefinition.operation_content_trust`
  (`browser.open`, `browser.control`'s page-reading operations,
  `http.request`, `web.search`, `mcp.client`'s `call_tool`,
  `document.manage`'s read operations) now reaches `worker.py`'s recorded
  history entry, and `_format_history` wraps such an entry's output in an
  explicit `[UNTRUSTED CONTENT ...]` / `[END UNTRUSTED CONTENT]` pair
  telling the model to read it as data and not follow anything it says.
  The wrapped content is neutralized against a payload that contains a
  literal copy of either marker first, so it cannot forge a fake boundary
  and make later content look like it's back outside the fence. This is a
  textual mitigation, not a guarantee - it does not prove a live model
  declines a given injection attempt, only that the boundary is explicit
  and cannot be trivially spoofed from inside the content itself.
  2 of 16 scenario fixtures needed new entries because this changed
  `_format_history`'s rendered text where untrusted content appeared
  earlier in a task's history; both were re-keyed by reversing the fence
  transformation on the newly-requested prompt to find the corresponding
  old fixture entry and copying its response forward, since the fence
  changes the prompt's presentation but not the correct decision - no live
  LLM time was needed.
- A completed task's replay plan can now be saved as a named, reusable
  "workflow" (new "Save as workflow" button on the trace page, alongside
  Replay) and run again later against different values, through the
  identical approval/retry/verification pipeline replay already provides.
  Saving lets specific literal values in the plan be named as parameters
  (e.g. a folder path becomes `{{folder}}`); running fills them back in.
  Not a workflow designer: the plan is always `build_replay_plan`'s real
  output from one actual run, and the only editing surface is naming
  values already present in it. Refuses to save (400, naming the exact
  steps) when the plan can't be fully captured - a delegated sub-task or a
  parallel-batch member `build_replay_plan` itself excludes - rather than
  silently saving a workflow missing part of what the source task did.
  New `workflows` table/repository, `TaskWorkflow` schema, and
  `POST /api/tasks/{id}/save_workflow`, `GET/DELETE /api/workflows[/{id}]`,
  `POST /api/workflows/{id}/run` endpoints. New Workflows page (reachable
  from the Agent hub) lists, runs, and deletes saved workflows - no visual
  workflow editor, matching this project's stated non-goal of a builder
  over a pipeline that isn't graph-shaped.

## [0.1.3] - 2026-08-11

### Fixed

- The publish-only release job now targets `oney-erge/YBM` explicitly after
  downloading artifacts, so it does not require a local Git checkout to create
  the release page.
- Artifact upload/download actions use their current Node.js 24-based releases,
  removing the hosted-runner Node.js 20 deprecation warning.

## [0.1.2] - 2026-08-11

### Changed

- The one-command Windows and macOS/Linux installers now download the complete
  latest release instead of source archives, so the admin console works without
  Node.js. Re-running an installer refreshes application files while preserving
  local state.
- Release archives use stable `YBM-windows.zip` and `YBM-unix.tar.gz` names so
  installers and people can download the latest build directly.
- Installation is presented as two three-step Windows choices and one
  three-step macOS/Linux path; advanced and development routes moved out of
  the README's critical path.
- The MSI now has a visible minimal install UI and a final **Launch YBM now**
  action. Release CI installs the MSI, provisions its runtime, checks backend
  health, stops it, and uninstalls it instead of treating a successful build as
  proof that installation works.
- Windows releases include `Install-YBM.bat` for a no-MSI double-click path and
  `Install-YBM.ps1` for direct inspection or terminal use. Downloaded archives
  are checked against the release's SHA256 list before extraction.
- Tagged releases publish the tested headless image as
  `ghcr.io/oney-erge/ybm:<version>` and `ghcr.io/oney-erge/ybm:latest`.

### Removed

- Superseded planning archives, stale first-run screenshots, and unused logo
  explorations. Git history remains the record for those design iterations.

## [0.1.1] - 2026-08-11

### Added

- **Containerised headless profile.** `Dockerfile`, `docker-compose.yml` and
  `.dockerignore`. Telegram/WhatsApp intake, the operator loop, the code
  interpreter, MCP and the admin console all run in a container; desktop
  control, screenshots and the VS Code bridge cannot, and `ybm doctor` now
  reports them as unavailable rather than failing at call time.
- `ybm start --foreground`, which supervises until a service exits or a signal
  arrives. `start_all` spawns detached children and returns - correct for an
  interactive start, and an immediate exit to a container or systemd.
- **A signed-off install path on every platform.** `.github/workflows/release.yml`
  builds the admin console, stages a runtime payload
  (`scripts/package_release.py`), compiles a per-user MSI
  (`packaging/windows/ybm.wxs`, WiX v5), and publishes the installer plus a
  `.zip` and a `.tar.gz` with checksums and Sigstore build provenance on a tag.
  None of them needs administrator rights or Node.js, because the console ships
  prebuilt - previously a source install had no console at all until the user
  installed Node 22.22+ themselves.
- **`ybm.sh`**, the macOS/Linux counterpart to `YBM.bat`. One file that installs
  whatever is missing - `uv` included - then starts the stack and opens the
  console, idempotently. macOS and Linux previously had no equivalent: the
  documented path was `scripts/install.sh` followed by
  `./backend/.venv/bin/ybm start --open`, two commands where one only works
  after the other. The Python CLI cannot fill that gap because it lives inside
  the virtualenv it would have to create.
- winget manifest templates (`packaging/winget/`) plus
  `scripts/render_winget_manifests.ps1`, which hashes the published installer
  rather than trusting a number copied out of a build log.
- `--dry-run`, `--verify`, `--no-prompt` and `--install-dir` on both installers.
- Scheduled daily dependency audit (`.github/workflows/security-audit.yml`)
  covering the Python and all Node lockfiles; it opens an issue rather than a
  pull request.
- Optional `.pre-commit-config.yaml` running the same ruff and gitleaks checks
  CI runs.
- CI coverage for the frontend, WhatsApp sidecar, packaged container assets,
  and Node dependency audits.
- `error_text.describe_exception`, so an error a human reads is never empty.
- `harness.assert_rejected`, which refuses to let a replay miss pass as a
  policy refusal.

### Changed

- **One file to double-click, first run and every run.** `ybm setup` installs
  `uv` when it is missing (`Install-YbmUv` in `scripts/lib/common.ps1`) instead
  of refusing to continue, so `YBM.bat` handles a cold machine on its own. That
  refusal was the entire reason a separate first-run entry point had to exist.
  `scripts/install.ps1` no longer bootstraps uv either; it fetches the source
  and hands off, leaving one implementation rather than two that can drift.
- **`scripts/install.sh` hands off to `./ybm.sh`**, the way `install.ps1` hands
  off to `ybm.ps1 run`. Both installers now do the same small job - get the code
  onto the machine - and the launcher owns uv, the virtualenv, setup, and start.
  install.sh also stopped installing the developer extras (pytest, telethon,
  ruff) for people who only wanted to run YBM; that is the documented
  `uv sync --extra test --extra dev` line, as on Windows.
- **Installers require nothing preinstalled.** The Python 3.12+ gate is gone -
  `uv` is a standalone binary and provides the interpreter. git is optional,
  with an archive fallback. The uv installer URL is pinned to a version.
- `.mcp.json` launches the MCP server through `uv run` instead of a bare
  `python` with a relative `PYTHONPATH`, which only resolved on a machine that
  happened to have a system Python carrying the dependencies.
- The first-run wizard preselects a recommended Ollama model, and distinguishes
  "Ollama running with nothing pulled" from "no Ollama" - previously identical
  states, and the only point in onboarding that sent the user elsewhere.
- Scenario fixtures are rebuilt rather than merged when re-recording, dropping
  roughly 4,500 lines of unreachable keys.
- The headless image now packages the WhatsApp Node runtime, production
  dependencies, bundled starter skills, and project license.
- Container base images and the optional Ollama service are pinned to reviewed
  manifest digests.

### Removed

- `YBM-Setup.cmd`. `YBM.bat` now covers the first run too, so a second
  double-clickable file with a different name was one more thing to explain and
  one more way to pick the wrong one.

### Fixed

- **Credential redaction missed two shapes.** A quoted key (`"api_key": "…"`,
  i.e. any JSON config) was never matched, and an unquoted value stopped at the
  first space, so a passphrase redacted to `*** horse battery staple`. The
  scrubbed answer is now written to the task row as well as the audit sink and
  the outbound message.
- Two harness defects that made every negative scenario test pass vacuously: a
  pytest `tmp_path` counter that changed each run so recorded keys could never
  be hit again, and `sort_keys=True` reordering recorded payloads so replay fed
  tools a differently-ordered dict than recording did.
- Workspace recovery from a user's message only ever matched Windows drive
  letters, so it was dead code on Linux and macOS.
- A test set `os.name` on the real `os` module, flipping `pathlib` to the
  Windows flavour process-wide and breaking every later `Path()` on POSIX.
- The anti-fabrication guard was disabled task-wide by any earlier write; it now
  compares claimed filenames against recorded ones.
- `filesystem.manage`'s desktop alias enumerated a directory that was not an
  allowed root - the one operation bypassing `_safe_path`.
- Admin token comparison is constant-time; scope matching refuses a `..`
  segment; three type-narrowing `assert`s that `python -O` strips became real
  raises.
- `pypdf` 6.14.2 → 6.15.0 (CVE-2026-71852, CVE-2026-71870).
- Artifact downloads no longer put the long-lived admin token in a URL. They
  use short-lived artifact-scoped grants, and active HTML/SVG content is forced
  to download instead of executing on the admin origin.
- Browser responses now carry a restrictive content security policy and
  framing, referrer, permissions, opener, resource, and MIME-sniffing headers.
- Frontend `nanoid` was updated past GHSA-2v37-7h3g-55p8.
- Onboarding now supports backward navigation, describes the default capability
  policy accurately, and avoids duplicated setup/safety banners on small screens.

### Known issues

See `docs/GAPS.md`.
