import { useEffect, useState } from "react"
import { ChevronLeft, ChevronRight, ShieldAlert } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { toast } from "sonner"
import { ApiError, RISK_ORDER, type RiskLevel } from "@/lib/api"
import type { ApprovalDecision } from "@/lib/api"
import { useDecideApproval, usePendingApprovals } from "@/lib/queries"
import { EvidencePack } from "@/components/approvals/EvidencePack"
import { ApprovalActions } from "@/components/approvals/ApprovalActions"
import { formatCountdown, useCountdown } from "@/lib/time"

/**
 * Persistent, unmissable, on every route (docs/archive/UI_REWRITE_PLAN.md §11.1) -
 * mounted once in AppShell above the router outlet. Renders nothing when
 * there is nothing pending; that's the common case and it should be silent.
 *
 * Phase 8 rework (docs/archive/UI_UX_AUDIT.md): the dialog previously stacked every
 * pending approval vertically inside one narrow scroll container, so with
 * more than one pending item the decision buttons were reliably below the
 * fold. Now it reviews one at a time with a pager, a risk-tinted header, and
 * an action bar pinned to the bottom of the dialog that never scrolls away.
 */
export function ApprovalBanner() {
  const { data } = usePendingApprovals()
  const [open, setOpen] = useState(false)
  const [index, setIndex] = useState(0)
  const decide = useDecideApproval()

  const items = data?.approvals ?? []
  const count = items.length

  // Deciding the last item shrinks the list under the cursor - clamp rather
  // than render a blank dialog, and close once nothing is left to review.
  useEffect(() => {
    if (count === 0) {
      setOpen(false)
      setIndex(0)
    } else if (index > count - 1) {
      setIndex(count - 1)
    }
  }, [count, index])

  if (count === 0) return null

  const highestRisk = items.reduce<RiskLevel>(
    (worst, item) => (RISK_ORDER[item.approval.risk_level] > RISK_ORDER[worst] ? item.approval.risk_level : worst),
    "low",
  )

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex w-full items-center justify-between gap-2 border-b border-warning/30 bg-warning/10 px-4 py-2 text-sm text-foreground transition-colors hover:bg-warning/15"
      >
        <span className="flex min-w-0 items-center gap-2 font-medium">
          <ShieldAlert className="size-4 shrink-0 text-warning" />
          <span>
            {count} pending approval{count === 1 ? "" : "s"}
          </span>
          <Badge variant={highestRisk === "critical" || highestRisk === "high" ? "destructive" : "secondary"}>
            {highestRisk} risk
          </Badge>
          <span className="hidden min-w-0 truncate font-mono text-xs font-normal text-muted-foreground sm:inline">
            {(items[0].approval.action_payload.tool_name as string | undefined) ?? ""}
          </span>
        </span>
        <span className="shrink-0 underline underline-offset-2">Review</span>
      </button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="flex max-h-[88vh] w-full flex-col gap-0 p-0 sm:max-w-3xl">
          <ReviewDialogBody
            items={items}
            index={Math.min(index, count - 1)}
            onIndexChange={setIndex}
            deciding={decide.isPending}
            onDecide={(approvalId, decision) => {
              decide.mutate(
                { approvalId, decision },
                {
                  onError: (error) => {
                    toast.error(error instanceof ApiError ? error.message : "Could not record the decision.")
                  },
                },
              )
            }}
          />
        </DialogContent>
      </Dialog>
    </>
  )
}

/**
 * Split from the dialog shell so the expiry countdown hook and the keyboard
 * handler mount against the currently-shown approval, not against whatever
 * was first in the list when the dialog opened.
 */
