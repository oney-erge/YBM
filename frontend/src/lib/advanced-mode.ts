import { createContext, useContext } from "react"

/**
 * The single global Advanced mode switch (docs/archive/UI_REWRITE_PLAN.md §6): one
 * toggle that reveals Level 2 everywhere, rather than a per-panel
 * disclosure a user has to find and click on every screen. Simple users
 * never touch it; power users flip it once.
 */
const STORAGE_KEY = "ybm-advanced-mode"

export function readAdvancedMode(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === "true"
  } catch {
    return false
  }
}

export function writeAdvancedMode(value: boolean): void {
  try {
    localStorage.setItem(STORAGE_KEY, value ? "true" : "false")
  } catch {
    // localStorage unavailable (private mode, etc.) - the toggle still
    // works for this page load, just won't persist across reloads.
  }
}

export const AdvancedModeContext = createContext<{
  advanced: boolean
  setAdvanced: (value: boolean) => void
} | null>(null)

export function useAdvancedMode() {
  const ctx = useContext(AdvancedModeContext)
  if (!ctx) throw new Error("useAdvancedMode must be used within AdvancedModeContext.Provider")
  return ctx
}
