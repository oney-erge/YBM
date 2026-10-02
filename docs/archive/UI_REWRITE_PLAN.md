# UI Rewrite Plan - React + FastAPI

> **Archive, not reference.** Status: **built, cut over, and bug-hunted** - all six phases
> shipped and Streamlit removed 2026-08-01. Kept for the design reasoning behind the console.
> Open items stay tracked where scoped: D2/D6/A1–A3 in §9–14, a committed Playwright suite in
> §15.1. Full narrative in [HISTORY.md](HISTORY.md) Part 5.

Replaces the single-page Streamlit console (1,776 lines, ~25 render functions on one page) with a
React single-page app served by the existing FastAPI backend.

**Design goal, stated once and enforced throughout:** *simple enough that a first-time user
never opens a menu, deep enough that an advanced user can debug, inspect, and change anything
without leaving the app.* Section 6 makes that testable rather than aspirational.

---

## 1. Competitive analysis - what we are actually up against

| Project | Scale | What it is | Better than us at | Gap it leaves |
|---|---|---|---|---|
| **Open WebUI** | ~136k★ | Default Ollama front end | Fastest setup, best pure-chat UX, huge community | Chat UI, not an agent with machine access. No capability policy, no approvals. |
| **LibreChat** | large | Polished ChatGPT clone | Multi-provider, SSO/OAuth, artifacts, token tracking, MCP, code interpreter | Team chat product. No OS-level tool governance. |
| **AnythingLLM** | large | RAG/workspace-first | Best document chat; per-workspace vector config | Not an actuating agent. |
| **OpenHands** | ~70k★ | SWE agent platform | Agent Canvas, built-in VS Code, delegation, **RBAC + audit trails**, event-stream arch, K8s | Scoped to software engineering in a sandbox. Not a personal, whole-machine assistant. |
| **OpenClaw** | very large | Multi-channel agent gateway | Channels, community, distribution | Governance is not the product. |

**Two honest conclusions:**

1. **We cannot win on chat UX.** Open WebUI and LibreChat are years ahead with far more
   contributors. Do not try to out-chat them.
2. **"Audit trail" is not a differentiator** - OpenHands already advertises audit trails *and*
   RBAC *and* delegation. Claiming it as unique is the kind of overclaim a knowledgeable reader
   instantly discounts.

**Where the real gap is:** every product above either runs the agent *in a sandbox* (OpenHands)
or *doesn't actuate at all* (the rest). YBM is a personal agent with real access to a real
machine. In that category the **governance and inspection surface is the product**, and nobody
is building that UI well.

---

## 2. Differentiation strategy

Grounded in 2026 human-in-the-loop research, which is blunt that naive approval UX is the failure
mode: *"Human-in-the-Loop Is Not a Button."* The guidance is to replace "Approve?" with a decision
surface carrying **intent, data lineage, permissions chain, expected blast radius, and rollback
plan**, compressed into an **"Evidence Pack" supporting sub-15-second decisions**. That is nearly
a specification for a UI YBM's backend can already populate and no competitor here ships.

| # | Differentiator | Backend status |
|---|---|---|
| **D1** | **Evidence-Pack approvals** - what/why/exactly-what/blast-radius/reversibility/authority/expiry | Data exists; `approval_reasons` shipped |
| **D2** | **Bounded approval** - one-shot *or* time-boxed, exactly scoped | One-shot tokens exist; time-box needs a small addition |
| **D3** | **Native local observability** - Langfuse-class trace, zero extra infra, zero egress | `build_task_trace()` already returns it |
| **D4** | **OpenTelemetry export** (opt-in) so users can pipe to their own Langfuse/Phoenix | New, small |
| **D5** | **Cost/token per task** with `by_source` split (operator/auditor/sub-agent) | Already computed |
| **D6** | **Deterministic replay** - re-run a task against recorded LLM responses | Scenario-fixture machinery exists |

**D3 deserves emphasis.** The 2026 observability stack is Langfuse or Arize Phoenix - both
excellent, both a *separate service to deploy*, both a data-egress decision. `build_task_trace()`
already returns `task`, `context`, `operator_history`, `timeline`, `tool_invocations`, `evidence`,
`approvals`, `artifacts`, `signals`, `audit`. That is a Langfuse-class agent trace, **already
implemented, entirely local, no extra infrastructure** - currently near-invisible because
Streamlit renders it as stacked expanders. Making it first-class converts existing backend work
into the headline feature.

