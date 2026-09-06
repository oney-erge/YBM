import { describe, expect, it } from "vitest"
import {
  ArtifactSchema,
  TaskRecordSchema,
  TaskStatusSchema,
} from "@/lib/api"

// These schemas are the one thing standing between a backend response and
// silently rendering `undefined` in the console (apiFetch's own comment:
// "surfacing this loudly is the whole point of parsing at the boundary").
// A schema drifting out of sync with the backend is invisible until
// something breaks in the browser - these tests fail in CI instead.

describe("TaskRecordSchema", () => {
  const validTask = {
    id: "task_abc123",
    objective: "Move invoices into Documents",
    status: "running",
    conversation_id: "conv_1",
    created_at: "2026-09-01T12:00:00Z",
    updated_at: "2026-09-01T12:05:00Z",
    metadata: { fulfillment_gap: null },
  }

  it("accepts a task record with no artifacts field", () => {
    // /api/tasks and /api/tasks/{id}/trace both omit this key on purpose.
    const result = TaskRecordSchema.safeParse(validTask)
    expect(result.success).toBe(true)
  })

  it("accepts a task record with artifacts, as chat endpoints send", () => {
    const result = TaskRecordSchema.safeParse({
      ...validTask,
      artifacts: [
        {
          id: "art_1",
          task_id: "task_abc123",
          type: "screenshot",
          uri: "/admin/api/artifacts/art_1",
          content_preview: null,
          metadata: {},
          created_at: "2026-09-01T12:04:00Z",
        },
      ],
    })
    expect(result.success).toBe(true)
  })

  it("rejects a status value outside the known enum", () => {
    const result = TaskRecordSchema.safeParse({ ...validTask, status: "in_progress" })
    expect(result.success).toBe(false)
  })

  it("rejects a task record missing a required field", () => {
    const { objective: _drop, ...withoutObjective } = validTask
    const result = TaskRecordSchema.safeParse(withoutObjective)
    expect(result.success).toBe(false)
  })
})

describe("ArtifactSchema", () => {
  it("accepts null uri and content_preview, both real backend states", () => {
    const result = ArtifactSchema.safeParse({
      id: "art_1",
      task_id: null,
      type: "text_log",
      uri: null,
      content_preview: null,
      metadata: {},
      created_at: "2026-09-01T12:00:00Z",
    })
    expect(result.success).toBe(true)
  })
})

describe("TaskStatusSchema", () => {
  it("carries every status value tests elsewhere assume exists", () => {
    for (const status of ["received", "running", "paused", "blocked", "completed", "failed", "cancelled"]) {
      expect(TaskStatusSchema.safeParse(status).success, status).toBe(true)
    }
  })
})
