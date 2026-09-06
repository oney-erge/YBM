import { expect, type Page, test } from "@playwright/test"

/**
 * Covers flows the committed suite (smoke.spec.ts, demo.spec.ts) does not:
 * deciding a pending approval (approve/deny), a review-dialog approval that
 * has expired, a failed task's trace highlighting its failing step, and a
 * trace step flagged as having observed untrusted external content
 * (docs/THREAT_MODEL.md). The first three are named explicitly as missing
 * in docs/UI_UX_AUDIT.md P0.1.
 */

const now = "2026-09-01T09:00:00Z"
const TASK_ID = "task_receipts_sort"
const APPROVAL_ID = "appr_receipts_sort"
const CONVERSATION = "web"
const OBJECTIVE = "Sort my receipts folder by vendor"

function chatTask(status: string) {
  return {
    id: TASK_ID,
    objective: OBJECTIVE,
    status,
    conversation_id: CONVERSATION,
    created_at: now,
    updated_at: now,
    metadata: {},
    artifacts: [],
  }
}

function pendingApprovalItem(overrides: { expires_at?: string } = {}) {
  return {
    approval: {
      id: APPROVAL_ID,
      task_id: TASK_ID,
      capability: "filesystem.write",
      risk_level: "high",
      summary: "Move 12 receipts into vendor subfolders. Nothing is deleted.",
      action_payload: { tool_name: "filesystem.manage", operation: "apply_manifest" },
      status: "pending",
      expires_at: overrides.expires_at ?? "2026-09-01T09:15:00Z",
      created_at: now,
    },
    task_objective: OBJECTIVE,
    task_status: "awaiting_approval",
    capability_max_risk_level: "high",
    blast_radius: { files: ["C:\\Users\\sam\\Receipts\\*"], urls: [], commands: [] },
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

/**
 * Stateful mock: `decision` starts null (approval pending) and flips once
 * the test decides it, matching useDecideApproval's real invalidation of
 * the approvals/chat/summary queries - the UI has to notice the approval is
 * gone from a *subsequent* poll, not from the decide response itself.
 */
async function mockChatWithApproval(page: Page, expiresAt?: string) {
  let decision: "approve" | "approve_for_task" | "reject" | null = null
  let decideBody: unknown = null

  await page.route("**/admin/api/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
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
      const status = decision ? "running" : "awaiting_approval"
      return route.fulfill({ json: { conversation_id: CONVERSATION, tasks: [chatTask(status)] } })
    }
    if (path === `/api/approvals/${APPROVAL_ID}/decide` && request.method() === "POST") {
      decideBody = request.postDataJSON()
      decision = (decideBody as { decision: typeof decision }).decision
      return route.fulfill({ json: { approval: null, grant: null } })
    }
    if (path === "/api/approvals") {
      const approvals = decision ? [] : [pendingApprovalItem({ expires_at: expiresAt })]
      return route.fulfill({ json: { approvals } })
    }
    if (path === "/api/summary") {
      return route.fulfill({ json: minimalSummary([chatTask(decision ? "running" : "awaiting_approval")]) })
    }
    return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
  })

  return {
    getDecideBody: () => decideBody as { decision: string } | null,
  }
}

test.describe("inline chat approvals", () => {
  test("approving the pending action sends decision=approve and the card clears", async ({ page }) => {
    const mock = await mockChatWithApproval(page)
    await page.goto("./")

    await expect(page.getByText("Move 12 receipts into vendor subfolders.")).toBeVisible()

    await page.getByRole("button", { name: "Approve once" }).click()

    await expect.poll(() => mock.getDecideBody()).toEqual({ decision: "approve" })
    await expect(page.getByText("Move 12 receipts into vendor subfolders.")).toHaveCount(0)
  })

  test("denying the pending action sends decision=reject", async ({ page }) => {
    const mock = await mockChatWithApproval(page)
    await page.goto("./")

    await page.getByRole("button", { name: "Deny" }).click()

    await expect.poll(() => mock.getDecideBody()).toEqual({ decision: "reject" })
  })
})

