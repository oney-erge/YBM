import { mkdir, rm } from "node:fs/promises"
import path from "node:path"
import { test, type Page } from "@playwright/test"

/**
 * Records the frames for the README demo (docs/screenshots/demo.gif).
 *
 * Every response here is mocked, so recording costs nothing, needs no model,
 * and produces the same frames on any machine - the previous GIF was captured
 * against a live backend, which made it unreproducible and pinned the story to
 * whatever that session happened to do (it ended up being the same trivia
 * question asked several times, which sells this as a chat window rather than
 * an agent that works on your computer).
 *
 * Not part of `npm run test:e2e`: it writes files and only exists to be run
 * deliberately. Re-record with:
 *
 *   cd frontend && YBM_RECORD_DEMO=1 npx playwright test demo.spec.ts
 *   cd .. && backend/.venv/Scripts/python scripts/make_demo_gif.py
 */

const FRAME_DIR = path.resolve(process.cwd(), ".demo-frames")

type Phase = "empty" | "typed" | "scanning" | "approval" | "executing" | "done"

const TASK_ID = "task_downloads_sort"
const APPROVAL_ID = "appr_downloads_sort"
const CONVERSATION = "web"
const OBJECTIVE = "Organize my Downloads folder by type"
const DOWNLOADS = "C:\\Users\\sam\\Downloads"

const STARTED = "2026-08-11T09:12:04Z"
const FINISHED = "2026-08-11T09:12:41Z"

const FINAL_ANSWER = `Sorted 128 files in your Downloads folder into 6 folders:

- **Documents** - 41 files
- **Images** - 33 files
- **Installers** - 22 files
- **Archives** - 14 files
- **Video** - 11 files
- **Spreadsheets** - 7 files

Nothing was deleted. The 3 files I could not classify are still where they were.`

/** The chat task, as it looks at each point in the story. */
function chatTask(phase: Phase) {
  const base = {
    id: TASK_ID,
    objective: OBJECTIVE,
    conversation_id: CONVERSATION,
    created_at: STARTED,
    updated_at: STARTED,
    artifacts: [],
  }
  if (phase === "scanning") {
    return {
      ...base,
      status: "running",
      metadata: {
        last_tool_result: {
          output: { summary: `Scanned ${DOWNLOADS} - 128 files across 6 types.` },
        },
      },
    }
  }
  if (phase === "approval") {
    return { ...base, status: "awaiting_approval", metadata: {} }
  }
  if (phase === "executing") {
    return {
      ...base,
      status: "running",
      metadata: {
        last_tool_result: { output: { summary: "Moving 128 files into 6 folders..." } },
      },
    }
  }
  return {
    ...base,
    status: "completed",
    updated_at: FINISHED,
    metadata: { synthesized_answer: FINAL_ANSWER },
  }
}

/** A believable history for the Tasks page: real machine work, and range. */
function taskHistory() {
  const done = (id: string, objective: string, at: string) => ({
    id,
    objective,
    status: "completed",
    conversation_id: CONVERSATION,
    created_at: at,
    updated_at: at,
    metadata: {},
  })
  return [
    { ...chatTask("done"), artifacts: undefined },
    done("task_invoices", "Pull this week's invoices out of my attachments folder", "2026-08-11T08:40:11Z"),
    {
      id: "task_dupes",
      objective: "Find duplicate photos in my Pictures folder",
      status: "awaiting_approval",
      conversation_id: CONVERSATION,
      created_at: "2026-08-11T08:31:02Z",
      updated_at: "2026-08-11T08:31:40Z",
      metadata: {},
    },
    done("task_pdfs", "Summarize the quarterly PDFs on my desktop", "2026-08-11T08:02:57Z"),
    {
      id: "task_uptime",
      objective: "Check the staging site every morning and message me if it is down",
      status: "running",
      conversation_id: CONVERSATION,
      created_at: "2026-08-11T07:30:00Z",
      updated_at: "2026-08-11T07:30:12Z",
      metadata: {},
    },
    done("task_backup", "Back up the project folder to the external drive", "2026-08-10T21:15:33Z"),
  ]
}

