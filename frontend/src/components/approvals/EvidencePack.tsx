import { useState, type ReactNode } from "react"
import { Link } from "react-router"
import { AlertTriangle, ChevronDown, ChevronRight, ExternalLink, FileText, Globe, TerminalSquare } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import type { PendingApprovalItem } from "@/lib/api"
import { RISK_ORDER } from "@/lib/api"
import { describeCapability } from "@/lib/capability"
import { AdapterReviewPanel } from "@/components/approvals/AdapterReviewPanel"

/**
 * The approval decision surface (docs/UI_REWRITE_PLAN.md §11.2), ordered
 * for a sub-15-second decision per the 2026 human-in-the-loop research this
 * plan cites: Why -> What -> Exactly what -> Blast radius -> Capability
 * -> Authority. Never a bare "Approve?" button.
 *
 * "Capability" (not "Reversibility"): describeCapability() deliberately
 * never claims whether a specific action can be undone (lib/capability.ts) -
 * a real Recovery field (fully/partially/not reversible) needs rollback
 * metadata this system doesn't compute yet, and a mislabeled field promising
 * that with false confidence is worse than not promising it.
 *
 * Phase 8 rework: two columns on wide screens so the decision-critical half
 * (why / what / what it touches) is never pushed below the fold by a long
 * parameter blob, and the raw JSON collapses by default behind a plain
 * count. `actions` is a slot rather than fixed buttons so the review dialog
 * can pin them to a sticky footer while Chat keeps them inline - same
 * buttons either way (ApprovalActions), no duplicated decision logic.
 */
export function EvidencePack({
  item,
  actions,
  footer,
}: {
  item: PendingApprovalItem
  actions?: ReactNode
  footer?: ReactNode
}) {
  const { approval, blast_radius: blastRadius } = item
  const [showRaw, setShowRaw] = useState(false)
  const operation = (approval.action_payload.input as { operation?: string } | undefined)?.operation
  const toolName = (approval.action_payload.tool_name as string | undefined) ?? "unknown tool"
  const exceedsCeiling =
    item.capability_max_risk_level != null &&
    RISK_ORDER[approval.risk_level] > RISK_ORDER[item.capability_max_risk_level]
  const touched = [
    ...blastRadius.files.map((value) => ({ kind: "file" as const, value })),
    ...blastRadius.urls.map((value) => ({ kind: "url" as const, value })),
    ...blastRadius.commands.map((value) => ({ kind: "command" as const, value })),
  ]
  const input = approval.action_payload.input ?? {}
  const inputKeys = Object.keys(input as Record<string, unknown>)
  const adapterDir =
    toolName === "adapter.factory" && operation === "promote_after_approval"
      ? (input as { adapter_dir?: string }).adapter_dir
      : undefined

  return (
    <div className="flex flex-col gap-4 text-sm">
      <div className="grid gap-4 md:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-4">
          <Field label="Why">
            <p className="leading-6 [overflow-wrap:anywhere]">{approval.summary}</p>
          </Field>

          <Field label="What">
            <p className="[overflow-wrap:anywhere]">
              <span className="font-mono text-xs">{toolName}</span>
              {operation && <span className="text-muted-foreground"> · {operation}</span>}
            </p>
          </Field>

          <Field label={`What it touches${touched.length > 0 ? ` (${touched.length})` : ""}`}>
            {touched.length > 0 ? (
              <div className="flex flex-col gap-1">
                {touched.map((entry) => (
                  <BlastRadiusRow key={`${entry.kind}:${entry.value}`} kind={entry.kind} value={entry.value} />
                ))}
              </div>
            ) : (
              <p className="text-muted-foreground">
                No specific files, URLs, or commands detected in the parameters.
              </p>
            )}
          </Field>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Field label="Authority">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={exceedsCeiling ? "destructive" : "secondary"}>{approval.risk_level} risk</Badge>
              <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                <span className="font-mono">{approval.capability}</span>
                {item.capability_max_risk_level && ` · ceiling: ${item.capability_max_risk_level}`}
              </span>
            </div>
            {exceedsCeiling && (
              <p className="mt-1.5 flex items-start gap-1 text-xs text-destructive">
                <AlertTriangle className="mt-px size-3.5 shrink-0" />
                This risk level exceeds the configured ceiling for this capability.
              </p>
            )}
            <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{describeCapability(approval.capability)}</p>
          </Field>

          <Field label="Exact parameters">
            <button
              type="button"
              onClick={() => setShowRaw((v) => !v)}
              className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              {showRaw ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
              {showRaw ? "Hide" : "Show"} {inputKeys.length} parameter{inputKeys.length === 1 ? "" : "s"}
              {!showRaw && inputKeys.length > 0 && (
                <span className="font-mono font-normal text-muted-foreground">
                  {" "}
                  ({inputKeys.slice(0, 3).join(", ")}
                  {inputKeys.length > 3 ? ", ..." : ""})
                </span>
              )}
            </button>
            {showRaw && (
              <pre className="mt-1.5 max-h-56 overflow-auto rounded-md bg-muted p-3 font-mono text-xs">
                {JSON.stringify(input, null, 2)}
              </pre>
            )}
          </Field>

          <Field label="Task">
            <Link
              to={`/tasks/${approval.task_id}`}
              className="flex items-start gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <span className="line-clamp-2 [overflow-wrap:anywhere]">
                {item.task_objective ?? approval.task_id}
              </span>
              <ExternalLink className="mt-0.5 size-3.5 shrink-0" />
            </Link>
          </Field>
        </div>
      </div>

      {adapterDir && <AdapterReviewPanel adapterDir={adapterDir} />}

      {footer}
      {actions}
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
      {children}
    </div>
  )
}

const BLAST_ICON = { file: FileText, url: Globe, command: TerminalSquare }

function BlastRadiusRow({ kind, value }: { kind: "file" | "url" | "command"; value: string }) {
  const Icon = BLAST_ICON[kind]
  return (
    <div className="flex items-start gap-2 rounded-md bg-muted/60 px-2 py-1.5">
      <Icon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 font-mono text-xs [overflow-wrap:anywhere]" title={value}>
        {value}
      </span>
    </div>
  )
}
