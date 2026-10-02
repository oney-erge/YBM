import type { TaskTrace } from "@/lib/api"

export type DurationSegmentKind = "tool" | "approval_wait" | "llm" | "inferred_think"
export type DurationOutcome = "success" | "failure" | "pending"

export interface DurationSegment {
  key: string
  kind: DurationSegmentKind
  label: string
  startMs: number
  durationMs: number
  outcome: DurationOutcome
}

const FAILED_STATUSES = new Set(["failed", "denied", "timeout"])

// Gaps smaller than this are scheduling/poll-loop noise, not meaningful
// "operator thinking" time worth its own row (docs/archive/UI_UX_AUDIT.md Phase 14).
const MIN_INFERRED_GAP_MS = 250

interface RealSegment {
  key: string
  kind: "tool" | "approval_wait" | "llm"
  label: string
  start: number
  end: number
  outcome: DurationOutcome
}

/**
 * The duration/Gantt view's data, built from what the trace response
 * carries: tool_invocations' own created_at/completed_at, approval wait
 * windows recovered by pairing the timeline's "approval requested"/
 * "approval decided" audit events by approval_id, and - since Phase 14d -
 * llm_calls' real per-call latency.
 *
 * Real, measured work (tool calls, approval waits, LLM calls) is placed
 * first; any gap left between them - or before the first / after the last -
 * is rendered as an inferred "operator thinking" segment, since that time is
 * real but not covered by any of the three measured sources (e.g. a source
 * not yet instrumented for LLM-call persistence, network/queueing overhead
 * around a call, or a task run before this shipped).
 */
export function buildDurationSegments(trace: TaskTrace): { segments: DurationSegment[]; totalMs: number } {
  const taskStart = new Date(trace.task.created_at).getTime()
  const taskEnd = Math.max(new Date(trace.task.updated_at).getTime(), taskStart)

  const real: RealSegment[] = []

  for (const invocation of trace.tool_invocations) {
    // A "needs_approval" row is the policy check that triggered a gate, not
    // real work - it completes almost instantly and the approval_wait
    // segment below already represents what actually took time here.
    if (invocation.status === "needs_approval") continue
    const start = new Date(invocation.created_at).getTime()
    const end = invocation.completed_at ? new Date(invocation.completed_at).getTime() : start
    real.push({
      key: `tool:${invocation.id}`,
      kind: "tool",
      label: invocation.tool_name,
      start,
      end: Math.max(end, start),
      outcome: FAILED_STATUSES.has(invocation.status) ? "failure" : invocation.status === "succeeded" ? "success" : "pending",
    })
  }

  const requestedAt = new Map<string, number>()
  for (const item of trace.timeline) {
    if (item.category !== "approval" || !item.at) continue
    const details = item.details ?? {}
    const approvalId = typeof details.approval_id === "string" ? details.approval_id : null
    if (!approvalId) continue
    const at = new Date(item.at).getTime()
    if ("decision" in details) {
      const start = requestedAt.get(approvalId)
      if (start != null) {
        real.push({ key: `approval:${approvalId}`, kind: "approval_wait", label: "Approval wait", start, end: Math.max(at, start), outcome: "pending" })
      }
    } else {
      requestedAt.set(approvalId, at)
    }
  }

  for (const call of trace.llm_calls) {
    const start = new Date(call.created_at).getTime()
    const end = call.latency_ms != null ? start + call.latency_ms : start
    real.push({
      key: `llm:${call.id}`,
      kind: "llm",
      label: `LLM (${call.source})`,
      start,
      end: Math.max(end, start),
      outcome: "pending",
    })
  }

  real.sort((a, b) => a.start - b.start)

  const segments: DurationSegment[] = []
  let cursor = taskStart
  for (const item of real) {
    if (item.start - cursor >= MIN_INFERRED_GAP_MS) {
      segments.push({
        key: `think:${item.key}`,
        kind: "inferred_think",
        label: "Operator thinking (inferred)",
        startMs: cursor - taskStart,
        durationMs: item.start - cursor,
        outcome: "pending",
      })
    }
    segments.push({
      key: item.key,
      kind: item.kind,
      label: item.label,
      startMs: item.start - taskStart,
      durationMs: item.end - item.start,
      outcome: item.outcome,
    })
    cursor = Math.max(cursor, item.end)
  }
  if (taskEnd - cursor >= MIN_INFERRED_GAP_MS) {
    segments.push({
      key: "think:final",
      kind: "inferred_think",
      label: "Operator thinking (inferred)",
      startMs: cursor - taskStart,
      durationMs: taskEnd - cursor,
      outcome: "pending",
    })
  }

  return { segments, totalMs: Math.max(taskEnd - taskStart, 1) }
}