function pendingApproval() {
  return {
    approval: {
      id: APPROVAL_ID,
      task_id: TASK_ID,
      capability: "filesystem.write",
      risk_level: "high",
      summary: `Move 128 files in ${DOWNLOADS} into 6 folders by type. Nothing is deleted or overwritten.`,
      action_payload: { tool_name: "filesystem.manage", operation: "apply_manifest" },
      status: "pending",
      expires_at: "2026-08-11T09:27:04Z",
      created_at: "2026-08-11T09:12:20Z",
    },
    task_objective: OBJECTIVE,
    task_status: "awaiting_approval",
    capability_max_risk_level: "high",
    blast_radius: {
      files: [`${DOWNLOADS}\\*`],
      urls: [],
      commands: [],
    },
  }
}

function receipt() {
  const moved = (name: string, into: string) => ({
    value: `${DOWNLOADS}\\${name} -> ${DOWNLOADS}\\${into}\\${name}`,
    tool_name: "filesystem.manage",
    at: FINISHED,
    effect: "moved" as const,
  })
  return {
    task_id: TASK_ID,
    objective: OBJECTIVE,
    status: "completed",
    result_summary: "128 files sorted into 6 folders. Nothing deleted.",
    changes: {
      files: [
        moved("q3-report.pdf", "Documents"),
        moved("statement-july.pdf", "Documents"),
        moved("screenshot-2026-08-02.png", "Images"),
        moved("node-v22.msi", "Installers"),
        moved("archive-2025.zip", "Archives"),
      ],
      urls: [],
      commands: [],
    },
    tools_used: [{ tool_name: "filesystem.manage", calls: 3, succeeded: 3, failed: 0 }],
    execution: { calls_attempted: 3, calls_succeeded: 3, calls_failed: 0 },
    verification: { checked: 128, verified: 128, missing: [], not_checked: 0, unverified_tools: [] },
    services_contacted: [],
    data_left_machine: false,
    llm_left_machine: false,
    approvals: [
      {
        id: APPROVAL_ID,
        capability: "filesystem.write",
        risk_level: "high",
        status: "approved",
        summary: "Move 128 files into 6 folders by type",
      },
    ],
    artifacts: [],
    token_usage: { calls: 7, total_tokens: 4318, last_model: "qwen3:8b" },
    duration_seconds: 37,
    uncertainties: [],
    created_at: STARTED,
    updated_at: FINISHED,
  }
}

/**
 * One route handler for the whole console. `phase` is read at request time, so
 * advancing the story is just reassigning it between screenshots.
 */
async function mockConsole(page: Page, getPhase: () => Phase) {
  await page.route("**/admin/api/**", async (route) => {
    const url = new URL(route.request().url())
    const p = url.pathname.replace(/^\/admin/, "")
    const phase = getPhase()

    if (p === "/api/bootstrap") {
      return route.fulfill({
        json: {
          token_required: false,
          onboarding_complete: true,
          llm_reachable: true,
          version: "0.1.0",
        },
      })
    }
    if (p === "/api/config/voice") {
      // Left off, which is the default - the composer should show the
      // microphone only where transcription can actually run.
      return route.fulfill({
        json: {
          enabled: false,
          provider: "faster_whisper",
          model: "base",
          installed: false,
          available: false,
          install_hint: "uv sync --extra voice",
        },
      })
    }
    if (p === "/api/chat/messages") {
      // Send and history share a path but not a response shape: the POST
      // returns the single task it just created, the GET returns the list.
      if (route.request().method() === "POST") {
        return route.fulfill({ json: { conversation_id: CONVERSATION, task: chatTask("scanning") } })
      }
      const tasks = phase === "empty" || phase === "typed" ? [] : [chatTask(phase)]
      return route.fulfill({ json: { conversation_id: CONVERSATION, tasks } })
    }
    if (p === "/api/approvals") {
      return route.fulfill({ json: { approvals: phase === "approval" ? [pendingApproval()] : [] } })
    }
    if (p.startsWith("/api/tasks/") && p.endsWith("/receipt")) {
      return route.fulfill({ json: receipt() })
    }
    if (p === "/api/tasks") {
      const tasks = taskHistory()
      return route.fulfill({
        json: {
          tasks,
          pagination: { limit: 100, offset: 0, total: tasks.length, has_more: false },
        },
      })
    }
    if (p === "/api/summary") {
      const tasks = taskHistory()
      return route.fulfill({
        json: {
          status: "ok",
          tasks,
          task_pagination: { total: tasks.length },
          vscode: {
            connected: false,
            status: "waiting",
            last_seen_at: null,
            last_seen_age_seconds: null,
            heartbeat: null,
            state: null,
            pending_terminal_commands: 0,
          },
          warnings: [],
          config: {
            llm: { default_profile: "qwen3:8b" },
            adapters: { workspace: { enabled: true, root_dir: ".agent_control/workspaces" } },
          },
          database: { database_url: "sqlite:///agent_control.db", path: "agent_control.db" },
          integrations: {
            telegram: { enabled: true, token_present: true },
            llm: { default_profile_configured: true },
          },
        },
      })
    }
    // Anything the demo does not stage is a bug in the demo, not something to
    // paper over with an empty 200 that renders as a broken panel.
    return route.fulfill({ status: 404, json: { detail: `demo has no mock for ${p}` } })
  })
}

