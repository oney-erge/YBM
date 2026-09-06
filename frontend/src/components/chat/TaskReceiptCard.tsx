import { useState } from "react"
import { Link } from "react-router"
import { AlertTriangle, Download, ExternalLink, Globe, HardDrive, LoaderCircle, ShieldCheck } from "lucide-react"
import type { TaskReceipt, TaskStatus } from "@/lib/api"
import { useTaskReceipt } from "@/lib/queries"
import { cn } from "@/lib/utils"
import { StatusBadge } from "@/components/tasks/StatusBadge"
import { formatDuration } from "@/lib/time"
import { EFFECT_DISPLAY } from "@/lib/evidence"

/** Plain-text export (docs/UI_UX_AUDIT.md Phase 2) - human-readable, not a
 * raw JSON dump, so it reads the same way the card does. Client-side only:
 * every field here already came from the receipt the card already fetched,
 * so there's nothing new to ask the backend for. */
function formatReceiptAsText(receipt: TaskReceipt): string {
  const lines: string[] = []
  lines.push(`YBM - Task Receipt`)
  lines.push(`Task: ${receipt.task_id}`)
  lines.push(`Objective: ${receipt.objective}`)
  lines.push(`Status: ${receipt.status}`)
  lines.push(`Started: ${receipt.created_at}`)
  lines.push(`Finished: ${receipt.updated_at}`)
  lines.push(`Duration: ${formatDuration(receipt.duration_seconds)}`)
  lines.push("")
  if (receipt.result_summary) {
    lines.push("Result:", receipt.result_summary, "")
  }
  const touched = [...receipt.changes.files, ...receipt.changes.commands, ...receipt.changes.urls]
  if (touched.length > 0) {
    // Each item carries its own real effect label (docs/UI_UX_AUDIT.md
    // Phase 14) - this list still merges tool inputs and outputs, so a
    // single task can genuinely contain both a "Read" and a "Modified"
    // item, which is exactly why the label lives per-item, not as one
    // blanket heading.
    lines.push("What this task touched:")
    for (const item of touched) lines.push(`- [${EFFECT_DISPLAY[item.effect].label}] ${item.value}`)
    lines.push("")
  }
  if (receipt.tools_used.length > 0) {
    lines.push("Tools used:")
    for (const t of receipt.tools_used) lines.push(`- ${t.tool_name} (${t.succeeded} succeeded, ${t.failed} failed)`)
    lines.push("")
  }
  lines.push(
    receipt.data_left_machine
      ? `Data left this computer: yes - ${receipt.services_contacted.map((s) => s.host).join(", ") || "cloud model"}`
      // Not "no": a tool that reports its own destination is covered
      // automatically (spec.py's operation_egress - http.request and
      // browser today), but a tool with no such declaration is still
      // invisible here, so this reflects what was recorded, not a
      // guarantee that nothing else contacted anywhere.
      : "No external transfer was recorded",
  )
  if (receipt.verification.checked > 0) {
    lines.push(
      receipt.verification.missing.length > 0
        ? `Verified: ${receipt.verification.verified}/${receipt.verification.checked} - ${receipt.verification.missing.length} missing`
        : `Verified: ${receipt.verification.verified}/${receipt.verification.checked}, all confirmed on disk`,
    )
  }
  if (receipt.approvals.length > 0) {
    lines.push("", "Approvals:")
    for (const a of receipt.approvals) lines.push(`- ${a.summary} -> ${a.status}`)
  }
  if (receipt.uncertainties.length > 0) {
    lines.push("", "Uncertainty:")
    for (const u of receipt.uncertainties) lines.push(`- ${u}`)
  }
  if (receipt.token_usage.total_tokens) {
    lines.push("", `Tokens used: ${receipt.token_usage.total_tokens} (model: ${receipt.token_usage.last_model ?? "unknown"})`)
    if (receipt.token_usage.fallback_used) {
      lines.push("A fallback model answered somewhere in this task - the primary was unavailable at least once.")
    }
  }
  return lines.join("\n")
}

