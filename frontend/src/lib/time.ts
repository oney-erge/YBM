import { useEffect, useState } from "react"

/** Live countdown formatting for approval expiry (docs/archive/UI_REWRITE_PLAN.md §11.3). */
export function secondsUntil(isoTimestamp: string): number {
  const target = new Date(isoTimestamp).getTime()
  return Math.max(0, Math.round((target - Date.now()) / 1000))
}

export function formatCountdown(totalSeconds: number): string {
  if (totalSeconds <= 0) return "expired"
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes === 0) return `${seconds}s`
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s`
}

/** Re-renders every second so an expiry countdown ticks live in the UI,
 * independent of the (much slower) approvals poll interval. */
export function useCountdown(isoTimestamp: string): number {
  const [remaining, setRemaining] = useState(() => secondsUntil(isoTimestamp))

  useEffect(() => {
    setRemaining(secondsUntil(isoTimestamp))
    const id = setInterval(() => setRemaining(secondsUntil(isoTimestamp)), 1_000)
    return () => clearInterval(id)
  }, [isoTimestamp])

  return remaining
}

/** How long a task ran/took - shared by the Task Receipt card and the
 * Tasks list outcome column (docs/archive/UI_UX_AUDIT.md Phase 9) rather than
 * each computing their own copy of the same rounding rules. */
export function formatDuration(seconds: number): string {
  if (seconds < 1) return "under a second"
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return rest > 0 ? `${minutes}m ${rest}s` : `${minutes}m`
}

/** How long one step took - a separate formatter from formatDuration
 * above, not a duplicate: that one is tuned for whole-task durations
 * (usually multi-second, "under a second" is precise enough), this one
 * is for individual tool-call/LLM-call durations shown in the Timeline,
 * Steps list, Graph, and duration chart (docs/archive/UI_UX_AUDIT.md Phase 14),
 * which are routinely sub-second and need real millisecond precision to
 * be useful at all. */
export function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`
}

/** Milliseconds between two ISO timestamps, or null if either is missing -
 * the client-side twin of admin.py's `_elapsed_ms`, used where a raw
 * ToolInvocation's created_at/completed_at is available directly (the
 * Graph view) rather than a server-computed duration_ms. */
export function elapsedMs(start: string | null | undefined, end: string | null | undefined): number | null {
  if (!start || !end) return null
  const elapsed = new Date(end).getTime() - new Date(start).getTime()
  return Number.isNaN(elapsed) ? null : Math.max(0, elapsed)
}

/** "2m ago" / "just now" - not live-ticking (no per-second re-render), the
 * task/summary polls this reads from already refresh every few seconds. */
export function formatRelativeTime(isoTimestamp: string): string {
  const deltaSeconds = Math.max(0, Math.round((Date.now() - new Date(isoTimestamp).getTime()) / 1000))
  if (deltaSeconds < 5) return "just now"
  if (deltaSeconds < 60) return `${deltaSeconds}s ago`
  const minutes = Math.floor(deltaSeconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}
