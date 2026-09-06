import { describe, expect, it } from "vitest"
import { CANCELLABLE, PAUSABLE, RESUMABLE } from "@/lib/task-signals"
import { TaskStatusSchema, type TaskStatus } from "@/lib/api"

const ALL_STATUSES = TaskStatusSchema.options

describe("task status → action gating", () => {
  it("only running/retrying tasks can be paused", () => {
    expect(PAUSABLE).toEqual(new Set<TaskStatus>(["running", "retrying"]))
  })

  it("only a paused task can be resumed", () => {
    expect(RESUMABLE).toEqual(new Set<TaskStatus>(["paused"]))
  })

  // Matches lib/chat.ts's own terminal grouping (completed/failed/blocked/
  // cancelled) - "blocked" is a done state (needs a human, not a pause), not
  // a live one.
  const TERMINAL: TaskStatus[] = ["completed", "failed", "blocked", "cancelled"]

  it("a terminal status is never pausable, resumable, or cancellable", () => {
    // Mirrors admin_streamlit.py's _action_disabled: once a task has
    // finished, none of these actions apply - if a new terminal status is
    // ever added to TaskStatusSchema without updating these sets, this is
    // the test that should fail.
    for (const status of TERMINAL) {
      expect(PAUSABLE.has(status)).toBe(false)
      expect(RESUMABLE.has(status)).toBe(false)
      expect(CANCELLABLE.has(status)).toBe(false)
    }
  })

  it("every schema-known status is classified as either cancellable or terminal", () => {
    // Not a tautology: this fails the moment a new TaskStatus is added to
    // the backend enum and forgotten here, instead of silently leaving the
    // new status un-cancellable with no test noticing.
    const terminal = new Set<TaskStatus>(TERMINAL)
    for (const status of ALL_STATUSES) {
      const known = CANCELLABLE.has(status) || terminal.has(status)
      expect(known, `unclassified TaskStatus: ${status}`).toBe(true)
    }
  })
})
