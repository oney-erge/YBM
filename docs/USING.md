# Using YBM

What you see after it starts, what each page is for, and how to check that it is working. To get it
running in the first place, see [INSTALL.md](INSTALL.md).

## Your first ten minutes

1. **It starts and signs you in.** The launcher creates your config and admin token, picks a model from
   what is already on the machine (your own LocalDeploy, a running Ollama, or a provider key in your
   environment or `.env`; it never calls a paid API to choose), and opens the console already signed in.
   The model it chose is named in the launcher output and in the chat header.
2. **If no model was found,** a short wizard offers a local model or a cloud provider, verifies access,
   and makes one small completion before saving. Skipping it leaves chat unavailable until you configure
   a model under **Settings**.
3. **Choose folders.** File access is off on a fresh install. Chat offers a card with your Downloads,
   Documents, Desktop, and Pictures folders: pick the ones YBM may work in, choose **Read-only** or
   **Write with approval**, and it takes effect on the next task with no restart. It will not grant a
   whole drive, your home folder, or an operating-system folder.
4. **Try a starter.** Chat suggests three: "Organize my Downloads folder by type", "Summarize the PDFs on
   my desktop", and "Find the largest files filling up my disk". The first deliberately stops for an
   approval so you meet the approval gate straight away: it shows what it intends to move, and nothing
   moves until you approve.
5. **Read the receipt.** When a task finishes, the chat card lists what it touched, what ran, what left
   the machine, and what it is unsure of. **Tasks** keeps the full trace.