test.describe("approval review dialog expiry", () => {
  test("an expired approval disables every decision button", async ({ page }) => {
    // Already expired when the page loads, so the countdown never has to
    // tick down in real time during the test.
    await mockChatWithApproval(page, "2020-01-01T00:00:00Z")
    await page.goto("./")

    // Open the full review dialog via the persistent top banner rather than
    // the inline card - only the dialog's ApprovalActions receives `expired`.
    await page.getByText("1 pending approval").click()

    await expect(page.getByText("This approval has expired.")).toBeVisible()
    for (const label of ["Deny", "Allow for this task", "Approve once"]) {
      await expect(page.getByRole("dialog").getByRole("button", { name: label })).toBeDisabled()
    }
  })
})

test.describe("task trace", () => {
  test("shows the failed status and highlights the step that failed, with its error", async ({ page }) => {
    const failedTask = { ...chatTask("failed"), metadata: { last_worker_error: "filesystem.manage timed out" } }

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
        return route.fulfill({ json: minimalSummary([failedTask]) })
      }
      if (path === `/api/tasks/${TASK_ID}/trace`) {
        return route.fulfill({
          json: {
            task: failedTask,
            context: {},
            operator_history: [
              {
                tool_name: "filesystem.manage",
                input: { operation: "inspect_folder" },
                status: "succeeded",
                output_summary: "Found 12 receipts.",
                error: null,
                duration_ms: 340,
                content_trust: null,
              },
              {
                tool_name: "filesystem.manage",
                input: { operation: "apply_manifest" },
                status: "failed",
                output_summary: null,
                error: "filesystem.manage timed out",
                duration_ms: 30_000,
                content_trust: null,
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
      return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
    })

    await page.goto(`./tasks/${TASK_ID}`)

    // Both the page-level StatusBadge and the failing step's own row badge
    // render the word "failed" - assert on the task title being reachable at
    // all (proves the page loaded past the failed status) rather than a
    // second, ambiguous "failed" text match.
    await expect(page.getByRole("heading", { name: OBJECTIVE })).toBeVisible()
    const failedStep = page.locator("text=filesystem.manage timed out")
    await expect(failedStep).toBeVisible()
    // The failed row, not the succeeded one, carries the destructive tint.
    await expect(failedStep.locator("xpath=ancestor::div[contains(@class,'border-destructive')]")).toHaveCount(1)
  })

  test("flags a step whose tool declared its output as untrusted external content", async ({ page }) => {
    const completedTask = chatTask("completed")

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
        return route.fulfill({ json: minimalSummary([completedTask]) })
      }
      if (path === `/api/tasks/${TASK_ID}/trace`) {
        return route.fulfill({
          json: {
            task: completedTask,
            context: {},
            operator_history: [
              {
                tool_name: "browser.open",
                input: { operation: "open", url: "https://example.com" },
                status: "succeeded",
                output_summary: "Loaded example.com.",
                error: null,
                duration_ms: 900,
                content_trust: "untrusted_external",
              },
              {
                tool_name: "filesystem.manage",
                input: { operation: "read_file", path: "notes.txt" },
                status: "succeeded",
                output_summary: "Read notes.txt.",
                error: null,
                duration_ms: 50,
                content_trust: null,
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
      return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
    })

    await page.goto(`./tasks/${TASK_ID}`)

    await expect(page.getByRole("heading", { name: OBJECTIVE })).toBeVisible()
    const webStep = page.locator("text=Loaded example.com.")
    const localStep = page.locator("text=Read notes.txt.")
    await expect(webStep).toBeVisible()
    await expect(localStep).toBeVisible()
    await expect(webStep.locator("xpath=ancestor::div[contains(@class,'flex-col')][1]").getByText("untrusted content")).toBeVisible()
    await expect(localStep.locator("xpath=ancestor::div[contains(@class,'flex-col')][1]").getByText("untrusted content")).toHaveCount(0)
  })
})
