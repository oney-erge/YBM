import { expect, type Page, test } from "@playwright/test"

/**
 * "I generated a connector. Here is exactly what it will access. Tests
 * pass. Install it?" (docs/ROADMAP.md "integration control plane") - a
 * pending adapter.factory promote_after_approval approval must show the
 * actual generated source and sandbox test result, not just the tool_input
 * (adapter_dir + approved=true) it technically carries.
 */

const now = "2026-09-01T09:00:00Z"
const TASK_ID = "task_new_connector"
const APPROVAL_ID = "appr_promote_weather"
const ADAPTER_DIR = "C:/agent/adapters/weather_lookup"

function chatTask() {
  return {
    id: TASK_ID,
    objective: "Build a connector for the weather API",
    status: "awaiting_approval",
    conversation_id: "web",
    created_at: now,
    updated_at: now,
    metadata: {},
    artifacts: [],
  }
}

function pendingApproval() {
  return {
    approval: {
      id: APPROVAL_ID,
      task_id: TASK_ID,
      capability: "filesystem.write",
      risk_level: "critical",
      summary: "Promote the generated weather_lookup adapter and hot-register it as a tool.",
      action_payload: {
        tool_name: "adapter.factory",
        input: { operation: "promote_after_approval", adapter_dir: ADAPTER_DIR, approved: true },
      },
      status: "pending",
      expires_at: "2026-09-01T09:15:00Z",
      created_at: now,
    },
    task_objective: chatTask().objective,
    task_status: "awaiting_approval",
    capability_max_risk_level: "critical",
    blast_radius: { files: [], urls: [], commands: [] },
  }
}

async function mockAdapterApproval(page: Page, testPassed: boolean) {
  await page.route("**/admin/api/**", async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname.replace(/^\/admin/, "")

    if (path === "/api/bootstrap") {
      return route.fulfill({
        json: { token_required: false, onboarding_complete: true, llm_reachable: true, version: "0.1.0-e2e" },
      })
    }
    if (path === "/api/config/voice") {
      return route.fulfill({
        json: { enabled: false, provider: "faster_whisper", model: "base", installed: false, available: false, install_hint: "" },
      })
    }
    if (path === "/api/chat/messages") {
      // This test never sends a message - only the GET (task list) path is
      // ever actually exercised.
      return route.fulfill({ json: { conversation_id: "web", tasks: [chatTask()] } })
    }
    if (path === "/api/approvals") {
      return route.fulfill({ json: { approvals: [pendingApproval()] } })
    }
    if (path === "/api/summary") {
      return route.fulfill({
        json: {
          status: "ok",
          tasks: [chatTask()],
          task_pagination: { total: 1 },
          vscode: { connected: false, status: "waiting", last_seen_at: null, last_seen_age_seconds: null, heartbeat: null, state: null, pending_terminal_commands: 0 },
          warnings: [],
          config: { llm: { default_profile: "smoke" }, adapters: { workspace: { enabled: true, root_dir: ".agent_control/workspaces" } } },
          database: { database_url: "sqlite:///smoke.db", path: "smoke.db" },
          integrations: { telegram: { enabled: false, token_present: false }, llm: { default_profile_configured: true } },
        },
      })
    }
    if (path === "/api/adapters/review") {
      return route.fulfill({
        json: {
          adapter_dir: ADAPTER_DIR,
          manifest: { name: "weather_lookup", objective: "Look up local weather.", capability: "http.request", operations: ["lookup"] },
          files: {
            "adapter.py": "class WeatherLookupAdapter:\n    async def execute(self, request):\n        ...\n",
            "test_adapter.py": "def test_it():\n    assert True\n",
            "README.md": "# weather_lookup\n",
          },
          test: {
            passed: testPassed,
            summary: testPassed ? "1 passed" : "1 failed",
            stdout: testPassed ? "1 passed in 0.01s" : "AssertionError",
            stderr: "",
          },
        },
      })
    }
    return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
  })
}

test("shows the generated adapter source and a passing sandbox test", async ({ page }) => {
  await mockAdapterApproval(page, true)
  await page.goto("./")

  await page.getByRole("button", { name: "Review details" }).click()

  await expect(page.getByText("Generated adapter")).toBeVisible()
  await expect(page.getByText("Sandbox tests pass")).toBeVisible()
  await expect(page.getByText("Look up local weather.")).toBeVisible()
  await expect(page.getByRole("button", { name: "adapter.py", exact: true })).toBeVisible()
  await expect(page.getByText("class WeatherLookupAdapter")).toBeVisible()
})

test("flags a failed sandbox test with its output", async ({ page }) => {
  await mockAdapterApproval(page, false)
  await page.goto("./")

  await page.getByRole("button", { name: "Review details" }).click()

  await expect(page.getByText("Sandbox tests failed")).toBeVisible()
  await expect(page.getByText("Test output")).toBeVisible()
  await expect(page.getByText("AssertionError")).toBeVisible()
})