> **What we do not claim:** better chat than Open WebUI/LibreChat, a bigger tool ecosystem than
> OpenClaw, or that audit trails are unique. Overclaiming against projects with 10–100× the
> contributors is the fastest way to lose the audience we want.

---

## 3. Decision, and its honest cost

**Decision:** rewrite the console as a React SPA. Retire Streamlit.

**The cost is real.** Comparable dashboard work is ~1–3 days in Streamlit vs 2–6 weeks for a
custom React front end, and Streamlit 1.60 already ships `st.navigation`/`st.Page`, so the
multi-page restructure alone was ~2 days in place. React buys control and a materially better
product surface, and costs weeks. That trade is the premise of this document.

**What makes it far smaller than a typical rewrite:** the backend does not change. The admin API
is complete - **24 JSON endpoints** under `/admin/api/*` (plus the `/admin` HTML route the SPA
replaces). Streamlit holds **no business logic**; it is a pure client. This is a **client swap**.

**Also gained:** one fewer service and port (`:8501` retires, `ybm start` drops a process), and
`streamlit` leaves the dependency tree.

---

## 4. Architecture - serving and auth

**The load-bearing section. Getting this wrong weakens the security model that is the product.**

`admin.py`'s `require_admin()` enforces two independent controls:

1. **Same-origin** - `_origin_is_trusted()` rejects any request whose `Origin` ≠ `Host`. There is
   deliberately **no `CORSMiddleware`**; the code says so explicitly, so no
   `Access-Control-Allow-Origin` is emitted and a malicious page's JS cannot read admin responses.
2. **Token** - `X-Agent-Control-Admin-Token` header (or `?token=`) vs `AGENT_ADMIN_TOKEN`.

### The constraint

A Vite dev server on `:5173` calling `:8765` is cross-origin and **refused by design**.

| Option | Verdict |
|---|---|
| Add permissive `CORSMiddleware` | ❌ **Never.** Deletes control #1, which exists to stop a malicious local page driving the agent. |
| Make the browser see one origin | ✅ Correct in dev and prod. |

**Production** - mount built assets on the existing app, replacing the `/admin` pointer route:

```
GET /admin           -> index.html (SPA shell)
GET /admin/assets/*  -> hashed JS/CSS
GET /admin/api/*     -> unchanged existing router
```

Same scheme/host/port ⇒ `Origin` matches `Host` ⇒ same-origin check passes untouched, **no CORS
relaxation anywhere**. Strictly better than today's two-origin Streamlit setup.

**Development** - Vite proxy:

```ts
server: { proxy: { "/admin/api": { target: "http://127.0.0.1:8765", changeOrigin: true } } }
```

`changeOrigin: true` rewrites the forwarded `Origin`. **Verify against `_origin_is_trusted()` in
Phase 0.1** - the highest-risk assumption here, proven before any UI is built.

**Token handling:** header only; **never** in a browser URL (lands in history/logs - `?token=`
stays for curl); kept in memory, not `localStorage`, to limit XSS blast radius.

---

## 5. Stack and packages

Every choice below is justified, and the notable rejections are recorded - a dependency you
didn't add is a dependency you never have to patch.

| Concern | Choice | Why this one |
|---|---|---|
| Build | **Vite + React + TypeScript** | Repo already has Node 22 + TS tooling for `vscode-extension/`. |
| UI kit | **shadcn/ui + Tailwind + Radix** | Components are *copied into the repo*, not a pinned dependency - no upgrade treadmill, full control. **shadcn CLI v4 (Mar 2026) ships `shadcn/skills` context packs and native AI-agent integration**, so coding agents scaffold components correctly. Directly relevant: this repo is built with AI assistance *and* runs coding agents. |
| Server state | **TanStack Query** (+ Devtools) | ~90% of this console is polled server state. Caching, `refetchInterval`, dedup, stale/error states free. Replaces Streamlit's 3s whole-page rerun with per-query intervals. |
| Tables | **TanStack Table** | Headless (~3M weekly downloads); **TanStack Table + shadcn/ui is the most common 2026 pairing**, and shadcn ships a production data-table recipe. Needed for tasks, audit, capabilities. |
| Graph / node UI | **React Flow (xyflow) v12** | The trace is a *tree*, not a list (see §7). Purpose-built, MIT core, actively maintained (v12, Jul 2026). |
| Routing | **React Router** | Four pages + deep links into traces. |
| Validation | **Zod** | Parse `/admin/api/*` at the boundary; the API returns untyped Python dicts. |
| JSON inspection | **`@uiw/react-json-view`** | ~20KB gzipped, actively maintained, optional editing. Preferred over legacy `react-json-view` (~50KB). |
| Code / prompt editing | **CodeMirror 6** via `@uiw/react-codemirror` | ~50kB tree-shakeable; used by Firefox DevTools and Replit. **Rejected Monaco**: 2–5MB for the full VS Code engine is disproportionate for a local console that must stay light and work offline. |
| Charts | **Recharts**, only if needed | For the cost panel. Do not add until a real chart exists. |
| Mocking | **MSW** | Develop/test UI states without a backend or LLM (§12.2). |

