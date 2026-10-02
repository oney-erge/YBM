import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { EvidencePack } from "@/components/approvals/EvidencePack"
import { ApprovalActions } from "@/components/approvals/ApprovalActions"
import { ApiError, type PendingApprovalItem } from "@/lib/api"
import { useDecideApproval } from "@/lib/queries"

/**
 * Approvals directly in Chat instead of only behind the Review dialog
 * (docs/archive/UI_UX_AUDIT.md Phase 1): most decisions should take under ten
 * seconds - Deny / Allow for this task / Approve once, right where the
 * task already is. "Review details" expands the same EvidencePack used in
 * the full dialog for the rare case that needs it; nothing about the
 * decision logic is duplicated.
 *
 * No "Always allow" here on purpose - that needs a revocation list to be
 * safe, which doesn't exist yet (docs/archive/UI_UX_AUDIT.md's explicit scope-down).
 */
export function InlineApproval({ item }: { item: PendingApprovalItem }) {
  const [expanded, setExpanded] = useState(false)
  const decide = useDecideApproval()

  function handleDecide(decision: "approve" | "approve_for_task" | "reject") {
    decide.mutate(
      { approvalId: item.approval.id, decision },
      {
        onError: (error) => {
          toast.error(error instanceof ApiError ? error.message : "Could not record the decision.")
        },
      },
    )
  }

  const risk = item.approval.risk_level
  const toolName = (item.approval.action_payload.tool_name as string | undefined) ?? "unknown tool"
  // A batch approval stands for several calls. Showing the count without the
  // calls would be asking someone to approve a number, so the list is rendered
  // inline rather than hidden behind "Review details".
  const batch = Array.isArray(item.approval.action_payload.batch)
    ? (item.approval.action_payload.batch as { tool_name?: string; risk_level?: string; input?: unknown }[])
    : []

  if (expanded) {
    return (
      <div className="mt-2.5 rounded-lg border border-warning/30 bg-warning/5 p-3">
        <EvidencePack
          item={item}
          actions={
            <ApprovalActions
              size="sm"
              deciding={decide.isPending}
              riskLevel={risk}
              toolName={toolName}
              onApprove={() => handleDecide("approve")}
              onApproveForTask={() => handleDecide("approve_for_task")}
              onDeny={() => handleDecide("reject")}
            />
          }
        />
      </div>
    )
  }

  return (
    <div className="mt-2.5 flex flex-col gap-2 rounded-lg border border-warning/30 bg-warning/5 p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {/* Risk is visible before any click, not only after expanding to the
            full Evidence Pack - a collapsed card with no severity indicator
            let a critical-risk approval look identical to a low-risk one. */}
        <Badge variant={risk === "critical" || risk === "high" ? "destructive" : "secondary"}>{risk} risk</Badge>
        <p className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">{toolName}</p>
      </div>
      <p className="text-xs">{item.approval.summary}</p>
      {batch.length > 0 && (
        <ol className="max-h-52 overflow-y-auto rounded-md border border-border/60 bg-background/60 text-[11px]">
          {batch.map((call, index) => (
            <li
              key={index}
              className="flex items-start gap-2 border-b border-border/40 px-2 py-1.5 last:border-b-0"
            >
              <span className="shrink-0 tabular-nums text-muted-foreground">{index + 1}.</span>
              <span className="min-w-0 flex-1">
                <span className="font-mono">{call.tool_name}</span>
                <span className="ml-1.5 break-all text-muted-foreground">
                  {JSON.stringify(call.input)}
                </span>
              </span>
              {(call.risk_level === "high" || call.risk_level === "critical") && (
                <span className="shrink-0 text-destructive">{call.risk_level}</span>
              )}
            </li>
          ))}
        </ol>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        <ApprovalActions
          size="sm"
          deciding={decide.isPending}
          riskLevel={risk}
          toolName={toolName}
          onApprove={() => handleDecide("approve")}
          onApproveForTask={() => handleDecide("approve_for_task")}
          onDeny={() => handleDecide("reject")}
        />
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs text-muted-foreground"
          onClick={() => setExpanded(true)}
        >
          Review details
        </Button>
      </div>
    </div>
  )
}
