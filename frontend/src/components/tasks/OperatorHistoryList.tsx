import { useEffect, useRef } from "react"
import { AlertCircle, CheckCircle2, Globe, Layers } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { formatDurationMs } from "@/lib/time"
import { cn } from "@/lib/utils"
import type { OperatorHistoryEntry } from "@/lib/api"

const FAILED_STATUSES = new Set(["failed", "denied", "timeout"])

/** Level 1 default trace view (docs/UI_REWRITE_PLAN.md §12.2) - the
 * step-by-step narrative of what the Operator decided and did, in order.
 * Scrolls to and highlights the first failed step on mount ("3.4
 * Failure-first affordance" - a debugging UI should open where it broke). */
export function OperatorHistoryList({ entries }: { entries: OperatorHistoryEntry[] }) {
  const firstFailedRef = useRef<HTMLDivElement>(null)
  const firstFailedIndex = entries.findIndex((e) => FAILED_STATUSES.has(e.status))

  useEffect(() => {
    firstFailedRef.current?.scrollIntoView({ block: "center", behavior: "smooth" })
  }, [])

  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No operator steps recorded for this task.</p>
  }

  return (
    <div className="flex flex-col gap-2">
      {entries.map((entry, index) => {
        const failed = FAILED_STATUSES.has(entry.status)
        return (
          <div
            key={index}
            ref={index === firstFailedIndex ? firstFailedRef : undefined}
            className={cn(
              "flex flex-col gap-1 rounded-md border px-3 py-2 text-sm",
              failed ? "border-destructive/40 bg-destructive/5" : "border-border",
            )}
          >
            <div className="flex items-center gap-2">
              {failed ? (
                <AlertCircle className="size-4 shrink-0 text-destructive" />
              ) : (
                <CheckCircle2 className="size-4 shrink-0 text-muted-foreground" />
              )}
              <span className="font-mono text-xs">{entry.tool_name ?? "(no tool)"}</span>
              <Badge variant={failed ? "destructive" : "outline"} className="text-[10px]">
                {entry.status}
              </Badge>
              {entry.duration_ms != null && (
                <span className="text-[11px] text-muted-foreground">{formatDurationMs(entry.duration_ms)}</span>
              )}
              {entry.parallel && (
                <Badge variant="secondary" className="flex items-center gap-1 text-[10px]">
                  <Layers className="size-3" /> parallel
                </Badge>
              )}
              {entry.content_trust === "untrusted_external" && (
                <Badge
                  variant="outline"
                  className="flex items-center gap-1 border-warning/40 text-[10px] text-warning"
                  title="This step's output is content YBM does not control (a web page, a document, an external server) - not a claim that anything went wrong."
                >
                  <Globe className="size-3" /> untrusted content
                </Badge>
              )}
            </div>
            {entry.output_summary && (
              <p className="pl-6 text-xs text-muted-foreground [overflow-wrap:anywhere]">{entry.output_summary}</p>
            )}
            {entry.error && <p className="pl-6 text-xs text-destructive [overflow-wrap:anywhere]">{entry.error}</p>}
          </div>
        )
      })}
    </div>
  )
}