**Deliberately rejected:** Monaco (bundle size), MUI/Ant/Chakra (heavy opinionated themes when
shadcn gives control), Redux/Zustand (TanStack Query owns server state; local UI state is small
enough for `useState`/context - add a store only when a real need appears), and any charting
library before there is a chart.

**Location:** `frontend/` at repo root, sibling to `backend/` and `vscode-extension/`.

---

## 6. The simple/advanced problem - made testable

The central ask: *simple and nice for normal use, a real admin/debug console for advanced users.*
These pull against each other, so the plan adopts a measurable standard rather than a vibe.

**Nielsen's 2026 "workbench test": a user should complete ~80% of their top tasks without opening
any secondary drawer or menu.** Supporting research: deferring advanced features produced 30–50%
faster initial task completion and ~40% fewer support tickets.

### The rule this project adopts

> **Every screen has one obvious primary action visible with zero clicks. Everything else lives
> behind exactly one clearly-labelled disclosure - never two.**

Two levels, never three. If something needs a third level, the information architecture is wrong.

| Surface | Level 1 (always visible) | Level 2 (one disclosure) |
|---|---|---|
| Chat | Message box, transcript, starter prompts | Per-message: token cost, task id, "open trace" |
| Approval | Why · What · Exactly what · Approve/Deny | Full parameter JSON, policy decision path, raw audit |
| Task | Status, objective, answer, duration | Full trace graph, tool I/O, raw audit, replay |
| Access | Capability on/off toggles | Risk ceilings, scopes, allow/deny patterns, active grants |
| Settings | Model picker, "Test connection" | Every adapter field, prompt overrides, OTel export |

### The "Advanced" affordance

A single global **Advanced mode** toggle, persisted per user, that reveals Level 2 by default
everywhere rather than requiring per-panel expansion. Simple users never touch it; power users
flip it once and get a dense admin console permanently. This is one switch, not a per-screen
maze - and it is why the answer to "simple *or* powerful" is "both, sequenced."

**Acceptance criterion for the whole rewrite:** a new user can send a message, read the answer,
and approve one action **without opening a single disclosure**. If that fails, Level 1 is wrong.

---

## 7. React Flow - where a node UI genuinely belongs (and where it does not)

The user asked whether a React Flow settings/workflow surface makes sense. The honest answer is
**yes for one thing, no for another**, and the distinction matters.

### ✅ Use it: the trace graph - because the trace is genuinely a tree

This is a real, verified need, not decoration. Two shipped features made execution non-linear:

- **`call_tools_parallel` (T1.1)** - N tool calls run concurrently via `asyncio.gather`.
- **`delegate` (T1.2)** - a step spawns a bounded inner operator loop (`DELEGATE_MAX_STEPS = 6`)
  with its own history.

A linear timeline **cannot** express "these three ran at once" or "this step spawned a sub-agent
that made four calls of its own." React Flow can, and that is exactly what it is for.

**Backend gap, verified in the schema, now closed.** `tool_invocations` is:

```sql
id, task_id, tool_name, capability, request_json, result_json, status, created_at, completed_at
```

There was **no parent/step correlation column**. `_run_delegate()` called the executor with the
*parent's* `task_id`, so a sub-agent's tool calls landed in the same flat list, indistinguishable
from the parent's own - and a parallel batch was likewise indistinguishable from three sequential
calls. The data was all captured; the structure was lost on write.

