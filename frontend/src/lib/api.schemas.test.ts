import { describe, expect, it } from "vitest"
import {
  ArtifactSchema,
  BootstrapResponseSchema,
  DecisionConfigSchema,
  DecisionTestSchema,
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

describe("BootstrapResponseSchema", () => {
  const base = { token_required: true, onboarding_complete: true, llm_reachable: true, version: "0.1.4" }

  it("reads whether this browser is already signed in", () => {
    const result = BootstrapResponseSchema.parse({ ...base, authenticated: false })
    expect(result.authenticated).toBe(false)
  })

  it("treats a backend that predates the field as signed in, never as locked out", () => {
    // Defaulting to false here would put the token screen in front of everyone
    // who runs a console newer than their backend.
    const result = BootstrapResponseSchema.parse(base)
    expect(result.authenticated).toBe(true)
  })
})

describe("DecisionConfigSchema", () => {
  // The shape admin_get_decision_config returns (backend/src/agent_control/admin.py).
  const body = {
    decision: {
      mode: "shadow",
      base_url: "http://127.0.0.1:8000",
      profile: null,
      api_token_env: null,
      timeout_seconds: 10,
      min_confidence: 0.9,
      cascade_routes: ["filesystem.manage"],
      cooldown_seconds: 60,
    },
    routes: { conversation: "chat, thanks, general knowledge, questions about the bot" },
    concierge_only_routes: ["coding.agent"],
    stats: {
      messages: 2,
      decided_alone: 0,
      unanswered: 0,
      compared: 2,
      agreed: 1,
      agreement: 0.5,
      median_ms: 640,
      recent_disagreements: [
        { text: "is report.pdf in Downloads?", decision_route: "conversation", decision_confidence: 0.6, llm_route: "filesystem.manage" },
      ],
    },
  }

  it("reads the saved choice and what has been recorded", () => {
    const result = DecisionConfigSchema.parse(body)
    expect(result.decision.mode).toBe("shadow")
    expect(result.stats.agreement).toBe(0.5)
  })

  it("reads a test call with an answer, or with the reason there is none", () => {
    const answered = DecisionTestSchema.parse({
      text: "hi",
      mode: "decision",
      decision: { route: "conversation", confidence: 0.98, top3: { conversation: 0.98 }, latency_ms: 640, decides_alone: true },
      llm: { route: "conversation", confidence: 0.97, latency_ms: 15000 },
    })
    expect("route" in answered.decision && answered.decision.decides_alone).toBe(true)
    const failed = DecisionTestSchema.parse({ text: "hi", mode: "off", decision: { error: "decision model unreachable" } })
    expect("error" in failed.decision).toBe(true)
  })
})
