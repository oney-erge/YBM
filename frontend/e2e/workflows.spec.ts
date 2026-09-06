import { expect, type Page, test } from "@playwright/test"

/**
 * Verified workflows (docs/ROADMAP.md "reusable verified workflows"): save
 * a completed task's replay plan as a named, parameterized template from
 * its trace page, then list/run/delete it from the Workflows page.
 */

const TASK_ID = "task_sort_downloads"
const WORKFLOW_ID = "workflow_sort_downloads"
const RUN_TASK_ID = "task_workflow_run_1"
const OBJECTIVE = "Sort my Downloads folder"

function chatTask(status: string) {
  return {
    id: TASK_ID,
    objective: OBJECTIVE,
    status,
    conversation_id: "web",
    created_at: "2026-09-01T09:00:00Z",
    updated_at: "2026-09-01T09:00:30Z",
    metadata: {},
    artifacts: [],
  }
}

function minimalSummary(tasks: unknown[]) {
  return {
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
      llm: { default_profile: "smoke" },
      adapters: { workspace: { enabled: true, root_dir: ".agent_control/workspaces" } },
    },
    database: { database_url: "sqlite:///smoke.db", path: "smoke.db" },
    integrations: {
      telegram: { enabled: false, token_present: false },
      llm: { default_profile_configured: true },
    },
  }
}

function baseWorkflow(overrides: Record<string, unknown> = {}) {
  return {
    id: WORKFLOW_ID,
    name: "Sort a folder",
    source_task_id: TASK_ID,
    objective_template: OBJECTIVE,
    plan: [
      {
        tool_name: "filesystem.manage",
        tool_input: { operation: "inspect_folder", root: "{{folder}}" },
      },
    ],
    parameters: ["folder"],
    created_at: "2026-09-01T09:00:30Z",
    ...overrides,
  }
}

async function mockCommonRoutes(page: Page, task: ReturnType<typeof chatTask>) {
  await page.route("**/admin/api/**", async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname.replace(/^\/admin/, "")

    if (path === "/api/bootstrap") {
      return route.fulfill({
        json: { token_required: false, onboarding_complete: true, llm_reachable: true, version: "0.1.0-e2e" },
      })
    }
    if (path === "/api/approvals") {
      return route.fulfill({ json: { approvals: [] } })
    }
    if (path === "/api/summary") {
      return route.fulfill({ json: minimalSummary([task]) })
    }
    if (path === `/api/tasks/${TASK_ID}/trace`) {
      return route.fulfill({
        json: {
          task,
          context: {},
          operator_history: [
            {
              tool_name: "filesystem.manage",
              input: { operation: "inspect_folder", root: "C:/Users/sam/Downloads" },
              status: "succeeded",
              output_summary: "Found 12 files.",
              error: null,
              duration_ms: 400,
              content_trust: null,
              verification: null,
            },
          ],
          timeline: [],
          tool_invocations: [],
          evidence: { files: [], urls: [], commands: [] },
          llm_calls: [],
          approvals: [],
          artifacts: [],
          signals: [],
          audit: [],
        },
      })
    }
    return route.fallback()
  })
}

test.describe("saving a workflow from a completed task", () => {
  test("posts the named parameter mapping and links to the workflows page", async ({ page }) => {
    const task = chatTask("completed")
    let saveRequestBody: unknown = null
    await mockCommonRoutes(page, task)
    await page.route("**/admin/api/**", async (route) => {
      const url = new URL(route.request().url())
      const path = url.pathname.replace(/^\/admin/, "")
      if (path === `/api/tasks/${TASK_ID}/save_workflow` && route.request().method() === "POST") {
        saveRequestBody = route.request().postDataJSON()
        return route.fulfill({ json: { workflow: baseWorkflow() } })
      }
      if (path === "/api/workflows") {
        return route.fulfill({ json: { workflows: [baseWorkflow()] } })
      }
      return route.fallback()
    })

    await page.goto(`./tasks/${TASK_ID}`)
    await expect(page.getByRole("heading", { name: OBJECTIVE })).toBeVisible()

    await page.getByRole("button", { name: "Save as workflow" }).click()
    await page.getByPlaceholder("Organize invoices").fill("Sort a folder")
    await page.getByPlaceholder("Exact value, e.g. C:/Users/sam/Downloads").fill("C:/Users/sam/Downloads")
    await page.getByPlaceholder("folder").fill("folder")
    await page.getByRole("button", { name: "Save workflow" }).click()

    await expect.poll(() => saveRequestBody).toEqual({
      name: "Sort a folder",
      parameters: { "C:/Users/sam/Downloads": "folder" },
    })
    await expect(page).toHaveURL(/\/workflows$/)
  })
})

