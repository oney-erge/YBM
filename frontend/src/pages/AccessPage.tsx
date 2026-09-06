import { useState } from "react"
import { toast } from "sonner"
import { Eye, Power, ShieldCheck, UserCheck, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { PageHeader } from "@/components/layout/PageHeader"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { AccessGroupCard } from "@/components/access/AccessGroupCard"
import { ActiveGrantsCard } from "@/components/access/ActiveGrantsCard"
import { SecretVaultCard } from "@/components/access/SecretVaultCard"
import { ACCESS_PRESETS, computePreset, type AccessPresetKey } from "@/lib/access-presets"
import { useAdvancedMode } from "@/lib/advanced-mode"
import { useEffectiveConfig, useUpdateAccessModes } from "@/lib/queries"
import { ApiError, type CapabilityAccessMode } from "@/lib/api"

type PendingAction = {
  modes: Record<string, CapabilityAccessMode>
  title: string
  description: string
  confirmLabel: string
}

/**
 * The security control room (docs/UI_REWRITE_PLAN.md §13). Level 1: kill
 * switch, presets, per-group capability toggles, secret vault, and active
 * time-boxed grants (ActiveGrantsCard). Level 2 (Advanced): read-only risk
 * ceiling/scope/allow-deny detail per capability, rendered inside each
 * AccessGroupCard.
 */
export function AccessPage() {
  const { data, isPending, isError, error } = useEffectiveConfig()
  const update = useUpdateAccessModes()
  const { advanced } = useAdvancedMode()
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)

  function apply(modes: Record<string, CapabilityAccessMode>, successMessage: string) {
    update.mutate(modes, {
      onSuccess: () => toast.success(successMessage),
      onError: (err) => {
        toast.error(err instanceof ApiError ? err.message : "Could not update access.")
      },
    })
  }

  if (isPending) {
    return (
      <div className="flex flex-col gap-4 p-6">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }
  if (isError || !data) {
    return (
      <div className="p-6">
        <Alert variant="destructive">
          <AlertTitle>Couldn&apos;t load access configuration</AlertTitle>
          <AlertDescription>{error?.message ?? "Unknown error"}</AlertDescription>
        </Alert>
      </div>
    )
  }

  const accessModes = data.access_modes
  const groups = Object.values(accessModes)
  const allOff = groups.length > 0 && groups.every((g) => g.mode === "off")
  const enabledCount = groups.filter((group) => group.mode !== "off").length
  const approvalCount = groups.filter((group) => group.mode !== "off" && group.requires_approval).length
  const autonomousCount = groups.filter((group) => group.mode !== "off" && !group.requires_approval).length

  function handleKillSwitch() {
    setPendingAction({
      modes: Object.fromEntries(groups.map((g) => [g.name, "off" as CapabilityAccessMode])),
      title: "Disable every capability?",
      description:
        "Sets every access group below to Off in one action. The worker keeps running but every gated capability stops being usable until you turn groups back on.",
      confirmLabel: "Disable everything now",
    })
  }

  function handlePreset(preset: AccessPresetKey) {
    const modes = computePreset(accessModes, preset)
    const info = ACCESS_PRESETS.find((p) => p.key === preset)
    if (!info) return
    if (info.destructive) {
      setPendingAction({
        modes,
        title: `Apply "${info.label}"?`,
        description: `${info.description} Applies to: ${groups.map((g) => g.label ?? g.name).join(", ")}.`,
        confirmLabel: `Apply ${info.label}`,
      })
    } else {
      apply(modes, `${info.label} applied.`)
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
      <PageHeader
        eyebrow="Policy control"
        title="Access"
        description="Choose what YBM can observe, what requires your review, and what may run autonomously. Runtime approval gates still apply to critical operations."
        actions={
          <Button variant="destructive" disabled={allOff || update.isPending} onClick={handleKillSwitch}>
            <Power className="size-4" />
            Disable all
          </Button>
        }
      />

      {data.warnings.length > 0 && (
        <Alert variant="destructive">
          <AlertTitle>Configuration warnings</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-4">
              {data.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      <Card className="bg-card shadow-sm ring-border">
        <CardContent className="grid gap-5 sm:grid-cols-[1fr_auto] sm:items-center">
          <div className="flex items-start gap-3">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
              <ShieldCheck className="size-5" />
            </span>
            <div>
              <h2 className="font-semibold">Current safety posture</h2>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                {allOff
                  ? "All capability groups are disabled. YBM can still chat, but gated tools cannot run."
                  : autonomousCount > 0
                    ? `${autonomousCount} group${autonomousCount === 1 ? "" : "s"} can act without a per-action review.`
                    : "Every enabled action group is either read-only or pauses for your review."}
              </p>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2 text-center">
            <Metric value={enabledCount} label="Enabled" tone="info" />
            <Metric value={approvalCount} label="Guarded" tone="warning" />
            <Metric value={autonomousCount} label="Autonomous" tone={autonomousCount > 0 ? "danger" : "neutral"} />
          </div>
        </CardContent>
      </Card>

      <section>
        <div className="mb-3">
          <h2 className="text-base font-semibold">Choose a starting posture</h2>
          <p className="mt-1 text-sm text-muted-foreground">Presets update every group; you can fine-tune individual capabilities below.</p>
        </div>
        <div className="grid gap-3 lg:grid-cols-3">
          {ACCESS_PRESETS.map((preset) => {
            const expected = computePreset(accessModes, preset.key)
            const active = Object.entries(expected).every(([name, mode]) => accessModes[name]?.mode === mode)
            const Icon = preset.key === "read_only" ? Eye : preset.key === "approval_required" ? UserCheck : Zap
            return (
              <button
                key={preset.key}
                type="button"
                aria-pressed={active}
                disabled={update.isPending || active}
                onClick={() => handlePreset(preset.key)}
                className={`group rounded-2xl border p-4 text-left transition-all disabled:cursor-default ${
                  active
                    ? "border-primary bg-primary/7 ring-2 ring-primary/15"
                    : preset.destructive
                      ? "border-destructive/20 bg-card hover:border-destructive/45 hover:bg-destructive/5"
                      : "border-border bg-card hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-md"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <span className={`flex size-9 items-center justify-center rounded-xl ${preset.destructive ? "bg-destructive/10 text-destructive" : "bg-primary/10 text-primary"}`}>
                    <Icon className="size-4.5" />
                  </span>
                  <span className={`text-xs font-semibold ${active ? "text-primary" : "text-muted-foreground"}`}>
                    {active ? "Current" : "Apply"}
                  </span>
                </div>
                <p className="mt-3 font-semibold">{preset.label}</p>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{preset.description}</p>
              </button>
            )
          })}
        </div>
      </section>

      <section>
        <div className="mb-3">
          <h2 className="text-base font-semibold">Capability groups</h2>
          <p className="mt-1 text-sm text-muted-foreground">Adjust one area without changing the rest of your policy.</p>
        </div>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {groups.map((group) => (
          <AccessGroupCard
            key={group.name}
            group={group}
            rawPolicies={data.config.capabilities}
            advanced={advanced}
            pending={update.isPending}
            onChange={(mode) =>
              apply(
                { [group.name]: mode },
                `${group.label ?? group.name} set to ${group.options.find((o) => o.value === mode)?.label ?? mode}.`,
              )
            }
          />
        ))}
        </div>
      </section>

      <ActiveGrantsCard />

      <SecretVaultCard />

      <ConfirmDialog
        open={pendingAction != null}
        onOpenChange={(open) => {
          if (!open) setPendingAction(null)
        }}
        title={pendingAction?.title ?? ""}
        description={pendingAction?.description ?? ""}
        confirmLabel={pendingAction?.confirmLabel ?? "Confirm"}
        pending={update.isPending}
        onConfirm={() => {
          if (pendingAction) apply(pendingAction.modes, "Access updated.")
        }}
      />
      </div>
    </div>
  )
}

function Metric({ value, label, tone }: { value: number; label: string; tone: "info" | "warning" | "danger" | "neutral" }) {
  const toneClass = {
    info: "bg-info/10 text-info",
    warning: "bg-warning/10 text-warning",
    danger: "bg-destructive/10 text-destructive",
    neutral: "bg-muted text-muted-foreground",
  }[tone]
  return (
    <div className={`min-w-17 rounded-xl px-2.5 py-2 ${toneClass}`}>
      <div className="text-lg font-semibold leading-none">{value}</div>
      <div className="mt-1 text-[10px] font-medium uppercase tracking-wide">{label}</div>
    </div>
  )
}