function ReviewDialogBody({
  items,
  index,
  onIndexChange,
  deciding,
  onDecide,
}: {
  items: ReturnType<typeof usePendingApprovals>["data"] extends infer T
    ? T extends { approvals: infer A }
      ? A
      : never
    : never
  index: number
  onIndexChange: (next: number) => void
  deciding: boolean
  onDecide: (approvalId: string, decision: ApprovalDecision) => void
}) {
  const item = items[index]
  const remaining = useCountdown(item.approval.expires_at)
  const expired = remaining <= 0
  const risk = item.approval.risk_level
  const severe = risk === "critical" || risk === "high"
  const toolName = (item.approval.action_payload.tool_name as string | undefined) ?? undefined

  // Single-key shortcuts, deliberately not modified with Ctrl/Cmd: this
  // dialog is modal and has no text input, so there is nothing to type into
  // and nothing to collide with. Guarded on an expired/in-flight approval so
  // a keystroke can't fire a decision the buttons themselves refuse.
  //
  // A and T are excluded here for `critical` risk - ApprovalActions gates
  // those behind its own ConfirmDialog, and firing onDecide directly from
  // this keydown handler would bypass that gate entirely (this handler
  // calls onDecide, not the buttons, so it has no confirm step of its own).
  // D (deny) is never gated: it's the safe direction.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey || event.altKey) return
      const key = event.key.toLowerCase()
      const approveKeysBlocked = risk === "critical" && (key === "a" || key === "t")
      if (key === "arrowright" && index < items.length - 1) {
        onIndexChange(index + 1)
      } else if (approveKeysBlocked) {
        return
      } else if (key === "arrowleft" && index > 0) {
        onIndexChange(index - 1)
      } else if (!deciding && !expired && (key === "a" || key === "t" || key === "d")) {
        const decision: ApprovalDecision = key === "a" ? "approve" : key === "t" ? "approve_for_task" : "reject"
        onDecide(item.approval.id, decision)
      } else {
        return
      }
      event.preventDefault()
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [index, items.length, deciding, expired, risk, item.approval.id, onIndexChange, onDecide])

  return (
    <>
      <DialogHeader
        className={`shrink-0 gap-2 rounded-t-xl border-b px-4 py-3 pr-12 ${
          severe ? "border-destructive/25 bg-destructive/8" : "border-warning/25 bg-warning/8"
        }`}
      >
        <div className="flex flex-wrap items-center gap-2">
          <ShieldAlert className={`size-4 shrink-0 ${severe ? "text-destructive" : "text-warning"}`} />
          <DialogTitle>Approval needed</DialogTitle>
          <Badge variant={severe ? "destructive" : "secondary"}>{risk} risk</Badge>
          {items.length > 1 && (
            <span className="text-xs text-muted-foreground">
              {index + 1} of {items.length}
            </span>
          )}
        </div>
        <DialogDescription>
          A runtime-owned gate that only you can clear - no access-mode preset, including Full
          Access, bypasses it.
        </DialogDescription>
      </DialogHeader>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <EvidencePack item={item} />
      </div>

      <div className="flex shrink-0 flex-col gap-2 rounded-b-xl border-t bg-muted/50 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className={`text-xs ${expired ? "text-destructive" : "text-muted-foreground"}`}>
            {expired ? "This approval has expired." : `Expires in ${formatCountdown(remaining)}`}
          </span>
          {items.length > 1 && (
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Previous approval"
                disabled={index === 0}
                onClick={() => onIndexChange(index - 1)}
              >
                <ChevronLeft className="size-4" />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Next approval"
                disabled={index >= items.length - 1}
                onClick={() => onIndexChange(index + 1)}
              >
                <ChevronRight className="size-4" />
              </Button>
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="hidden text-[11px] text-muted-foreground sm:inline">
            {risk === "critical" ? (
              <>critical risk requires confirming on the buttons - no keyboard shortcut</>
            ) : (
              <>
                <kbd className="rounded border border-border px-1 font-mono">A</kbd> approve ·{" "}
                <kbd className="rounded border border-border px-1 font-mono">T</kbd> allow for task ·{" "}
              </>
            )}
            <kbd className="rounded border border-border px-1 font-mono">D</kbd> deny
          </span>
          <div className="ml-auto">
            <ApprovalActions
              deciding={deciding}
              expired={expired}
              riskLevel={risk}
              toolName={toolName}
              onApprove={() => onDecide(item.approval.id, "approve")}
              onApproveForTask={() => onDecide(item.approval.id, "approve_for_task")}
              onDeny={() => onDecide(item.approval.id, "reject")}
            />
          </div>
        </div>
      </div>
    </>
  )
}