> **Done - turned out simpler than the migration originally scoped here.** `request_json` already
> serializes the *entire* `ToolCallRequest` generically (confirmed: most existing fields -
> `risk_level`, `scope_target`, `idempotency_key` - already have no dedicated column, they ride
> inside this blob). So `ToolCallRequest` gained two plain fields, `origin: str = "operator"` and
> `parent_step_id: str | None = None`, with **no migration and no repository change** - they flow
> into `request_json` automatically, exactly like every other field already does.
> `_run_parallel_calls` generates one `parallel_batch:<id>` per batch and tags every call in it
> (including a nested batch inside a delegate, which composes to
> `subagent:<id>/parallel_batch:<id>`); `_run_delegate` generates one `subagent:<id>` and tags
> both its own tool calls *and* the summary entry it returns to the parent's `operator_history`,
> so the parent-visible "delegate" step and its children share the same id and a trace UI can
> nest them without prefix-parsing heuristics. Verified end to end (not just unit-level) by two
> new tests in `test_worker_parallel_and_delegate.py` that assert the *same* origin string
> appears in both `operator_history` and the persisted `tool_invocations` rows for a batch and for
> a delegated sub-task.

### ✅ Use it: a read-only live pipeline diagram

`Concierge → Operator loop → Auditor`, with the current task's position highlighted live. Small,
honest, and genuinely educational - it teaches the architecture in one glance and makes the
approval gate's position obvious.

### ❌ Do not use it: a drag-and-drop "agent workflow builder"

This is the trap, and it is worth being explicit about. The pipeline is **not data-driven**:
Concierge, Operator, and Auditor are hardcoded classes; `OperatorConfig` contains exactly one
field (`max_steps`). A node editor implying you can rewire or add pipeline stages would be **a UI
that lies about the system** - the single fastest way to lose a technical user's trust. Build it
only if and when the backend actually becomes a configurable graph, which is a large change
nobody has asked for yet.

---

## 8. Agent settings - evaluation and a realistic roadmap

The user asked about supporting different agents in future. Here is the honest current state and
a staged path, ordered by backend cost.

**Today:** three fixed roles - **Concierge** (classify intake), **Operator** (the decide loop),
**Auditor** (verify `done` is grounded). Prompts are markdown files under `prompts/base/`
(system) and `prompts/tasks/` (user). Model selection is *global* (`default_profile`,
`major_profile`, `fallback_profile`), not per-role. `delegate_tools` already lets a sub-agent be
restricted to a tool subset, and skills are user-droppable markdown.

| Stage | What | Backend cost | Value |
|---|---|---|---|
| **A1. Per-role model** | Concierge on a cheap model, Operator on a strong one, Auditor on a cheap one | **Small** - profiles already exist; add optional per-role profile keys | **High.** Immediate cost win; classification and audit do not need the strong model. Extends the existing `prefer_major` tiering philosophy. |
| **A2. Per-role prompt override** | Edit any agent's system prompt from Settings; stored as an override in `.agent_control/`, falling back to the shipped file | **Small–medium** - prompts are already files | **High for advanced users**, and a strong differentiator: most tools hide their prompts. Ship with a "reset to default" and a visible diff. |
| **A3. Named delegate presets** | Save `{name, objective template, delegate_tools}` - e.g. *researcher* (browser + knowledge), *coder* (code.interpreter) - selectable by the Operator | **Medium** - builds directly on existing `delegate_tools` | **High.** This is the realistic "different agents" feature, and it needs no pipeline change. |
| **A4. Pluggable pipeline / agent registry** | Add or reorder pipeline stages | **Large** - the pipeline must become data-driven | **Deferred.** No demand yet; this is where a node *editor* would eventually belong. |

**Recommendation: ship A1 + A2, design A3, defer A4.** A1 and A2 land inside the Settings page in
this plan (Phase 5). A3 gets a Settings section once designed. A4 is explicitly out of scope, and
§7 explains why building its UI early would be dishonest.

**Prompt editing needs a guardrail.** Prompt text is load-bearing: changing it invalidates
recorded scenario fixtures (they are keyed on exact prompt text). The editor must warn about
that, and A2 must never silently break the deterministic test tier.

---

## 9–14. Phases 0–5 - backend readiness through Settings - ✅ all done

