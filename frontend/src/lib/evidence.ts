import {
  Eye,
  FilePlus,
  Globe,
  HelpCircle,
  Pencil,
  Send,
  Terminal,
  Trash2,
  Move,
  type LucideIcon,
} from "lucide-react"
import type { EvidenceEffect } from "@/lib/api"

/**
 * Display metadata for each real per-item effect (docs/archive/UI_UX_AUDIT.md
 * Phase 14) - shared between the Receipt card and the trace's Evidence
 * section so the same effect always looks the same everywhere. Colors
 * reuse the existing semantic roles (success/warning/info/danger), no new
 * palette.
 */
export const EFFECT_DISPLAY: Record<EvidenceEffect, { label: string; icon: LucideIcon; className: string }> = {
  read: { label: "Read", icon: Eye, className: "text-muted-foreground" },
  created: { label: "Created", icon: FilePlus, className: "text-success" },
  modified: { label: "Modified", icon: Pencil, className: "text-info" },
  moved: { label: "Moved", icon: Move, className: "text-info" },
  deleted: { label: "Deleted", icon: Trash2, className: "text-destructive" },
  command_executed: { label: "Ran", icon: Terminal, className: "text-warning" },
  website_visited: { label: "Visited", icon: Globe, className: "text-info" },
  message_sent: { label: "Sent", icon: Send, className: "text-success" },
  other: { label: "Touched", icon: HelpCircle, className: "text-muted-foreground" },
}
