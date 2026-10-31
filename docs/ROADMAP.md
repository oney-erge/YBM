# Roadmap

What is built, what is next, what is deliberately not on the list, and the limits that exist today.

The spine every item serves:

**Plan -> Approve -> Execute -> Verify -> Receipt**

And the three jobs that have to be excellent:

1. Safely organise and change files on your actual computer.
2. Do browser and desktop work, asking before anything consequential.
3. Run a long multi-step task and prove afterwards exactly what happened.

## Built

- **Ask once, not thirty times.** Organising 128 files used to ask 128 times, because the only batching
  primitive failed any call that needed approval. A parallel batch is now pre-flighted through the
  policy engine and the user gets one approval listing every call; each call still has its own approval
  bound to its exact request, so authority does not widen. How it works:
  [ARCHITECTURE.md](ARCHITECTURE.md#decisions-decide-can-return).
- **Continuity.** If the process dies or the machine reboots, a task resumes instead of being lost; an
  ambiguous in-flight write is never silently retried or skipped, it asks. How it works:
  [ARCHITECTURE.md](ARCHITECTURE.md#task-states).
- **A first run with nothing to enter.** The launcher creates the config and admin token, picks a model
  from what is already on the machine, signs the console in, and offers to grant a folder; access,
  folders, and the model apply to the next task without a restart.
- **Editable projects.** Save folders, instructions, corrections, and criteria; open a project-specific
  chat and history. New tasks capture that context without granting file access. Existing tasks and
  schedules keep their original context when a project changes.
- **Recurring saved workflows.** Schedule a recorded workflow with parameters, timezone, and optional
  project context. Inspect the next run and last receipt, pause, resume, or delete it. Approval waits
  cannot pile up overlapping runs or block unrelated schedules.
- **Document evidence exports.** Compare literal passages across supported documents into XLSX and
  Markdown, with source fingerprints, extraction limits, collision-safe filenames, and output checks.
  This is an evidence collection tool, not a semantic ranking engine.
- **Folder-triggered workflows.** Bind a saved workflow's input-file parameter to an inbox. New or
  changed files settle before they run, one at a time. Observations survive restarts; task creation
  and event acknowledgement share a transaction. Normal approvals apply, and registered artifact
  outputs are excluded to prevent feedback loops. Read access is checked on every poll. See
  [USING.md](USING.md#projects-and-recurring-work) for the top-level polling limits.

## Next: features that make YBM useful every week

These are proposed features, not implemented capabilities. Prioritize a complete user outcome and
measure repeat use after a real task. The differentiator to validate is a local work system that keeps
project instructions, produces reviewable deliverables, and repeats successful work with receipts.

| Priority | User-visible feature | Completion condition |
|---|---|---|
| 1 | **Whole-batch inbox reports:** drop several related files and get one combined result. | Add reviewed multi-file bindings, version snapshots, exclusion rules, and recursive watching. Current inbox triggers persist and deduplicate observed changes, wait for stable files, recover after restart, and show the triggering file, but process files separately rather than combining a batch. |
| 2 | **Decision-ready comparisons:** an evidence-backed recommendation using the user's rubric. | Extract typed facts with source page/section citations; label missing and conflicting evidence; let the user set weights; recompute scores deterministically; export a readable report. Evaluate against reviewed documents, including misleading numbers and scanned files. Current keyword tables do not meet this bar. |
| 3 | **Reviewed project memory:** a correction improves the next relevant task. | Offer a proposed correction with provenance, ask before retaining it, retrieve it only in its project, and support edit/delete. Measure repeat-error reduction. Manual instructions and task history exist; automatic retrieval and outcome learning do not. |
| 4 | **One complete connected-work flow:** import selected work documents, prepare a deliverable, and draft its handoff. | Choose one ecosystem based on pilot users; implement OAuth, least-privilege scopes, secure token storage, refresh/revocation, account selection, and reviewed sends. Existing MCP/HTTP primitives are not a finished connector experience. |
| 5 | **Explicit browser/file handoff:** act on exactly what the user selected. | Show the selected tab or file, capture its identity and context, detect stale selections, and confirm destinations before consequential actions. Generic browser control is not this workflow. |
| 6 | **Recoverable file work:** inspect changes and restore a mistaken operation. | Record a change manifest and recoverable originals, preview the restore, detect files modified since the run, and verify restoration. A receipt alone is not undo. |

Ship each vertical slice with a short guided example and a real-stack integration test. Track time to
first usable artifact, verified completion, corrections per task, repeat workflow use, and retained
weekly users. Do not use tool count or a scripted demo as evidence that users will prefer YBM.

## Next: proof

Two concrete things, both about not having to take the agent's word:

- **Screenshots as evidence.** Capture the screen before and after a consequential browser or desktop
  action and attach it to the receipt. Today that work leaves no visual record at all, which is most of
  job 2.
- **Check the goal, do not infer it.** `fulfillment.py` infers success from the wording of the request.
  An approved batch already states what it intends to do, so check that instead: "you asked for the
  folder sorted; 34 files are now under Documents; confirmed."

Smaller items that are real and unfinished:

- A WhatsApp card in Settings (QR pairing and the allowlist), so it no longer needs a config edit.
- A human clean-machine pass of the installers on Windows, macOS, and Linux.

## Not doing

- **An embedding stack by default.** Start project retrieval with explicit corrections and provenance.
  Add semantic retrieval only when measured recall failures justify its dependency and resource cost.
- **Multi-agent runtimes** (gateways, swarms, agent-to-agent). Feature competition; none of it makes the
  three jobs better. `delegate` stays a bounded inner loop.
- **A plugin marketplace.** If extensibility becomes a priority, the differentiated answer is the
  existing `adapter.factory`: "I do not know how to operate your application. I generated a connector.
  Here is exactly what it will access. Tests pass. Install it?" A hosted registry is a network-effects
  game and a supply-chain surface.
- **Keyword-triggered plans.** Plans and batches are surfaced by the model's judgment, never by matching
  on words like "plan". `config.py` records that the previous plan path "and its keyword-driven recovery
  were deleted, not just defaulted off"; rebuilding it would repeat a mistake this repository already
  made once.

## Known limits

The current list of product and engineering limitations. Completed plans and review snapshots live in
Git history and [archive/](archive/), not here.

### Security boundaries

- **Indirect prompt injection remains possible.** Tool results, web pages, documents, and MCP output are
  untrusted. Runtime capability policy, allowlists, workspace boundaries, and one-shot approvals limit
  impact. Operations that return content this machine does not control are labelled
  (`ToolDefinition.operation_content_trust`), and the Operator prompt wraps that content in an explicit
  `[UNTRUSTED CONTENT ...]` fence, neutralised first so a payload cannot forge a fake boundary. This is a
  textual mitigation, not a guarantee: the deterministic suite covers the fence's mechanics, but only a
  live model can show whether it declines a given injection. See the [threat model](THREAT_MODEL.md).
- **Redaction is pattern-based.** Known secret fields, configured secret values, and provider-token
  shapes are redacted. A novel high-entropy secret with no recognizable name or prefix may pass through.
  Entropy scanning is off because it would also hide hashes, UUIDs, and generated identifiers.
- **YBM is single-operator software.** It is not a hardened multi-tenant or Internet-facing control
  plane. Keep the backend and preview servers on loopback and use an admin token when the bind host is
  broader.

### Behavior still needing live validation

- The project/comparison/automation flow has a browser test using the real API, SQLite, worker,
  approvals, and file adapter with scripted model decisions. It does not demonstrate that a live model
  selects the right tools or follows a user's comparison rubric. Live model and desktop/Office checks
  remain necessary before advertising that reliability.

- The Auditor checks grounding and objective completion, but does not reliably challenge an implausible
  value such as a zero total for a non-empty expense file. Improving it needs a reviewed live fixture
  re-record.
- The starter suggestions have deterministic UI and worker coverage, but the wording has not been
  exercised against a configured live model.
- Voice failure paths and transcription are tested with simulated audio. A real microphone recording and
  Telegram voice note have not been transcribed end to end in the release environment.
- WhatsApp's sidecar imports and health are checked, but live QR pairing and send/receive need a real
  account.
- The credentialed live E2E suite (28 Telegram-driven cases under `e2e/`) is separate from deterministic
  CI. It is not run in CI and no recent pass rate is recorded, so the deterministic scenario tier is the
  trustworthy signal meanwhile.

### Product limitations

- Desktop observation/control and computer-use actions are Windows-only.
- WhatsApp is text-only (no buttons, voice, or artifact delivery) and uses an unofficial client.
- GitHub Copilot Chat panel responses cannot be captured through the VS Code API; the bridge and the
  CLI-based coding-agent flow are supported.
- The Windows PowerShell supervisor and the cross-platform `ybm` supervisor are separate
  implementations.
- The task worker and the Telegram and WhatsApp intake follow `config.yaml` and `.env` while they run,
  except that rotating a channel's own token needs a restart.
- The launchers (`run.*`, `scripts/ybm.ps1`) and the tray icon assume the default port 8765 when they
  probe or open the console. The backend, `ybm start`, and `ybm doctor` honour `server.port`, but if you
  change it, open the console with `ybm admin-url` rather than relying on the launcher's readiness check.
- Receipts include the Concierge's call once a task exists. A classification for a message that never
  becomes a task, and conversation-memory summary calls, have no task to attach to and are not recorded.
- Status requests cost two LLM calls. The old LLM-free keyword shortcut was deliberately not rebuilt: it
  was brittle and silently misroutable.
- Installing from a source checkout needs Node.js 22.22 or newer to build the console, because the built
  console is not committed; with less, the launcher continues with the JSON API only. Release installs
  ship it prebuilt.

### Maintainability

- `orchestration/worker.py`, `admin.py`, and the frontend API client remain large coordination modules.
  Shared path/text helpers have been extracted, but decomposing them should be incremental and
  contract-tested rather than a release-blocking rewrite.

### Release validation

- CI installs the MSI on a clean Windows runner, provisions the packaged runtime, checks backend health,
  stops it, and uninstalls it, and the launchers are smoke-tested on Windows, macOS, and Linux runners.
  The final visual interaction with the MSI dialogs and first-run browser wizard still needs a human
  clean-machine pass before onboarding is called stable.
