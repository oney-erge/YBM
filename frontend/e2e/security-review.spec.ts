import { expect, type Page, test } from "@playwright/test"

/**
 * Security review (docs/ROADMAP.md "Finish the Proof"): a page, not a CLI
 * command, reporting this machine's actual exposure - network reachability,
 * admin token presence, live grants, capabilities that need no approval,
 * MCP servers, external hosts contacted, and unsandboxed code execution.
 */

function review(overrides: Record<string, unknown> = {}) {
  return {
    window_days: 7,
    network: {
      host: "127.0.0.1",
      port: 8765,
      reachable_beyond_this_machine: false,
      admin_enabled: true,
      admin_token_set: true,
    },
    active_grants: [
      {
        id: "grant_1",
        task_id: "task_1",
        tool_name: "filesystem.manage",
        capability: "filesystem.write",
        scope: "C:\\Users\\sam\\Downloads",
        expires_at: "2026-09-01T10:00:00Z",
        operations_used: 3,
        max_operations: 200,
      },
    ],
    capability_access: { enabled: 6, no_approval_required: ["telegram.receive"] },
    mcp_servers: [{ name: "fake", enabled: true, command: "npx", risk_level: "low" }],
    external_hosts_contacted: ["example.com"],
    code_execution: { sandboxed_runs: 4, unsandboxed_runs: 0 },
    ...overrides,
  }
}

async function mockSecurityReviewPage(page: Page, getReview: () => ReturnType<typeof review>) {
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
    if (path === "/api/security-review") {
      return route.fulfill({ json: getReview() })
    }
    return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
  })
}

test("shows network exposure, active grants, and unrestricted capabilities", async ({ page }) => {
  await mockSecurityReviewPage(page, () => review())
  await page.goto("./security-review")

  await expect(page.getByText("loopback only")).toBeVisible()
  await expect(page.getByRole("cell", { name: "filesystem.manage" })).toBeVisible()
  await expect(page.getByText("telegram.receive")).toBeVisible()
  await expect(page.getByText("example.com")).toBeVisible()
  await expect(page.getByText("fake")).toBeVisible()
})

test("flags a non-loopback bind and unsandboxed execution", async ({ page }) => {
  await mockSecurityReviewPage(page, () =>
    review({
      network: {
        host: "0.0.0.0",
        port: 8765,
        reachable_beyond_this_machine: true,
        admin_enabled: true,
        admin_token_set: false,
      },
      code_execution: { sandboxed_runs: 2, unsandboxed_runs: 3 },
    }),
  )
  await page.goto("./security-review")

  await expect(page.getByText("reachable beyond this machine")).toBeVisible()
  await expect(page.getByText("Admin token: not set")).toBeVisible()
  await expect(page.getByText("3", { exact: true })).toBeVisible()
})

test("is reachable from the Access page", async ({ page }) => {
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
    if (path === "/api/config/effective") {
      return route.fulfill({ json: { config: { capabilities: {} }, access_modes: {}, warnings: [] } })
    }
    if (path === "/api/secrets") {
      return route.fulfill({ json: { available: true, key_env: "AGENT_SECRET_VAULT_KEY", services: {} } })
    }
    if (path === "/api/grants") {
      return route.fulfill({ json: { grants: [] } })
    }
    if (path === "/api/security-review") {
      return route.fulfill({ json: review() })
    }
    return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
  })

  await page.goto("./access")
  await page.getByRole("link", { name: "Security review" }).click()

  await expect(page).toHaveURL(/\/security-review$/)
  await expect(page.getByRole("heading", { name: "Security review" })).toBeVisible()
})
