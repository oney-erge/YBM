# Live Telegram E2E Tests

These tests send real Telegram user messages to the bot, wait for the backend
to create and finish tasks, query the admin trace, and write full case logs.

## Why Telethon Is Required

Telegram Bot API can't *create* an inbound user message to your bot. A true
Telegram E2E test needs a user session through MTProto. This harness uses
Telethon for that and still uses the backend admin API for trace evidence.

## Setup

`ybm setup` already installs the `e2e` extra (Telethon). Start the stack first:

```powershell
.\scripts\ybm.ps1 start
```

Set these environment variables (or put them in `.env` at the repo root):

```powershell
$env:TELEGRAM_API_ID="your Telegram API id"
$env:TELEGRAM_API_HASH="your Telegram API hash"
$env:TELEGRAM_BOT_USERNAME="@your_bot_username"
$env:TELEGRAM_USER_SESSION=".agent_control/telegram_e2e_user"
```

If admin auth is enabled:

```powershell
$env:AGENT_ADMIN_TOKEN="your admin token"
```

On first run, Telethon may ask for your phone number and Telegram login code.
That creates the local session file. You can also create the session
explicitly:

```powershell
.\scripts\ybm.ps1 e2e-login
```

After the session file exists, the runner can run unattended.

## Run

Run all non-guarded cases from the consolidated catalogue:

```powershell
.\scripts\ybm.ps1 e2e
```

Filter by case id:

```powershell
.\scripts\ybm.ps1 e2e --only browser_dizibox_new_shows,desktop_inspection
```

Filter by size (`small`, `medium`, `long-running`):

```powershell
.\scripts\ybm.ps1 e2e --sizes small,medium
```

Run the distinct autonomy suite (persistent recovery, approval resume,
long-running progress, capability gaps, and quota-aware external agents):

```powershell
.\scripts\ybm.ps1 e2e --suite autonomy
```

Run the `evolution` suite - the complementary question to autonomy's "can it
finish a hard job?": *does it stay trustworthy while doing so?* Covers learned
preferences, credential redaction, honoring a refusal, compound instructions,
admitting a capability gap, real scaffolding, scheduled continuation instead of
a retry loop, and refusing to invent a missing file's contents:

```powershell
.\scripts\ybm.ps1 e2e --suite evolution
```

Run the `gaps` suite - requests the other suites do not make, written to find what breaks rather than to
confirm what works: follow-ups that say only "that file" and "it", sorting 40 files and then undoing it, Word and
Excel files and a zip, one fact on page 47 of a 60-page PDF, a PDF that tells the assistant to delete files, a vague
"clean up" that must not become deleting, and a many-step report that has to keep the user informed in plain
language. Several are expected to fail today; each failure names a missing capability (see `gap_fixtures.py`
for the generated files):

```powershell
.\scripts\ybm.ps1 e2e --suite gaps
```

Run the two guarded, human-style coding-agent tests. These deliberately sound
like ordinary Telegram requests rather than test scripts, and verify the
Claude Code and Codex VS Code-extension files on disk (including command
wiring), not just the assistants' completion prose:

```powershell
.\scripts\ybm.ps1 e2e --suite human_autonomy --include-guarded
```

Include guarded cases (Claude Code / Codex / Copilot / external quota - usually need
credentials we don't have locally):

```powershell
.\scripts\ybm.ps1 e2e --include-guarded
```

## Case catalogue

`all_cases.json` is the single source of truth. Each case declares its message,
required fixtures, expected behavior, pass criteria, and any follow-up turns.
Add new cases there; the runner picks them up automatically.

Assertions a case can declare, beyond the positive ones (`tools_all`,
`metadata_any`, `*_min`, `bot_reply_contains_any`):

- `tools_none` - these tools must **not** have been invoked. Use for safety
  cases where the point is that nothing happened (a denied approval must leave
  no write behind).
- `bot_reply_excludes_all` - none of these strings may appear in the reply or
  in what Telegram actually received. Use for credential canaries and for
  overclaims like "installed successfully".
- `audit_excludes_all` - none of these strings may appear anywhere in the
  task's audit events. A secret redacted from the reply but persisted into an
  event payload is still a leak.
- `metadata_equals` - dotted metadata keys must equal the declared values;
  useful for proving the requested external provider actually ran.
- `files_exist_all` - every named file must be present among the tool-reported
  changed files and must still exist on disk.
- `file_contains_all` - maps a produced file to required content fragments,
  so a scaffold is checked for coherent command wiring rather than file count
  alone.

A case may also set `expects_task: false` (on the case or on a single
follow-up) for the **chat route**, where the correct behavior is a direct reply
and no persisted task. Those turns report `final_status: "chat"` and are judged
purely on what the channel sent back.

`fixtures.py` builds the Desktop folders, mock documents, and optional local
static-file web server that the case templates reference (e.g.
`{{documents_folder}}`, `{{episode_url}}`).

When a selected case requires `fake_mcp_server`, the runner temporarily adds
the local echo server to YBM's MCP config and catalog, restarts the services,
and performs preflight again. At the end of the run it restores the exact
original config and catalog bytes and restarts once more, including when a
test fails. The local MCP case is therefore part of the default catalogue and
does not require a manually configured external service.

## Logs

Every run writes:

```text
.agent_control/e2e_results/run_<timestamp>/
    summary.md           # at-a-glance pass/fail table
    summary.json         # machine-readable
    <NN>_<case_id>/
        result.json      # full structured result
        timeline.txt     # human-readable status flow + plan + answer
        audit.json       # every audit event for the task
        decision_trace.json # structured decisions, actions, and outcomes
        diagnosis.md     # only present for failed stages - explains why
```

The summary is rewritten after every stage, so a partial run is still useful.
`decision_trace.json` contains observable structured decision fields and tool
outcomes for diagnosis; it does not contain private chain-of-thought.
