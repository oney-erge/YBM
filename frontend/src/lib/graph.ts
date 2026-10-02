import type { ApprovalRequest, LLMCall, OperatorHistoryEntry, TaskTrace, ToolInvocation } from "@/lib/api"

export type GraphNodeStatus = "success" | "failure" | "pending" | "neutral"

export interface StepDetail {
  stepId: string
  decision?: LLMCall
  historyEntry?: OperatorHistoryEntry
  toolInvocations: ToolInvocation[]
  approval?: ApprovalRequest
}

export interface GraphNode {
  id: string
  kind: "query" | "step" | "final_answer"
  lane: number
  row: number
  label: string
  subtitle: string | null
  durationMs: number | null
  totalTokens: number | null
  status: GraphNodeStatus
  detail: StepDetail | { text: string } | null
}

export interface GraphEdge {
  id: string
  source: string
  target: string
}

const FAILED_STATUSES = new Set(["failed", "denied", "timeout"])

interface StepGroup {
  stepId: string
  order: number
  laneKey: string
  laneLabel: string
  decision?: LLMCall
  historyEntry?: OperatorHistoryEntry
  toolInvocations: ToolInvocation[]
  approval?: ApprovalRequest
}

function classifyOrigin(origin: string | undefined): { key: string; label: string } {
  if (!origin || origin === "operator") return { key: "operator", label: "Main sequence" }
  if (origin.startsWith("subagent:")) {
    return { key: origin, label: origin.includes("/parallel_batch:") ? "Sub-task - parallel batch" : "Delegated sub-task" }
  }
  if (origin.startsWith("parallel_batch:")) return { key: origin, label: "Parallel batch" }
  return { key: origin, label: origin }
}

function buildStepGroups(trace: TaskTrace): StepGroup[] {
  const groups = new Map<string, StepGroup>()

  function group(stepId: string): StepGroup {
    let existing = groups.get(stepId)
    if (!existing) {
      existing = { stepId, order: Number.POSITIVE_INFINITY, laneKey: "operator", laneLabel: "Main sequence", toolInvocations: [] }
      groups.set(stepId, existing)
    }
    return existing
  }

  for (const call of trace.llm_calls) {
    if (!call.step_id) continue
    const g = group(call.step_id)
    g.decision = call
    g.order = Math.min(g.order, new Date(call.created_at).getTime())
  }

  for (const invocation of trace.tool_invocations) {
    const stepId = invocation.request?.parent_step_id
    if (typeof stepId !== "string" || !stepId) continue
    const g = group(stepId)
    g.toolInvocations.push(invocation)
    g.order = Math.min(g.order, new Date(invocation.created_at).getTime())
    const origin = invocation.request?.origin
    const { key, label } = classifyOrigin(typeof origin === "string" ? origin : undefined)
    if (key !== "operator") {
      g.laneKey = key
      g.laneLabel = label
    }
  }

  for (const approval of trace.approvals) {
    const stepId = approval.action_payload.parent_step_id
    if (typeof stepId !== "string" || !stepId) continue
    group(stepId).approval = approval
  }

  for (const entry of trace.operator_history) {
    if (!entry.step_id) continue
    group(entry.step_id).historyEntry = entry
  }

  return [...groups.values()].sort((a, b) => a.order - b.order)
}

function stepStatus(g: StepGroup): GraphNodeStatus {
  if (g.toolInvocations.some((inv) => FAILED_STATUSES.has(inv.status))) return "failure"
  if (g.historyEntry && FAILED_STATUSES.has(g.historyEntry.status)) return "failure"
  if (g.toolInvocations.some((inv) => inv.status === "running" || inv.status === "needs_approval")) return "pending"
  if (g.toolInvocations.length > 0 || g.historyEntry) return "success"
  return "neutral"
}

function stepLabel(g: StepGroup): { label: string; subtitle: string | null } {
  const realTools = g.toolInvocations.filter((inv) => inv.status !== "needs_approval")
  if (realTools.length > 1) {
    return { label: `${realTools.length} tools`, subtitle: "ran in parallel" }
  }
  if (realTools.length === 1) {
    return { label: realTools[0].tool_name, subtitle: g.approval ? "approval required" : null }
  }
  const action = g.historyEntry?.tool_name ?? g.historyEntry?.status ?? g.decision?.source ?? "decision"
  return { label: action, subtitle: null }
}

