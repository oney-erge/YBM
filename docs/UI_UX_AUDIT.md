# UI/UX Audit and Product Roadmap

> **Archive + roadmap, not reference.** Audited 2026-08-01 against the live console, React
> source, admin API, and config schema. **Roadmap items are not implemented unless marked
> shipped.** For current behavior see [ARCHITECTURE.md](ARCHITECTURE.md) and
> [CAPABILITIES.md](CAPABILITIES.md).

> Historical implementation record: current limitations and release priorities live in
> [GAPS.md](GAPS.md) and [ROADMAP.md](ROADMAP.md). Statements below such as
> “nothing is currently open” describe the audit at that time, not the current release state.

## Outcome

YBM's strongest product idea is not generic chat. It is governed local agency: powerful tools,
runtime-owned approvals, and a trace that proves what happened. The console already exposes all
three, but the previous presentation made the product look like an unstyled settings utility.
The redesign establishes a clearer control-plane hierarchy:

1. **Chat** is the simple front door.
2. **Approvals** interrupt every route only when a decision is required.
3. **Tasks and traces** explain what ran and where it failed.
4. **Access** communicates safety posture before individual configuration.
5. **Settings** holds integrations and advanced operator controls.

## Where We Are

Phases 0-16 are done - Phase 16 shipped in two halves: the channel-adapter interface, then a
real second channel (WhatsApp). Phases 3 and 7 are blocked on the repository owner, and nothing
is currently open. Full write-ups of shipped phases were removed on 2026-08-01 to keep this
document about what is *left*; the detail lives in git history (`git log --grep "Phase N"`) and
in the code comments each change left behind.

### Shipped

