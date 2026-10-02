import { expect, type Page, test } from "@playwright/test"

// The first-run experience, with the backend mocked at the HTTP boundary:
// a model that was picked for you is named, the first request's missing folder
// access is asked for once, a blocked task offers to fix itself, and a browser
// that is not signed in is told the easy way back in before it is asked for a
// token.

const now = "2026-10-02T12:00:00Z"

type Json = Record<string, unknown>

function bootstrap(overrides: Json = {}): Json {
  return {
    token_required: false,
    authenticated: true,
    onboarding_complete: true,
    llm_reachable: true,
    model: {
      configured: true,
      model: "gpt-4.1",
      provider: "OpenAI",
      local: false,
      key_env: "OPENAI_API_KEY",
    },
    version: "0.1.4-e2e",
    ...overrides,
  }
}

interface MockState {
  tasks: Json[]
  folders: { file_access: string; work_folders: string[] }
  posts: { path: string; body: unknown }[]
}

async function mockAdminApi(page: Page, options: { bootstrap?: Json; state?: Partial<MockState> } = {}) {
  const state: MockState = {
    tasks: [],
    folders: { file_access: "off", work_folders: [] },
    posts: [],
    ...options.state,
  }
  await page.route("**/admin/api/**", async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace(/^\/admin/, "")

    if (path === "/api/bootstrap") return route.fulfill({ json: bootstrap(options.bootstrap) })
    if (path === "/api/chat/messages" && request.method() === "POST") {
      const body = request.postDataJSON() as { text: string }
      state.posts.push({ path, body })
      const created = {
        id: `task-${state.posts.length}`,
        objective: body.text,
        status: "received",
        conversation_id: "web",
        created_at: now,
        updated_at: now,
        metadata: {},
        artifacts: [],
      }
      state.tasks = [created]
      return route.fulfill({ json: { conversation_id: "web", task: created } })
    }
    if (path === "/api/chat/messages") return route.fulfill({ json: { conversation_id: "web", tasks: state.tasks } })
    if (path === "/api/approvals") return route.fulfill({ json: { approvals: [] } })
    if (path === "/api/setup/folders") {
      return route.fulfill({
        json: {
          suggested: [
            { name: "Downloads", path: "C:\\Users\\me\\Downloads" },
            { name: "Documents", path: "C:\\Users\\me\\Documents" },
          ],
          file_access: state.folders.file_access,
          allowed_roots: [".agent_control/workspaces", ...state.folders.work_folders],
          work_folders: state.folders.work_folders,
        },
      })
    }
    if (path === "/api/setup/work-folders" && request.method() === "POST") {
      const body = request.postDataJSON() as { folders: string[]; mode: string }
      state.posts.push({ path, body })
      state.folders = { file_access: body.mode, work_folders: body.folders }
      return route.fulfill({
        json: {
          suggested: [],
          file_access: body.mode,
          allowed_roots: [".agent_control/workspaces", ...body.folders],
          work_folders: body.folders,
        },
      })
    }
    if (path === "/api/config/access-modes" && request.method() === "POST") {
      state.posts.push({ path, body: request.postDataJSON() })
      return route.fulfill({ json: { config_file: "config.yaml", access_modes: {} } })
    }
    if (path === "/api/session") return route.fulfill({ json: { status: "ok", persistent: true } })
    if (path === "/api/summary") {
      return route.fulfill({
        json: {
          status: "ok",
          tasks: state.tasks,
          task_pagination: { total: state.tasks.length },
          warnings: [],
          config: { llm: { default_profile: "onboard" }, adapters: { workspace: { enabled: true, root_dir: "w" } } },
          database: { database_url: "sqlite:///e2e.db", path: "e2e.db" },
          integrations: { telegram: { enabled: false, token_present: false }, llm: { default_profile_configured: true } },
        },
      })
    }
    return route.fulfill({ status: 404, json: { detail: `No first-run mock for ${path}` } })
  })
  return state
}

test("the chat header says which model was picked, and names the key variable but never its value", async ({ page }) => {
  await mockAdminApi(page)
  await page.goto("./")

  const chip = page.getByRole("link", { name: /Model: gpt-4\.1/ })
  await expect(chip).toBeVisible()
  await expect(chip).toContainText("gpt-4.1")
  const label = (await chip.getAttribute("aria-label")) ?? ""
  expect(label).toContain("OPENAI_API_KEY")
  expect(label).toContain("from your environment")
})