function downloadReceipt(receipt: TaskReceipt) {
  const blob = new Blob([formatReceiptAsText(receipt)], { type: "text/plain;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = `ybm-receipt-${receipt.task_id}.txt`
  link.click()
  URL.revokeObjectURL(url)
}

const OUTCOME_STYLE: Partial<Record<TaskStatus, { border: string; bg: string; text: string }>> = {
  completed: { border: "border-success/25", bg: "bg-success/5", text: "text-success" },
  failed: { border: "border-destructive/25", bg: "bg-destructive/5", text: "text-destructive" },
  blocked: { border: "border-destructive/25", bg: "bg-destructive/5", text: "text-destructive" },
  cancelled: { border: "border-border", bg: "bg-muted/40", text: "text-muted-foreground" },
}
const DEFAULT_OUTCOME_STYLE = { border: "border-success/25", bg: "bg-success/5", text: "text-success" }

/**
 * The "Done" result format (docs/UI_UX_AUDIT.md Phase 2), extended in
 * Phase 8 to every terminal state, not only "completed" - a task that
 * modified files, contacted a service, or spent an approval before
 * failing is exactly when the user most needs to see what happened. The
 * header now reflects the real outcome (StatusBadge's own colors/icons)
 * instead of always showing a green checkmark regardless of status.
 */
export function TaskReceiptCard({ taskId }: { taskId: string }) {
  const { data: receipt, isPending } = useTaskReceipt(taskId)
  // Declared before the loading early-return: hooks cannot be conditional.
  const [expanded, setExpanded] = useState(false)

  if (isPending || !receipt) {
    return (
      <div className="mt-2.5 flex items-center gap-1.5 text-xs text-muted-foreground">
        <LoaderCircle className="size-3 animate-spin" />
        Loading receipt...
      </div>
    )
  }

  const changedItems = [...receipt.changes.files, ...receipt.changes.commands]
  const usedTools = receipt.tools_used.map((t) => t.tool_name)
  const contactedHosts = [...new Set(receipt.services_contacted.map((s) => s.host).filter(Boolean))] as string[]
  const style = OUTCOME_STYLE[receipt.status] ?? DEFAULT_OUTCOME_STYLE
  const partial = receipt.status !== "completed" && (changedItems.length > 0 || receipt.tools_used.length > 0)

  // A plain answer that touched nothing carried a full card - status chip,
  // "Receipt", "No external transfer was recorded", duration, and two actions -
  // for a one-line reply, so the receipt outweighed the message it described
  // Collapse exactly that case: completed,
  // nothing changed, no tool used, nothing left the machine. Anything with
  // real effects keeps the full card, which is the point of having one.
  const unremarkable =
    receipt.status === "completed" &&
    changedItems.length === 0 &&
    usedTools.length === 0 &&
    !receipt.data_left_machine

  if (unremarkable && !expanded) {
    return (
      <div className="mt-2.5 flex items-center gap-3 text-[11px] text-muted-foreground">
        <span>{formatDuration(receipt.duration_seconds)}</span>
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="underline underline-offset-2 hover:text-foreground"
        >
          Receipt
        </button>
        <Link to={`/tasks/${taskId}`} className="hover:text-foreground">
          Full trace
        </Link>
      </div>
    )
  }

  return (
    <div className={cn("mt-2.5 flex flex-col gap-2 rounded-lg border p-3 text-xs", style.border, style.bg)}>
      <div className={cn("flex items-center gap-1.5 font-medium", style.text)}>
        <StatusBadge status={receipt.status} />
        Receipt
      </div>
      {partial && (
        <p className="text-muted-foreground">
          Work happened before this task {receipt.status} - shown below, exactly as recorded.
        </p>
      )}

      {changedItems.length > 0 && (
        <div>
          {/* Each item shows its own real effect (docs/UI_UX_AUDIT.md
              Phase 14) - the backend still merges tool inputs and outputs
              into one list, so a single task can genuinely contain both a
              Read and a Modified item; that's exactly why the label is
              per-item, not one blanket "Changed"/"Touched" heading. */}
          <p className="font-medium text-muted-foreground">What this task touched</p>
          <ul className="mt-0.5 flex flex-col gap-0.5">
            {changedItems.slice(0, 8).map((item) => {
              const effect = EFFECT_DISPLAY[item.effect]
              const Icon = effect.icon
              return (
                <li key={item.value} className="flex items-start gap-1.5 [overflow-wrap:anywhere]">
                  <Icon className={cn("mt-0.5 size-3 shrink-0", effect.className)} />
                  <span>
                    <span className={cn("font-medium", effect.className)}>{effect.label}</span> {item.value}
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {usedTools.length > 0 && (
        <p className="text-muted-foreground">
          <span className="font-medium text-foreground">Used: </span>
          {usedTools.join(", ")}
        </p>
      )}

      <div
        className={cn(
          "flex items-center gap-1.5",
          receipt.data_left_machine ? "text-warning" : "text-muted-foreground",
        )}
      >
        {receipt.data_left_machine ? <Globe className="size-3.5 shrink-0" /> : <HardDrive className="size-3.5 shrink-0" />}
        {receipt.data_left_machine
          ? `Contacted: ${contactedHosts.length > 0 ? contactedHosts.join(", ") : "a cloud model"}`
          : // Not an absolute "nothing left" claim: a tool covers itself by
            // declaring operation_egress (spec.py) - http.request and
            // browser today - so this reflects what was recorded, not a
            // guarantee that nothing else contacted anywhere.
            "No external transfer was recorded"}
      </div>

      {receipt.verification.checked > 0 && (
        <div
          className={cn(
            "flex items-center gap-1.5",
            receipt.verification.missing.length > 0 ? "text-destructive" : "text-success",
          )}
        >
          {receipt.verification.missing.length > 0 ? (
            <AlertTriangle className="size-3.5 shrink-0" />
          ) : (
            <ShieldCheck className="size-3.5 shrink-0" />
          )}
          {receipt.verification.missing.length > 0
            ? `Verified ${receipt.verification.verified}/${receipt.verification.checked} - ${receipt.verification.missing.length} missing`
            : `Verified ${receipt.verification.verified}/${receipt.verification.checked}, confirmed on disk`}
        </div>
      )}

      {receipt.token_usage.fallback_used && (
        <div className="flex items-center gap-1.5 text-warning">
          <AlertTriangle className="size-3.5 shrink-0" />
          {receipt.token_usage.last_model
            ? `Answered by a fallback model (${receipt.token_usage.last_model}) - the primary was unavailable`
            : "Answered by a fallback model - the primary was unavailable"}
        </div>
      )}

      <div className="flex items-center justify-between border-t border-current/15 pt-2 text-[11px] text-muted-foreground">
        <span>Time: {formatDuration(receipt.duration_seconds)}</span>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => downloadReceipt(receipt)}
            className="flex items-center gap-1 hover:text-foreground"
          >
            Download <Download className="size-3" />
          </button>
          <Link to={`/tasks/${taskId}`} className="flex items-center gap-1 hover:text-foreground">
            Full trace <ExternalLink className="size-3" />
          </Link>
        </div>
      </div>
    </div>
  )
}
