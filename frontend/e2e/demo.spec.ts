import { mkdir, rm, writeFile } from "node:fs/promises"
import path from "node:path"
import { test, type Page } from "@playwright/test"

/**
 * Records the README demo (docs/screenshots/demo.gif) as a screencast.
 *
 * Every response here is mocked, so recording costs nothing, needs no model,
 * and produces the same story on any machine - the previous GIF was captured
 * against a live backend, which made it unreproducible and pinned the story to
 * whatever that session happened to do (it ended up being the same trivia
 * question asked several times, which sells this as a chat window rather than
 * an agent that works on your computer).
 *
 * The console runs in an iframe on a stage page beside a phone: a plain chat
 * view of the same approval (YBM approves over Telegram or WhatsApp; the phone
 * here stands for either and shows no real account). The recorder types the
 * request, taps Approve on the phone and writes JPEG frames with their times
 * and named marks; scripts/make_demo_gif.py sets the pace.
 *
 * Not part of `npm run test:e2e`: it writes files and only exists to be run
 * deliberately. Re-record with:
 *
 *   cd frontend && YBM_RECORD_DEMO=1 npx playwright test demo.spec.ts
 *   cd .. && backend/.venv/Scripts/python scripts/make_demo_gif.py
 *
 * Live mode records a real run instead: set YBM_DEMO_LIVE_URL to a running
 * YBM's console (for example http://127.0.0.1:8765/admin/) with a Downloads
 * folder granted and a model configured. Nothing is mocked: the model plans
 * the moves, the approval is the real one (the phone shows what it asks, built
 * from its manifest the way the chat channels describe it), tapping Approve on
 * the phone approves it through the console's API, and the phone's last
 * message is the start of the real answer. A clock on the stage shows real
 * time, so the GIF can speed through the model's thinking without hiding how
 * long it took. YBM_DEMO_CAPTION replaces the caption (name the model).
 */

const LIVE_URL = process.env.YBM_DEMO_LIVE_URL
const LIVE_CAPTION =
  process.env.YBM_DEMO_CAPTION ?? "A real run on a local model. The phone is a plain chat view of the same approval."

type ManifestItem = { operation?: string; source?: string; destination?: string }

/** The folder a path is in, for either separator. */
function parentName(file: string): string {
  const parts = file.split(/[\\/]+/).filter(Boolean)
  return parts.length > 1 ? parts[parts.length - 2] : ""
}

/** What the approval asks, as the chat channels word it (backend human_text._manifest_sentence). */
function approvalSentence(input: { manifest?: ManifestItem[] } | undefined): string {
  const items = (input?.manifest ?? []).filter((item) => item.source && item.destination)
  if (!items.length) return "Move or rename files."
  const homes = new Map<string, number>()
  for (const item of items) homes.set(parentName(item.source!), (homes.get(parentName(item.source!)) ?? 0) + 1)
  const home = [...homes.entries()].sort((a, b) => b[1] - a[1])[0][0]
  const folders = [...new Set(items.map((item) => parentName(item.destination!)))].sort()
  const files = items.length === 1 ? "1 file" : `${items.length} files`
  const where = folders.length === 1 ? `into ${folders[0]}` : `into ${folders.length} folders: ${folders.join(", ")}`
  return `Move ${files} in ${home} ${where}.`
}

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

