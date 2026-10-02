/**
 * Chat column width, persisted per-browser (docs/archive/UI_UX_AUDIT.md Phase 12).
 * Chat was hardcoded to max-w-3xl (768px) everywhere - fine on a laptop,
 * a thin ribbon of text on a wide monitor with a code block scrolling
 * inside it. Same read/write-to-localStorage shape as advanced-mode.ts,
 * but plain state instead of a Context: unlike Advanced mode, nothing
 * outside ChatPage itself needs to know this value.
 */
const STORAGE_KEY = "ybm-chat-width"

export type ChatWidth = "comfortable" | "wide" | "full"

const WIDTH_CLASS: Record<ChatWidth, string> = {
  comfortable: "max-w-3xl",
  wide: "max-w-5xl",
  full: "max-w-none",
}

export function chatWidthClass(width: ChatWidth): string {
  return WIDTH_CLASS[width]
}

export function readChatWidth(): ChatWidth {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === "comfortable" || stored === "wide" || stored === "full") return stored
    return "comfortable"
  } catch {
    return "comfortable"
  }
}

export function writeChatWidth(value: ChatWidth): void {
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    // localStorage unavailable (private mode, etc.) - still works for this
    // page load, just won't persist across reloads.
  }
}