All six build phases shipped; the day-by-day build log (what was found, what broke, what was
verified at each step) lives in `docs/archive/HISTORY.md` Part 5, not duplicated here. This section keeps
only what a future reader needs that isn't obvious from the code:

- **Phase 0 (backend readiness):** the Vite dev proxy needed an explicit `Origin` rewrite to pass
  `_origin_is_trusted()` (`changeOrigin` alone only rewrites `Host`). Trace correlation
  (`origin`/`parent_step_id` on `ToolCallRequest`) needed no DB migration - it already flows
  through `tool_invocations.request_json` generically.
- **Phase 1 (Shell + Chat):** Chat is the landing route on purpose (§10's original "first-time
  user must get an answer without visiting another screen" still holds).
- **Phase 2 (Approvals):** the Evidence Pack ships in full (Why → What → Exactly what → Blast
  radius → Reversibility → Authority → expiry). **D2 (time-boxed grants) not built** - it's the
  one change in this whole plan that makes the system *more* permissive, and needs real new
  backend machinery (scope-widening tests, capped TTL, executor-side enforcement) that deserves
  its own pass, not a rushed add-on. No "Approve for 10 min" button exists anywhere, not even
  disabled.
- **Phase 3 (Tasks + Trace):** Level 1 renders `operator_history` (not the originally-planned raw
  `timeline` field - real data showed `operator_history` is already the right shape, with
  `origin`/`parallel` built in). Level 2 is a React Flow **lane graph** grouped by `origin`
  (main sequence / parallel batch / delegated sub-task) - a disclosed simplification, not a full
  precision tree; it does not draw an edge from a lane back to the exact step that spawned it.
  **D6 (replay) not built** - always flagged cuttable. TanStack Table + React Flow pushed the main
  bundle over Vite's 500kB warning; fixed with route-level code-splitting so Chat's own load
  stays light.
- **Phase 4 (Access):** needed zero new backend endpoints. Presets (Read-only / Approval required
  / Full autonomy) are client-composed, not a backend concept - each walks a mode-preference list
  against every group's own `options` array (not every group supports every mode). Confirm
  dialogs are gated on the one transition that actually removes a human checkpoint (selecting
  Full access). Per-capability risk ceilings/scopes/allow-deny patterns are shown **read-only** -
  there's no write endpoint for those individual fields, only the coarse access-mode groups.
- **Phase 5 (Settings + wizard):** found and fixed a real, unrelated **security bug** while
  wiring MCP status: `config.py`'s `safe_summary()` returned MCP server `env` values (commonly
  API tokens) completely unredacted, unlike every other secret it already masked. Fixed to return
  `env_keys` only. The first-run wizard reuses existing config endpoints (zero new backend
  surface); skipping every step never writes `config.yaml`, so the wizard reappears next load -
  arguably correct (nothing was configured) rather than a bug.