test.describe("the workflows page", () => {
  test("lists a saved workflow and runs it with a filled-in parameter value", async ({ page }) => {
    let runRequestBody: unknown = null
    await page.route("**/admin/api/**", async (route) => {
      const url = new URL(route.request().url())
      const path = url.pathname.replace(/^\/admin/, "")

      if (path === "/api/bootstrap") {
        return route.fulfill({
          json: { token_required: false, onboarding_complete: true, llm_reachable: true, version: "0.1.0-e2e" },
        })
      }
      if (path === "/api/approvals") {
        return route.fulfill({ json: { approvals: [] } })
      }
      if (path === "/api/workflows") {
        return route.fulfill({ json: { workflows: [baseWorkflow()] } })
      }
      if (path === `/api/workflows/${WORKFLOW_ID}/run` && route.request().method() === "POST") {
        runRequestBody = route.request().postDataJSON()
        return route.fulfill({
          json: {
            task: {
              id: RUN_TASK_ID,
              objective: "Sort a folder (workflow run)",
              status: "received",
              conversation_id: null,
              created_at: "2026-09-01T09:05:00Z",
              updated_at: "2026-09-01T09:05:00Z",
              metadata: { workflow_id: WORKFLOW_ID },
              artifacts: [],
            },
          },
        })
      }
      if (path === `/api/tasks/${RUN_TASK_ID}/trace`) {
        return route.fulfill({
          json: {
            task: {
              id: RUN_TASK_ID,
              objective: "Sort a folder (workflow run)",
              status: "received",
              conversation_id: null,
              created_at: "2026-09-01T09:05:00Z",
              updated_at: "2026-09-01T09:05:00Z",
              metadata: {},
              artifacts: [],
            },
            context: {},
            operator_history: [],
            timeline: [],
            tool_invocations: [],
            evidence: { files: [], urls: [], commands: [] },
            llm_calls: [],
            approvals: [],
            artifacts: [],
            signals: [],
            audit: [],
          },
        })
      }
      return route.fallback()
    })

    await page.goto("./workflows")

    await expect(page.getByText("Sort a folder")).toBeVisible()
    await expect(page.getByText("parameters: {{folder}}")).toBeVisible()

    await page.getByRole("button", { name: "Run" }).click()
    await page.getByLabel("{{folder}}").fill("C:/Users/sam/Desktop")
    await page.getByRole("button", { name: "Run", exact: true }).last().click()

    await expect.poll(() => runRequestBody).toEqual({ values: { folder: "C:/Users/sam/Desktop" } })
    await expect(page).toHaveURL(new RegExp(`/tasks/${RUN_TASK_ID}$`))
  })

  test("deleting a workflow removes it from the list after confirming", async ({ page }) => {
    let deleted = false
    await page.route("**/admin/api/**", async (route) => {
      const url = new URL(route.request().url())
      const path = url.pathname.replace(/^\/admin/, "")

      if (path === "/api/bootstrap") {
        return route.fulfill({
          json: { token_required: false, onboarding_complete: true, llm_reachable: true, version: "0.1.0-e2e" },
        })
      }
      if (path === "/api/approvals") {
        return route.fulfill({ json: { approvals: [] } })
      }
      if (path === "/api/workflows") {
        return route.fulfill({ json: { workflows: deleted ? [] : [baseWorkflow()] } })
      }
      if (path === `/api/workflows/${WORKFLOW_ID}` && route.request().method() === "DELETE") {
        deleted = true
        return route.fulfill({ json: { status: "deleted" } })
      }
      return route.fallback()
    })

    await page.goto("./workflows")
    await expect(page.getByText("Sort a folder")).toBeVisible()

    await page.getByRole("button", { name: "Delete Sort a folder" }).click()
    await page.getByRole("button", { name: "Delete", exact: true }).click()

    await expect(page.getByText("No workflows saved yet.")).toBeVisible()
  })
})