/** The phone beside the console: a plain chat app, no real account, no messaging-app branding. */
const STAGE_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body { margin: 0; height: 100%; background: radial-gradient(120% 90% at 20% 10%, #18213a 0%, #0b0f1a 60%); overflow: hidden;
    font-family: "Segoe UI", system-ui, sans-serif; }
  .window { position: absolute; left: 36px; top: 40px; width: 1136px; height: 820px; border-radius: 14px; overflow: hidden;
    background: #0d1117; box-shadow: 0 30px 80px rgba(0,0,0,.55), 0 0 0 1px rgba(255,255,255,.06); }
  .bar { height: 34px; display: flex; align-items: center; gap: 8px; padding: 0 14px; background: #161b26; color: #8b93a7; font-size: 12px; }
  .dot { width: 11px; height: 11px; border-radius: 50%; background: #2c3445; }
  .url { margin-left: 14px; padding: 4px 12px; border-radius: 6px; background: #0d1117; }
  iframe { width: 100%; height: calc(100% - 34px); border: 0; display: block; background: #0d1117; }
  .phone { position: absolute; right: 44px; top: 70px; width: 336px; height: 690px; border-radius: 42px; background: #05070c;
    box-shadow: 0 30px 80px rgba(0,0,0,.6), 0 0 0 2px #262c3a, inset 0 0 0 9px #11151f; padding: 16px; box-sizing: border-box; }
  .screen { height: 100%; border-radius: 30px; overflow: hidden; background: #0f1420; display: flex; flex-direction: column; }
  .top { padding: 18px 16px 12px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #1d2433; }
  .avatar { width: 34px; height: 34px; border-radius: 50%; background: linear-gradient(135deg, #7c5cff, #2bc4a9); display: grid;
    place-items: center; color: white; font-weight: 700; font-size: 13px; }
  .who b { display: block; color: #e8ecf5; font-size: 14px; } .who span { color: #7f8aa3; font-size: 11px; }
  .msgs { flex: 1; padding: 14px 12px; display: flex; flex-direction: column; gap: 10px; justify-content: flex-end; }
  .bubble { max-width: 86%; padding: 10px 12px; border-radius: 16px; font-size: 13px; line-height: 1.35; color: #e8ecf5;
    background: #1b2233; border-bottom-left-radius: 5px; opacity: 0; transform: translateY(8px); transition: all .35s ease; }
  .bubble.show { opacity: 1; transform: none; }
  .bubble.me { align-self: flex-end; background: #3b5bdb; border-bottom-left-radius: 16px; border-bottom-right-radius: 5px; }
  .bubble .tag { display: inline-block; font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: #f5c542;
    margin-bottom: 4px; font-weight: 700; }
  .actions { display: flex; gap: 8px; margin-top: 10px; }
  .actions button { flex: 1; padding: 8px 6px; border-radius: 10px; border: 0; font: 600 12px "Segoe UI", sans-serif; }
  .approve { background: #2bc4a9; color: #062019; } .deny { background: #273046; color: #c9d1e3; }
  .approve.pressed { filter: brightness(.82); transform: scale(.97); }
  .caption { position: absolute; left: 36px; bottom: 6px; color: #6f7a92; font-size: 12px; }
  .clock { position: absolute; right: 44px; top: 34px; width: 336px; text-align: center; color: #8b93a7;
    font: 600 13px "Segoe UI", sans-serif; letter-spacing: .03em; font-variant-numeric: tabular-nums; }
  #cursor { position: fixed; left: -60px; top: -60px; z-index: 10; pointer-events: none; transition: left .5s ease, top .5s ease;
    filter: drop-shadow(0 2px 3px rgba(0,0,0,.5)); }
  #ring { position: fixed; width: 44px; height: 44px; margin: -22px 0 0 -22px; border-radius: 50%; border: 3px solid #ff8a3d;
    z-index: 9; pointer-events: none; opacity: 0; transform: scale(.3); }
  #ring.go { animation: ring .45s ease-out; }
  @keyframes ring { 0% { opacity: .95; transform: scale(.3); } 100% { opacity: 0; transform: scale(1.25); } }
</style></head><body>
  <div class="window"><div class="bar"><span class="dot"></span><span class="dot"></span><span class="dot"></span>
    <span class="url" id="url">127.0.0.1:8765/admin</span></div><iframe id="console"></iframe></div>
  <div class="clock" id="clock"></div>
  <div class="phone"><div class="screen">
    <div class="top"><div class="avatar">Y</div><div class="who"><b>YBM</b><span>your computer</span></div></div>
    <div class="msgs">
      <div class="bubble" id="ask"><span class="tag">Approval needed</span><br><span id="ask-text">Move 128 files in Downloads
        into 6 folders by type. Nothing is deleted or overwritten.</span><div class="actions"><button class="deny">Deny</button>
        <button class="approve" id="approve">Approve once</button></div></div>
      <div class="bubble me" id="yes">Approve once</div>
      <div class="bubble" id="done"><span id="done-text">Done: 128 files sorted into 6 folders. Nothing deleted.</span></div>
    </div></div></div>
  <div class="caption" id="caption">Scripted responses. The phone is a plain chat view of the same approval.</div>
  <svg id="cursor" width="26" height="26" viewBox="0 0 24 24"><path d="M4 2 L4 19 L8.5 15 L11.5 22 L14.5 20.7 L11.6 14 L17.5 14 Z"
    fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg><div id="ring"></div>
</body></html>`

// Record with an installed browser instead of downloading Playwright's (YBM_DEMO_CHANNEL=msedge).
// Screencast frames come at CSS size unless the whole browser renders at 1.5x.
test.use({
  channel: process.env.YBM_DEMO_CHANNEL || undefined,
  deviceScaleFactor: 1.5,
  launchOptions: { args: ["--force-device-scale-factor=1.5"] },
})

/** Records the README demo as a screencast: JPEG frames, their timestamps, and named marks. */
test("record the README demo", async ({ page, baseURL }) => {
  test.skip(process.env.YBM_RECORD_DEMO !== "1", "Set YBM_RECORD_DEMO=1 to re-record the README demo.")
  test.setTimeout(LIVE_URL ? 900_000 : 180_000)

  await rm(FRAME_DIR, { recursive: true, force: true })
  await mkdir(FRAME_DIR, { recursive: true })

  let phase: Phase = "empty"
  if (!LIVE_URL) await mockConsole(page, () => phase)
  await page.setViewportSize({ width: 1600, height: 900 })
  // One-time notices are not the product's steady state, and the demo is in the dark theme.
  await page.addInitScript(() => {
    localStorage.setItem("ybm-safety-tour-dismissed", "true")
    localStorage.setItem("ybm.setup.dismissed", "1")
    localStorage.setItem("theme", "dark")
  })
  await page.emulateMedia({ colorScheme: "dark" })
  // The stage is served from the console's own origin, so the console in its iframe keeps its
  // local storage (a page with no origin would leave the iframe without one).
  const consoleUrl = LIVE_URL ?? baseURL ?? "http://127.0.0.1:4173/admin/"
  const stageUrl = new URL("/demo-stage.html", consoleUrl).toString()
  await page.route(stageUrl, (route) => route.fulfill({ contentType: "text/html", body: STAGE_HTML }))
  await page.goto(stageUrl)
  const api = (route: string) => new URL(`api/${route}`, consoleUrl).toString()
  if (LIVE_URL) {
    // The console refuses to be framed (X-Frame-Options: DENY, frame-ancestors 'none'), as it
    // should. The stage needs it in an iframe, so this browser alone drops those two headers from
    // the console's pages; the server sends them as before.
    await page.route(`${new URL(consoleUrl).origin}/admin/**`, async (route) => {
      if (route.request().resourceType() !== "document") return route.continue()
      const response = await route.fetch()
      const headers = { ...response.headers() }
      delete headers["x-frame-options"]
      if (headers["content-security-policy"]) {
        headers["content-security-policy"] = headers["content-security-policy"].replace(/frame-ancestors[^;]*;?\s*/, "")
      }
      await route.fulfill({ response, headers })
    })
    await page.evaluate(
      ([host, caption]) => {
        document.getElementById("url")!.textContent = host
        document.getElementById("caption")!.textContent = caption
      },
      [`${new URL(consoleUrl).host}/admin`, LIVE_CAPTION] as const,
    )
  }
  /** The stage clock: real seconds since the request was sent, until it is stopped. */
  const startClock = () =>
    page.evaluate(() => {
      const started = performance.now()
      const clock = document.getElementById("clock")!
      const tick = () => {
        const seconds = Math.floor((performance.now() - started) / 1000)
        clock.textContent = `Real time ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
      }
      tick()
      ;(window as unknown as { clockTimer: number }).clockTimer = window.setInterval(tick, 250)
    })
  const stopClock = () =>
    page.evaluate(() => window.clearInterval((window as unknown as { clockTimer: number }).clockTimer))
  await page.evaluate((src) => {
    ;(document.getElementById("console") as HTMLIFrameElement).src = src
  }, consoleUrl)
  const app = page.frameLocator("#console")
  const composer = app.getByRole("textbox", { name: "Message YBM" })
  await composer.waitFor()
  await app.getByRole("heading", { name: "What can I help you do?" }).waitFor()
  // React Query's devtools button belongs to the development build, not the product.
  for (const frame of page.frames()) {
    await frame
      .addStyleTag({ content: ".tsqd-parent-container, [data-tsqd-parent-container] { display: none !important; }" })
      .catch(() => undefined)
  }
  await page.waitForTimeout(800)

  // ---- screencast ---------------------------------------------------------------------------
  const cdp = await page.context().newCDPSession(page)
  const times: { file: string; t: number }[] = []
  const marks: Record<string, number> = {}
  let start = 0
  const writes: Promise<void>[] = []
  cdp.on("Page.screencastFrame", (event) => {
    const t = event.metadata.timestamp ?? Date.now() / 1000
    if (!start) start = t
    const file = `${String(times.length).padStart(5, "0")}.jpg`
    times.push({ file, t: t - start })
    writes.push(writeFile(path.join(FRAME_DIR, file), Buffer.from(event.data, "base64")))
    void cdp.send("Page.screencastFrameAck", { sessionId: event.sessionId })
  })
  const mark = (name: string) => {
    marks[name] = times.length ? times[times.length - 1].t : 0
  }
  const hold = (ms: number) => page.waitForTimeout(ms)
  /** Glide the drawn cursor to the middle of an element (page coordinates, iframe included). */
  const glideTo = async (box: { x: number; y: number; width: number; height: number } | null, ms = 650) => {
    if (!box) throw new Error("nothing to point at")
    await page.evaluate(
      ([x, y, d]) => {
        const c = document.getElementById("cursor")!
        c.style.transition = `left ${d}ms ease, top ${d}ms ease`
        c.style.left = `${x - 4}px`
        c.style.top = `${y - 2}px`
      },
      [box.x + box.width / 2, box.y + box.height / 2, ms] as const,
    )
    await hold(ms + 60)
  }
  const ring = async (box: { x: number; y: number; width: number; height: number }) => {
    await page.evaluate(([x, y]) => {
      const r = document.getElementById("ring")!
      r.style.left = `${x}px`
      r.style.top = `${y}px`
      r.classList.remove("go")
      void r.offsetWidth
      r.classList.add("go")
    }, [box.x + box.width / 2, box.y + box.height / 2] as const)
  }
  const show = (id: string) => page.evaluate((i) => document.getElementById(i)!.classList.add("show"), id)

  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, everyNthFrame: 1 })
  await page.evaluate(() => {
    const c = document.getElementById("cursor")!
    c.style.transition = "none"
    c.style.left = "560px"
    c.style.top = "700px"
  })
  mark("empty")
  await hold(1200)

  // The request, typed the way a person types it.
  const box = await composer.boundingBox()
  await glideTo(box)
  if (box) await ring(box)
  await composer.click()
  mark("typing")
  await composer.pressSequentially(OBJECTIVE, { delay: 55 })
  await hold(500)
  phase = "scanning"
  await composer.press("Enter")
  mark("sent")
  let approval: { id: string; task_id: string; action_payload?: { input?: { manifest?: ManifestItem[] } } } | undefined
  if (LIVE_URL) {
    // The model plans the moves; nothing to stage here but the clock.
    await startClock()
    await app.getByRole("button", { name: "Approve once" }).waitFor({ timeout: 600_000 })
    const listed = (await (await page.request.get(api("approvals"))).json()) as {
      approvals: { approval?: typeof approval; status?: string }[]
    }
    approval = listed.approvals
      .map((item) => (item.approval ?? item) as NonNullable<typeof approval> & { status?: string })
      .find((item) => item.status === "pending")
    if (!approval) throw new Error("the console shows an approval, but none is pending")
    const asked = approvalSentence(approval.action_payload?.input)
    await page.evaluate((text) => {
      document.getElementById("ask-text")!.textContent = text
    }, asked)
  } else {
    await app.getByText("Scanned", { exact: false }).waitFor()
    await hold(1500)
    phase = "approval"
    await app.getByRole("button", { name: "Approve once" }).waitFor({ timeout: 15_000 })
  }

  // Nothing moves until a person says so: the request reaches the console and the phone.
  mark("approval")
  await hold(700)
  await show("ask")
  await hold(2200)
  const approve = await page.locator("#approve").boundingBox()
  await glideTo(approve, 800)
  if (approve) await ring(approve)
  await page.evaluate(() => document.getElementById("approve")!.classList.add("pressed"))
  if (approval) {
    const decided = await page.request.post(api(`approvals/${approval.id}/decide`), { data: { decision: "approve" } })
    if (!decided.ok()) throw new Error(`approving ${approval.id} failed: ${decided.status()} ${await decided.text()}`)
  }
  mark("approved")
  await show("yes")
  await hold(500)

  phase = "done"
  await app.getByText("What this task touched").waitFor({ timeout: LIVE_URL ? 600_000 : 15_000 })
  if (approval) {
    await stopClock()
    const tasks = (await (await page.request.get(api("tasks"))).json()) as {
      tasks: { id: string; metadata?: Record<string, unknown> }[]
    }
    const meta = tasks.tasks.find((task) => task.id === approval!.task_id)?.metadata ?? {}
    const answer = String(meta.synthesized_answer ?? meta.final_answer ?? "").trim()
    const first = answer.split("\n")[0].replace(/:\s*$/, ".")
    await page.evaluate((text) => {
      document.getElementById("done-text")!.textContent = text
    }, first.length > 160 ? `${first.slice(0, 157)}...` : first)
  }
  mark("done")
  await show("done")
  await hold(800)
  // The receipt: what changed, verified, and that nothing left the machine. The cursor waits
  // beside it rather than on it.
  await app.getByText("What this task touched").scrollIntoViewIfNeeded()
  const receiptBox = await app.getByText("What this task touched").boundingBox()
  if (receiptBox) await glideTo({ ...receiptBox, x: receiptBox.x + 600, y: receiptBox.y + 40 }, 700)
  await hold(3200)
  mark("end")
  await cdp.send("Page.stopScreencast")
  await Promise.all(writes)
  await writeFile(path.join(FRAME_DIR, "times.json"), JSON.stringify(times))
  await writeFile(path.join(FRAME_DIR, "marks.json"), JSON.stringify(marks))
})
