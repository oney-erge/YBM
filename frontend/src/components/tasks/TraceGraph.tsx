import { useMemo, useState } from "react"
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { CheckCircle2, MessageSquare, ShieldAlert, Wrench } from "lucide-react"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import type { TaskTrace } from "@/lib/api"
import { buildGraph, type GraphNode, type GraphNodeStatus, type StepDetail } from "@/lib/graph"
import { formatDurationMs } from "@/lib/time"
import { cn } from "@/lib/utils"

/**
 * Graph v2 (docs/archive/UI_UX_AUDIT.md Phase 14f) - rooted at the actual query
 * (not just tool calls), one node per real step (docs/archive/UI_UX_AUDIT.md
 * Phase 14e's step_id), with duration/token badges, a status ring, and a
 * click-to-inspect panel showing the exact prompt sent, the raw response,
 * tool input/output, and the approval gate, if any. lib/graph.ts owns the
 * lane/grouping logic; this component is display + the inspect dialog only.
 */
const LANE_WIDTH = 260
const ROW_HEIGHT = 96

const STATUS_BORDER: Record<GraphNodeStatus, string> = {
  success: "var(--success)",
  failure: "var(--destructive)",
  pending: "var(--warning)",
  neutral: "var(--border)",
}

// Hoisted to module scope - React Flow re-registers custom node types
// whenever this object's reference changes, so an inline literal on every
// render would defeat its own memoization.
const NODE_TYPES = { default: GraphNodeCard }

export function TraceGraph({ trace }: { trace: TaskTrace }) {
  const { nodes: graphNodes, edges: graphEdges } = useMemo(() => buildGraph(trace), [trace])
  const [selected, setSelected] = useState<GraphNode | null>(null)

  if (graphNodes.length <= 1) {
    // Only the root Query node built - either nothing ran yet, or this task
    // predates step_id linking (docs/archive/UI_UX_AUDIT.md Phase 14e) and its real
    // tool calls exist but aren't linkable here; either way, Steps/Timeline
    // still show what happened.
    const message = trace.tool_invocations.length > 0
      ? "This task's steps predate the graph's step linking - see Steps or Timeline instead."
      : "No steps recorded for this task."
    return <p className="text-sm text-muted-foreground">{message}</p>
  }

  const nodes: Node[] = graphNodes.map((node) => ({
    id: node.id,
    position: { x: node.lane * LANE_WIDTH, y: node.row * ROW_HEIGHT },
    data: { graphNode: node },
    style: {
      border: `1px solid ${STATUS_BORDER[node.status]}`,
      background: "var(--card)",
      borderRadius: 8,
      padding: 8,
      width: LANE_WIDTH - 40,
    },
  }))
  const edges: Edge[] = graphEdges.map((edge) => ({ id: edge.id, source: edge.source, target: edge.target }))

  return (
    <div className="h-96 w-full overflow-hidden rounded-md border border-border">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_event, node) => setSelected(node.data.graphNode as GraphNode)}
        nodeTypes={NODE_TYPES}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
      <NodeInspectDialog node={selected} onOpenChange={(open) => !open && setSelected(null)} />
    </div>
  )
}

function GraphNodeCard({ data }: { data: { graphNode: GraphNode } }) {
  const node = data.graphNode
  const Icon = node.kind === "query" ? MessageSquare : node.kind === "final_answer" ? CheckCircle2 : Wrench
  const failed = node.status === "failure"
  return (
    <div className="flex cursor-pointer flex-col gap-0.5 text-left">
      <div className="flex items-center gap-1.5">
        <Icon className={cn("size-3 shrink-0", failed ? "text-destructive" : "text-muted-foreground")} />
        <span className="truncate font-mono text-xs">{node.label}</span>
        {node.detail && "approval" in node.detail && node.detail.approval && (
          <ShieldAlert className="size-3 shrink-0 text-warning" />
        )}
      </div>
      {node.subtitle && <span className="truncate text-[10px] text-muted-foreground">{node.subtitle}</span>}
      <div className="flex items-center gap-1.5">
        {node.durationMs != null && (
          <span className="text-[10px] text-muted-foreground">{formatDurationMs(node.durationMs)}</span>
        )}
        {node.totalTokens != null && (
          <span className="text-[10px] text-muted-foreground">· {node.totalTokens} tok</span>
        )}
      </div>
    </div>
  )
}

function NodeInspectDialog({ node, onOpenChange }: { node: GraphNode | null; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={node != null} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{node?.label}</DialogTitle>
          <DialogDescription>{node?.kind === "step" ? "What happened at this step" : node?.subtitle}</DialogDescription>
        </DialogHeader>
        {node && <NodeInspectBody node={node} />}
      </DialogContent>
    </Dialog>
  )
}

function NodeInspectBody({ node }: { node: GraphNode }) {
  if (!node.detail) return null
  if ("text" in node.detail) {
    return <p className="max-h-96 overflow-auto whitespace-pre-wrap text-sm [overflow-wrap:anywhere]">{node.detail.text}</p>
  }
  const detail = node.detail as StepDetail
  return (
    <div className="flex max-h-[70vh] flex-col gap-4 overflow-auto text-sm">
      {detail.decision && (
        <section className="flex flex-col gap-1.5">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            LLM call · {detail.decision.source}
            {detail.decision.model && ` · ${detail.decision.model}`}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {detail.decision.latency_ms != null && <Badge variant="outline">{formatDurationMs(detail.decision.latency_ms)}</Badge>}
            {detail.decision.total_tokens != null && <Badge variant="outline">{detail.decision.total_tokens} tokens</Badge>}
          </div>
          <pre className="max-h-40 overflow-auto rounded-md bg-muted p-2.5 font-mono text-[11px]">
            {JSON.stringify(detail.decision.messages, null, 2)}
          </pre>
          {detail.decision.response_text && (
            <p className="whitespace-pre-wrap rounded-md bg-muted p-2.5 font-mono text-[11px] [overflow-wrap:anywhere]">
              {detail.decision.response_text}
            </p>
          )}
        </section>
      )}
      {detail.toolInvocations.map((invocation) => (
        <section key={invocation.id} className="flex flex-col gap-1.5">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Tool · {invocation.tool_name} · {invocation.status}
          </p>
          <pre className="max-h-40 overflow-auto rounded-md bg-muted p-2.5 font-mono text-[11px]">
            {JSON.stringify(invocation.request?.input ?? {}, null, 2)}
          </pre>
          {invocation.result && (
            <pre className="max-h-40 overflow-auto rounded-md bg-muted p-2.5 font-mono text-[11px]">
              {JSON.stringify(invocation.result, null, 2)}
            </pre>
          )}
        </section>
      ))}
      {detail.approval && (
        <section className="flex flex-col gap-1.5">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Approval gate</p>
          <p className="text-xs [overflow-wrap:anywhere]">{detail.approval.summary}</p>
          <div className="flex flex-wrap gap-1.5">
            <Badge variant="outline">{detail.approval.capability}</Badge>
            <Badge variant="outline">{detail.approval.risk_level}</Badge>
            <Badge variant={detail.approval.status === "approved" ? "secondary" : "outline"}>{detail.approval.status}</Badge>
          </div>
        </section>
      )}
      {detail.historyEntry?.error && (
        <section className="flex flex-col gap-1.5">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Error</p>
          <p className="text-xs text-destructive [overflow-wrap:anywhere]">{detail.historyEntry.error}</p>
        </section>
      )}
    </div>
  )
}
