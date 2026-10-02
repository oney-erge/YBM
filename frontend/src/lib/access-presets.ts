import type { CapabilityAccessMode, CapabilityAccessSummary } from "@/lib/api"

/**
 * Access-mode presets (docs/archive/UI_REWRITE_PLAN.md §13's "access-mode
 * presets") - client-composed, not a backend concept. There is no
 * `/api/config/access-modes/preset` endpoint (unlike the LLM presets,
 * which the backend does define); each preset here just computes a
 * `modes` dict and POSTs it through the same `/api/config/access-modes`
 * endpoint a manual per-group change already uses.
 *
 * Each group only supports a subset of the four modes (desktop_screenshot
 * has no write_access/full_access at all; terminal has no read_only) - the
 * preference list is walked against that group's own `options`, never a
 * mode assumed to exist everywhere.
 */
const PRESET_PREFERENCE: Record<string, CapabilityAccessMode[]> = {
  read_only: ["read_only", "off"],
  approval_required: ["write_access", "read_only", "off"],
  full_autonomy: ["full_access", "write_access", "read_only", "off"],
}

export type AccessPresetKey = keyof typeof PRESET_PREFERENCE

export const ACCESS_PRESETS: {
  key: AccessPresetKey
  label: string
  description: string
  destructive: boolean
}[] = [
  {
    key: "read_only",
    label: "Read-only",
    description: "Every capability can look but never write or act.",
    destructive: false,
  },
  {
    key: "approval_required",
    label: "Approval required",
    description: "Every capability can act, but every write waits for your approval first.",
    destructive: false,
  },
  {
    key: "full_autonomy",
    label: "Full autonomy",
    description: "Every capability runs without per-action approval prompts. Installing servers, promoting generated code, running unsandboxed code and sending secrets still ask first.",
    destructive: true,
  },
]

export function computePreset(
  accessModes: Record<string, CapabilityAccessSummary>,
  preset: AccessPresetKey,
): Record<string, CapabilityAccessMode> {
  const preference = PRESET_PREFERENCE[preset]
  const modes: Record<string, CapabilityAccessMode> = {}
  for (const [name, group] of Object.entries(accessModes)) {
    const available = new Set(group.options.map((option) => option.value))
    const picked = preference.find((mode) => available.has(mode))
    modes[name] = picked ?? group.mode
  }
  return modes
}
