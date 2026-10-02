# History - why it's built this way

> **Archive, not reference.** For how the system works *now*, read
> [ARCHITECTURE.md](../ARCHITECTURE.md). This file is the decision record: what was broken, what
> was tried, and what was deliberately rejected.

**The goal this repo exists to serve:** *I talk to my local computer, and agents on my local
computer do whatever I ask.* Every decision below is judged against that sentence.

| Part | Date | What it covers |
|---|---|---|
| [1 - P0–P6](#part-1---p0p6-the-architecture-overhaul) | 2026-07-26 | The architecture overhaul: from "doesn't reliably install" to the three-agent design |
| [2 - N1–N6](#part-2---n1n6-the-post-migration-audit) | 2026-07-28 | The audit that found P3 had regressed observability, and the fix |
| [3 - The way forward](#part-3---the-way-forward) | 2026-07-29 | Prioritised next steps; five real bugs the dark test tier had hidden |
| [4 - Multi-agent build](#part-4---multi-agent-and-feature-parity-build-2026-07-30) | 2026-07-30 | Parallel calls, delegation, skills, persona, knowledge base, cost tracking |
| [5 - React console](#part-5---react-admin-console-build-and-cutover-2026-08-01) | 2026-08-01 | Admin console build and cutover |
| [6 - WhatsApp](#part-6---whatsapp-as-a-second-real-channel-2026-08-02) | 2026-08-02 | A second real channel, and the channel-agnostic core it forced |

**Section labels are load-bearing.** Parts keep their original `P0`–`P6`, `N1`–`N6`, and `§1.1`
labels because ~90 code comments cite them. Arriving from a comment like
`(docs/archive/HISTORY.md P3 §2.2)`? Search this file for that exact string.

The fine-grained evidence trail - test names, line-count deltas, before/after reproductions -
lives in git history: `git log --oneline` plus commit bodies from 2026-07-27 onward.

---

# Part 1 - P0–P6: the architecture overhaul

## 0. Evidence (as observed 2026-07-26, before any of this plan was executed)

**Scale then:** 200 tracked files, 37,017 lines of Python, 22 scripts (2,759 lines), 401 unit
tests, 72 E2E cases at a 49% pass rate (`run_20260525_125109`, stale even then).

**Findings, now fixed (P0/P1):** undeclared dependencies resolving only via a global Anaconda
install; no virtualenv anywhere; hardcoded absolute LocalDeploy paths; `start_stack.ps1`
reporting success without checking the process was still alive, so a crashing worker
restarted silently forever; a script referencing a test file that didn't exist.

**Findings, load-bearing for later phases:**
- **Config drift.** `config/config.yaml` is gitignored and high-impact capabilities start disabled
  by default - the dominant real-world failure mode. `ybm setup` bootstraps a file now, but
  capabilities still start off by design.
- **No test tier between unit and live.** Nothing deterministic exercised the worker loop end
  to end before P2 built scenario tests.
- **Stale state silently reanimates.** No retention, no startup reconciliation existed -
  closed in P6.
- **Two admin UIs, overlapping.** `admin.py` (embedded HTML) and `admin_streamlit.py` -
  resolved in P4 by deleting the embedded-HTML one.
- **Security gaps around an otherwise-good policy engine**: no CORS/Origin/CSRF check on the
  admin API, admin token optional on loopback - addressed in P5 item 1.
- **The brain plans once; it does not loop.** `worker.py` called the planner once, got a
  static plan, replanned from scratch on failure, capped at 2. No observe→decide→act cycle.
  This was the single biggest architectural gap - closed in P3.
- **Routing was keyword patchwork.** `default_plans.py` was 1,277 lines, 68 keyword-match
  sites; even model selection used a keyword list - closed in P3.
- **Ten LLM roles, 17 tools in one 997-line registry function** - closed in P3 item 4 and
  item 6 respectively.

## 1. Diagnosis - seven root causes

| # | Root cause | Symptom it produces |
|---|---|---|
| R1 | **No environment contract.** No venv, no lockfile, undeclared deps, hardcoded paths. | "It doesn't install what's missing." Works only on this machine. |
| R2 | **Startup reports intent, not reality.** No readiness gate, no preflight, silent restart loop. | Green output, dead stack. |
| R3 | **No deterministic test tier.** Unit-with-mocks, then live-everything. | Can't tell a real regression from a flaky LLM. |
| R4 | **Config is untracked and defaults to off.** | `tool adapter not registered`; unreproducible runs. |
| R5 | **Plan-once architecture.** Static plan + replan-from-scratch instead of an agent loop. | Recovery has to be faked with keyword rules. |
| R6 | **Keyword routing substitutes for reasoning.** | `default_plans.py`; every new failure adds another `if`. |
| R7 | **No product surface.** Two half-UIs, no install path, no first-run flow. | Not shippable, even to yourself on a new laptop. |

R5 and R6 are the same wound: because the executor couldn't adapt mid-task, the only place
left to encode adaptation was a hand-written if/else chain over error strings. All seven are
now closed or substantially closed; R5/R6 were closed structurally in P3, not patched.

## 2. Target architecture (as designed; current state lives in ARCHITECTURE.md)

### 2.1 Three agents, many tools

Collapse 10 LLM roles into 3, as *prompt sections*, not modules: **Concierge** (front door -
chat vs. task, conversation memory, progress reporting), **Operator** (the agent loop - given
a goal + tool catalogue + policy, runs observe→decide→act until done/blocked/out-of-budget),
**Auditor** (did we actually achieve the goal - grounded answer + evidence). Everything else
(computer-use decisions, OCR, code-interpreter prompts) is a tool with an internal LLM call,
not a fourth agent.

### 2.2 The Operator loop replaces plan-once

```
goal + tool catalogue + policy + budget
  ↓
┌─────────────────────────────────────────┐
│ observe   → current state, last result  │
│ decide    → next tool call | done | ask │  ← ONE LLM call, structured output
│ gate      → policy engine (unchanged)   │
│ act       → tool                        │
│ record    → audit + operator_history    │
└──────────────┬──────────────────────────┘
               └── loop until done / blocked / budget
```

This deletes the need for keyword-driven recovery - "the next `decide()` call sees the error
in context," which is what an LLM is actually good at.

### 2.3 Naming: two concepts, not four

**Capability** (a permission scope, e.g. `filesystem.write`) and **tool** (a callable unit
with a JSON schema, e.g. `filesystem.manage`) stay. **Adapter** and **connector** were retired
from the vocabulary - an adapter is just a tool's implementation.

### 2.4 Framework decision: build the loop, buy the sandbox and the protocol

Do not port the orchestrator to LangGraph or AWS Strands - this repo already has durable
SQLite task state, an atomic worker claim, an audit trail, an approval gate, and a capability
policy engine; a graph framework would duplicate the task store and make it *easier* to route
around the policy gate, the one invariant that must never be bypassable. The genuinely missing
piece was the agent loop (§2.2), ~200 lines, not a dependency. Do buy: the sandbox (Docker for
generated code - see P5 item 4) and the protocol (MCP as the single tool interface - already
in place via `mcp_client`/`mcp_server`).

## 3. The plan

### P0 - Make it install and start honestly *(done, 2026-07-27)*

`uv`-managed venv + lockfile with all deps declared; `ybm doctor` preflight (Python version,
venv, imports, DB, ports, config, Telegram token); `ybm setup` bootstraps a fresh config;
`YBM_LOCALDEPLOY_ROOT` replaces hardcoded paths; supervisor crash-loop breaker (3 restarts/60s
→ `failed`, last 20 log lines surfaced); `ybm start` waits for real readiness signals instead
of trusting `Start-Process`.

### P1 - Collapse 22 scripts into one CLI *(done, 2026-07-27)*

One entry point, `ybm` (`setup | doctor | start | stop | restart | status | logs | test | e2e
| db | config | trace | scenario`), backed by `cli.py`. 15 of 22 scripts deleted or relocated;
`scripts/` now holds a handful of thin launchers plus `ybm.ps1` itself.

### P2 - Build the missing test tier *(foundation done 2026-07-27; re-recording not done)*

Scenario tests: real DB/registry/policy/worker loop/tools against a temp filesystem, with a
scripted LLM (`testing/scripted_llm.py`) replaying recorded fixtures - deterministic, no
Telegram/network/GPU, whole suite in seconds. 16 cases were ported against the *old*
plan-based path before P3 deleted that path. **Only 2 of those 18 scenario tests still have
valid fixtures** - the other 16 are `pytest.mark.skip`'d, not deleted, because their fixtures
are keyed on the old planner's exact prompt text, which no longer exists post-P3.
Re-recording them against the Operator loop's prompt needs a live LLM call per case - this is
N3, explicitly gated on a cost/scope check-in before spending on live API calls, not yet done.
The live E2E suite was trimmed 2026-07-28 from 72 to 11 representative smoke cases (one per
major capability: filesystem inspect/search/organize, document, browser, desktop observation,
code interpreter, artifact delivery, scheduling, MCP, adapter authoring) - not re-baselined
yet, since that needs a real run against the live stack.

### P3 - Rebuild the brain *(done, 2026-07-28)*

All six items landed: (1) the Operator loop is the sole execution path - flipped by explicit
user decision after being shown the gaps, with approval flow/fulfillment-gap
checking/rate-limit backoff/background-session resume/model escalation all ported across
first; (2)+(3) `default_plans.py`, `recovery_policy.py`, `failure_diagnosis.py`,
`attempt_history.py`, `llm/planner.py`, `llm/synthesizer.py`, `llm/validator.py` all deleted
outright (`worker.py`: 1,761 → 978 lines); (4) Concierge absorbs classifier +
telegram-gateway into one call, Auditor absorbs validator + synthesizer + fulfillment, wired
into the Operator loop's `done` path (conversation-memory and pre-task clarification
deliberately *not* merged - ordering and failure-isolation reasons, see git history for the
full rationale); (5) model selection is now reactive (escalate only after an observed
structured-output failure), not keyword-triggered; (6) the 997-line registry function is split
into per-tool `register()` functions in `tools/spec.py` + each adapter module.

**Known, disclosed regression:** the 16 scenario tests that exercised the deleted plan-based
path cannot run against the Operator loop without re-recording (see P2, N3). Full unit suite
was green throughout, verified at every step, not just at the end. `ybm doctor` clean.

### P4 - One real admin UI *(pick-one-UI done 2026-07-27; console restructure not started)*

**Done:** deleted the 1,288-line embedded-HTML admin (`admin.py`'s `_ADMIN_HTML`), kept only
the JSON API it exposed; `GET /admin` now returns a small pointer page to Streamlit instead of
a second competing SPA. Verified against a real running server, not just `TestClient`.
Investigated and found **not reproduced** in current Streamlit source: KPI truncation,
duplicate health rendering, mojibake encoding - likely artifacts of the deleted embedded-HTML
page, not Streamlit; not visually re-confirmed either way (no browser tool in this
environment).

**Not started:** four-page console restructure (*Now* / *Tasks* / *Access* / *Settings*) -
see N6.

### P5 - Close the security gaps *(items 1, 2, 5 done 2026-07-27; 3 partial; 4 addressed differently)*

1. **Done.** Admin API had zero middleware - any local page's JS could POST a capability
   change to loopback without needing to read the response. Fixed with an Origin-vs-Host
   same-origin check in `require_admin()` (OWASP's "Verifying Origin With Standard Headers"),
   not a separate CSRF token - sufficient for a single-user local API.
2. **Already true**, not this session's work - `ybm setup` already generates the admin token
   unconditionally and binds to `127.0.0.1` by default.
3. **Partially done.** `allowed_imports` enforcement itself was never broken. What was broken:
   `importlib.import_module("os")` bypassed the `blocked_imports` denylist entirely (a
   function call, not an `Import` AST node) - fixed by adding `importlib`, `multiprocessing`,
   `winreg` to the default denylist. Forcing a non-empty default *allowlist* (safer than a
   denylist) is still not done - needs careful curation to avoid breaking routine generated
   scripts, deliberately not rushed.
4. **Addressed via a sharper, less disruptive fix than "default to Docker."** Found that
   generated code was exempted from approval *unconditionally*, and silently fell back to
   unsandboxed `local_subprocess` whenever Docker wasn't running (true on this machine, every
   session). Fixed by requiring approval specifically when that silent fallback occurs, not
   when Docker succeeds or when a user has explicitly chosen local execution via config.
5. **Done, including the follow-up N5 found.** Found the `AWAITING_APPROVAL` Telegram message
   pointed users at an admin-UI approve action that didn't exist and never mentioned the one
   thing that worked (replying `approve` in chat) - fixed the message and added a
   human-readable preview (title/risk/tool/input) of what's pending. The Telegram
   inline-keyboard callback parser (`channels/telegram.py`) had zero code sending the button -
   half-built and dead. **N5 wired it up** (both approve and reject) rather than leaving it
   dead or deleting it.

### P6 - Operational hygiene *(done, 2026-07-27)*

Startup reconciliation (`reconcile_orphaned_tasks()` explicitly fails any task left
`RUNNING`/`INTERPRETING` by a dead worker instead of silently resuming or re-running side
effects); retention already existed pre-P6 (`ybm db clean`, 30-day default; N5 extended it to
orphaned audit events); schedule hygiene (`max_consecutive_failures`, default 5, auto-pauses a
schedule and notifies instead of respawning a permanently-broken task forever - the exact
pattern that produced 7 dead May schedules in the original evidence); `agent_control.db`
moved under `.agent_control/` with a tested, non-destructive one-time migration for anyone
with the old path.

## 4. Reaching the actual goal

Three things beyond this plan, cheap once the Operator loop exists, expensive before it:

- **Claude Code / Codex / Copilot as first-class backends.** `coding_agent.py` already shells
  out to all three CLIs. Adding the Claude Agent SDK as an in-process backend (streaming
  progress, structured tool events instead of scraped stdout) is what makes "show me what
  it's doing" possible.
- **Tool authoring that closes the loop.** `adapter.factory` writes a proposal and stops. Wire
  it end to end: write → sandbox-test → register over MCP → use it in the same task.
- **Computer use as a real capability**, not three top-level prompts capped by `max_steps` -
  one tool with an internal loop, gated on the Access page like everything else.

## 5. Delete list

| Path | Why | Status |
|---|---|---|
| `PHASED_APPROACH.md`, `STEP_BY_STEP_IMPLEMENTATION.md`, `docs/FIX_PLAN.md`, `docs/PROJECT_GAPS.md`, `docs/EXTENSION_IMPLEMENTATION_PLAN.md`, `docs/PROMPT_GAP_ANALYSIS.md` | Superseded planning docs, one historical finding carried into P3. | **Deleted** |
| `docs/FLOW.md`, `docs/FLOW_DIAGRAMS.md`, `docs/TASK_FLOW.md` | Three overlapping flow docs. | **Deleted**, merged into `docs/ARCHITECTURE.md` |
| `docs/GETTING_STARTED_ADMIN_TELEGRAM_LLM.md` | Duplicated `LOCAL_SETUP.md` and `ARCHITECTURE.md`. | **Deleted**, unique content merged |
| `scripts/*` (15 of 22) | Listed in P1. | **Deleted/relocated** |
| `backend/src/agent_control/admin.py` HTML (2,210 lines) | Second UI. | **Deleted** (P4) |
| `default_plans.py`, `recovery_policy.py`, `failure_diagnosis.py`, `attempt_history.py`, `llm/planner.py`, `llm/synthesizer.py`, `llm/validator.py` | Plan-based path, replaced by the Operator loop. | **Deleted** (P3) |
| `scripts/benchmark_models.py`, `scripts/benchmark_progress.py` | One-off scripts, superseded by `ybm test`/scenario tests. | **Deleted** (N4) |
| 13 of 14 stale scenario fixture JSONs | Keyed to the deleted planner's prompt text, permanently unmatchable. | **Deleted** (N4) - kept `operator_loop_filesystem_search.json` |
| `PlanModel`, `PlanStep`, `ApprovalGate`, `SubtaskRecord`, `ToolObservation`, `ApprovalDecision`, `PlanRepository`, `ToolRegistry.validate_plan()`, `plans`/`subtasks` tables, `tasks.plan_id`/`current_step_id` columns | §1.1 found these unreachable; physically deleted in a later 2026-07-28 pass (see Part 2's closing note). | **Deleted** |
| `docs/ROADMAP.md`, `docs/AUDIT.md` | Merged into this file. | **Deleted, merged here** |

Docs now: `README.md`, `CLAUDE.md`, `docs/archive/HISTORY.md`, `docs/ARCHITECTURE.md`,
`docs/INSTALL.md`, `docs/USING.md`, `docs/DATABASE_INSPECTION.md`,
`e2e/README.md`.

## 6. Honest unknowns

- The 49% E2E figure is from May and was **not re-measured**. Re-baseline after config-drift
  and N3's re-recording pass; the true number is probably higher now.
- `browser.py` (963 lines) and `computer_use.py` (513 lines) were not audited for correctness,
  only for how they're routed to.
- Effort estimates assume working with an agent, not by hand - relative weights, not
  commitments.
- Whether Streamlit stays sufficient at P4's ambition is the one call worth revisiting after
  building the *Now* page.

---

# Part 2 - N1–N6: the post-migration audit

Written 2026-07-28, immediately after P3 landed. Every claim below was **observed on this
machine** - commands and reproductions are cited inline.

## 0. Headline

The P3 migration succeeded structurally and **regressed observability badly**. The system now
executes through one clean agent loop, but at the time this was written: you could not see
what the agent did (the admin UI rendered the deleted plan concept, showing "Plan Steps: 0"
for every task, while the real execution record `operator_history` was written and read by
nothing); there was no logging configuration anywhere in the codebase; three confirmed
behavioral bugs had been introduced by the migration. All of this is now fixed - see N1/N2.

## 1. Redundancy found (since deleted - see the closing note at the end of this part)

### 1.1 Dead code left by the P3 migration (confirmed unreachable, since physically deleted)

The plan-once path was deleted, but its data model and consumers were not, at first. Nothing
called `repositories.plans.create()` anywhere in `backend/src` - plans were never created, so
everything downstream of them was dead: `PlanStep`, `RecoveryAction`,
`_postconditions_from_plan()` (~106 lines), `expected_fulfillment()`, `fulfillment_gap()`,
`ToolRegistry.validate_plan()`, `PlanRepository`, the `plans` DB table, `attempt_history`
metadata, `current_step_id`. All of it, plus `PlanModel`, `ApprovalGate`, `SubtaskRecord`,
`ToolObservation`, and `ApprovalDecision` (found in the same pass but not yet listed here),
was deleted in a follow-up cleanup pass the same day - see this part's closing note.

### 1.2 Stale documentation (fixed)

`docs/ARCHITECTURE.md` described 8 already-deleted files as current - rewritten. `README.md`
claimed "structured planner with plan persistence" - fixed. `docs/ROADMAP.md` was 1,074 lines
of mostly-changelog - trimmed, then merged into this file.

### 1.3 Scripts

`scripts/benchmark_models.py` (640 lines, stale since May) and `scripts/benchmark_progress.py`
(120 lines) deleted; `ybm benchmark`/`ybm benchmark-status` removed with them.
`scripts/test_e2e.py` was nearly deleted as a mistaken duplicate but is actually the
implementation behind `ybm send` - kept.

### 1.4 Repo clutter (cleaned)

`.agent_control/live_e2e_runs` (82 MB), `.agent_control/e2e_results` (15 MB, since re-wiped
after the E2E trim), `.agent_control/benchmark_*`, and an untracked `sales_data.xlsx` were
removed. `ybm clean` extended to cover `e2e_fixtures`/`e2e_runs`/`live_e2e_runs` so they don't
reaccumulate silently.

### 1.5 The 16 skipped scenario fixtures

Deleted (the test files were kept as a re-recording checklist) - each embedded the entire
deleted `planner_system.md` text as its fixture key, permanently unmatchable.

## 2. Development speed - the real bottlenecks (all fixed - see N1)

### 2.1 There was no logging at all

The stdlib root logger defaulted to WARNING with a last-resort stderr handler - every
`logger.debug()` call (17 of them) was discarded, `structlog` was a declared dependency
imported by nothing, and service logs were unstructured stdout capture. **Fixed:**
`logging_setup.py` (structlog JSON + console per service, called from every entry point),
`task_id` bound as a contextvar for the duration of each `process_task()` call so every line
for one task is greppable by one id, debug-only excepts upgraded to warnings where the
failure was a real degradation rather than a routine expected-to-fail probe.

### 2.2 You could not see what the agent did

`operator_history` - the real step-by-step execution record - was written and read by
nothing; the admin UI still rendered the deleted plan concept ("Plan Steps: 0" for every
task). **Fixed:** rendered in both the trace endpoint and Streamlit's task view; dead
plan-rendering functions deleted.

### 2.3 No fast feedback loop between "unit test" and "hours-long live E2E"

Scenario tests (deterministic, real stack + recorded LLM, seconds to run) existed but 16 of
18 had gone dark after P3 changed the prompt they were keyed on. **Fixed (tooling; execution
still pending):** `ybm scenario record <name>` (N3) makes re-recording a one-liner; the live
E2E suite was separately cut from 72 to 11 cases the same day.

### 2.4 No one-command task post-mortem

Debugging a failed task needed the stack running, then the admin UI, then a click into a
trace - no CLI surface existed for the `/admin/api/tasks/{id}/trace` endpoint's data.
**Fixed:** `ybm trace <task_id>` reads the DB directly, no running backend required.

### 2.5 Test boilerplate

9 test files each defined an identical `_repos(tmp_path)` fixture; minor friction, not
addressed in this pass.

## 3. Bugs - all three reproduced, not inferred (all fixed - see N2)

### 3.1 Gap checks consumed the tool-call step budget

`_fulfillment_check`/`_audit_check` pseudo-entries counted against `operator_max_steps`
alongside real tool calls - with defaults (`max_steps=8`), 2 fulfillment + 2 audit gaps could
burn half the budget on bookkeeping, worst case exhausting it with zero real tool calls.
**Fixed:** `_tool_call_count()` excludes check entries from the budget count.

### 3.2 The Auditor judged a 2,000-character truncation

The Auditor's sufficiency check ("are all 5 episodes present?") was given
`output_summary`, truncated to 2,000 characters, while the full text (up to 20,000) sat
right there in `metadata["last_tool_output_text"]` - producing false INSUFFICIENT verdicts
on exactly the long-content objectives it existed to check. **Fixed:** Auditor now reads the
full stored text.

### 3.3 Progress notifications were dead

The RUNNING/RETRYING dedupe key was built from `attempt_history`/`current_step_id`, both
zero-writer plan-era fields, so it collapsed to the constant `"running"` and never changed -
a 30-step task sent one "working on it" and then silence for minutes. The same root cause
dropped the "Latest attempt: X ended with Y" explanation from every failure message.
**Fixed:** key on `len(operator_history)`; `_latest_attempt_summary()` rewritten against
`operator_history`.

### 3.4 `operator.max_steps` was undocumented

Present in neither `config/config.example.yaml` nor `config/config.yaml`, despite being the
sole backstop against a runaway loop - the config-drift root cause (R4) reappearing.
**Fixed:** reconciled, and 25 further config keys present live-but-not-in-example (and 15
the other way - notably the whole `code_interpreter.docker` sandbox block) were found in the
same pass. A real, live security gap surfaced alongside it: this machine's `blocked_imports`
list was missing 3 entries a prior session had added to the code default, unprotected because
pydantic-settings replaces rather than merges explicit YAML lists.

## 4. Feature-level gaps found - status after N5

1. ~~No approve/reject UI; inline keyboard dead; no reject at all.~~ **Fixed by N5** - real
   approve/reject in Streamlit, inline keyboard wired up on both decisions.
2. ~~No evidence view.~~ **Fixed by N5** - key-based file/URL/command extraction from
   `tool_invocations`, rendered in both Streamlit and `ybm trace`.
3. ~~No kill switch.~~ **Fixed by N5** - one confirm-then-disable-everything control reusing
   the existing access-modes endpoint.
4. **Secret vault has no UI.** `storage/secrets.py` (Fernet) exists; nothing exposes it.
   `AGENT_SECRET_VAULT_KEY` is unset (doctor warns). **Still open.**
5. ~~No retention for audit_events/tool_invocations.~~ **Fixed by N5** - orphaned
   (`task_id IS NULL`) audit events now covered by `db_clean`.
6. ~~Auditor never runs on the delivery path.~~ **Not a real gap - earlier claim was wrong.**
   `artifact.deliver` is indeed absent from `CONTENT_TOOLS`, but
   `_last_content_tool_history_entry()` scans history *backwards past* non-content tools, so
   a search-then-deliver task audits the search results, which is the correct thing to ground
   an answer in. Adding `artifact.deliver` would make the Auditor try to extract an answer
   from a delivery receipt (`{delivered: true, path: ...}`) - meaningless at best. The real
   (narrower, harder) gap is that nothing verifies the *delivered file's content* against the
   objective; see item 11.
7. ~~`code.interpreter` can't chain multi-step file work within one task.~~ **Fixed
   2026-07-29.** `_workspace()` appended `uuid4().hex[:8]`, giving every call its own fresh
   directory. Task ids are already unique, so that suffix only ever separated calls *within*
   one task - exactly what must be shared. Step 2 landed in an empty directory and could never
   see step 1's file; `inspect_state` always reported zero files. Reproduced on three
   independent live recording attempts. Fixed by making the workspace stable per task
   (`root / f"task_{task_id}"`), with three regression tests in `test_code_interpreter.py`
   (verified failing against the old code, passing against the new) and the
   `code_interpreter_csv_summary` scenario now green end to end - two real calls, one shared
   workspace, second call reading back the first's file.
8. ~~`mcp.client`'s tool catalog format misleads the model into malformed `call_tool` input.~~
   **Fixed 2026-07-29.** `mcp_catalog_summary()` printed each tool as a single dotted string
   (`"- fake.echo: Echo text; ..."`) while `MCPClientInput` requires `server` and `tool` as
   separate fields; the model copied the dotted string wholesale into one field, reproducibly,
   across two independent recording attempts with zero self-correction (`server="fake.echo"`
   one run, `tool="fake.echo"` with `server` missing the next). Fixed by labelling the fields
   the way the schema names them (`- server="fake" tool="echo" - Echo text; ...`) plus a
   header line and a realistic worked example. Re-recording immediately produced the correct
   split input, first try; `mcp_call_fake_echo` now asserts the split explicitly as a
   regression guard.
9. **Generated code must never hardcode the workspace's absolute path.** Found 2026-07-29
   after fixing item 7, when a recorded fixture still failed on replay: the generated script
   had baked in the *recording run's* absolute workspace path, which no longer exists on a
   later run. The more serious consequence is not the fixture - it is that the Docker backend
   bind-mounts the workspace at a **different path inside the container**
   (`workspace_mount_target`, default `/workspace`), so any script carrying an absolute host
   path fails outright under the sandbox backend, the one that is supposed to be the secure
   default. Both backends already run the script with the workspace as cwd, so relative paths
   are always correct and portable. **Fixed** by rewriting
   `prompts/base/code_interpreter_system.md` and `prompts/tasks/code_interpreter_user.md` to
   demand relative paths and label the shown path reference-only, with a regression test
   asserting the instruction survives in the prompt actually sent.
10. **Three dead metadata reads in failure messaging.** `intervention_summary`,
    `planning_error`, and `last_replan_reason` were read in `telegram_notifications.py` with
    zero writers anywhere - plan-era keys that could only ever contribute `None` to an `or`
    chain. **Fixed** (removed), along with `_last_command_id()`/`_last_usage()`, two helpers
    orphaned when the dead message formatters were deleted. Found by a systematic
    read-vs-write sweep across all metadata keys, which also confirmed `computer_use_actions`
    (written via `worker.py`'s dynamic copy map) and `task_budget_seconds` (a documented
    per-task override) are **not** dead despite looking that way to a naive grep.
11. **Content correctness of generated files is still unverified.** Nothing checks that a
    script the code interpreter wrote actually did what the objective asked - only that it
    ran and produced files. `generate_and_run` regenerates on `SyntaxError`/`ValueError`, not
    on "valid Python that computes the wrong thing", and a delivery-ending task has no
    Auditor pass over the delivered file's contents. **Still open**; this is the honest
    residue of what item 6 was gesturing at.
12. **A failed MCP handshake stranded the server subprocess.** Found 2026-07-29 while
    diagnosing item 13. `_session.__aenter__` entered `stdio_client` (spawning the
    subprocess), then called `initialize()`; when initialize raised, the exception propagated
    out of `__aenter__`, so the caller's `async with` never ran `__aexit__` and the
    already-entered stdio context was never closed. The orphaned async generator was
    eventually finalized by the event loop at shutdown - in a different task than had entered
    it - surfacing as a confusing `RuntimeError: Attempted to exit cancel scope in a different
    task`. **Fixed** by rebuilding `_session` on `AsyncExitStack`, which unwinds whatever was
    entered, in reverse order, in the same task, on both the failure and normal paths.
    Regression test asserts both layers close when `initialize()` raises.
13. **The MCP scenario tests' 10s handshake timeout was marginal, not generous.** Timed
    directly: the Python-subprocess spawn plus MCP `initialize()` round trip takes **11.5s**
    on this machine with LocalDeploy/Ollama also running (10s -> fails at 10.3s; 30s ->
    succeeds at 11.5s). It presented as an opaque `anyio.WouldBlock`/`CancelledError` rather
    than a clear timeout, and had been intermittently passing purely on machine load.
    **Fixed** by a single shared `MCP_HANDSHAKE_TIMEOUT_SECONDS = 30` in the scenario harness,
    with the measurement recorded next to it so the number isn't re-guessed later.
14. **Four orphaned prompt files.** `tools/copilot_development.md`, `tools/copilot_web_app.md`,
    `tools/adapter_factory_copilot.md`, `tasks/computer_use_validation.md` (47 lines) were
    referenced by nothing - their only consumer was `default_plans.py`, deleted in P3.
    `ARCHITECTURE.md` still listed two of them as live tool prompts. **Fixed** (deleted, doc
    corrected). Found by diffing prompt files on disk against prompt paths referenced in
    source.
15. **`ybm doctor` reported LocalDeploy unreachable while it was running.** `_http_ok()`'s
    liveness probe used a 2.0s timeout, but LocalDeploy's `/health` enumerates Ollama's
    installed models before replying and measured ~2.06s here. Doctor printed
    "LocalDeploy not reachable ... fallback profile 'openai_saved' will be used" in the same
    run that printed "Port 8000 (LocalDeploy) listening" - actively misleading, since it tells
    the user their free local model is down and a **paid** API is about to be billed when
    neither is true. **Fixed** (6.0s, with the measurement recorded at the call site); doctor
    now reports 23 ok / 4 warnings instead of 22 / 5.
16. **Seven groups of duplicated code, found by AST-normalised structural comparison** (names
    and literals stripped, so renamed copies still collide) rather than by reading. All
    **fixed 2026-07-29** by extracting one definition each; the win is single-source-of-truth,
    not line count - a change to any of these previously meant editing N files or, far more
    likely, editing one and silently diverging the rest:
    - `_failed()` - **13 byte-identical copies**, one in every tool adapter. Now
      `tools/spec.py:failed_result()`, which every adapter already imports. This is the
      adapter failure *contract*; having 13 copies meant a new field or a different
      `ErrorClass` could be applied inconsistently across tools without any test noticing.
    - `_repos(tmp_path)` - 8 identical copies across test files. Now
      `tests/helpers.py:make_repos()`. This was flagged in the original audit (§2.5) and had
      been open since.
    - `_settings(...)` - 4 identical copies in scenario tests → `harness.filesystem_settings()`.
    - `_mcp_settings(...)` - 2 copies → `harness.mcp_settings()`.
    - `_task_chat_id()` - 2 copies in **src** (`channels/` and `tools/`, with no import
      direction between them that would let one reuse the other) → `schemas.task_chat_id()`,
      which is where it belongs since it is pure logic over `TaskRecord`'s own fields.
    - `_backend_base_url()` - 2 copies in **src** → `config.backend_base_url()`.
    - Markdown code-fence stripping - 2 copies in **src** (`llm/providers.py`,
      `tools/code_interpreter.py`) → `providers.strip_code_fences()`.

    A re-run of the same detector afterwards reports **1 remaining group**, and that one is a
    false positive: two deliberately-different fake LLM providers in `test_code_interpreter.py`
    that return different canned scripts but have the same shape.
17. **The MCP stdio tests were genuinely flaky under CPU contention, not just marginally
    timed.** Found 2026-07-29 running the full suite while LocalDeploy/Ollama were still up
    from the W1 fixture re-recording pass: `test_mcp_client.py` failed 2 of 4 consecutive
    full-suite runs at the 30s handshake timeout (item 13's fix), passed every time in
    isolation or after LocalDeploy was stopped. Not a code bug - closed by stopping the
    now-unneeded LocalDeploy process, confirmed with 3 consecutive clean full-suite runs
    afterward. Worth remembering operationally: **don't leave LocalDeploy running during a
    full test-suite pass** once a live-recording batch is done; nothing in the suite needs it
    once fixtures are committed.

## 5. The plan - N1 through N6

### N1 - Make it observable *(done, 2026-07-28)*

`logging_setup.py` (structlog JSON + console, called from every `cli.py` entry point,
`serve_backend.py`, and `admin_streamlit.py`); `task_id` contextvar bound in `process_task()`;
~10 silent-debug exception handlers upgraded to warnings; `print()` → logger at real service
call sites; `operator_history` rendered in the trace endpoint and Streamlit (dead
plan-rendering functions deleted); `ybm trace <task_id> [--json]`.

### N2 - Fix the three confirmed bugs *(done, 2026-07-28)*

See Part 2 §3. Each got a regression test.

### N3 - Restore the fast test loop *(done, 2026-07-29 - all 16 fixtures recorded, 33/33 green)*

`ybm scenario record <name> [--profile <name>]` resolves a fixture name to its test file(s),
flips their skip marker, and runs them through pytest with a live provider built from this
machine's real config. Built and verified mechanically first (name resolution, error paths,
profile lookup) without spending on a live call, per the standing decision not to incur real
API cost without a user check-in - that check-in happened 2026-07-29 and the re-recording
pass ran against `localdeploy_qwen3vl_8b` (free, local - LocalDeploy was started for this
specifically to avoid the paid `openai_saved` fallback).

**All 15 fixtures needing re-recording now succeed** (the 16th,
`operator_loop_filesystem_search`, already had a valid one). The scenario tier went from
**2 of 18 green to 33 of 33 green, with zero skips** - the first time every scenario case
has passed since the P3 migration.

The last two (`code_interpreter_csv_summary`, `mcp_call_fake_echo`) were initially left
skipped because each hit a real, reproducible product bug rather than a recording problem.
Both bugs were then fixed (§4 items 7, 8, 9, 12, 13) and both fixtures re-recorded green.
Fixing them invalidated other fixtures too - the workspace-path change and the
relative-path prompt rewrite both alter prompt text, and fixtures are keyed on exact prompt
text - so all five `code_interpreter_*` fixtures were re-recorded a final time afterwards.
That cascade is the system working as intended: a prompt or tool-schema change *should*
fail its fixtures loudly rather than replay stale data.

**Real findings surfaced along the way, not papered over:**
- `RecordingLLMProvider` had no `.calls` tracking (unlike `ScriptedLLMProvider`), breaking any
  test asserting call count while recording. Fixed - both providers now share the interface.
- `status_request`'s "zero LLM calls" premise predated the plan-based path's deletion - the
  Operator loop has no equivalent deterministic shortcut, so it now costs 2 real `decide()`
  calls. Test updated to assert the real (correct, just not free) behavior; the missing
  fast-path is a disclosed, not-yet-built optimization.
- `test_code_interpreter_generate_file.py` had a real pre-existing bug: its own docstring said
  "no-delivery" but its assertions checked for delivery anyway (copy-paste from the delivery
  sibling test, never removed). Fixed - the Operator's actual (correct) behavior was right,
  the assertion was wrong.
- `test_mcp_discover_tools.py`'s original fulfillment-gap bug (self-declared `adapter_proposal`
  postcondition a `mcp.client` call could never satisfy) is now structurally gone - the
  Operator loop has no self-declared `plan.postconditions` left to reach for the wrong type.
  Test updated from "documents the bug" to "confirms the fix." Its capability-disabled sibling
  test also had a too-strict assertion (checking the call was never *attempted* instead of
  correctly *denied*) - fixed to match the pattern used elsewhere for gated tools.
- Two objectives were genuinely ambiguous English and were rewritten rather than worked
  around: "echo hello from E2E" (echo `"hello"`, attributed to E2E? or echo the string
  `"hello from E2E"`?) and the CSV two-step, which under-specified the intermediate file so
  the model reasonably computed the total inline in one call. In both cases the model's
  reading was defensible and the instructions were at fault.
- `test_code_interpreter_csv_summary.py` also carried the same copy-pasted delivery
  assertion as its sibling, asserting a Telegram send the objective never requested. Removed;
  `artifact.deliver` is already covered end to end by two other scenario tests.

### N4 - Delete the dead weight *(done, 2026-07-28)*

See Part 2 §1.

### N5 - Close the safety gaps *(done, 2026-07-28)*

Approve/reject control (admin API + Streamlit + Telegram inline keyboard), kill switch,
evidence view, orphaned-audit retention - see Part 2 §4. New tests for every item; full suite
green throughout.

### N6 - Then, and only then, P4's UI restructure *(not started)*

The 4-page Streamlit split is worth doing **after** N1/N2 (now done), because before that it
would have been restructuring a UI that displayed the wrong data.

---

# Part 3 - The way forward

Written 2026-07-29, after N1–N5 closed and the scenario tier reached 33/33. Ordered by value
per hour, with the reasoning for the order rather than just the list.

**The single most important change in posture:** the deterministic scenario tier is now
trustworthy. Five real product bugs (§4 items 7, 8, 9, 12, 13) were found *by recording
fixtures*, not by reading code - three of them invisible to a 464-test unit suite. Anything
below that would benefit from scenario coverage should get it, because that is now where bugs
actually surface.

### W1 - Verify generated-file content, not just that files appeared *(done, 2026-07-29)*

§4 item 11. A code-interpreter task used to pass if the script ran and produced files;
nothing checked the file said what the objective asked for - `_terminal_output()` listed
file NAMES and stdout only. A script that computed the wrong number, or wrote an empty file,
sailed through, because nothing downstream ever looked inside the file.

**Fixed** by adding a bounded content preview (`code_interpreter.py`'s
`_file_content_previews()`: 3 files max, 600 chars each, text-decodable only, `script.py`
excluded as an implementation detail) to every `run_python`/`generate_and_run` result. It
flows through the existing pipe with no new mechanism: `_terminal_output()` renders it as
`Content of <file>:` lines → `worker.py`'s `_tool_output_text()` picks it up from
`terminal_output` → `last_tool_output_text` → the Auditor's `raw_output` argument. The
Auditor can now actually check a created file's content against the objective, not just its
existence.

Verified three ways: unit tests proving the preview is text-only (binaries silently skipped),
capped, and excludes `script.py`; one unit test that builds a `ToolCallResult` shaped exactly
like a real code-interpreter output and confirms the content reaches `_tool_output_text()` -
i.e. reaches the Auditor - with no LLM call needed; and full scenario-tier re-verification
after the terminal_output text changed shape (see the fixture note below).

**Deliberately not attempted:** the Auditor still doesn't verify *correctness* (that 16 is
the right sum of 3+5+8, not just that a `total` key is present) - its prompt is a
presence/count/topic checker, not a calculator. Doing that would mean either teaching the
Auditor to reason about arithmetic (prompt-risk, hard to keep general) or giving it code
execution of its own (a materially bigger change). Flagged, not built - the structural
blindness (content invisible at all) was the dominant risk and is now closed; semantic
correctness-checking is a separate, larger project.

**Fixture fallout, expected and handled the same way as before:** changing what
`_terminal_output()` renders changes the Operator's next-step prompt for any task with a
second step after `code.interpreter`, so 4 fixtures (`code_interpreter_csv_summary`,
`code_interpreter_generate_file`, `code_interpreter_json_transform`,
`implicit_code_interpreter_numbers_report`) failed on replay until re-recorded against
`localdeploy_qwen3vl_8b` - all 4 re-recorded clean, scenario tier back to 33/33.

### W2 - Give the Access page and the secret vault a UI *(done, 2026-07-29)*

§4 item 4. `storage/secrets.py` (Fernet) existed with zero way to populate it short of
writing Python - no admin route, no UI, `set_secret`'s only caller was a test.

**Fixed**: `SecretVault` gained `list_secrets()` (service → key names, **never values** - the
one invariant the whole feature depends on) and `delete_secret()`. Three admin routes
(`GET`/`POST`/`DELETE /admin/api/secrets`) all fail with a clear "run `ybm setup`" message
when `AGENT_SECRET_VAULT_KEY` is unset rather than a raw vault exception. A "Secret Vault"
section under Streamlit's Configuration panel lists `service.key` pairs with delete buttons
and a form to add/replace one (password-masked input); `set`/`delete` are audited by
service+key, deliberately never by value.

Verified: 6 `SecretVault` unit tests (round-trip, empty-vault, delete-of-missing,
value-never-in-listing), 4 admin-route tests including one that inspects the actual audit
event payload to confirm the value never lands there, and 2 Streamlit `AppTest` smoke tests
(renders the list, shows the setup hint when the key is unset, and asserts no `sk-`-shaped
string appears anywhere in the rendered page).

**Bug found and fixed along the way, unrelated to secrets:** `_render_kill_switch()`'s
`already_off = access_modes and all(...)` returned the empty dict itself (not `False`) when
`access_modes == {}`, and `st.checkbox(disabled=...)` raises `TypeError` on a non-bool -
crashing the entire admin page for a genuinely reachable state (zero capabilities
configured). Found because the new Streamlit smoke test used a minimal fixture with an empty
`access_modes`, unlike every pre-existing fixture in that test file. Fixed with `bool(...)`.

### W3 - Re-baseline the live E2E suite *(not attempted - needs a live sit-down)*

11 cases, untouched since the 72→11 trim; the last real pass rate (49%) predates P3 entirely,
so it measures a system that no longer exists. Needs a running stack and real Telegram/E2E
credentials this session doesn't have queued up - a deliberate sit-down with the user, not
something to do silently in the background. Until then the scenario tier (33/33, seconds to
run, zero live cost) is the honest signal and is described that way in ARCHITECTURE.md.

### W4 - P4's 4-page console restructure (N6) *(not attempted - large, scoped separately)*

Genuinely unblocked now (the UI shows correct data, W2 added the last missing Configuration
piece), but it's a multi-page UI redesign, not a bounded bug fix - the kind of larger,
multi-session effort CLAUDE.md says to scope and propose separately rather than fold into a
gap-closing pass. Ready to start whenever it's prioritized on its own.

### W5 - A fast path for status requests *(considered, deliberately not built)*

The deleted plan path had an LLM-free shortcut for status-shaped objectives, reached via a
hardcoded keyword list (`"current status"`, `"what's happening"`, ...) checked *before* the
LLM ever saw the objective. Rebuilding it would mean reintroducing exactly the brittle,
keyword-matching, case-by-case routing that this codebase's own redesign deliberately moved
away from in favor of the Operator loop always deciding via LLM judgment - and unlike the old
plan-based version (which still went through plan validation), a bare pre-check would
fully bypass that judgment. A message that merely mentions "status" for something unrelated
("check on the status of my package delivery") would misroute silently, with nothing to catch
it. The saved cost is one extra `decide()` call on a rare, cheap request type; the risk is a
new, permanent, un-reviewed special case in the routing logic. Not a good trade - correctly
declined, not silently skipped. If this is revisited, it should be an Operator-loop-level
optimization (e.g. cheaper/faster model tier for single-tool objectives in general), not a
keyword list for one request type.

### Deliberately NOT on this list

- **More scenario cases for their own sake.** 33 green cases cover every tool category. Add
  one when it pins down a specific behaviour (W1 needs one); don't pad the count.
- **Chasing the remaining "duplicate" the detector reports.** It is two intentionally
  different fake providers (§4 item 16).
- **Trimming docs further.** README/ARCHITECTURE/HISTORY now have distinct jobs - how to run
  it, how it works, why it is this way. HISTORY is long because it is a ledger; that is its
  purpose, and it is not loaded by anything at runtime.

## 6. What was not verified

- **Prompt quality.** No live LLM call was made in this audit or the P3 work. Whether the
  merged Concierge/Auditor prompts perform as well as the originals is unmeasured - the N3
  re-recording pass is what would measure it.
- **The live E2E pass rate.** Last broad run was May (49%), on the old 72-case suite. Not
  re-measured against the new 11-case suite either.
- **Anything visual.** No browser/screenshot tooling is available in this environment, so
  every UI claim is from source inspection, not from looking at the rendered page.

## Closing note: the further cleanup pass (2026-07-28, same day)

After N1–N5 landed, a full redundancy sweep (systematic zero-reference symbol search, not
spot-checking) found and fixed:

- **§1.1's dead code was flagged but not yet deleted** - this pass deleted it for real:
  `PlanModel`, `PlanStep`, `PlanPostcondition`'s sibling `ApprovalGate`, `SubtaskRecord`,
  `ToolObservation`, `ApprovalDecision`, `PlanRepository`, `ToolRegistry.validate_plan()`, the
  capability-alias-mapping cluster only those classes used, the `plans`/`subtasks` tables, and
  `tasks.plan_id`/`current_step_id` (both the Pydantic fields and the DB columns for fresh
  installs - existing databases keep the harmless unused columns/tables rather than risk a
  destructive migration). `PlanPostcondition` itself was kept - despite the name, it's the
  Operator loop's own live postcondition record, not part of the deleted plan model.
- **A dead message-builder cluster** in `telegram_notifications.py` (146 lines, `_task_message`
  and friends, superseded by `_user_facing_task_message` since P3 but never removed) was
  deleted. Removing it **exposed a real, live bug**: a completed workspace-launch task replied
  "The local workspace is ready." with no URL - the working `preview_url` link the dead
  formatter used to include was silently lost. Fixed with `_with_result_links()`.
- **The dead "Step" metric** in Streamlit's Live Activity panel (always showed "-", reading
  the same zero-writer `current_step_id`) now shows the real operator step count.
- **3 of 4 candidate-dead admin API routes** (`GET /api/schedules`, `GET
  /api/database/summary`, `GET /api/vscode` - each an exact duplicate of data already embedded
  in `/api/summary`) were deleted. `POST /api/vscode/terminal-commands` was investigated and
  **kept** - it's a real, unique write capability (manually queue a VS Code terminal command
  from the admin API), not a duplicate; the Streamlit UI just never built a button for it.
- **The live E2E suite** was cut from 72 to 11 cases (see Part 1, P2) - the E2E runner script
  itself (`run_all_e2e_tests.py`, 1,241 lines) was deliberately left untouched beyond a
  docstring fix, since verifying a refactor of live-Telegram-driving code isn't possible
  without actually running the full stack against real credentials.
- **`docs/ROADMAP.md` and `docs/AUDIT.md` were merged into this file** - see Part 1 §5's
  delete list. All ~98 code comments citing either by path were mechanically rewritten to cite
  this file instead, and the §2.x/§3.x subsection labels those comments cite by number were
  kept as real headers in Part 2 rather than collapsed into prose, so every citation still
  resolves to a specific section, not just "somewhere in this file."
- **`config/config.yaml` had 128 keys that were exact matches for the Pydantic schema
  default** (verified per-key, not guessed: each candidate key was removed and the resulting
  effective settings compared byte-for-byte against the original before being accepted) -
  317 lines → 144. Notably, this removed the explicit `blocked_imports` list added earlier the
  same day to close the P5 item 3 `importlib` bypass - that list now matches the code default
  exactly, so removing it doesn't change current behavior, and it means this machine will
  automatically inherit any *future* code-level denylist improvement instead of needing a
  manual sync, closing the exact class of bug (explicit YAML silently freezing out a later
  default fix) that P5 item 3 found in the first place. `config/config.example.yaml` was left
  untouched - its job is to document every field explicitly for `ybm setup` to bootstrap from,
  which is a different purpose than the live config's "diff from defaults" role.
- **A real, live bug in `ybm.ps1`'s `clean` command**, found while using it to clear stale E2E
  artifacts: `clean_agent_control.ps1`'s parameters are all `[switch]`, but `ybm clean -Caches`
  passed them through via array-splatting (`@cleanArgv`), which sends each element
  *positionally* rather than re-parsing `"-Caches"` as an actual flag reference - every switch
  was silently dropped, and `ybm clean -Caches` (or any flag) always printed the "choose at
  least one switch" usage error, no matter what was passed. Every invocation of `ybm clean`
  with a flag had been silently broken since P1 introduced `ybm.ps1`. Fixed by building a
  hashtable from the raw tokens and splatting that instead (hashtable splatting binds correctly
  to named/switch parameters; array splatting does not). Verified end-to-end: created a file
  under a target directory, ran `ybm clean -Caches`, confirmed it was removed.
- **Investigated whether `channels/responder.py`'s fallback chat path is truly dead**, per N6's
  open question. Traced it by code, not a live call: `MessageClassification.reply` is an
  *optional* field (`str | None = None`, not enforced by any validator) - the Concierge prompt
  instructs the model to always populate it for `is_task=false`, but nothing at the schema
  level guarantees a weaker/local model follows that instruction. `_non_task_response()` in
  `channels/telegram.py` falls back to the separate `responder.answer()` call whenever `.reply`
  comes back empty, and `cli.py` wires `responder` up by default whenever an LLM provider
  exists (the same condition nearly everything else in the system uses) - so the fallback is
  live-reachable in production, not just defensive dead code. **Kept, not removed.** Found and
  closed a real test-coverage gap in the process: the only existing test proved the *opposite*
  direction (`.reply` populated → responder NOT called); nothing proved the fallback actually
  fires. Added `test_telegram_non_task_falls_back_to_responder_when_concierge_reply_is_empty`.

# Part 4 - Multi-agent and feature-parity build (2026-07-30)

Context: a competitive analysis against the closest neighbor projects (OpenClaw - a
self-hosted multi-channel chat-to-agent gateway, 346k★; the SemaClaw research paper on
general-purpose personal AI agent "harness engineering"; OpenHands, Open Interpreter, gptme)
found YBM strong on policy/approval infrastructure and the deterministic scenario tier, but
genuinely behind on the multi-agent dimension: no parallelism, no sub-agent context isolation,
no cost tiering beyond reactive error-escalation, no skills/persona/knowledge-base layer, and
a single channel. That analysis produced an 8-item, two-tier plan (T1.1–T1.4, T2.5–T2.8); this
part records what was actually built, what was verified, and the trade-offs made along the way.
Numbering follows that original plan for traceability, not strict build order.

### T1.4 - LLM token/cost tracking *(done, 2026-07-30)*

Every OpenAI-compatible response carries a `usage` object; it was silently discarded, so there
was no way to see what a task actually cost - a real gap given the standing "no live LLM spend
without check-in" policy and that `openai_saved` is the automatic paid fallback when LocalDeploy
is down.

**Built:** `OpenAICompatibleProvider.last_usage` (normalized `prompt_tokens`/`completion_tokens`/
`total_tokens`/`model`, with an Ollama-style `prompt_eval_count`/`eval_count` fallback for
LocalDeploy-shaped responses) - `None` when a server reports nothing, never a fabricated zero.
`FailoverLLMProvider` proxies whichever inner provider actually served the last call.
`OperatorLoopService` and `AuditorService` each expose `self.last_usage` after their own calls.
`TaskWorker._record_llm_usage()` accumulates into `task.metadata["token_usage"]` - a running
total plus a `by_source` breakdown (`operator`/`auditor`/`subagent`) and `last_model` - after
every operator decide() and auditor audit() call. Surfaced in `ybm trace` (a `tokens  N total
over M call(s) (operator=X, auditor=Y)` line) and in the Streamlit trace view (a 5th metric
column plus a breakdown caption).

**Verified:** unit tests on provider usage capture (OpenAI-shaped, Ollama-shaped, missing-usage,
failover proxying), worker-level accumulation across multiple operator+auditor calls, `ybm
trace` output, and a Streamlit `AppTest` smoke test. No fixture cost - nothing here touches
prompt text.

### T1.1 / T1.2 - Parallel fan-out and sub-agent delegation *(done, 2026-07-30)*

The core architecture change: two new `OperatorAction` values, `call_tools_parallel` and
`delegate`, alongside the existing `call_tool`/`done`/`ask_user`/`blocked`.

**`call_tools_parallel`**: `OperatorDecision.parallel_calls: list[ParallelToolCall]` (2+ items,
enforced by a model validator), executed concurrently via `asyncio.gather` in
`TaskWorker._run_parallel_calls()`. Deliberately narrow - skips the
approval/retry/background-wait machinery `call_tool` has, because none of it generalizes to N
calls at once (which one pauses the task for approval? what does "retry" mean when 2 of 5
succeeded?). A call that needs approval or starts a background session fails cleanly with a
message telling the model to reissue it alone via `call_tool`; every other call in the batch
still runs. Verified concurrency directly (not just "did it not crash"): adapters record their
own start/finish timestamps, and every call's start time is asserted to be before the earliest
finish time - real interleaving, not sequential-but-fast. Safe against the same task's metadata
being written from multiple concurrent coroutines: each call gets its own `ToolCallRequest` (own
id, so `tool_invocations.create()` is an independent insert), and `_record_tool_result` is a
plain synchronous method with no `await` inside it, so it runs atomically with respect to the
single-threaded asyncio event loop - no read/write can interleave with another call's.

**`delegate`**: `OperatorDecision.delegate_objective` (required) and `delegate_tools` (optional
allow-list, enforced in code, not just by prompt instruction). `TaskWorker._run_delegate()` runs
a bounded (`DELEGATE_MAX_STEPS = 6`, independent of `operator_max_steps`) inner operator loop
with its own history, starting from nothing. Only a compact summary - one `"delegate"` history
entry - crosses back into the parent's history; the sub-loop's own step-by-step work never does.
That is the entire value: a long or exploratory sub-task doesn't bloat the parent's context the
way inlining the same steps via plain `call_tool` would. A sub-task cannot itself delegate
(recursion refused in code, forcing it to try something else - its own bounded step budget is
the backstop if it keeps trying), cannot pause for approval, cannot wait on a background session,
and cannot ask the user a question - each of those needs task-level state a synchronous,
in-process sub-loop doesn't have; hitting one fails that sub-task cleanly with a reason the
parent sees, rather than hanging.

One subtlety resolved deliberately, not by accident: a sub-task's own tool calls DO update the
parent task's `last_tool_output_text`/`last_tool_name` metadata (via the same
`_record_tool_result` the sub-loop calls). This is not a context-isolation leak - it's what lets
the audit gate ground a `done` that immediately follows a `delegate` step in the sub-agent's real
last tool output. `"delegate"` was added to `AuditorService.CONTENT_TOOLS` for exactly this:
without it, a task that delegates and then immediately finishes would skip the audit gate
entirely, since `"delegate"` itself isn't a content-producing tool name for the backward history
scan to recognize. Only the *history* (the step-by-step record) stays isolated; the answer stays
grounded.

**Verified:** 9 dedicated tests (`test_worker_parallel_and_delegate.py`) covering concurrency,
budget accounting (a parallel batch costs N budget steps, not 1 - no free unlimited fan-out),
approval/background-session failure isolation (one call failing doesn't fail the batch), history
isolation, tool-set restriction enforcement, recursion refusal, step-budget exhaustion, and
cross-task token-usage accumulation. `operator_system.md`/`operator_user.md` were extended to
teach the model both actions and when to prefer plain `call_tool` instead - this, being a system
prompt change, invalidated every scenario fixture (see the re-recording note below).

### T1.3 - Skills system *(done, 2026-07-30)*

User-droppable capability packs: a markdown file with YAML frontmatter (`name`, `description`)
under `adapters.skills.root_dir`, discovered fresh on every call - copying a file in is the whole
installation step, no code change, no restart. New tool `skills.use` with `list` (name +
one-line description only - progressive disclosure, so having 30 skills installed doesn't bloat
every prompt) and `read` (loads one skill's full body by name, called only once the Operator has
decided it's relevant). A malformed skill file (bad YAML, missing frontmatter, no `name`) is
silently skipped on its own, without hiding every other valid skill in the same directory or
crashing tool registration.

**Verified:** 11 tests covering list/read, progressive disclosure (the body never appears in a
`list` response), malformed-file isolation, sorting/capping, and registry gating (on the adapter
config's own `enabled` flag and on the `TELEGRAM_RECEIVE` capability, which it reuses rather than
inventing a new capability for - reading local instructional text has no side effects beyond what
that capability already gates).

### T2.5 - Persona and learned preferences *(done, 2026-07-30)*

One global, cross-conversation identity/preference document - "prefers concise answers,"
"timezone is America/Chicago" - distinct from `channels/memory.py`'s `ConversationMemoryService`,
which is per-conversation short-term recall keyed by `conversation_id` and rebuilt fresh each
thread. Storage is a single file (`.agent_control/persona.md`), read fresh on every Operator step
and injected into `cli.py`'s `_worker_config_context()` (the same factory that already builds the
tool catalog + vault summary text, re-invoked before every `decide()` call) as a `## User persona
and preferences` section - empty string, at zero prompt cost, when nothing has been recorded.
New tool `persona.manage` (`get`/`update`) lets the model persist a durable preference itself,
same read-then-write-the-whole-thing model as an ordinary file edit, not an append/merge (so
there is exactly one place the current state lives).

**Verified:** 17 tests covering the read/write module directly, the tool adapter, registry
gating, and - importantly - the actual `_worker_config_context()` integration point itself (not
just the standalone helper in isolation), proving the persona text really reaches the string that
becomes the Operator's prompt.

### T2.6 - Model tiering as routing *(done, 2026-07-30)*

Previously, `major_provider` was used only reactively - escalated within a single `decide()`
call after a caught JSON-parse/validation failure. The codebase's own existing reasoning for that
design (documented in `operator.py`) explicitly rejects guessing complexity from objective text
up front as worse than reacting to *observed* difficulty. T2.6 extends that same philosophy to a
second, already-available signal rather than replacing it: `OperatorLoopService.decide()` gained
a `prefer_major: bool` parameter that selects `major_provider` (when configured) from the very
first call, not just after a caught exception. `TaskWorker` sets it when `history` already
contains an audit-gap or fulfillment-gap retry marker - a concrete, local, zero-cost-to-check
sign that a `done` was already rejected once on this task, which is exactly the kind of observed
difficulty the reactive escalation was designed to react to, just detectable before the call this
time instead of only from a caught exception during it.

Deliberately **not** applied to delegated sub-loops: `_run_delegate()` never sets `prefer_major`,
so a sub-task defaults to the cheaper model like any other step. This matches the 2026
supervisor/worker cost pattern research turned up (orchestrator on a capable model, workers on
cheaper ones, cutting cost 40–60% in the cited pattern) - a delegated sub-task is the "worker"
side of that split, and giving it the stronger model just because delegation is happening would
work against the pattern's whole point, not reproduce it.

**Verified:** unit tests on `decide()`'s provider selection (major-from-the-start when
`prefer_major=True`, graceful fallback to default when no `major_provider` is configured, no
change to default behavior when `prefer_major` is left False) and one worker-level integration
test driving a real audit-gap-then-retry sequence, asserting the exact sequence of
`prefer_major` values passed to each of the three `decide()` calls (`[False, False, True]`).

### T2.7 - Personal knowledge base *(done, 2026-07-30)*

Local, keyword-overlap search over a folder of the user's own reference material - deliberately
not embeddings/vector search: no extra model dependency (an embedding step would need either a
live API call per index/query or bundling a local embedding model), fully deterministic, and
testable with zero network or GPU. A real trade-off (semantic near-misses aren't found), not a
placeholder for "real" search later - recorded as such rather than glossed over.

`knowledge_base.py` reuses `filesystem_manage.py`'s existing text-extraction helpers
(`_extract_supported_text`, `_is_text_file` - plain text/HTML tag-stripping/PDF via `pypdf` with
a raw-bytes fallback) rather than duplicating that logic, imported directly since promoting it to
a shared module was more churn than the reuse needed. Each file is chunked (1200-char passages,
so a match deep in a long document isn't diluted by the rest of the file) and scored by
distinct-word overlap with the query, normalized by query size. Re-indexed fresh on every search
call, same reasoning as skills.use: a personal-scale document set (dozens to low hundreds of
files) makes this cheap enough that a persistent index with cache invalidation would be
solving a problem that doesn't exist yet. New tool `knowledge.search` (`list_sources`/`search`).

**Verified:** 15 tests including real PDF text extraction (a hand-built minimal PDF, proving the
reuse actually works end to end, not just that the import resolves), chunk-splitting on a long
document with two separated "hot spots," empty-query and no-match handling, `max_results`
capping, and registry gating.

### T2.8 - Local web chat channel *(done, 2026-07-30)*

A second channel, so basic use doesn't require Telegram to be configured or reachable - the
original pitch was explicit about this being the point. Scoped deliberately narrow rather than
reimplementing Telegram's intake pipeline (classification, voice, command parsing, inline
approval keyboards): two new admin routes, `GET`/`POST /admin/api/chat/messages`, backed by one
fixed local conversation (`WEB_CHAT_ID = "local"` - this is a personal, single-user system, not
multi-tenant, so one thread is the right v1 scope, the same way there is one Telegram user). A
sent message becomes a normal `TaskRecord` via the exact same `repositories.tasks.create()` path
Telegram intake uses, with `metadata["source_chat_id"] = "local"` - meaning it goes through the
identical worker/policy/approval pipeline as any other channel, no parallel code path to drift
out of sync. New repository method `TaskRepository.list_for_conversation()` (oldest-first, unlike
`list_recent`'s newest-first, since a chat transcript reads top-to-bottom). Streamlit UI: a
"Chat" expander using `st.chat_message`/`st.chat_input` (confirmed to work correctly nested
inside `st.expander` in the installed Streamlit 1.60 via an `AppTest` smoke test, not assumed).

**Explicitly out of scope, disclosed rather than silently broken:** `artifact.deliver`-style
file delivery is Telegram-specific (`tools/artifact_delivery.py`'s `telegram_client`). A web-chat
task that tries to "send" a file will fail with its own error text rather than silently
pretending to have sent something; text answers and any `preview_url`/`workspace_dir` already in
`task.metadata` (the same fields Telegram's own `_with_result_links` surfaces inline) work
identically regardless of channel, which covers the large majority of conversational use.

**Verified:** 3 new `TaskRepository` tests (ordering, scoping to the right conversation, limit),
4 admin-route tests (send-then-list round trip, empty state, validation, audit trail), and a
Streamlit `AppTest` smoke test proving the chat section renders as alternating chat bubbles with
the right text, and that `st.chat_input` inside an expander doesn't raise.

### A concurrent, independently-landed security hardening *(context, not built here)*

Partway through this build, `orchestration/executor.py`, `policy/engine.py`, and
`storage/repositories.py` changed underneath this work - not as part of it. The bare `approved:
bool` flag `ToolExecutor.execute()` used to take (trivially bypassable by any caller passing
`approved=True`) was replaced with a real, atomic, single-use `approval_id` token
(`ApprovalRepository.consume_approved()`/`decide_pending()`, race-safe via conditional
`WHERE status = 'pending'` updates, with a `_approval_matches()` binding check that verifies the
approved request's task/tool/capability/risk/input/scope are byte-for-byte identical to what's
being dispatched). Separately, the dead `require_approval_at_or_above` threshold this document
flagged as a real bug earlier the same day (§4, an early return in `_requires_approval()` that
made the global approval floor unreachable) was fixed in the same pass - the early return is
simply gone now.

This work's own code was adapted to match (the `_run_parallel_calls`/`_run_delegate` calls
already only ever passed `approved=False`, i.e. "run unapproved, handle NEEDS_APPROVAL as a
clean failure" - semantically identical to omitting the parameter entirely under the new
signature, so no logic change was needed there), and four pre-existing tests that encoded the
old (buggy) approval-floor-never-fires behavior as "expected" were updated to reflect the
now-correct behavior (a HIGH-risk call now genuinely requires approval, since HIGH ≥ the
configured `require_approval_at_or_above: medium` floor) rather than left red.

### Re-recording note, and what re-recording surfaced

`operator_system.md`/`operator_user.md` (T1.1/T1.2's two new actions) invalidated every scenario
fixture that exercises the Operator loop - effectively all 16 fixture files, since every scenario
test runs through it. Re-recorded against `localdeploy_qwen3vl_8b` (free, local), consistent with
every prior re-recording pass in this document, across three passes as issues were found and
fixed. Most of what re-recording surfaced was the concurrent approval-token hardening interacting
with the test harness and existing scenario tests, not T1.1–T2.8's own code - the last two items
below are plain model non-determinism instead:

- **The scenario harness itself was missing `AWAITING_APPROVAL` from `TERMINAL_STATUSES`.**
  `run_task_to_completion()`'s tick loop only recognizes a task as "settled" for a fixed set of
  statuses; `AWAITING_APPROVAL` - conceptually identical to `CLARIFYING`, which was already in
  that set ("no further autonomous progress without external input") - was not. The one scenario
  test that deliberately proves an approval gate fires and is never approved
  (`test_code_interpreter_default_settings_need_approval_without_docker.py`) got stuck calling
  `process_task()` on an unchanging task until the harness's own tick budget raised, rather than
  the harness recognizing the task had genuinely settled. This is a real, standing gap in the
  harness - unrelated to this build's own code - fixed by adding `AWAITING_APPROVAL` to
  `TERMINAL_STATUSES`.
- **The concurrent approval hardening lost a real piece of human-facing context.** Before, a
  tool that needed approval for its own reasons (e.g. code_interpreter.py's `ApprovalRequired`
  when a run would go unsandboxed) raised an exception whose message ended up as the
  human-readable "why" on the approval. Under the new policy-engine-level gate, a
  `NEEDS_APPROVAL` result carries only `{"approval_id": ...}`, and the `ApprovalRequest.summary`
  is now the generic `"Approve code.interpreter using terminal.run"` - accurate, but the specific
  "this would run unsandboxed" reasoning a human approving it used to see is gone. Not fixed here
  (redesigning another change's approval-summary plumbing is out of scope for this build); the
  one test that asserted on the old reason text was updated to assert what's actually still true
  (an approval record naming the right tool/capability/risk level exists) instead. Worth a
  follow-up: give `ToolAdapter.execute()` a way to attach a specific reason to a policy-level
  approval request again.
- **Six more scenario tests got stuck at `AWAITING_APPROVAL` - but not because of the global
  approval floor.** First theory, and wrong: since the global floor (previous item) now
  genuinely fires, raising each affected test's `approval_policy.require_approval_at_or_above`
  to `CRITICAL` should unstick them. It didn't - same failure, identical symptom, after the
  "fix." Direct tracing of `tool_invocations` (`request["risk_level"]`, `request["capability"]`)
  found the real cause: a completely different, *unconditional* gate,
  `ToolDefinition.approval_required_operations`, already present on `code_interpreter.py` and
  `schedule_manage.py` for specific operations (`run_python`, `generate_and_run`, `create`, …) -
  explicitly documented in that code as "runtime-owned and cannot be bypassed by Full Access or
  by setting input.approved in model output." No floor, no policy override, no settings knob
  reaches it; it is not a bug, it is by-design defense in depth, and the five
  `code.interpreter` scenario test files (`test_code_interpreter.py`'s csv_summary/
  generate_file/json_transform siblings plus the numbers-summary and CSV-fibonacci tests) and
  `test_schedule_create.py` simply had no way to get past it. The `approval_policy=CRITICAL`
  overrides were reverted (they did nothing and implied the wrong mental model). Fixed properly
  by giving the harness itself a way to simulate a human clicking "approve": `harness.py`'s
  `run_task_to_completion()` gained an `auto_approve: bool = False` parameter - when a task
  reaches `AWAITING_APPROVAL`, it now approves every pending `ApprovalRequest` for that task via
  `ApprovalRepository.decide_pending()` and keeps ticking, instead of returning immediately. Off
  by default, so a test whose actual subject *is* the approval gate (like the "stuck at
  `AWAITING_APPROVAL` on purpose" test above) is unaffected and still observes it as terminal.
  Each of the six tests now passes `auto_approve=True` at its one call site, with a comment
  naming the specific unconditional gate it's standing in for. Covered by a dedicated,
  fixture-free unit test (`backend/tests/scenario/test_harness.py`) that drives the mechanism
  directly against a scripted fake worker - proving both the approve-and-continue path and the
  stays-terminal-by-default path - without needing a real Operator loop or recorded fixture.
- **A second, unrelated data-consistency bug, also newly exposed by the same floor:**
  `test_mcp_call_fake_echo.py`'s fake server's `MCPServerConfig` never set `risk_level`,
  defaulting to `HIGH` - but the same test's own MCP catalog entry advertised `risk=low` for
  that tool, which the model reasonably imitated when declaring `risk_level` on its `call_tool`
  request. `mcp_client.py`'s per-server risk resolver (`_mcp_required_risk`) uses the actual
  `MCPServerConfig.risk_level`, not the catalog's cosmetic display value, so the model's
  imitated `low` understated the real requirement and was rejected before ever reaching the
  approval gate. The two values had silently disagreed since this test was written; nothing
  enforced them being equal until the risk-understatement check (also part of the concurrent
  hardening) started checking. Fixed by setting `risk_level=RiskLevel.LOW` explicitly on the
  test's `MCPServerConfig`, matching what its own catalog already claimed.
- **`mcp_call_fake_echo`'s disabled-capability test kept failing even after the risk_level fix
  above** - three independent clean re-records in a row, same failure every time:
  `MCPClientInput` has both `arguments: dict` (call_tool's key-value tool arguments) and
  `args: list[str]` (install_server's unrelated command-line argument list), and the model
  reliably reached for `args` first, got Pydantic's raw `"Input should be a valid list
  [type=list_type, ...]"` back, and - across all three recordings - never once connected that
  message to "use `arguments` instead," repeating the identical wrong shape for up to 8 retries
  in a row until the operator loop's step budget ran out. Two fixes, verified in the right order
  before recording again: first, `test_mcp_call_fake_echo_disabled_by_capability_policy`'s own
  `assert all(status == "denied" ...)` was too strict - the exact fragility already fixed the
  same way in `test_code_interpreter_default_settings_need_approval_without_docker.py` (a
  validation-error retry can precede the real target assertion; the gate firing *at least once*
  is what the test is about), so it was changed to `assert any(...)` over calls actually reaching
  `denied`. Second, and the one that actually addresses the model's behavior rather than just the
  test's tolerance for it: `MCPClientInput` gained a `field_validator("args", mode="before")` that
  rejects a dict with an explicit, actionable message ("key-value tool arguments for call_tool
  belong in 'arguments' instead") in place of Pydantic's generic type error. Re-recorded once more
  after that change: the model still guesses `args` on its very first attempt (that first guess
  looks deterministic, not just likely), but now self-corrects to `arguments` on every retry after
  seeing the clearer message, instead of repeating the mistake for all 8 - confirmed by diffing
  the resulting fixture's recorded attempts, not assumed. Covered by two new unit tests in
  `test_contracts.py` (dict `args` rejected with the new message; a real `list[str]` `args` for
  `install_server` still passes).
- **`send_found_pdf` needed one clean re-record for ordinary model non-determinism, not a bug:**
  the local model read the PDF via `filesystem.manage` and declared `done` without ever calling
  `artifact.deliver`, despite the objective directly asking to have the file sent. Re-recorded
  cleanly on retry with the same settings and objective text - genuinely just a different sample
  from the same model, not a regression tied to this build.
- **`status_request`'s second test needed its objective reworded, not just retried.** With an
  extra pre-existing task already in the DB, the objective `"current status"` (bare, two words)
  made the model end at `CLARIFYING` instead of `COMPLETED` - twice in a row, on two independent
  clean recordings, with the model asking essentially the same question both times: *"What is the
  current status you would like to check or update?"*. That's not noise, it's the model correctly
  flagging a real ambiguity in a two-word phrase (query vs. update) that a plain retry was never
  going to fix. Reworded to `"what's the current task status?"` - unambiguous about being a query,
  short enough to stay a meaningfully different phrasing from the sibling test's `"give me the
  current status"` above so both stay covered - and it passed clean immediately.

Final tally: all 16 fixture files across three recording passes (the first invalidated by
T1.1/T1.2's prompt changes; a second, narrower pass for 6 of the 9 that needed a settings/harness
fix or a clean retry; a third, narrower still, for the 3 that turned out to need a real contract
message improvement, a test-assertion fix matching existing precedent, and a reworded objective,
respectively) - scenario tier back to fully green, `backend` unit + scenario suite passing end to
end (`pytest tests`, exit 0).

# Part 5 - React admin console: build and cutover (2026-08-01)

Context: `docs/archive/UI_REWRITE_PLAN.md` (written after a competitive UI/UX pass, 2026-07-31) called for
replacing the 1,776-line single-page Streamlit console with a React SPA served by the existing
FastAPI backend, in six phases (0 backend readiness, 1 shell+chat, 2 approvals, 3 tasks+trace, 4
access, 5 settings+wizard, 6 cutover). All six shipped; this entry records the cutover and the two
things found only by actually building it rather than by re-reading the plan.

**Phases 0–5, in brief** (full detail, including every scoping decision and what was deliberately
cut, lives in `docs/archive/UI_REWRITE_PLAN.md` §9–§14, kept current phase by phase as each landed):
Vite + React 19 + TanStack Query/Table + React Flow + Zod, served from
`backend/src/agent_control/static/admin/` at `/admin` with a dev-proxy fallback; an
Evidence-Pack approval flow ordered for a sub-15-second decision; a Tasks/Trace view with a real
lane graph grounded in a new `origin`/`parent_step_id` correlation field (zero DB migration - it
already serialized generically); an Access page (capability toggles, kill switch, presets, secret
vault); a Settings page (LLM/Telegram/adapters/MCP/diagnostics/audit) and a first-run wizard.
Three items were deliberately not built and are disclosed as such in the plan doc rather than
rushed: **D2** (time-boxed approval grants - real new backend machinery, the one change that
widens the security surface), **D6** (task replay), and **A1–A3** (per-role model/prompt/delegate
presets - need new config schema + storage, no shortcut available).

**Before cutover: an actual feature audit, not just the plan's own checklist.** The plan's Phase 6
section said "cut over once parity is reached"; taking that literally meant checking the React
console against every one of Streamlit's `_render_*` functions, not just the ones the plan had
already named. Four real, un-disclosed gaps turned up this way:

- **VS Code bridge live-connection status** (heartbeat, active file, workspace folders) - shown in
  Streamlit's header, absent from React entirely; the client never even fetched `summary.vscode`.
- **"Live Activity"** - Streamlit's landing page listed every currently-running task (any channel,
  not just local chat) with inline pause/cancel and a live output preview; React required opening
  Tasks, filtering, then opening a trace to do the same thing.
- **Computer-use live session monitor** - Streamlit showed the current computer-use task's
  screenshot path and action count with a stop button; React's Computer Use card was config-only.
- **A per-domain health strip** (LLM/Telegram/VS Code/Workspace/Database) - Streamlit showed this
  as a persistent chip row; React had collapsed it to a single dot + active-task count.

All four were closed before deleting anything: `ActiveTasksPanel` (reuses `useTasks`/
`useTaskSignal`, no new endpoint) at the top of Tasks; a live-session block added to the Computer
Use settings card; a connection-status block added to the VS Code settings card
(`admin.py`'s existing `_vscode_summary()`); and a health breakdown added to `HealthIndicator` as
a hover tooltip rather than a new persistent strip or a dropdown-menu popover - the first version
used `@base-ui/react`'s Menu primitive and measurably cost the always-loaded Chat landing page
~27kB gzip for a panel that duplicates data already in Settings; swapped for `Tooltip`, already
loaded unconditionally by `main.tsx`, at zero marginal cost.

**A real, unrelated security fix found while wiring the VS Code/MCP status data**:
`config.py`'s `safe_summary()` - the function every admin/settings response is built from -
redacted the Telegram token and every LLM `api_key`, but dumped `mcp.servers[*].env` completely
raw. MCP servers routinely carry secrets there (API tokens for whatever the server wraps), so this
was a real, live exposure to any admin caller, not a hypothetical. Fixed to strip `env` and return
only `env_keys` - the same "list the key, never the value" invariant the secret vault already
enforces - covered by `test_safe_summary_strips_mcp_server_env_values`. Confirmed against this
machine's own real MCP servers (`ybm`, `filesystem`, `fetch`) post-fix: `env_keys` came back
correctly, no `env` key present in the response at all.

**Cutover, once parity actually held**: deleted `admin_streamlit.py` (1,776 lines) and
`test_admin_streamlit.py` (599 lines); dropped `streamlit` from `REQUIRED_MODULES`
(`bootstrap.py`) and from `backend/pyproject.toml`'s dependencies, and the `playwright` test extra
(its only stated purpose - "Browser UI diagnosis (Streamlit admin at :8501)" - no longer applies;
Playwright E2E for the React console is future work tracked in the plan doc, §15.1, not yet
built); re-locked and re-synced the venv (`uv lock`, `uv sync --extra test --extra e2e --extra
voice --extra desktop --extra dev`), which also dropped 22 now-unused transitive packages. Removed
the `admin_ui` service from both supervisors (`agent_control.supervisor.build_service_specs()` and
`scripts/ybm.ps1`/`scripts/lib/common.ps1`), the `-NoAdminUi`/`--no-admin-ui` flag from both (dead
once there is nothing left to skip), the `admin_ui` entry from `_expected_services()`
(`runtime_status.py` - it read a status file only the deleted Streamlit supervisor ever wrote),
port 8501 from `bootstrap.py`'s `_check_ports()`, and `scripts/services/run_admin_ui.ps1`.
Every "Admin UI" banner/status-check across both supervisors, `onboarding.py`, and the docs now
points at `http://127.0.0.1:8765/admin` (the same backend, same port, no second service) instead
of `:8501`. `admin.py`'s `_ADMIN_HTML` fallback (case 3 of `_serve_admin_app` - no build present
at this checkout) no longer points at Streamlit; it tells the operator to run `ybm ui-build`.

**Verified:** full backend suite green post-deletion (574 passed), `ruff check .` clean, `frontend`
`tsc -b --noEmit` and `npm run build` both clean. Live: `ybm start`/`status`/`stop` with no
`admin_ui` entry anywhere in the output; `/admin` serves the real React build; the health/VS-Code
status panels confirmed against this machine's actual running VS Code bridge session (the real
`active_file` it reported matched what was genuinely open at the time). **Not verified:** an actual
Playwright/browser click-through end to end (still the standing gap §15.1 tracks) and the
first-run wizard's "fresh checkout, no `config.yaml` yet" branch (would have meant deleting this
machine's real config to trigger it, avoided as unnecessarily destructive for a UI check - covered
instead by reading `admin_bootstrap()`'s exact `CONFIG_FILE_PATH.exists()` condition).

**Same day: the user reported `ybm start` "didn't work" and asked for a real Playwright pass
instead of another curl-only check** - a fair challenge, since every phase above had explicitly
disclosed "not verified: an actual browser click-through" as a standing gap. `@playwright/test`
was added to `frontend/` (dev dependency) and used ad hoc - navigate the real running console,
screenshot every page, capture console/network errors - which is exactly what the disclosed gap
predicted might be hiding something, and it was:

- **A genuine blank-page bug at the exact URL every banner and README prints.**
  `main.tsx`'s `<BrowserRouter basename={import.meta.env.BASE_URL}>` used Vite's own `base`
  reflection, which carries a trailing slash (`/admin/`, from `vite.config.ts`'s `base` setting).
  React Router's `basename` matches by exact string prefix, and `/admin` (no trailing slash - the
  literal address `ybm start`'s own banner, `README.md`, and every doc print) does not start with
  `/admin/`, so the router logged a warning and rendered nothing at all. Confirmed via the
  console warning first, then via a totally blank page. Fixed by stripping the trailing slash
  before passing it as `basename`. This is very likely the exact bug the user hit.
- **A flexbox bug collapsing cards to a few pixels tall on every content-heavy page.**
  `AccessPage`/`SettingsPage`/`TaskTracePage`/`TasksPage` all share
  `<div className="flex h-full flex-col gap-4 overflow-y-auto ...">`. Flex items shrink
  (`flex-shrink: 1`, the CSS default) *before* `overflow-y-auto` ever kicks in, so once a page's
  total content exceeded the viewport, every child got proportionally squeezed instead of the
  container scrolling - worst on the smallest cards (Access's Kill switch and Presets rendered as
  a ~32px pill with a real button and description sitting fully present in the DOM, just given
  zero visible height, confirmed via `getBoundingClientRect()` before guessing at a fix). Fixed by
  adding `[&>*]:shrink-0` to all four containers. Re-screenshotted at a deliberately oversized
  viewport (1440×3600) to force a true full-page capture and confirmed every card - Kill switch,
  Presets, all nine access groups, the full Settings stack down through Diagnostics/Audit -
  renders completely.
- **A minor, related finding, fixed rather than patched around:** the trace graph's React Flow
  `<MiniMap>` was rendering partially clipped by its container's `overflow-hidden` corner. Given
  these graphs only ever hold a handful of lanes/nodes, a minimap adds little navigational value -
  removed rather than fighting a third-party widget's positioning CSS.

**Also found while comparing the two install paths (unprompted, while evaluating whether
`install.ps1`/`install.sh` duplicate `ybm.ps1`'s start/setup logic):** they'd drifted.
`scripts/ybm.ps1`'s `Invoke-YbmSetup` installs `--extra test --extra e2e --extra voice
[--extra desktop]`; `scripts/install.sh` installed only `--extra dev` (ruff) - meaning a fresh
Linux/macOS install via the documented one-liner never got `pytest`, `telethon`, or the
voice/desktop extras at all. Fixed both to install the same extras (`test`, `e2e`, `voice`,
`desktop`, `dev`), and added `dev` to `ybm.ps1` itself, which was *also* missing it - meaning a
fresh Windows setup couldn't run the `uv run --frozen ruff check .` step AGENTS.md/CONTRIBUTING.md
both document, either. `install.ps1`/`install.sh` otherwise already delegate correctly (clone →
`ybm.ps1 setup`/`uv sync` → `ybm onboard`, which itself calls `start_all()`) rather than
duplicating start logic - the real, deliberate duplication left standing is
`agent_control.supervisor.py` (Python, cross-platform, for pip-installed non-Windows users) versus
`scripts/ybm.ps1` (Windows-tested PowerShell) each independently implementing "start N services,
check readiness" - documented and justified in `supervisor.py`'s own module docstring, not
accidental drift, and not merged here.

**Verified:** `frontend` `tsc -b --noEmit` and `npm run build` both clean after every fix; the
basename fix confirmed by loading `http://127.0.0.1:8765/admin` (no trailing slash) and getting
the full app instead of a blank page; the flex-shrink fix confirmed via `getBoundingClientRect()`
before and after (Kill switch card: 32px → 126px) and visually via full-page screenshots of
Access, Settings (both Advanced and Level 1), and the Trace graph. **Not verified:** the
install.sh extras fix was not tested with a real fresh clone/install run (would mean installing a
second copy of the whole toolchain); reviewed by direct comparison against `ybm.ps1`'s
already-proven-working extras list instead.

# Part 6 - WhatsApp as a second real channel (2026-08-02)

Context: `docs/archive/UI_UX_AUDIT.md` Phase 16 shipped its "channel-adapter interface" half
(2026-08-01) - `channels/base.py`'s `classify_and_spawn_task`/`resume_clarifying_reply`/
`status_summary` extracted out of `TelegramIntakeService` behind a `ChannelAdapter` Protocol -
but deliberately deferred an actual second channel, since there was nothing real yet to validate
the extraction against. The user asked for WhatsApp specifically. WhatsApp has no free official
bot API shaped like Telegram's; the official Business Cloud API needs a public HTTPS webhook this
local-only product has no infrastructure for. Checked how OpenClaw (the reference self-hosted
project already named in the roadmap, 150k+ GitHub stars) actually does it before assuming:
[Baileys](https://github.com/WhiskeySockets/Baileys), an unofficial WhatsApp Web client,
QR-code device linking, no Meta account or webhook - the same architecture OpenClaw ships as
production-ready, including their own documented practice of linking a secondary number rather
than a primary one to limit account-flagging risk. **Hard constraint for this work, set by the
user:** no real phone number, primary or secondary, was available or would be provided this
session - build the feature generically so anyone running this repo supplies their own number
later, never commit a real number anywhere, and perform no live QR-pairing, account-linking, or
send/receive test.

**What shipped.** A Node.js sidecar, `whatsapp-bridge/` (plain JS, no build step - it's a
standalone process, not bundled into anything), wrapping Baileys behind three loopback-only,
shared-secret-gated HTTP routes (`/health`, `/updates?offset=N`, `/send`) and printing the
link QR straight to its own stdout on first run, which is exactly what `ybm logs whatsapp
-Follow` already tails. `channels/whatsapp_bridge_process.py`'s `WhatsAppBridgeProcess` spawns
and owns that child for the whole lifetime of `cli.py`'s new `poll-whatsapp` entry point - the
secret is generated fresh per run, handed to the child by env var, and also written to
`.agent_control/run/whatsapp_bridge.json` (gitignored, removed on stop) so a separate process
(`run-worker`) can reach the same bridge to send notifications - so `ybm.ps1`'s service list
never had to learn a new, non-Python process type: Node stayed an
internal implementation detail of one more Python service, the condition the user accepted the
new Node.js runtime dependency on. A separate process (`run-worker`) needs that same bridge's
base_url/secret to send completion notifications; solved by writing them to
`.agent_control/run/whatsapp_bridge.json` and re-reading it fresh on every notify call (never
cached), the same convention `run_supervised.ps1`'s own per-service status files already
established.

`channels/whatsapp.py` (`WhatsAppBridgeClient`, `WhatsAppAdapter`, `WhatsAppIntakeService`,
`WhatsAppPollingRunner`) is a real second consumer of `channels/base.py`, and being one
justified three more extractions the first half of Phase 16 had deliberately deferred rather than
guessed at: `schemas.py`'s `task_chat_id(task)` generalized to `channel_chat_id(task, channel)`;
`approve_latest_pending` moved out of a `TelegramIntakeService` private method into
`channels/base.py`; and `format_task_message` - the pure `TaskRecord.metadata` → text formatting,
confirmed to have no Telegram API calls in it - extracted from `telegram_notifications.py` into
`channels/task_notify.py`, now shared by `TelegramTaskNotifier` and the new
`WhatsAppTaskNotifier`. `cli.py`'s `run_worker()` gained a `RoutingNotificationSink` that picks
the right notifier by `task.metadata["source_channel"]`, replacing the single hardcoded Telegram
notifier every task used to get regardless of source.

**A real, pre-existing bug found and fixed along the way, not asked for:**
`AuditEventType.MESSAGE_RECEIVED`/`MESSAGE_SENT` were already channel-generic by their own enum
semantics, but `storage/audit_view.py` displayed them as Telegram-only everywhere (category
`"raw_telegram"`, "Telegram message received/sent" titles, `_source()` only recognizing a
`"telegram:"` actor prefix) - meaning WhatsApp's own message events would have shown up
mislabeled as Telegram's in the audit trail. Generalized to a channel-neutral `"raw_message"`
category/title and an actor-prefix-agnostic `_source()`, with `frontend/src/lib/api.ts` and
`timeline.ts` updated to match. A new `AuditEventType.CHANNEL_ACCESS_DECISION` was added
alongside (not reusing) Telegram's own `TELEGRAM_ACCESS_DECISION` for the same reason - reusing
it would have mislabeled every WhatsApp allow/deny decision as a Telegram one.

**Safe-by-default, deliberately not matching Telegram's own pattern.** Telegram's service entry
in both `ybm.ps1` and `supervisor.py` is `required=True`; `poll_telegram()` has no `enabled`
check at all and simply raises if no token is configured, which is fine because Telegram is this
product's original, expected-to-be-configured channel. WhatsApp is new and off by default
(`channels.whatsapp.enabled: false`) in this same change - mirroring `required=True` would have
meant every existing user's `ybm start`/`ybm run` starts hard-failing the moment this ships,
purely because they haven't touched a feature they never asked for. Deliberately diverged:
WhatsApp's entry is `required=False` in both `ybm.ps1`'s `Invoke-YbmStart` and
`supervisor.py`'s `build_service_specs()`, so an unconfigured install shows one clearly-worded,
non-blocking `[FAIL]` line instead. `bootstrap.py` gained a non-fatal Node.js presence check and
a `_check_whatsapp()` doctor check (ok when disabled, fail when enabled without `node`, warn when
enabled but not yet linked, ok once `.agent_control/whatsapp_auth/` has session files) plus a
best-effort `npm install` in `whatsapp-bridge/` during `ybm setup`, mirroring the admin console's
own non-fatal-if-npm-missing handling.

**Deliberately v1/plain-text-only**, matching Telegram's own plain-text command subset
(`approve`/`status`/"remember that ..."): no `/command` slash syntax, inline buttons, voice
transcription, or artifact/screenshot delivery over WhatsApp - all stay Telegram-only for now,
the same reasoning Phase 16's first half already applied to `tools/artifact_delivery.py`. No
admin-console Settings form for WhatsApp exists yet; it is `config.yaml`-only.

**Verified:** full backend suite green, `ruff check .` clean. `whatsapp-bridge`: `npm install`
succeeded (92 packages, 0 vulnerabilities) and `npm run check` (parses AND loads the module
graph) clean, against this machine's real Node.js v22.11.0/npm 9.8.0. New test coverage:
`test_whatsapp.py` (adapter allowlist deny/allow across
disabled/empty-allowlist/not-listed/allowed, task creation, clarification resume, plain
approve/status commands, polling-runner offset/error handling), `test_whatsapp_notifications.py`
(`WhatsAppTaskNotifier` routing and no-ops for other channels/missing chat id),
`test_whatsapp_bridge_process.py` (node-binary resolution, start/stop and shared-state
read/write against a fake spawner and a faked health check, the not-found and
never-becomes-healthy error paths), `test_supervisor.py` (the `whatsapp` spec is present and
`required=False` by default, `telegram_polling` stays `required=True`, `no_whatsapp=True`
excludes it), and new `test_bootstrap.py` cases for `_check_node`/`_check_whatsapp`/
`_install_whatsapp_bridge_deps`. Live, read-only: `.\scripts\ybm.ps1 doctor` run against this
machine's actual, already-configured `config/config.yaml` (which predates this change and has no
`channels.whatsapp` key at all) came back 27 ok / 0 warn / 0 fail, with `WhatsApp   disabled in
config` and `Node.js   C:\nodejs\node.EXE` both reporting correctly - confirming both that the
new config defaults handle an old config file missing the new key, and that the feature is
genuinely inert for an existing user who hasn't touched it.

**Not verified, disclosed rather than silently skipped:** no live QR-pairing, no linking of a
real WhatsApp account, and no live send/receive - no phone number was available or provided this
session, and none was added to the repo, `config.example.yaml`, or any commit. `ybm start`/`ybm
run` was not run live against this machine's already-running instance (it has real services on
ports 8000/8765 already up) to avoid disrupting whatever the user currently has live; `doctor`
(read-only) was used instead to confirm safe-by-default behavior. Whoever runs this repo links
their own number by following `docs/INSTALL.md`'s new "Link WhatsApp" section.

### Review pass: the sidecar could not start at all *(same day)*

A follow-up review found the shipped bridge was **completely non-functional**, and found it only
by probing the module loader directly rather than re-reading the code.
`@whiskeysockets/baileys@6.7.24` is `"type": "module"` (ESM-only); the sidecar was
`"type": "commonjs"` and `require()`d it, which throws `ERR_REQUIRE_ESM` on Node < 22.12, where
`require(esm)` is still flag-gated. On this machine (v22.11.0) the bridge would have died on its
first line, every time.

The reason this shipped "verified" is the lesson worth keeping: **`node --check` parses syntax
and never resolves imports**, so it passes happily on a file whose very first `require` cannot
work. The check was real, it just did not test the thing that was broken - a
verification-theatre failure, not a missed step. Fixed by converting the package to ESM
(`import` throughout, verified against the real installed package's actual export shape rather
than assumed), and by replacing the syntax check with `npm run check`, which parses *and* loads
the whole import graph. To make that loadable without side effects, the listen/connect calls
moved behind a `main()` guard that only fires when the file is the process entry point - both
directions verified (importing it opens no socket and exits 0; running it refuses to start
without a secret and exits 1).

Four further defects fixed in the same pass, all of which the ESM failure had been masking:
`extractText` didn't unwrap `ephemeralMessage`/`viewOnceMessage`, so any chat with disappearing
messages enabled (a per-chat default on many accounts) would have had every message silently
extract to `""` and vanish with no error; a superseded socket could still emit `close` and
schedule its own reconnect, fanning one dropped connection out into several parallel reconnect
chains; hand-rolled `@g.us` string matching replaced with Baileys' own `isJidGroup`/
`isJidBroadcast`/`isJidNewsletter` predicates; and both `WhatsAppBridgeProcess.start()` and the
`doctor` check now fail fast and specifically on a missing `whatsapp-bridge/node_modules`
instead of a 60-second health-check timeout, with `_wait_until_healthy` additionally noticing a
child that has already exited rather than waiting out the full deadline.

### Review pass 2: startup waste, QR robustness, outbound volume, audit generalization *(same day)*

A third review, this time of things that worked but weren't yet good - four fixes:

**Disabled WhatsApp no longer attempts to start at all.** `build_service_specs()`
(`supervisor.py`) and `Invoke-YbmStart` (`ybm.ps1`) previously always attempted the `whatsapp`
service and relied on `required=False` to keep a failure non-fatal - correct for not blocking
`ybm start`, but it still meant every install that has never touched WhatsApp crash-looped it 4
times (~20s wasted) on every single start, leaving a permanently alarming `[FAIL]` status line
for a feature nobody asked to run. Both now check `channels.whatsapp.enabled` first and skip the
attempt entirely when it's off (the Python side calls `load_settings()` directly and fails closed
- treats an unreadable config as disabled rather than crashing `ybm start` itself; the PowerShell
side has no native YAML reader, so a new minimal `ybm channel-enabled <name>` CLI command reports
it via exit code, no stdout parsing needed).

**QR delivery now has an encoding-proof fallback.** The disclosed risk from the first WhatsApp
pass: `qrcode-terminal`'s Unicode half-block art, read back through `ybm logs whatsapp` under
Windows PowerShell 5.1's default console encoding, could mangle into an unscannable mess with no
error and no other way to link an account. The bridge now also prints the raw QR payload string
as plain ASCII text alongside the art - pasteable into any online QR generator if the rendered
code doesn't look like a clean grid.

**WhatsApp now sends fewer autoreplies than Telegram, deliberately.** `channels/base.py`'s
`classify_and_spawn_task` sends a "Got your message, figuring out what to do…" progress ping
before every classification, then either the real chat reply (from `classify_and_spawn_task`
itself) or an "On it - I'll send the result here when it's done." ping if a task was spawned. For
Telegram (official bot API) that's free. For WhatsApp (Baileys, unofficial, real account-flagging
risk - the whole reason a secondary number is recommended) every extra message is extra exposure.
The two literal strings were named constants (`ACKNOWLEDGMENT_TEXT`, `TASK_STARTED_TEXT`) so a
channel's own `send_progress` can recognize and selectively skip one without touching the shared
function's signature or Telegram's behavior; `WhatsAppIntakeService._send_progress` now skips
only the acknowledgment (pure filler - the real reply/task-started message follows regardless in
the same handling of the update) and still sends the task-started ping, since a real task can run
for minutes and silence until completion would be worse than the volume tradeoff.

**Two audit-view functions now generalize the way their own comments already claimed to.**
`CHANNEL_ACCESS_DECISION` had a category and title but no `_summary()` branch, falling through to
a bare `payload["reason"]` string like `"allowlist_empty"` instead of `TELEGRAM_ACCESS_DECISION`'s
readable `"Denied Telegram message"` - added the equivalent branch, reading the channel name from
the payload each adapter's own `_audit_access()` already stamps rather than hardcoding one channel.
`_source()`'s actor-prefix allowlist was hardcoded to `{"telegram", "whatsapp"}` despite a comment
claiming "generalizes this for free" - meant a web-chat-originated event (a channel that predates
WhatsApp) silently lost its source attribution, since `classify_and_spawn_task`'s actor prefix is
literally `inbound.channel.value` for every channel. Now checks against `ChannelType`'s own values
instead of a hand-maintained set.

**One documented, not "fixed" - WhatsApp's privacy-preserving LID addressing** (`<opaque-id>@lid`,
distinct from `<number>@s.whatsapp.net`) sends an id in place of the real phone number for some
senders. There is no lookup exposed by Baileys to resolve it back to a number, so it can never
match `allowed_numbers` no matter how it's configured - not a bug to fix, since there's nothing to
resolve, but the denial reason (`lid_jid_no_resolvable_number`, surfaced via a new `is_lid` flag
the bridge stamps using Baileys' `isLidUser`) is now labeled distinctly from an ordinary
not-on-the-allowlist denial so it doesn't read as a config mistake with a config-shaped fix.

**Verified:** full backend suite green (783 passed, up from 776), `ruff check .` clean,
`whatsapp-bridge`'s `npm run check` clean, `frontend`'s `tsc -b --noEmit` clean. Live, read-only:
`.\scripts\ybm.ps1 doctor` and a direct `ybm channel-enabled whatsapp` invocation against this
machine's real config both confirm WhatsApp is still correctly seen as disabled (exit 1),
confirming the new start-time gate would correctly skip attempting the service. New/updated test
coverage: `test_supervisor.py` (excludes whatsapp when disabled by default, config-unreadable,
and `no_whatsapp`; includes it not-required when actually enabled), `test_whatsapp.py` (the
acknowledgment-skip/task-started-sent behavior, the LID denial reason), `test_audit_view.py`
(every `ChannelType` round-trips through `_source()`, a non-channel actor prefix is not
misattributed, `CHANNEL_ACCESS_DECISION`'s summary names the real channel). Not verified: live
QR scanning (still no phone number available this session) - the encoding-proof fallback and the
periodic QR reissue (Baileys rotates roughly every 20-60s until scanned) are reasoned from
Baileys' documented behavior, not observed against a real linking flow.

### The `evolution` E2E suite, and the three defects it found *(same day)*

The `autonomy` suite asks whether YBM can finish a hard job. A second suite, `evolution`,
asks the complementary question: does it stay trustworthy while doing so? Eight live
Telegram cases covering learned preferences, credential handling, honoring a refusal,
compound instructions, admitting a capability gap, real scaffolding, scheduled
continuation instead of a retry loop, and refusing to invent a missing file's contents.
Full evidence and reasoning in `docs/archive/E2E_FINDINGS.md`; the short version:

**The harness could not have produced trustworthy results first.** Four runner defects
were fixed before the suite could mean anything. A "last 5 minutes" audit lookup compared
`isoformat()` timestamps (`…T14:05…`) against SQLite's `datetime('now','-5 minute')`, whose
space separator sorts below `T` - so every same-day row matched and a re-run could inherit
an earlier run's classifier verdict; the two near-duplicate copies of that query became
one. `HARD_CEILING_S` clipped every case declaring 900s because the runner adds a 30s
margin, silently under-budgeting AUTO-5/6/7 (a clipped case reads as "the agent stalled").
Chat-route turns were untestable at all - "no task spawned" was always a failure, leaving
the entire conversational surface uncovered - so cases can now declare
`expects_task: false`. And the assertion vocabulary was positive-only, meaning a run could
satisfy every assertion while leaking a credential or executing a denied write; `tools_none`,
`bot_reply_excludes_all`, and `audit_excludes_all` close that.

**Three product defects, all reproduced.** A credential read out of a user's config file
was echoed to Telegram *and* written to the audit trail, despite the request explicitly
saying not to: `redact_payload` matched key names and known vault values, and a secret
embedded in a free-text blob under an innocuous `summary` key is neither. Redaction now
also scans string *content* for assignment lines and provider token shapes, applied at both
sinks - `AuditLogger.append` for audit, and `format_task_message` (the one formatter every
channel renders) for replies. The key's name is deliberately preserved; only its value goes.

Worse: asked to scaffold a VS Code extension, the operator called a nonexistent
`filesystem.manage:list_directory`, received an error naming every valid operation
including `write_text_file`, stopped, and synthesized "The following files were created:"
for an empty workspace - completing the task. Two guards failed together. The backstop that
should have caught it vanished because `_postconditions_from_objective` matched exact
tokens against `task.objective`, which is the *classifier's paraphrase*: the user wrote
"Create the real files" (which yields a WORKSPACE_DIR obligation) and the paraphrase said
"creating package.json" (which yielded none). Postconditions are now derived from the
original message as well as the objective and unioned, so a paraphrase can add an
obligation but never drop one, and trigger words match ordinary inflections - generated
from the known trigger vocabulary rather than by stemming arbitrary input, since a false
positive here invents a gap nothing can close. Separately, a final answer that claims files
were written now requires matching evidence (a succeeded write operation, a write-capable
tool, or the same promoted metadata keys fulfillment already checks) or it re-enters the
existing bounded gap loop. Negated claims are exempt: "I could not create the files" is the
honesty the guard exists to encourage, and flagging it would penalize exactly that.

No special-case retry was added for the unsupported-operation error itself. The valid
operation list already reaches `operator_history` via `result.error_message` - the operator
had the information and lacked only a consequence for ignoring it, which the claimed-write
rule now supplies. A bespoke branch would have been the prompt-specific special-casing this
repo's guidance warns against.

Third: a preference stated in chat ("always answer summaries with three bullet points…")
drew "Understood, all future summaries will be…" and persisted **nothing** - zero audit
events, zero memory facts - so the next task ignored it. Saying it learned while learning
nothing is worse than declining, because the user gets no signal. The chat route now
recognizes standing-instruction phrasings, persists them as `USER_STATED` memory facts
before replying, and appends what was actually stored to the acknowledgment. Since both the
chat and task paths already build `memory_context(remembered_facts=...)`, a rule stated in
chat reaches the next task's operator context with no further wiring - covered end to end by
a test, not just at the write.

A new `MEMORY_UPDATED` audit event carries this, with its `audit_view` category, title, and
`_summary()` branch added at the same time rather than left to fall through to "system" -
the same omission the previous review pass found in `CHANNEL_ACCESS_DECISION`.

**Two of the eight failures were the tests' fault, and are recorded as such.** EVO-3
asserted the approval gate; YBM used the clarifying route instead, resumed the same task on
"No. Do not make that change.", and never wrote - both are legitimate "ask before touching
disk", so the assertion was widened while the safety property (`tools_none`) stayed. EVO-8
asserted a failed read; YBM inspected the folder first and reported the file missing
honestly, which is the better strategy.

**Also fixed:** a settings validation error rendered `input_value=` for every field, and
`ybm doctor` formats that exception straight into output users paste into bug reports -
where, since settings come from `.env`, those values are the OpenAI key, Telegram bot token,
admin token, and vault key. `load_settings` now raises `ConfigValidationError` naming only
the field path and error type. It is raised *outside* the `except` block deliberately:
`raise … from None` sets only `__suppress_context__`, which hides the original from a printed
traceback while leaving it reachable on `__context__`, where any reporter that walks the
chain still finds the secret.
