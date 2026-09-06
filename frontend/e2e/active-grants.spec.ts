import { expect, type Page, test } from "@playwright/test"

/**
 * Active grants visibility and revocation (docs/ROADMAP.md "scoped
 * temporary authority") - previously created silently with no way to see
 * or revoke one short of querying the database directly.
 */

const GRANT_ID = "grant_downloads_sort"

function grantItem(overrides: { operations_used?: number; scope?: string | null } = {}) {
  return {
    grant: {
      id: GRANT_ID,
      task_id: "task_downloads_sort",
      tool_name: "filesystem.manage",
      capability: "filesystem.write",
      granted_from_approval_id: "appr_1",
      created_at: "2026-09-01T09:00:00Z",
      expires_at: "2026-09-01T09:30:00Z",
      scope: overrides.scope ?? "C:/Users/sam/Downloads",
      max_operations: 200,
      operations_used: overrides.operations_used ?? 3,
      revoked: false,
    },
    task_objective: "Organize my Downloads folder by type",
    task_status: "running",
  }
}

async function mockAccessPage(page: Page, getGrants: () => ReturnType<typeof grantItem>[]) {
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
      return route.fulfill({ json: { grants: getGrants() } })
    }
    if (path.startsWith("/api/grants/") && path.endsWith("/revoke") && route.request().method() === "POST") {
      return route.fulfill({ json: { grant: { ...grantItem().grant, revoked: true }, revoked: true } })
    }
    return route.fulfill({ status: 404, json: { detail: `no e2e mock for ${path}` } })
  })
}

test("shows an active grant's scope, usage, and expiry", async ({ page }) => {
  await mockAccessPage(page, () => [grantItem()])
  await page.goto("./access")

  await expect(page.getByText("Active grants")).toBeVisible()
  await expect(page.getByText("filesystem.manage")).toBeVisible()
  await expect(page.getByText("Organize my Downloads folder by type")).toBeVisible()
  await expect(page.getByText("3/200 used")).toBeVisible()
  await expect(page.getByText("scope: C:/Users/sam/Downloads")).toBeVisible()
})

test("shows nothing pending when there are no active grants", async ({ page }) => {
  await mockAccessPage(page, () => [])
  await page.goto("./access")

  // Not asserting on "Active grants" here too: that heading text is a
  // substring (case-insensitively) of the empty-state copy just below it,
  // which getByText would then resolve ambiguously - the other test
  // already covers the heading being real.
  await expect(page.getByText("No active grants right now.")).toBeVisible()
})

test("revoking a grant removes it from the list after confirming", async ({ page }) => {
  let revoked = false
  await mockAccessPage(page, () => (revoked ? [] : [grantItem()]))
  await page.route("**/admin/api/grants/*/revoke", async (route) => {
    revoked = true
    return route.fulfill({ json: { grant: { ...grantItem().grant, revoked: true }, revoked: true } })
  })
  await page.goto("./access")

  await page.getByRole("button", { name: "Revoke" }).click()
  await page.getByRole("button", { name: "Revoke", exact: true }).last().click()

  await expect(page.getByText("No active grants right now.")).toBeVisible()
})