function stepDuration(g: StepGroup): number | null {
  const real = g.toolInvocations.filter((inv) => inv.status !== "needs_approval" && inv.completed_at)
  if (real.length > 0) {
    const start = Math.min(...real.map((inv) => new Date(inv.created_at).getTime()))
    const end = Math.max(...real.map((inv) => new Date(inv.completed_at as string).getTime()))
    return Math.max(end - start, 0)
  }
  return g.decision?.latency_ms ?? null
}

/**
 * Graph v2 (docs/archive/UI_UX_AUDIT.md Phase 14f): rooted at the actual query,
 * one node per real step (docs/archive/UI_UX_AUDIT.md Phase 14e's step_id groups
 * that step's LLM decision, tool call(s), and approval gate together, since
 * they're one unit of "why did it do that"), chained chronologically within
 * a lane, with distinct lanes for parallel batches and delegated sub-tasks
 * (same lane technique the v1 graph used, over real steps instead of raw
 * tool_invocations). Not connected back to their spawning step in the main
 * lane - the same disclosed gap v1 had, since a delegated sub-task's own
 * step_ids aren't linked to the outer step_id that spawned it (only
 * ToolCallRequest.origin ties them together, informally).
 */
export function buildGraph(trace: TaskTrace): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const groups = buildStepGroups(trace)
  const lanes: string[] = []
  const laneLabels = new Map<string, string>()
  for (const g of groups) {
    if (!lanes.includes(g.laneKey)) lanes.push(g.laneKey)
    laneLabels.set(g.laneKey, g.laneLabel)
  }
  lanes.sort((a, b) => (a === "operator" ? -1 : b === "operator" ? 1 : 0))

  const nodes: GraphNode[] = []
  const edges: GraphEdge[] = []

  nodes.push({
    id: "query",
    kind: "query",
    lane: 0,
    row: 0,
    label: "Query",
    subtitle: trace.task.objective,
    durationMs: null,
    totalTokens: null,
    status: "neutral",
    detail: { text: trace.task.objective },
  })

  const laneRowOffset = new Map<string, number>()
  for (const laneKey of lanes) laneRowOffset.set(laneKey, laneKey === "operator" ? 1 : 0)

  let previousInLane = new Map<string, string>()
  previousInLane.set("operator", "query")

  for (const g of groups) {
    const laneIndex = lanes.indexOf(g.laneKey)
    const row = laneRowOffset.get(g.laneKey) ?? 0
    laneRowOffset.set(g.laneKey, row + 1)
    const { label, subtitle } = stepLabel(g)
    const totalTokens = g.decision?.total_tokens ?? null
    nodes.push({
      id: g.stepId,
      kind: "step",
      lane: laneIndex,
      row,
      label,
      subtitle,
      durationMs: stepDuration(g),
      totalTokens,
      status: stepStatus(g),
      detail: { stepId: g.stepId, decision: g.decision, historyEntry: g.historyEntry, toolInvocations: g.toolInvocations, approval: g.approval },
    })
    const previous = previousInLane.get(g.laneKey)
    if (previous) {
      edges.push({ id: `${previous}->${g.stepId}`, source: previous, target: g.stepId })
    }
    previousInLane.set(g.laneKey, g.stepId)
  }

  const finalAnswer = trace.task.metadata.synthesized_answer
  if (typeof finalAnswer === "string" && finalAnswer.trim()) {
    const lastMain = previousInLane.get("operator") ?? "query"
    const row = laneRowOffset.get("operator") ?? 1
    nodes.push({
      id: "final_answer",
      kind: "final_answer",
      lane: 0,
      row,
      label: "Answer",
      subtitle: null,
      durationMs: null,
      totalTokens: null,
      status: "success",
      detail: { text: finalAnswer },
    })
    edges.push({ id: `${lastMain}->final_answer`, source: lastMain, target: "final_answer" })
  }

  return { nodes, edges }
}