If a task needs something that is switched off, it says which switch to flip (for example "Turn on
Browser") and offers a button, instead of failing with a code.

## The console

| Page | What it is for |
|---|---|
| **Chat** | Talk to YBM. A question gets a direct reply; a request becomes a task shown inline with its approvals and receipt. Slash commands are not used here; plain `approve`, `status`, and `remember that ...` work. |
| **Tasks** | Every task, with filters and a detailed trace per task: each tool call, its result, approvals, the model calls and their cost, and timing. Completed tasks can be replayed or saved as a workflow. |
| **Insights** | How often work actually finishes *verified*, not just how often the model said it did: success rates by tool, which tools have no mechanical check yet, cost and duration. |
| **Access** | What YBM can observe, what needs your review, and what may run on its own, grouped into Off, Read-only, Write with approval, and Full access. Runtime approval gates still apply to critical operations. **Security review** (linked from here) shows what the machine is exposed to right now, from configuration and recorded activity rather than a live scan. |
| **Agent** | What YBM is made of. **Projects**: reusable folders, instructions, and success criteria. Selecting one in chat snapshots that context onto the task without granting folder access. **Memory**: durable facts it remembers, with where each came from; correct or delete any of them. **Skills**: instructions it reads when relevant (a runbook, a style guide); a skill cannot execute anything. **Tools**: the tool catalog and the capability each needs. **Workflows**: completed tasks saved as named, reusable plans; run or automate one on a cadence through the same approval and verification checks as any task. |
| **Settings** | The model (including a one-message test), per-role models, Telegram, MCP servers, computer use, voice, the VS Code bridge, the workspace, diagnostics, and the audit log. WhatsApp is configured in `config/config.yaml`, not here; see [INSTALL.md](INSTALL.md#link-whatsapp). |

The status strip in the header shows the model, Telegram, VS Code, workspace, and database at a glance.

## Projects and recurring work

In **Agent -> Projects**, create a project with folders, instructions, and success criteria. Use
**Edit** to record corrections for future tasks. **Open project chat and history** opens a separate
conversation; a reply to one project's clarification cannot resume another project's task. General
chat remains separate. Project questions go to the worker so it receives the project's context.

New tasks snapshot the project's instructions. Editing or deleting a project does not rewrite past
tasks or schedules. Folder paths are context, not permission: grant the required access in **Access**.
The history view is task history, not automatic retrieval or learning from all previous outcomes.
For project-specific corrections, edit the project's instructions; global Memory remains separate.

From a completed task's trace, choose **Save as workflow**. In **Agent -> Workflows**, choose
**Automate**, fill any parameters, select a project if desired, and set a cadence and timezone.
Supported cadences are `daily`, `weekly`, `every day`, `every week`, or `every N minutes/hours/days/weeks`
with N from 1 to 9999. Use an IANA timezone such as `America/Chicago` or `UTC`. The first run is one
interval from creation; a daily schedule keeps the local time across daylight-saving changes.

The Automations card shows the next due time, status, failure count, and a link to the last run and
receipt. **Pause** stops future runs; **Resume** restarts the trigger and clears the failure streak
(time-based schedules start a new interval from now); **Delete automation** removes the schedule from
this list. Already-created tasks retain their state. A run waiting for work or approval prevents
overlapping runs of the same schedule. Every run
still uses the normal policy, approvals, and verification pipeline. YBM must be running for scheduled
work to start. Schedules retain the workflow parameters and project context captured at creation;
recreate a schedule to use changed parameters or project instructions.

To process incoming files, first complete a task on an example file. In **Save as workflow**, turn that
file's exact input path from the task trace into a parameter such as `input_file`. In **Automate**,
choose **When a file changes**, enter the inbox folder, select that parameter, and choose extensions
and a settling delay. YBM fills this parameter with each changed file's path; other parameter values
and project instructions remain as saved. This repeats the recorded tool steps; it does not ask a
model to invent a new workflow for each file.

The watcher observes top-level regular files of 1 byte to 10 MB in an inbox with at most 500 entries.
Existing files form its baseline; new or modified files must remain unchanged across polls for the
settling delay. The scheduler's polling interval also affects response time (30 seconds by default).
Files run one at a time. Pending observations survive restarts and collect while a prior task waits
for approval. Repeated observations do not create duplicate runs. After a file has triggered a task,
rewrites with identical content are also deduplicated.
YBM artifact outputs, symlinks, hidden files, temporary downloads, and subfolders are excluded.

Folder observation requires currently permitted filesystem read access; adding a project folder does
not grant it. A revoked permission or scan error appears on the automation card and prevents a new
run until corrected. Every actual workflow tool call still uses its normal approval and access checks.
The chat shows which file triggered a task, and its trace records the observed file fingerprint.
The input remains a live path: editing it after task creation can change what the approved workflow
reads. Replay a failed task from its trace to retry that version's work; resuming a watcher handles
future changes and any pending files rather than automatically retrying failed tasks.

Polling coalesces changes to the latest observed state of each path. A file created and deleted between
polls, or transient versions overwritten before observation, cannot be recovered. Recursive watching,
multi-file batch reports, version snapshots, and explicit exclusion rules are still future work.

For document comparisons, provide two or more supported source files and explicit criteria, for
example: `Compare these two proposals by cost and timeline; create a spreadsheet and evidence report`.
The current tool extracts literal keyword matches into XLSX and Markdown. It does not evaluate which
proposal is best. Review the source locations, missing matches, and extraction-limit notices before
using the result. Supported formats and limits are in [CAPABILITIES.md](CAPABILITIES.md#document-comparison).

## Approvals

Anything consequential stops and asks, showing what it will do before it does it. An approval belongs to
one task, one tool, and one exact request; it expires, and it is used once. Changing the request or
replaying the approval fails closed. When a task needs several approvals for related changes it asks
once with the whole list. Approve in the console, or on Telegram with the inline buttons or by replying
`approve`. An approval nobody answers expires, and the task is marked blocked rather than waiting
forever.

Access modes control availability, never approvals: even **Full access** does not skip the approvals a
tool declares for risky operations such as installing an MCP server, promoting generated code, or running
generated Python. See [CAPABILITIES.md](CAPABILITIES.md) and the [threat model](THREAT_MODEL.md).

## Check that the whole chain works

This proves intake, classification, persistence, the worker, tool execution, the audit trail, and result
delivery are all connected. It needs a working model and nothing else.

```mermaid
flowchart LR
    M["message"] --> C["Concierge classifies"]
    C --> T["task persisted"]
    T --> W["worker picks it up"]
    W --> X["Operator calls a tool"]
    X --> R["result back to the channel"]
    R --> A["audit trail + trace"]
```

1. Start YBM and open the console (the launcher does both).
2. In **Settings**, press **Send a test message to the current model**. It must pass; all text is
   processed by it. General chat uses classification; project messages go directly to the task worker.
3. In **Chat**, ask `what can you do?`. It answers directly and creates no task.
4. Ask `what tasks are running right now?`. It creates a task that calls `task.status` and replies.
5. Confirm: **Tasks** shows it reaching `completed`; `ybm trace-task <task_id>` (Windows wrapper:
   `.\scripts\ybm.ps1 trace <task_id>`) lists the tool call and its result; **Settings -> Audit** shows
   the classification and task-created events.

For a task that produces files, grant a folder first (above) and try
`create a hello world web app and launch it`. Expect a localhost preview URL plus files under
`.agent_control/workspaces/task_<id>`. The Operator chooses its own tool sequence each run, so judge the
result, not the route.

### Telegram

Telegram adds real channel intake. You need a BotFather token and your Telegram user ID and chat ID.

1. Add `TELEGRAM_BOT_TOKEN=...` to `.env`, or paste the token in the **Telegram** card under Settings,
   which verifies it before saving.
2. Enable Telegram, add your user ID and chat ID to the allowlist, and save. An empty allowlist denies
   every message.
3. Restart YBM so the Telegram intake process starts.
4. Send `what tasks are running right now?` to your bot, then `/tasks`.
5. Confirm the reply comes back in the same chat and **Tasks** shows the task completing.

## If something stalls

Commands here use the `ybm` CLI; [INSTALL.md](INSTALL.md#runtime-interfaces) gives the exact path or
Windows wrapper command for your install.

| Symptom | Check |
|---|---|
| Message ignored | The allowlist. An empty one denies everything. |
| Chat says no model is configured | Settings, **Model**. Add a local endpoint or an API key. |
| Task stuck in `received` | Is the worker running? `ybm status` (Windows wrapper: `.\scripts\ybm.ps1 status`). |
| Task `awaiting_approval` | Approve it in the console, or on Telegram. |
| Tool "denied" or "blocked" | The capability is off. Follow the card in chat, or see Access and [CAPABILITIES.md](CAPABILITIES.md). |
| The console asks for a token | Run the launcher again (it opens a signed-in console), or `ybm admin-url` for a link, or copy `AGENT_ADMIN_TOKEN` from `.env`. |
| Anything else | `ybm trace-task <task_id>` and `ybm logs worker --follow`, then `ybm doctor`. |

`ybm doctor` checks the runtime, ports, model, and the capability configuration and says what to fix.
Stop everything with the command for how you started it: `run.bat stop` or `./run.sh stop` from a checkout,
`.\scripts\ybm.ps1 stop` in the install folder for a Windows installer copy (the MSI and `Install-YBM.bat`),
or `ybm stop` on macOS and Linux. The two Windows and cross-platform supervisors are separate, so
`ybm stop` does not see services the PowerShell wrapper started.