- **Not built anywhere (A1–A3, D4):** per-role model selection, per-role prompt overrides,
  delegate presets, and an OpenTelemetry export toggle are all genuinely new backend machinery
  (§8's own cost table flagged this) - deferred for the same "don't rush it" reasoning as D2/D6.

---

## 15. Developer tooling & debugging

Deliberately substantial: this project's thesis is inspectability, and the dev loop should reflect
it.

### 15.1 Playwright - installed and proven useful; no formal harness yet

`@playwright/test` is a `frontend/` dev dependency (the Python extra pointed at the now-deleted
Streamlit admin was dropped at cutover, §19). It has been used once, ad hoc (not as committed spec
files) to screenshot every page of the real running console end to end - and found two real,
shipped bugs a curl/API-contract-only verification pass could never have caught: `BrowserRouter`'s
`basename` carried Vite's trailing slash, so the exact URL every banner/README prints
(`http://.../admin`, no trailing slash) made the router refuse to render anything at all; and
several pages' `flex h-full flex-col ... overflow-y-auto` containers let their children shrink
below content size (flexbox's default `flex-shrink: 1`) instead of scrolling, silently collapsing
the least "greedy" cards (Access's Kill switch/Presets) to a couple of pixels tall whenever a
page's content exceeded the viewport. Both fixed; see `docs/archive/HISTORY.md` for the full account.
**What's still open** is a committed, CI-running spec suite - the list below is unchanged from the
original plan:

- **Trace viewer on by default in CI and on retry locally:** `trace: "retain-on-failure"`,
  `video: "retain-on-failure"`, `screenshot: "only-on-failure"`. The bundle carries **DOM
  snapshots, network calls, and console output** - DOM snapshots being the widely underused part:
  you can select elements inside a past moment of a run and see exactly what the browser saw. For
  an approval-gated agent UI that is the difference between "the button didn't work" and a root
  cause.
- **ARIA snapshots over CSS selectors** - assert against the accessibility tree, not brittle class
  names. More stable, and it forces the console to be accessible.
- **Upload traces as CI artifacts** so a failed run is debuggable without local reproduction.
- **Use the Playwright CLI, not the MCP server, for agent-assisted work** - Microsoft's own 2026
  guidance is that the CLI uses ~4× fewer tokens per session. Directly relevant: YBM runs coding
  agents.

### 15.2 Frontend dev loop

- **TanStack Query Devtools** - every query, its state, refetch timing. Highest-value devtool here
  given the console is almost entirely polled server state.
- **MSW** - develop and test UI states (empty, loading, error, pending-approval, failed-task,
  mid-delegation) without a backend or a real LLM. Reuse the recorded API responses from 15.4.
- **Error boundary + toast on every mutation.** Streamlit's failure mode was a silent rerun; the
  React app must never fail silently.
- **shadcn `skills` context packs** - keep them current so coding agents working in this repo
  scaffold components correctly instead of inventing markup.

### 15.3 Backend-side debuggability (keep and extend)

- `ybm trace <task_id>` already prints a full post-mortem from the DB with no running backend -
  keep it; it shares `build_task_trace()` with the API so the two cannot drift.
- `ybm logs <service> -f`, `ybm doctor`, `ybm status` - unchanged.
- Add `ybm ui-dev` / `ybm ui-build` so the frontend is reachable from the same CLI as everything
  else.

### 15.4 Contract tests - the drift guard

The API returns untyped Python dicts; the SPA parses with Zod. A backend shape change would
otherwise surface as `undefined` in the UI. Record real `/admin/api/*` responses as fixtures and
parse them against the Zod schemas in CI - the same philosophy as the backend's recorded scenario
fixtures, applied to the API boundary.

### 15.5 CI

Add a `frontend` job (Node 22, matching the existing `vscode-extension` job): `npm ci`, `lint`,
`test` (Vitest), `build`, `npm audit --audit-level=high`, plus Playwright E2E with trace artifact
upload.

---

## 16. Testing

| Layer | Tool | Scope |
|---|---|---|
| Unit | Vitest + Testing Library | Status mapping, expiry/countdown, blast-radius derivation, graph-tree construction, form validation |
| Contract | Vitest + Zod | Schemas vs. recorded real API responses (15.4) |
| E2E | Playwright | Chat → task → answer; approval → Evidence Pack → approve → resume; capability toggle persists; wizard completes; **workbench test** - first answer + first approval with zero disclosures opened |
| Backend | existing pytest | Add: static mount, SPA fallback, `/admin/api/bootstrap`, trace correlation field, time-boxed grant scope-widening attempts |

---

## 17. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Vite proxy fails `_origin_is_trusted()` | **High** | Proven in 0.1 before UI work; documented fallbacks; never blanket CORS |
| Time-boxed grants (D2) widen the security surface | **High** | Bind to exact tool+operation; short max TTL; visible + revocable in Access; backend tests for scope-widening |
| Trace graph without 0.6 correlation data | **High** | Do not build the graph until the field exists. A subtly-wrong tree is worse than an honest list |
| Scope creep turns weeks into months | **High** | Phases 1–2 are the value. Ship Chat + Approvals before polishing anything |
| React Flow tempts a fake workflow builder | Medium | §7 is explicit: read-only diagram + trace graph only, until the pipeline is genuinely data-driven |
| Prompt overrides (A2) silently break scenario fixtures | Medium | Fixtures key on exact prompt text - warn in-editor, and cover with a test |
| Long parity gap where neither UI is good | Medium | Keep Streamlit working until Phase 6; never delete ahead of parity |
| Built assets not shipped to pip users | Medium | Phase 0.5; CI asserts the bundle exists |
| Token handling regression | Medium | Header-only; never query string; never `localStorage`; Playwright assertion |

---

## 18. Effort

| Phase | Estimate |
|---|---|
| 0 - Backend readiness (incl. trace correlation) | ~1.5 days |
| 1 - Shell + Chat | ~3 days |
| 2 - Evidence-Pack approvals | ~3 days |
| 3 - Tasks + Trace (timeline + graph) | ~4.5 days |
| 4 - Access | ~2 days |
| 5 - Settings + agents + wizard | ~4 days |
| 6 - Cutover | ~1 day |
| Dev tooling (§15, spread across phases) | ~2 days |
| **Total** | **~21 working days** |

**Minimum shippable slice: Phases 0 + 1 + 2 (~7.5 days)** - a React console that can chat and
approve, i.e. the two things that define the product, while Streamlit serves everything else.

**Highest value per day: Phase 3 (Trace).** It converts already-written backend capability into
the feature most likely to make a technical user choose this project.

---

## 19. Phase 6 - Cutover (~1 day) - ✅ done, 2026-08-01

Streamlit (`admin_streamlit.py`, 1,776 lines) and its test file deleted; the `admin_ui` service
removed from both supervisors (`agent_control.supervisor` and `scripts/ybm.ps1`) along with the
now-pointless `-NoAdminUi` flag; `streamlit` and the `playwright` (Python) test extra dropped from
`pyproject.toml`; every "Admin UI" URL across supervisors/docs/onboarding repointed at
`http://127.0.0.1:8765/admin`. Full mechanics and the parity-check process (a real audit against
every Streamlit `_render_*` function, not just this section's own checklist - it found four
undisclosed gaps, closed before deleting anything) are in `docs/archive/HISTORY.md` Part 5.

---

## 20. Open questions

- **Dark mode** - implemented after parity with semantic light/dark tokens and a persisted system-aware toggle; see `docs/archive/UI_UX_AUDIT.md`.
- **Mobile/responsive** - implemented after parity with a mobile header, bottom navigation, responsive page widths, and overflow checks at 390px.
- **SSE vs polling** - deferred by design (0.4); decide from real usage.
- **Storybook** - likely overkill at this size; MSW + Vitest should cover it. Revisit if the
  component count grows.
- **A3 delegate presets** - needs a design pass before implementation (§8).
- **Bundled vs CDN fonts/icons** - bundle. The console must work offline and air-gapped.

---

## 21. References

- [Open WebUI vs LibreChat vs AnythingLLM](https://www.local-llm.net/compare/open-webui-vs-librechat-vs-anythingllm/) ·
  [OpenHands platform](https://ai-infrastructure.net/openhands-platform/)
- [Human-in-the-Loop Is Not a Button](https://digitalthoughtdisruption.com/2026/07/12/human-in-the-loop-ai-agent-approval-paths/) ·
  [HITL approval framework pattern](https://www.agentic-patterns.com/patterns/human-in-loop-approval-framework/)
- [Top agent observability tools 2026](https://mlflow.org/top-5-agent-observability-tools/) ·
  [Best LLM tracing tools 2026](https://www.braintrust.dev/articles/best-llm-tracing-tools-2026)
- [Progressive disclosure - UXPin](https://www.uxpin.com/studio/blog/what-is-progressive-disclosure/) ·
  [Progressive disclosure - IxDF](https://ixdf.org/literature/topics/progressive-disclosure)
- [React Flow / xyflow](https://reactflow.dev/) ·
  [React Flow guide 2026](https://velt.dev/blog/react-flow-guide-advanced-node-based-ui)
- [Best React UI component libraries 2026](https://hashbyt.com/blog/best-react-ui-component-libraries) ·
  [Best React table libraries 2026](https://www.pkgpulse.com/guides/best-react-table-libraries-2026)
- [Monaco vs CodeMirror 6 vs Sandpack](https://www.pkgpulse.com/guides/monaco-editor-vs-codemirror-6-vs-sandpack-in-browser-2026) ·
  [Best JSON editor libraries for React 2026](https://www.merge-json-files.com/blog/best-json-editor-for-react)
- [Playwright end-to-end story - Microsoft](https://developer.microsoft.com/blog/the-complete-playwright-end-to-end-story-tools-ai-and-real-world-workflows/) ·
  [Playwright AI ecosystem 2026](https://testdino.com/blog/playwright-ai-ecosystem)
