import { expect, test } from "@playwright/test"

test("decision preference is saved without changing access policy", async ({ page }) => {
  await page.goto("./settings?token=ybm-isolated-browser-test")
  const card = page.locator("[data-slot=card]").filter({ hasText: "Decision preference" })
  await expect(card.getByText("3 - Balanced")).toBeVisible()
  const before = await (await page.request.get("/admin/api/config/effective")).json()
  await card.getByRole("combobox").click()
  await page.getByRole("option", { name: "4 - Proactive" }).click()
  await card.getByRole("button", { name: "Save preference" }).click()
  await expect(page.getByText("Decision preference saved.")).toBeVisible()
  await page.reload()
  await expect(card.getByText("4 - Proactive")).toBeVisible()
  const after = await (await page.request.get("/admin/api/config/effective")).json()
  expect(after.config.capabilities).toEqual(before.config.capabilities)
  expect(after.config.approval_policy).toEqual(before.config.approval_policy)
})
