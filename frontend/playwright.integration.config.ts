import path from "node:path"
import { defineConfig, devices } from "@playwright/test"

const python = path.resolve("../backend/.venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
const server = path.resolve("../backend/tests/project_ui_server.py")

export default defineConfig({
  testDir: "./integration",
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:4181/admin/",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `"${python}" "${server}"`,
    url: "http://127.0.0.1:4181/admin/api/bootstrap",
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
