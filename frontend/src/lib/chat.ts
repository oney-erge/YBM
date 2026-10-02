import type { TaskRecord } from "@/lib/api"

/**
 * Ports admin_streamlit.py's _extract_last_output / _chat_answer_text
 * exactly, so the web console's chat behaves identically to the Streamlit
 * one it replaces. Keep these two in sync with the Python originals until
 * Streamlit is retired (docs/archive/UI_REWRITE_PLAN.md §19).
 */

interface LastToolResult {
  output?: {
    stdout?: string
    summary?: string
    response?: string
    content?: string
    text?: string
    result?: string
  }
  error_message?: string
}

export function extractLastOutput(task: TaskRecord): string | null {
  const meta = task.metadata
  const result = (meta.last_tool_result ?? {}) as LastToolResult
  const out = result.output ?? {}
  const text =
    out.stdout ?? out.summary ?? out.response ?? out.content ?? out.text ?? out.result
  if (text) return sanitizeDisplayedText(String(text))
  if (result.error_message) return `Error: ${sanitizeDisplayedText(result.error_message)}`
  if (typeof meta.last_worker_error === "string") return `Error: ${sanitizeDisplayedText(meta.last_worker_error)}`
  return null
}

export function chatAnswerText(task: TaskRecord): string {
  const meta = task.metadata
  if (typeof meta.synthesized_answer === "string" && meta.synthesized_answer) {
    return sanitizeDisplayedText(meta.synthesized_answer)
  }
  const lastOutput = extractLastOutput(task)
  switch (task.status) {
    case "completed":
      return lastOutput ?? "Done."
    case "failed":
      return lastOutput ?? "Failed."
    case "blocked":
      return lastOutput ?? "Blocked."
    case "cancelled":
      return "Stopped."
    case "awaiting_approval":
      return "Wants to do something that needs your approval."
    case "clarifying":
      return typeof meta.clarifying_question === "string"
        ? meta.clarifying_question
        : "Needs more information."
    default:
      return lastOutput ?? `Working... (${task.status || "received"})`
  }
}

export function isTerminal(status: TaskRecord["status"]): boolean {
  return status === "completed" || status === "failed" || status === "blocked" || status === "cancelled"
}

// admin.py folds machine-facing notes into the objective text (a
// clarification answer, an attachment's file name and path) the same way
// clarification.py already did for Telegram - context the operator needs,
// not something a user wants to read back at themselves in their own
// message bubble. Strips from the first such marker, whichever comes first.
const OBJECTIVE_NOTE_MARKERS = ["\n[User clarification:", "\n\n[Attached files:"]

export function displayedObjective(objective: string): string {
  let cut = objective.length
  for (const marker of OBJECTIVE_NOTE_MARKERS) {
    const index = objective.indexOf(marker)
    if (index !== -1 && index < cut) cut = index
  }
  return objective.slice(0, cut)
}

// Defense in depth for old task records created before transport errors were
// sanitized server-side. Telegram embeds its bot token in request URLs, and
// httpx includes those URLs in exception text. Never render that credential
// shape even if a historical record still contains it.
export function sanitizeDisplayedText(value: string): string {
  return value.replace(/(\/bot)\d{5,}:[A-Za-z0-9_-]{20,}/g, "$1***")
}