test("record the README demo frames", async ({ page }) => {
  test.skip(process.env.YBM_RECORD_DEMO !== "1", "Set YBM_RECORD_DEMO=1 to re-record the README demo.")
  test.setTimeout(120_000)

  await rm(FRAME_DIR, { recursive: true, force: true })
  await mkdir(FRAME_DIR, { recursive: true })

  let frame = 0
  const shot = async (name: string) => {
    frame += 1
    await page.screenshot({ path: path.join(FRAME_DIR, `${String(frame).padStart(2, "0")}-${name}.png`) })
  }

  let phase: Phase = "empty"
  await mockConsole(page, () => phase)

  await page.setViewportSize({ width: 1280, height: 760 })
  // Both banners are one-time notices, not part of the product's steady
  // state; a demo that opens on them shows chrome instead of the thing.
  await page.addInitScript(() => {
    localStorage.setItem("ybm-safety-tour-dismissed", "true")
    localStorage.setItem("ybm.setup.dismissed", "1")
  })

  await page.goto("./")
  // The recorder drives the Vite dev server, so React Query's devtools button
  // is mounted and floats over the bottom-right corner. It belongs to the
  // development build, not to the product being demonstrated.
  await page.addStyleTag({
    content: ".tsqd-parent-container, [data-tsqd-parent-container] { display: none !important; }",
  })
  const composer = page.getByRole("textbox", { name: "Message YBM" })
  await composer.waitFor()
  await page.getByRole("heading", { name: "What can I help you do?" }).waitFor()
  // Fonts settle a beat after first paint; screenshotting into that reflow
  // produced a frame with the wrong metrics.
  await page.waitForTimeout(600)
  await shot("empty")

  // Typing, in two frames, so the request reads as something a person asked.
  await composer.fill("Organize my Downloads")
  await page.waitForTimeout(120)
  await shot("typing")
  await composer.fill(OBJECTIVE)
  await page.waitForTimeout(120)
  await shot("typed")

  phase = "scanning"
  await composer.press("Enter")
  await page.getByText("Scanned", { exact: false }).waitFor()
  await page.waitForTimeout(250)
  await shot("scanning")

  phase = "approval"
  await page.getByRole("button", { name: "Approve once" }).waitFor({ timeout: 15_000 })
  await page.waitForTimeout(250)
  await shot("approval")

  phase = "done"
  // The receipt is fetched only once the task settles, so wait for a heading
  // the card renders rather than sleeping and hoping.
  await page.getByText("What this task touched").waitFor({ timeout: 15_000 })
  await page.waitForTimeout(500)
  await shot("done")

  await page.getByRole("link", { name: "Tasks" }).click()
  // An objective in flight appears twice - once in "Active now", once in the
  // table below - so this has to say which one it is waiting for.
  await page.getByText("Check the staging site every morning").first().waitFor({ timeout: 15_000 })
  await page.waitForTimeout(500)
  await shot("tasks")
})
