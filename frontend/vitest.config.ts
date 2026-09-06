import path from "node:path"
import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"

// Separate from vite.config.ts on purpose: that file's `build` block writes
// into backend/src/agent_control/static/admin and defines manualChunks for
// the shipped bundle - neither means anything to a test run, and importing
// it here would make `vitest` sensitive to production build config it has
// no business depending on.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
})