| # | What landed | Deliberately not done |
|---|---|---|
| 0 | Stabilized the baseline: fixed a mislabeled Evidence Pack field and an absolute README secrets claim, re-recorded broken fixtures, web chat resumes a clarifying task instead of spawning a new one | - |
| 1 | Chat: sanitized Markdown, Stop button, inline clarifications, artifact cards, inline approvals backed by real task-scoped grants, file attachments | Folder selection (shipped separately as Phase 13) |
| 2 | Task Receipts: a "Done" card in Chat plus a full receipt (result, what was touched, services contacted, approvals, duration/cost), with export | - |
| 4 | Structured memory: `MemoryFact` schema with provenance/confidence/category, a `memory_facts` table, admin CRUD, a Memory page, and a `memory.manage` tool stamped `task_derived` | Retrieval, contradiction handling (now Phase 15) |
| 5 | Skills lifecycle: `version`/`tools` manifest fields, install/uninstall endpoints, a Skills page, inferred tool references | Version pinning and integrity verification - no registry exists to pin against |
| 6 | Packaging: tray icon, `ybm autostart`, `ybm backup`, `ybm check-updates` | A compiled `.msi`/`.exe` - no build toolchain present |
| 8 | P0 correctness and honesty: cancellation cleanup, receipt wording that stops over-claiming, receipts for every terminal state, artifact download, skill-label wording. Second pass: a pending approval no longer blocks the single worker from later tasks | Per-item effect classification (now Phase 14) |
| 9 | Console surfaces over data that already existed: approval window rebuilt (pager, sticky actions, keyboard), Tasks outcome column, clear-history, a Timeline tab, Diagnostics rebuilt with service cards and a doctor runner | Per-task delete (bulk clear only) |
| 10 | One command to run it: `ybm run` + double-clickable `YBM.bat`, consistent logo across console/tray/favicon. Second pass: fingerprinted dependency sync and console build - a fully-warm launch went from ~60-90s to ~9s | Consolidating the CLI's several per-command Python process launches (the remaining gap to "a few seconds") |
| 11 | Console regrouped: a Tools page, an Agent hub over Tools/Skills/Memory, and a bundled starter skill catalog | MCP server add/edit/test, an `adapter.factory` review UI, skill edit-in-place |
| 12 | Console UX pass: a chat width control, expired approvals swept out of the list instead of leading it, breadcrumbs + fixed nav active-state on every sub-page, one entry point for adding a skill, an explicit disabled-tool affordance on Tools | - |
| 13 | Server-side folder picker: `GET /admin/api/folders`, scoped to the same `computer_use.allowed_roots` `filesystem.manage` uses, with a `FolderPicker` dialog in the Chat composer | - |
| 14 | Timeline gained real categories, colors, and durations; Steps and the Graph gained real per-step/per-node durations; a new Duration/Gantt tab showing tool calls, approval waits (rendered as an outline), and LLM-call latency on one time axis, with any uncovered gap honestly labeled "inferred"; real per-item effect classification for receipts and evidence; `llm_calls` persistence (`task_id`, `source`, `model`, `step_index`, messages, response, tokens, latency), redacted and size-capped, wired into `ybm db clean`/`db reset`; a stable `step_id` stamped through operator history, `ToolCallRequest.parent_step_id`, approvals, and LLM calls, surviving an approval or background-external wait; and Graph v2 - rooted at the task's query, one node per real step (LLM decision + tool call(s) + approval gate grouped together), with duration/token badges and a click-to-inspect dialog showing the exact prompt, response, and tool input/output | Connecting a parallel/subagent lane in the graph back to the exact step that spawned it - step_id doesn't nest, only `origin` does |
| 15 | Deterministic relevance selection (`score_facts`: keyword/entity overlap with the objective, category relevance, recency; facts with no `task_id` always included as durable global preferences) replacing "inject every fact into every task, uncapped"; a real `user_stated` route via `detect_remember_request` ("remember that ..." / "don't forget that ..."), detected at the runtime level before any LLM sees the message, wired into Telegram's plain-text command layer; `memory.manage`'s `forget` operation gated behind approval (medium risk floor) while `list`/`remember` stay free, via the same `operation_risks`/`approval_required_operations` mechanism `schedule.manage` uses | The fifth scoring signal (current folder/service context) - no caller has that signal available today; the same "remember that ..." interception for the web-chat intake path, which has no equivalent pre-classifier hook yet |
| 16 | Channel-adapter interface (first half): the "classify -> task" stage extracted from `TelegramIntakeService` into channel-agnostic functions (`channels/base.py`'s `classify_and_spawn_task`, `resume_clarifying_reply`, `status_summary`, later joined by `approve_latest_pending`) behind a `ChannelAdapter` Protocol and shared `ChannelUpdateResult` type. Second half: WhatsApp as a real second channel, via a Node.js Baileys sidecar (`whatsapp-bridge/`) the backend spawns and polls over loopback HTTP - `WhatsAppAdapter`/`WhatsAppIntakeService`/`WhatsAppTaskNotifier`, `task_chat_id` generalized to `channel_chat_id(task, channel)`, and `format_task_message` extracted to `channels/task_notify.py` and shared with Telegram. Disabled by default, no real phone number ships in the repo | Plain-text only in v1 - no `/command` syntax, inline buttons, voice transcription, or artifact/screenshot delivery over WhatsApp (all Telegram-only for now); live QR-pairing and live send/receive were not performed in this session - no phone number was available, see the Phase 16 write-up below |

### Blocked on the repository owner

| # | What it needs |
|---|---|
| 3 | Connections (GitHub, Google). Registering an OAuth App and a Google Cloud OAuth client requires the owner's own accounts. |
| 7 | Public launch: website, hosting, public security docs, first version tag. Public content and hosting are the owner's decisions. |

### Open

Nothing open. Phases 3 and 7 are blocked on the repository owner (see above); everything else is
shipped.

## Current Feature Coverage

| Area | Implemented today | Console coverage | Important gap |
|---|---|---|---|
| Agent runtime | Concierge, bounded Operator loop, Auditor, fulfillment checks, parallel calls, delegation | Task status, step list, timeline, advanced lane graph | The graph cannot connect a delegated/parallel lane to the exact spawning decision (Phase 14) |
| Channels | Telegram text/voice, WhatsApp text (Baileys, disabled by default), and one local web conversation | Web chat plus Telegram settings; WhatsApp is config-only, no admin UI settings page yet | Web chat has no voice, multiple threads, edit/regenerate, or message queue; WhatsApp has no buttons, voice, or artifact delivery |
| Safety | Capability policies, risk ceilings, allowlists, one-shot approvals, task-scoped grants, disabled-by-default tools | Persistent approval banner, Evidence Pack, Access posture and modes | Fine-grained scopes/patterns are read-only; no grant revocation list |
| Tooling | Filesystem, terminal, browser, computer use, workspace, coding agents, documents, schedules, MCP, HTTP, knowledge, persona, skills, memory | Tools page, Agent hub, Access groups, diagnostics | Tool availability and permission live on separate pages; no combined capability-health matrix |
| Observability | Structured audit, task trace, timeline, receipts, evidence, token usage, cost data, CLI trace | Tasks with outcomes, trace list/graph/timeline, audit viewer | No per-step duration, latency trend, failure-rate view, evaluation signal, or cross-task cost dashboard |
| Configuration | LLM profiles/presets, Telegram, WhatsApp, VS Code, workspace, computer use, MCP summary | Settings forms and advanced mode | No per-role model, prompt overrides, delegate presets, or editable advanced policies; MCP is read-only; WhatsApp is `config.yaml`-only, no admin UI form yet |
| Secrets | Encrypted local vault and reference-based injection | List/add/delete/init without returning values | Rotation/last-used state and integration validation are absent |
| Operations | Setup, doctor, `ybm run`, tray, autostart, backup, update check, logs, scheduler, supervised services | Health indicator, Diagnostics with service cards and a doctor runner | Two supervisor implementations remain; no compiled installer |
| Developer quality | Backend unit/scenario suite (761 tests), Zod API parsing, frontend typecheck/build | Query devtools in development | No committed frontend unit, contract, accessibility, or Playwright regression suite |

## Competitive Review

The goal is not to imitate a general chat product. The useful patterns are:

- [Open WebUI's official feature set](https://docs.openwebui.com/features/chat-conversations/)
  establishes the interaction baseline for self-hosted chat: attachments, message queueing,
  conversation organization, and tools that stay inside the conversation. YBM should adopt the
  message ergonomics, not its multi-user breadth.
- [OpenHands' security model](https://docs.openhands.dev/sdk/guides/security) pairs explicit
  confirmation policies with risk analysis. YBM's runtime gates are already a strong match; the
  Access page should keep making the difference between "can run," "needs review," and "cannot
  run" visible before an action occurs.
- [Langfuse's agent graphs](https://langfuse.com/docs/observability/features/agent-graphs) offer
  aggregated and expanded views, while its
  [trace guidance](https://langfuse.com/docs/observability/best-practices) emphasizes correct
  nesting, cost, and token metadata. YBM has enough correlation data for a useful lane graph, but
  needs exact parent edges and an expandable tree to become a first-class debugger.
- [AnythingLLM's official documentation](https://docs.anythingllm.com/) combines workspaces,
  agents, model routing, and explicit flows. YBM should add named delegate presets and per-role
  models first. It should not show a drag-and-drop workflow builder until the backend pipeline is
  genuinely configurable.

## Gap Analysis by Priority

Shipped entries were removed on 2026-08-01; what remains is still open. Items now scheduled as a
numbered phase say so.

### P0 - Trust and regression safety

1. Add a committed Playwright suite for token entry, chat wrapping, theme persistence, mobile
   navigation, approval review, access-mode changes, and a failed-task trace.
2. Add frontend unit/contract tests for status mapping, API schemas, access preset computation,
   and secret masking. Today TypeScript and production build are the only automated UI gates.
   **Still the single largest quality gap** - every UI bug this pass was caught by hand.
3. Add a React error boundary. A render-time component failure can still blank the current route.
4. Audit every external adapter's exception path for credential-bearing URLs or command text.
   Telegram is fixed and admin responses redact configured values, but prevention should happen
   at every adapter boundary. Related: only `http.request` calls `record_egress`, so browser, MCP,
   coding-agent, and Telegram traffic is invisible to receipts.

### P1 - Daily-use product quality

1. Add real task pagination and server-side search/filtering. The API exposes offsets, but the UI
   currently fetches and filters only the first 100 tasks.
2. Add a trace tree beside the graph: collapsed by default, failure opened automatically, exact
   parent/child edges, and duration per step. **Phase 14.**
3. Add a capability-health matrix combining configured access, adapter readiness, policy ceiling,
   and last failure. Access answers permission; Diagnostics answers availability; operators need
   both in one view.
4. Make advanced capability scopes and allow/deny patterns editable through validated backend
   endpoints with before/after confirmation.

### P2 - Differentiating control-plane features

1. ~~Per-role models for Concierge, Operator, and Auditor~~ - shipped (`concierge_profile`/
   `operator_profile`/`auditor_profile`, plus an ordered `fallback_chain` with per-profile
   cooldown). Estimated cost/latency impact per role is not shown anywhere yet.
2. Versioned prompt overrides with diff, reset, and an explicit scenario-fixture warning.
3. Named delegate presets such as Researcher or Coder, restricted to selected tools.
4. ~~Time-boxed grants with capped TTL, visible expiry, and revocation~~ - shipped (Access page's
   Active Grants card: path/target scope inherited from the approved call, `max_operations` cap,
   live list across tasks, one-click revoke). Still tool+capability-wide, not per-operation within
   one tool (e.g. a grant covering `filesystem.manage` covers every operation on it, not just the
   one that was approved).
5. ~~Cross-task reliability and cost dashboards~~ - shipped (Insights page, `GET /api/dashboard`):
   completed/verified-completed/failed rates, retry count, token spend, per-tool failure rates with
   a least-reliable-tool callout, and per-model/per-task-type breakdowns, over a 7- or 30-day
   window. Not built: per-call tool latency (the receipt/trace views have per-step duration; this
   dashboard doesn't aggregate it yet) and approval waiting time.
6. SSE for task and approval events after measuring current polling load; keep polling fallback.

### P3 - Expansion only after evidence

1. Multiple local chat threads, search, archive, and export.
2. ~~Re-run/replay from a trace with a clear statement of what can cause side effects again~~ -
   shipped (trace page's Replay button, `POST /api/tasks/{id}/replay`): reissues a completed
   task's own succeeded tool calls through the same approval/retry/verification pipeline, so a
   step that needed approval the first time asks for it again rather than carrying authority over
   silently. Delegated and parallel-batch steps are not yet replayable.
3. Configurable workflow graphs only after the runtime becomes data-driven.
4. Multi-user/RBAC only if the product moves beyond its current trusted-local-operator boundary.

## Delivery Plan and Acceptance Criteria

Phases 0-11 shipped; the ledger under "Where We Are" above summarizes them. Their full write-ups
were removed on 2026-08-01 to keep this section about what is left - the detail is in git history
(`git log --grep "Phase N"`) and in the code comments each change left behind.

Two findings from earlier reviews are repeated here because open phases depend on them:

- `ToolCallRequest.parent_step_id` exists but is always `null` - the exact field needed to close
  the gap `TraceGraph`'s own docstring discloses (a delegated or parallel lane cannot be linked to
  the step that spawned it). Phase 14.
- LLM prompts are **not persisted anywhere**; `render_prompt()` builds them per call. This is the
  only genuinely new backend capability any open phase needs. Phase 14.

### Phase 12 - Console UX pass (**shipped**)

Four unrelated daily irritations, grouped because they were all small, all in the console, and all
verified in the code before being written down.

- **Chat width.** `ChatPage` hardcoded `max-w-3xl` in three places. `lib/chat-width.ts` (same
  read/write-`localStorage` shape as `advanced-mode.ts`, plain state instead of a Context since
  nothing outside `ChatPage` needs it) adds a three-way comfortable/wide/full toggle in the header.
- **Expired approvals.** `ApprovalRepository.list_pending()` now calls a new `expire_stale()` first
  (sweeps `PENDING` rows past `expires_at` to `EXPIRED` in one indexed `UPDATE`) and orders the
  remainder by `expires_at ASC` instead of `created_at ASC`. An expired approval no longer appears
  at all, let alone leads the list. Verified live against two real approvals from earlier in this
  session that were still sitting `PENDING` a full day past expiry - both swept to `EXPIRED` on the
  next list call, confirmed by querying the database directly, not just trusting the API response.
- **Navigation.** A new `PageBreadcrumb` component (wrapping the `Breadcrumb` primitive that
  existed and was used by nothing) now appears on Memory, Skills, Tools, and a task's trace.
  `AppShell`'s active-state matching gained a `matchPaths` list per nav item instead of relying on
  `NavLink`'s own prefix match, so "Agent" now highlights correctly on `/memory`, `/skills`, and
  `/tools` - the exact gap Phase 11 recorded and left open.
- **"Add a skill."** Collapsed "Browse catalog" and "Install a skill" (two buttons, two stacked
  panels, plus a third route mentioned only in the empty state) into one `[+ Add a skill]` button
  opening one panel with two tabs (`Tabs`/`TabsList`/`TabsContent`, unused elsewhere in the app
  until now). `SkillInstallForm` gained a `bare` prop so it can drop its own `Card` wrapper when
  nested inside the tab panel's. Tools' read-only-by-design stance is now stated on the page itself,
  and a disabled tool gets an explicit warning-toned "Disabled - enable `<capability>` in Access"
  link instead of the same subdued link an enabled tool gets.
- Acceptance: chat uses the width the operator chose; the approvals list contains only items that
  can actually be decided; every sub-page can be left without the browser back button; and there is
  exactly one obvious way to add a skill.

### Phase 13 - Server-side folder picker (**shipped**)

- `GET /admin/api/folders[?path=...]`: no `path` returns the configured
  `computer_use.allowed_roots` (the exact same boundary `filesystem.manage` itself is scoped to,
  not a second one to keep in sync); a `path` inside one of them returns its immediate
  subdirectories, validated against the roots on every call - never a traversal outside them, and
  never dependent on the browser's own directory-upload as a stand-in. A `FolderPicker` dialog in
  the Chat composer browses this and inserts the selected absolute path into the draft text -
  there is no new "folder reference" concept, it's the same plain path-in-the-objective
  `filesystem.manage`'s alias resolution already understands.
- Verified live against this machine's real configured roots (`C:\projects`, `C:\Users\<you>`):
  listing roots, browsing into one, and rejecting a path outside them (`C:\Windows`, 400) all
  behaved correctly against the running backend, not just in tests.
- Acceptance: "organize this folder" is expressible from the console without typing a path.

### Phase 16 - A second channel

**Channel-adapter interface (shipped, first half).** The "classify -> task" half of intake ->
classify -> task -> notify never actually depended on anything Telegram-specific - it was just
inlined as private methods on `TelegramIntakeService`. It's now `channels/base.py`:
`classify_and_spawn_task`, `resume_clarifying_reply`, and `status_summary` are plain functions
over `InboundMessage`/`Repositories`, plus a `ChannelAdapter` Protocol and shared
`ChannelUpdateResult` type `TelegramAdapter` now formally implements. `ChatResponder`/
`LLMChatResponder`/`StaticChatResponder` (`channels/responder.py`) lost their misleading
Telegram-flavored names - the implementations were already channel-agnostic, only the names
weren't. Verified both by the full existing Telegram test suite staying green (the extraction
moved code, it didn't rewrite behavior) and by new tests calling `classify_and_spawn_task`
directly with `ChannelType.DISCORD`/`SLACK` inbound messages, confirming the audit trail, task
metadata, and outbound replies are genuinely channel-derived, not secretly still Telegram-shaped.

**WhatsApp channel (shipped, second half).** A real second consumer of `channels/base.py`,
validating the seam the first half only guessed at. Uses
[Baileys](https://github.com/WhiskeySockets/Baileys) - an unofficial WhatsApp Web client,
QR-code device linking, no Meta developer account or public webhook - the same architecture
OpenClaw (MIT, self-hosted, ~25 chat platforms through one local gateway) ships as
production-ready; confirmed by checking their actual implementation rather than assuming the
official Business Cloud API was viable (it needs a public HTTPS webhook this local-only product
has no infrastructure for). Runs as a Node.js sidecar (`whatsapp-bridge/`, plain JS, no build
step) that `cli.py poll-whatsapp` spawns and owns as a child process
(`channels/whatsapp_bridge_process.py`) for its whole lifetime, so `ybm.ps1`'s service list never
had to learn a new, non-Python process type - Node stayed an internal implementation detail of
one more Python entry point. Python and Node talk over loopback HTTP with a secret generated
fresh per run, never persisted.

Being a genuine second consumer justified three more extractions the first half deliberately
deferred: `task_chat_id()` generalized to `channel_chat_id(task, channel)` (schemas.py, both
callers updated), `approve_latest_pending` moved from a `TelegramIntakeService` private method
into `channels/base.py`, and `format_task_message` (the pure `TaskRecord.metadata` -> text
formatting, no Telegram API calls in it) extracted from `telegram_notifications.py` into
`channels/task_notify.py`, now shared by `TelegramTaskNotifier` and the new
`WhatsAppTaskNotifier`. Also surfaced and fixed a real, pre-existing bug this work would otherwise
have made worse: `MESSAGE_RECEIVED`/`MESSAGE_SENT` audit events were already channel-generic by
their own enum semantics but displayed as Telegram-only everywhere (`storage/audit_view.py`);
now genuinely channel-generic, with a new `AuditEventType.CHANNEL_ACCESS_DECISION` added
alongside (not reusing) Telegram's own `TELEGRAM_ACCESS_DECISION`.

Deliberately v1/plain-text-only, matching Telegram's own plain-text command layer subset
(`approve`/`status`/"remember that ..."): no `/command` slash syntax, inline buttons, voice
transcription, or artifact/screenshot delivery over WhatsApp - all Telegram-only for now, the
same reasoning the first half already applied to `tools/artifact_delivery.py`. Disabled by
default (`channels.whatsapp.enabled: false`); the WhatsApp service entry is `Required $false` in
both `ybm.ps1` and `supervisor.py` (unlike Telegram's `$true`), specifically so a fresh,
unconfigured checkout doesn't hard-fail `ybm start`/`ybm run`. No real phone number was available
this session and none ships in the repo or config.example.yaml - live QR-pairing, linking an
account, and live send/receive were explicitly not performed; whoever runs this repo links their
own number (see `docs/LOCAL_SETUP.md`).

**What's left, disclosed not silent:** rich WhatsApp features (buttons, lists, media, read
receipts) and artifact/screenshot delivery over WhatsApp; a JS test runner for the sidecar (no JS
test tooling exists in this repo outside `frontend`/`vscode-extension`'s own toolchains); every
new channel is new untrusted input, and the taint-tracking idea in the research notes should land
before the channel count grows further.

## Explicit Non-goals

- No fake workflow builder over the current hardcoded agent pipeline.
- No generic enterprise RBAC for a local single-operator product.
- No color-only status communication; text and icons remain required.
- No UI control that implies a backend policy exists when it does not.
- No secret value display, including in errors, trace JSON, or historical records returned by the
  admin API.