test("there is no model chip when the backend reports none", async ({ page }) => {
  await mockAdminApi(page, {
    bootstrap: { model: { configured: false, model: null, provider: null, local: false, key_env: null } },
  })
  await page.goto("./")

  await expect(page.getByRole("heading", { name: "What can I help you do?" })).toBeVisible()
  await expect(page.getByRole("link", { name: /Model:/ })).toHaveCount(0)
})

test("the first request's missing folder access is asked for once, up front", async ({ page }) => {
  const state = await mockAdminApi(page)
  await page.goto("./")

  await expect(page.getByRole("region", { name: "Choose folders YBM may work in" })).toBeVisible()
  // Downloads is what the headline request names, so it starts selected.
  await expect(page.getByRole("button", { name: "Downloads", exact: true })).toHaveAttribute("aria-pressed", "true")
  await expect(page.getByRole("button", { name: "Documents", exact: true })).toHaveAttribute("aria-pressed", "false")
  // The careful choice is the default.
  await expect(page.getByRole("radio", { name: /Ask before changing anything/ })).toHaveAttribute("aria-checked", "true")

  await page.getByRole("button", { name: "Allow", exact: true }).click()

  await expect.poll(() => state.posts.find((p) => p.path === "/api/setup/work-folders")?.body).toEqual({
    folders: ["C:\\Users\\me\\Downloads"],
    mode: "write_access",
  })
  // Once granted there is nothing left to ask, so the card goes away.
  await expect(page.getByRole("region", { name: "Choose folders YBM may work in" })).toHaveCount(0)
})

test("a person can choose more folders, or look-only, before allowing", async ({ page }) => {
  const state = await mockAdminApi(page)
  await page.goto("./")

  await page.getByRole("button", { name: "Documents", exact: true }).click()
  await page.getByRole("radio", { name: /Look only/ }).click()
  await page.getByLabel("Or another folder (full path)").fill("D:\\Projects")
  await page.getByRole("button", { name: "Allow", exact: true }).click()

  await expect.poll(() => state.posts.find((p) => p.path === "/api/setup/work-folders")?.body).toEqual({
    folders: ["C:\\Users\\me\\Downloads", "C:\\Users\\me\\Documents", "D:\\Projects"],
    mode: "read_only",
  })
})

test("Allow is disabled until a folder is chosen", async ({ page }) => {
  await mockAdminApi(page)
  await page.goto("./")

  await page.getByRole("button", { name: "Downloads", exact: true }).click() // untick the default
  await expect(page.getByRole("button", { name: "Allow", exact: true })).toBeDisabled()
  await expect(page.getByText("Choose at least one folder.")).toBeVisible()
})

test("the folder card is not shown once folders are already granted", async ({ page }) => {
  await mockAdminApi(page, {
    state: { folders: { file_access: "write_access", work_folders: ["C:\\Users\\me\\Downloads"] } },
  })
  await page.goto("./")

  await expect(page.getByRole("heading", { name: "What can I help you do?" })).toBeVisible()
  await expect(page.getByRole("region", { name: "Choose folders YBM may work in" })).toHaveCount(0)
})

test("the folder card shows the server's refusal instead of failing silently", async ({ page }) => {
  await mockAdminApi(page)
  await page.route("**/admin/api/setup/work-folders", (route) =>
    route.fulfill({ status: 400, json: { detail: "A whole drive is too much to hand over. Pick a folder inside it." } }),
  )
  await page.goto("./")

  await page.getByLabel("Or another folder (full path)").fill("C:\\")
  await page.getByRole("button", { name: "Allow", exact: true }).click()

  await expect(page.getByRole("alert")).toContainText("A whole drive is too much to hand over")
})

function blockedTask(access: Json) {
  return {
    id: "task-blocked",
    objective: "Organize my Downloads folder by file type",
    status: "blocked",
    conversation_id: "web",
    created_at: now,
    updated_at: now,
    metadata: {
      last_worker_error: "YBM tried to use File system, but that access is turned off. Turn it on in Access, then ask again.",
      blocked_access: access,
    },
    artifacts: [],
  }
}

