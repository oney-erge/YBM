import {
  AlertTriangle,
  FileText,
  Globe,
  Info,
  ListPlus,
  MessageSquare,
  Settings,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  Sparkles,
  Wrench,
  type LucideIcon,
} from "lucide-react"
import type { TimelineItem } from "@/lib/api"

const FAILED_TOOL_STATUSES = new Set(["failed", "denied", "timeout"])

const CATEGORY_DISPLAY: Record<string, { icon: LucideIcon; className: string }> = {
  approval: { icon: ShieldAlert, className: "text-warning" },
  task_state: { icon: ListPlus, className: "text-muted-foreground" },
  artifact: { icon: FileText, className: "text-success" },
  egress: { icon: Globe, className: "text-warning" },
  classification: { icon: Sparkles, className: "text-info" },
  failed_classification: { icon: AlertTriangle, className: "text-destructive" },
  spawned_task: { icon: ListPlus, className: "text-info" },
  policy: { icon: ShieldCheck, className: "text-muted-foreground" },
  config: { icon: Settings, className: "text-muted-foreground" },
  tool: { icon: Wrench, className: "text-info" },
  raw_message: { icon: MessageSquare, className: "text-muted-foreground" },
  telegram_access: { icon: ShieldQuestion, className: "text-muted-foreground" },
  channel_access: { icon: ShieldQuestion, className: "text-muted-foreground" },
  error: { icon: AlertTriangle, className: "text-destructive" },
  system: { icon: Info, className: "text-muted-foreground" },
}

/**
 * Icon + color for one timeline row (docs/archive/UI_UX_AUDIT.md Phase 14) -
 * replacing the earlier two-kind ("tool" blue wrench / "audit" grey
 * shield) system with a real vocabulary. Category picks the base
 * treatment; a failed tool call always overrides to danger regardless of
 * its category, since "what kind of event" and "did it succeed" are two
 * different questions and a reader cares about the second one first.
 */
export function timelineItemDisplay(item: TimelineItem): { icon: LucideIcon; className: string } {
  if (item.kind === "tool" && item.summary && FAILED_TOOL_STATUSES.has(item.summary)) {
    return { icon: AlertTriangle, className: "text-destructive" }
  }
  return CATEGORY_DISPLAY[item.category] ?? CATEGORY_DISPLAY.system
}
