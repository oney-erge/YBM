import { buildDurationSegments, type DurationSegment } from "@/lib/duration-chart"
import { formatDurationMs } from "@/lib/time"
import { cn } from "@/lib/utils"
import type { TaskTrace } from "@/lib/api"

const LABEL_WIDTH = "w-44 sm:w-56"
const MIN_BAR_PERCENT = 0.6
const TICK_COUNT = 5

/**
 * "Where did the time go" (docs/archive/UI_UX_AUDIT.md Phase 14) - the requested
 * horizontal bar / Gantt view, one row per segment in chronological order,
 * bar length proportional to real elapsed wall-clock time. Deliberately not
 * a charting library: plain positioned divs over a shared percentage track,
 * the same technique DiagnosticsCard already uses for its table-count bars.
 *
 * Approval waits render as an outline, not a fill - the whole point is that
 * a task that looks slow because of a long bar is often just a task that
 * spent most of its life waiting on a human, which is not a performance
 * problem. Gaps between measured segments render as a dashed "inferred"
 * segment: real time, but not directly measured until LLM-call persistence
 * (Phase 14d) turns it into a real number.
 */
export function DurationChart({ trace }: { trace: TaskTrace }) {
  const { segments, totalMs } = buildDurationSegments(trace)

  if (segments.length === 0) {
    return <p className="text-sm text-muted-foreground">No timed steps recorded for this task.</p>
  }

  const ticks = Array.from({ length: TICK_COUNT + 1 }, (_, i) => (totalMs * i) / TICK_COUNT)

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
        <div className={cn(LABEL_WIDTH, "shrink-0")} />
        <div className="relative h-4 flex-1">
          {ticks.map((tick, index) => (
            <span
              key={index}
              className="absolute -translate-x-1/2 whitespace-nowrap"
              style={{ left: `${(tick / totalMs) * 100}%` }}
            >
              {formatDurationMs(tick)}
            </span>
          ))}
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        {segments.map((segment) => (
          <DurationRow key={segment.key} segment={segment} totalMs={totalMs} />
        ))}
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        Completed {formatDurationMs(totalMs)} total
      </p>
    </div>
  )
}

function DurationRow({ segment, totalMs }: { segment: DurationSegment; totalMs: number }) {
  const leftPercent = (segment.startMs / totalMs) * 100
  const widthPercent = Math.max((segment.durationMs / totalMs) * 100, MIN_BAR_PERCENT)
  const inferred = segment.kind === "inferred_think"

  return (
    <div className="flex items-center gap-3 text-xs">
      <span className={cn(LABEL_WIDTH, "shrink-0 truncate font-mono text-[11px]", inferred && "text-muted-foreground italic")}>
        {segment.label}
      </span>
      <div className="relative h-4 flex-1 rounded-sm bg-muted/40">
        <div
          className={cn("absolute inset-y-0 rounded-sm", segmentClassName(segment))}
          style={{ left: `${leftPercent}%`, width: `${widthPercent}%` }}
          title={`${segment.label} - ${formatDurationMs(segment.durationMs)}`}
        />
      </div>
      <span className="w-14 shrink-0 text-right text-[11px] text-muted-foreground">
        {formatDurationMs(segment.durationMs)}
      </span>
    </div>
  )
}

function segmentClassName(segment: DurationSegment): string {
  if (segment.kind === "inferred_think") {
    return "border border-dashed border-muted-foreground/50 bg-transparent"
  }
  if (segment.kind === "approval_wait") {
    return "border-2 border-warning bg-warning/10"
  }
  if (segment.outcome === "failure") return "bg-destructive"
  if (segment.outcome === "pending") return "bg-info"
  return "bg-success"
}
