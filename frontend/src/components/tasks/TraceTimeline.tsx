import { useState } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import type { TimelineItem } from "@/lib/api"
import { timelineItemDisplay } from "@/lib/timeline"
import { formatDurationMs } from "@/lib/time"

/**
 * The full chronological record - audit events (policy decisions,
 * approvals, classification) and tool calls, merged and time-sorted -
 * rendering build_task_trace's own already-computed `timeline`
 * (docs/archive/UI_UX_AUDIT.md Phase 9). Richer than the Steps list: that view
 * only shows tool decisions, this shows everything the runtime logged
 * about the task, in the order it actually happened, starting from
 * whatever triggered the task in the first place.
 *
 * Phase 14: each row's icon/color now comes from its real category
 * (approval, tool, artifact, egress, ...) instead of a flat two-kind
 * tool/audit split, and shows real duration for tool calls.
 */
export function TraceTimeline({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">No timeline events recorded for this task.</p>
  }

  return (
    <div className="flex flex-col">
      {items.map((item, index) => (
        <TimelineRow key={`${item.at}-${index}`} item={item} isLast={index === items.length - 1} />
      ))}
    </div>
  )
}

function TimelineRow({ item, isLast }: { item: TimelineItem; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const { icon: Icon, className } = timelineItemDisplay(item)
  const bgClassName = className.replace("text-", "bg-") + "/10"
  const hasDetails = item.details != null && Object.keys(item.details).length > 0

  return (
    <div className="flex gap-3">
      <div className="flex flex-col items-center">
        <span className={`mt-1 flex size-5 shrink-0 items-center justify-center rounded-full ${bgClassName} ${className}`}>
          <Icon className="size-3" />
        </span>
        {!isLast && <span className="w-px flex-1 bg-border" />}
      </div>
      <div className="min-w-0 flex-1 pb-4">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-sm font-medium [overflow-wrap:anywhere]">{item.title ?? item.kind}</span>
          <span className={`rounded-full px-1.5 py-0 text-[10px] font-medium uppercase tracking-wide ${bgClassName} ${className}`}>
            {item.category.replace(/_/g, " ")}
          </span>
          {item.duration_ms != null && (
            <span className="text-[11px] font-medium text-muted-foreground">{formatDurationMs(item.duration_ms)}</span>
          )}
          {item.at && (
            <span className="text-[11px] text-muted-foreground">{new Date(item.at).toLocaleString()}</span>
          )}
          {item.actor && <span className="text-[11px] text-muted-foreground">· {item.actor}</span>}
        </div>
        {item.summary && (
          <p className="mt-0.5 text-xs text-muted-foreground [overflow-wrap:anywhere]">{item.summary}</p>
        )}
        {hasDetails && (
          <>
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-1 flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
            >
              {expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
              {expanded ? "Hide details" : "Show details"}
            </button>
            {expanded && (
              <pre className="mt-1.5 max-h-56 overflow-auto rounded-md bg-muted p-2.5 font-mono text-[11px]">
                {JSON.stringify(item.details, null, 2)}
              </pre>
            )}
          </>
        )}
      </div>
    </div>
  )
}