test("a task blocked by missing access offers to turn it on and retry", async ({ page }) => {
  const state = await mockAdminApi(page, { state: { folders: { file_access: "write_access", work_folders: ["C:\\x"] } } })
  state.tasks = [
    blockedTask({
      evidence: "denied",
      groups: [{ group: "browser", label: "Browser", recommended_mode: "write_access" }],
    }),
  ]
  await page.route("**/admin/api/tasks/task-blocked/receipt", (route) => route.fulfill({ status: 404, json: { detail: "n/a" } }))
  await page.goto("./")

  await expect(page.getByText("This needs access that is off.")).toBeVisible()
  await page.getByRole("button", { name: "Turn on Browser" }).click()
  await expect.poll(() => state.posts.find((p) => p.path === "/api/config/access-modes")?.body).toEqual({
    modes: { browser: "write_access" },
  })

  await expect(page.getByText("Access is on. Try your request again.")).toBeVisible()
  await page.getByRole("button", { name: "Try again" }).click()
  await expect
    .poll(() => state.posts.filter((p) => p.path === "/api/chat/messages").map((p) => p.body))
    .toContainEqual({ text: "Organize my Downloads folder by file type", attachment_ids: [] })
})

test("when the agent only gave up, the hint says 'may', not 'needs'", async ({ page }) => {
  const state = await mockAdminApi(page)
  state.tasks = [
    blockedTask({
      evidence: "off",
      groups: [{ group: "filesystem", label: "File system", recommended_mode: "write_access" }],
    }),
  ]
  await page.route("**/admin/api/tasks/task-blocked/receipt", (route) => route.fulfill({ status: 404, json: { detail: "n/a" } }))
  await page.goto("./")

  await expect(page.getByText("This may need access that is off.")).toBeVisible()
  await expect(page.getByRole("button", { name: "Choose folders for YBM" })).toBeVisible()
})

test("a blocked task with no access evidence shows no hint", async ({ page }) => {
  const state = await mockAdminApi(page)
  const task = blockedTask({ evidence: "off", groups: [] })
  state.tasks = [task]
  await page.route("**/admin/api/tasks/task-blocked/receipt", (route) => route.fulfill({ status: 404, json: { detail: "n/a" } }))
  await page.goto("./")

  await expect(page.getByText(/needs access that is off|may need access that is off/)).toHaveCount(0)
})

test("a browser that is not signed in is told the easy way back in before being asked for a token", async ({ page }) => {
  await mockAdminApi(page, { bootstrap: { token_required: true, authenticated: false } })
  await page.goto("./")

  await expect(page.getByText("Sign in to YBM")).toBeVisible()
  await expect(page.getByText(/open YBM from its shortcut/)).toBeVisible()
  await expect(page.getByLabel("Or paste the admin token")).toBeVisible()
})

test("a browser that is already signed in never sees the token screen", async ({ page }) => {
  await mockAdminApi(page, { bootstrap: { token_required: true, authenticated: true } })
  await page.goto("./")

  await expect(page.getByText("Sign in to YBM")).toHaveCount(0)
  await expect(page.getByRole("heading", { name: "What can I help you do?" })).toBeVisible()
})

test("the launch link's token is exchanged for a session and removed from the address bar", async ({ page }) => {
  const state = await mockAdminApi(page, { bootstrap: { token_required: true, authenticated: true } })
  const sessionRequest = page.waitForRequest(
    (request) => request.url().endsWith("/admin/api/session") && request.method() === "POST",
  )

  await page.goto("./?token=launch-token-123")
  const request = await sessionRequest

  expect(request.headers()["x-agent-control-admin-token"]).toBe("launch-token-123")
  expect(page.url()).not.toContain("launch-token-123")
  expect(page.url()).not.toContain("token=")
  expect(state).toBeTruthy()
})

test("a slow first response shows that YBM is starting instead of a blank window", async ({ page }) => {
  await page.route("**/admin/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/admin/, "")
    if (path === "/api/bootstrap") {
      await new Promise((resolve) => setTimeout(resolve, 1500))
      return route.fulfill({ json: bootstrap() })
    }
    if (path === "/api/approvals") return route.fulfill({ json: { approvals: [] } })
    return route.fulfill({ json: {} })
  })
  await page.goto("./")

  await expect(page.getByRole("status")).toHaveText("Starting YBM...")
  await expect(page.getByRole("link", { name: "Chat" })).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole("status").filter({ hasText: "Starting YBM" })).toHaveCount(0)
})
