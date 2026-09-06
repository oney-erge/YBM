import { expect, type Page, test } from "@playwright/test"

/**
 * The reliability dashboard (docs/ROADMAP.md "reliability dashboard") -
 * the metric that distinguishes it from "completed": a verified-success
 * rate computed from mechanical proof, not the model's own word.
 */

function dashboard(overrides: Record<string, unknown> = {}) {
  return {
    window_days: 7,
    tasks_attempted: 20,
    completed: 16,
    completed_pct: 80.0,
    verified_completed: 12,
    verified_completed_pct: 60.0,
    failed: 2,
    failed_pct: 10.0,
    blocked: 1,
    cancelled: 1,
    tasks_with_retries: 3,
    mean_retries: 0.35,
    fallback_tasks: 1,
    total_tokens: 48213,
    avg_task_duration_seconds: 42.5,
    tool_call_failure_rate_pct: 4.2,
    most_unreliable_tool: "browser.control",
    tools: [
      { tool_name: "browser.control", calls: 8, succeeded: 5, failed: 3, failure_rate_pct: 37.5 },
      { tool_name: "filesystem.manage", calls: 30, succeeded: 30, failed: 0, failure_rate_pct: 0.0 },
    ],
    by_model: [{ model: "qwen3:8b", tasks: 18, completed: 15, total_tokens: 40000 }],
    by_task_type: [{ task_type: "file_management", tasks: 10, completed: 9, failed: 1 }],
    ...overrides,
  }
}

async function mockInsightsPage(page: Page, getDashboard: () => ReturnType<typeof dashboard>) {
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
    if (path === "/api/dashboard") {
      return route.fulfill({ json: getDashboard() })
    }
    return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
  })
}

test("shows stat tiles, the least-reliable tool, and per-tool/model/type tables", async ({ page }) => {
  await mockInsightsPage(page, () => dashboard())
  await page.goto("./insights")

  await expect(page.getByText("80%")).toBeVisible()
  await expect(page.getByText("60%")).toBeVisible()
  await expect(page.getByText("Least reliable tool: browser.control")).toBeVisible()
  await expect(page.getByRole("cell", { name: "browser.control" })).toBeVisible()
  await expect(page.getByRole("cell", { name: "qwen3:8b" })).toBeVisible()
  await expect(page.getByRole("cell", { name: "file_management" })).toBeVisible()
})

test("switching the window re-fetches the dashboard for that range", async ({ page }) => {
  let requestedWindow: string | null = null
  await mockInsightsPage(page, () => dashboard())
  await page.route("**/admin/api/dashboard*", async (route) => {
    requestedWindow = new URL(route.request().url()).searchParams.get("window_days")
    return route.fulfill({ json: dashboard({ window_days: Number(requestedWindow) }) })
  })
  await page.goto("./insights")
  await expect(page.getByText("80%")).toBeVisible()

  await page.getByRole("button", { name: "30d" }).click()

  await expect.poll(() => requestedWindow).toBe("30")
})

test("shows an empty state when there are no tasks in the window", async ({ page }) => {
  await mockInsightsPage(page, () =>
    dashboard({
      tasks_attempted: 0,
      completed: 0,
      failed: 0,
      blocked: 0,
      cancelled: 0,
      tools: [],
      by_model: [],
      by_task_type: [],
      most_unreliable_tool: null,
      avg_task_duration_seconds: null,
    }),
  )
  await page.goto("./insights")

  await expect(page.getByText("No tasks in the last 7 days.")).toBeVisible()
})
