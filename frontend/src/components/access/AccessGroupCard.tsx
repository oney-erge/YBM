import { useState } from "react"
import { Eye, LockKeyhole, ShieldCheck, Zap } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { describeCapability } from "@/lib/capability"
import { cn } from "@/lib/utils"
import type { CapabilityAccessMode, CapabilityAccessSummary, CapabilityPolicy } from "@/lib/api"

/**
 * One access-mode group (docs/archive/UI_REWRITE_PLAN.md §13 Level 1) - a Select
 * bound directly to the group's own `options` (never a hardcoded mode
 * list, since not every group supports every mode: desktop_screenshot has
 * no write_access/full_access at all). Selecting Full access always drops
 * `requires_approval` to false server-side (admin.py's _apply_group), so
 * that one transition gets the "confirm dialog naming the concrete
 * consequence" the plan calls for; every other transition applies
 * immediately, matching the Streamlit console's own behavior.
 */
export function AccessGroupCard({
  group,
  rawPolicies,
  advanced,
  onChange,
  pending,
}: {
  group: CapabilityAccessSummary
  rawPolicies: Record<string, CapabilityPolicy>
  advanced: boolean
  onChange: (mode: CapabilityAccessMode) => void
  pending: boolean
}) {
  const [confirmMode, setConfirmMode] = useState<CapabilityAccessMode | null>(null)

  function handleSelect(value: string | null) {
    if (value == null) return
    const mode = value as CapabilityAccessMode
    if (mode === group.mode) return
    if (mode === "full_access") {
      setConfirmMode(mode)
    } else {
      onChange(mode)
    }
  }

  const currentLabel = group.options.find((o) => o.value === group.mode)?.label ?? group.mode
  const state = accessState(group.mode, group.requires_approval)
  const StateIcon = state.icon

  return (
    <Card className="shadow-sm ring-border transition-shadow hover:shadow-md">
      <CardHeader className="flex flex-row items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle>{group.label ?? group.name}</CardTitle>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{state.description}</p>
        </div>
        <Badge variant="outline" className={cn("gap-1 border-transparent", state.badgeClass)}>
          <StateIcon className="size-3" />
          {currentLabel}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid gap-1.5 rounded-xl bg-muted/70 p-1.5" style={{ gridTemplateColumns: `repeat(${group.options.length}, minmax(0, 1fr))` }}>
          {group.options.map((option) => (
            <button
              key={option.value}
              type="button"
              aria-pressed={option.value === group.mode}
              disabled={pending}
              onClick={() => handleSelect(option.value)}
              className={cn(
                "min-w-0 rounded-lg px-2 py-2 text-xs font-medium leading-4 transition-all disabled:opacity-50",
                option.value === group.mode
                  ? "bg-card text-foreground shadow-sm ring-1 ring-border"
                  : "text-muted-foreground hover:bg-card/60 hover:text-foreground",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>

        <div className="flex flex-wrap gap-1.5">
          {group.capabilities.map((capability) => (
            <span key={capability} className="rounded-md bg-muted px-2 py-1 font-mono text-[10px] text-muted-foreground">
              {capability}
            </span>
          ))}
        </div>

        {advanced && (
          <div className="flex min-w-0 flex-col gap-2 rounded-xl border border-border/60 bg-muted/35 p-3 text-xs">
            <p className="font-medium text-muted-foreground">
              Risk ceilings & patterns (read-only - edit config.yaml to change)
            </p>
            {group.capabilities.map((capability) => {
              const policy = rawPolicies[capability]
              if (!policy) return null
              return (
                <div key={capability} className="flex min-w-0 flex-col gap-0.5">
                  <span className="font-mono">{capability}</span>
                  <span className="text-muted-foreground [overflow-wrap:anywhere]">
                    {describeCapability(capability)} · ceiling: {policy.max_risk_level}
                    {policy.scopes.length > 0 && ` · scopes: ${policy.scopes.join(", ")}`}
                    {policy.allow_patterns.length > 0 && ` · allow: ${policy.allow_patterns.join(", ")}`}
                    {policy.deny_patterns.length > 0 && ` · deny: ${policy.deny_patterns.join(", ")}`}
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </CardContent>

      <ConfirmDialog
        open={confirmMode != null}
        onOpenChange={(open) => {
          if (!open) setConfirmMode(null)
        }}
        title={`Set ${group.label ?? group.name} to full access?`}
        description={`This removes the approval prompt for ${group.capabilities.join(", ")}. Matching actions run immediately, without you reviewing them first.`}
        confirmLabel="Set to full access"
        pending={pending}
        onConfirm={() => {
          if (confirmMode) onChange(confirmMode)
        }}
      />
    </Card>
  )
}

function accessState(mode: CapabilityAccessMode, requiresApproval: boolean) {
  if (mode === "off") {
    return {
      icon: LockKeyhole,
      description: "This capability group cannot be used.",
      badgeClass: "bg-muted text-muted-foreground",
    }
  }
  if (mode === "read_only") {
    return {
      icon: Eye,
      description: "YBM can observe, but cannot change anything here.",
      badgeClass: "bg-info/10 text-info",
    }
  }
  if (requiresApproval) {
    return {
      icon: ShieldCheck,
      description: "Actions pause so you can review the exact operation.",
      badgeClass: "bg-warning/10 text-warning",
    }
  }
  return {
    icon: Zap,
    description: "Allowed actions run immediately without a per-action review.",
    badgeClass: "bg-destructive/10 text-destructive",
  }
}
