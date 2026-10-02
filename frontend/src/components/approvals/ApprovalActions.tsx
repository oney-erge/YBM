import { useState } from "react"
import { Button } from "@/components/ui/button"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import type { RiskLevel } from "@/lib/api"

/**
 * The three decision buttons, extracted so the Chat inline card, the
 * expanded Evidence Pack, and the review dialog's sticky footer all render
 * the same choices in the same order without duplicating decision logic
 * (docs/archive/UI_UX_AUDIT.md Phase 8).
 *
 * Still no "Always allow" - that needs a revocation list to be safe, which
 * doesn't exist yet (the original scope-down in Phase 1 still holds).
 *
 * A `critical`-risk decision (desktop control, browser control, GitHub push,
 * MCP install, generated-adapter promotion, ...) requires an extra confirm
 * step before Approve/Allow fires - everywhere these buttons render,
 * including the collapsed Chat card, so there is exactly one gate to reason
 * about rather than one per surface. Scoped to `critical` only, not `high`
 * too: most real approvals are `high` (filesystem write, terminal run) and
 * the product's explicit design goal is a sub-15-second decision for the
 * common case - `critical` is specifically the one-accidental-keystroke
 * failure this exists to close. Deny is never gated: it's the safe direction.
 */
export function ApprovalActions({
  onApprove,
  onApproveForTask,
  onDeny,
  deciding,
  expired,
  size = "default",
  riskLevel,
  toolName,
}: {
  onApprove: () => void
  onApproveForTask: () => void
  onDeny: () => void
  deciding: boolean
  expired?: boolean
  size?: "default" | "sm"
  riskLevel: RiskLevel
  toolName?: string
}) {
  const [confirming, setConfirming] = useState<"approve" | "approve_for_task" | null>(null)
  const disabled = deciding || expired
  const compact = size === "sm"
  const buttonSize = compact ? "sm" : "default"
  const extra = compact ? "h-7 px-2.5 text-xs" : ""
  const requiresConfirmation = riskLevel === "critical"

  function clickApprove() {
    if (requiresConfirmation) setConfirming("approve")
    else onApprove()
  }
  function clickApproveForTask() {
    if (requiresConfirmation) setConfirming("approve_for_task")
    else onApproveForTask()
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5">
        <Button type="button" variant="outline" size={buttonSize} className={extra} disabled={disabled} onClick={onDeny}>
          Deny
        </Button>
        <Button
          type="button"
          variant="outline"
          size={buttonSize}
          className={extra}
          disabled={disabled}
          onClick={clickApproveForTask}
        >
          Allow for this task
        </Button>
        <Button type="button" size={buttonSize} className={extra} disabled={disabled} onClick={clickApprove}>
          Approve once
        </Button>
      </div>
      <ConfirmDialog
        open={confirming !== null}
        onOpenChange={(open) => !open && setConfirming(null)}
        title={confirming === "approve_for_task" ? "Allow for this task?" : "Approve this action?"}
        description={
          <>
            This is <strong>critical</strong> risk{toolName ? ` (${toolName})` : ""}.{" "}
            {confirming === "approve_for_task"
              ? "Every matching call for the rest of this task will run without asking again."
              : "This exact call will run once it's confirmed."}{" "}
            Review the evidence above before confirming.
          </>
        }
        confirmLabel={confirming === "approve_for_task" ? "Allow for this task" : "Approve once"}
        pending={deciding}
        onConfirm={() => {
          if (confirming === "approve_for_task") onApproveForTask()
          else if (confirming === "approve") onApprove()
        }}
      />
    </>
  )
}
