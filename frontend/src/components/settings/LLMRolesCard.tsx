import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ApiError, type LLMRolesInput, type SettingsSummary } from "@/lib/api"
import { useServerForm } from "@/lib/use-server-form"
import { useSettingsSummary, useUpdateLLMRoles } from "@/lib/queries"

const SAME_AS_DEFAULT = "__default__"

type Draft = {
  concierge: string
  operator: string
  auditor: string
  fallbackChain: string
}

function deriveDraft(data: SettingsSummary): Draft {
  return {
    concierge: data.config.llm.concierge_profile ?? SAME_AS_DEFAULT,
    operator: data.config.llm.operator_profile ?? SAME_AS_DEFAULT,
    auditor: data.config.llm.auditor_profile ?? SAME_AS_DEFAULT,
    fallbackChain: data.config.llm.fallback_chain.join(", "),
  }
}

function parseChain(value: string): string[] {
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
}

/**
 * Per-role model routing (docs/ROADMAP.md "per-role models") - Concierge,
 * Operator, and Auditor default to sharing one profile; this is where that
 * gets pulled apart, e.g. a fast local model for Concierge and a stronger
 * one for Operator. Advanced-only: SettingsPage's own comment used to list
 * this as unbuilt ("real new backend machinery") - it now is built, so this
 * card is what fills that gap.
 */
export function LLMRolesCard() {
  const { data, isPending } = useSettingsSummary()
  const [draft, setDraft, resetDraft] = useServerForm(data, deriveDraft)
  const updateRoles = useUpdateLLMRoles()

  if (isPending || !draft || !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Per-role models</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-24 w-full" />
        </CardContent>
      </Card>
    )
  }

  const profileNames = Object.keys(data.config.llm.profiles)
  const defaultLabel = `Same as default (${data.config.llm.default_profile})`

  function roleValue(role: string): string | null {
    return role === SAME_AS_DEFAULT ? null : role
  }

  function handleSubmit() {
    const input: LLMRolesInput = {
      concierge_profile: roleValue(draft!.concierge),
      operator_profile: roleValue(draft!.operator),
      auditor_profile: roleValue(draft!.auditor),
      fallback_chain: parseChain(draft!.fallbackChain),
    }
    updateRoles.mutate(input, {
      onSuccess: () => {
        toast.success("Per-role models saved. Restart long-running processes to pick it up.")
        resetDraft()
      },
      onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not save per-role models."),
    })
  }

  const roles: { key: keyof Pick<Draft, "concierge" | "operator" | "auditor">; label: string; description: string }[] = [
    { key: "concierge", label: "Concierge", description: "Classifies each incoming message and replies to plain chat." },
    { key: "operator", label: "Operator", description: "Plans and executes every task step." },
    { key: "auditor", label: "Auditor", description: "Checks a task's result actually answers the request before it completes." },
  ]

  return (
    <Card>
      <CardHeader>
        <CardTitle>Per-role models</CardTitle>
        <CardDescription>
          Concierge, Operator, and Auditor share the default model unless given their own here - a
          fast local model for Concierge, a stronger one for Operator, for example.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {roles.map((role) => (
            <Label key={role.key} className="flex flex-col gap-1">
              <span className="text-xs font-medium">{role.label}</span>
              <span className="text-xs text-muted-foreground">{role.description}</span>
              <Select
                value={draft[role.key]}
                onValueChange={(value) => value && setDraft({ ...draft, [role.key]: value })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SAME_AS_DEFAULT}>{defaultLabel}</SelectItem>
                  {profileNames.map((name) => (
                    <SelectItem key={name} value={name}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>
          ))}
        </div>

        <Label className="flex flex-col gap-1 border-t border-border pt-3">
          <span className="text-xs font-medium">Fallback chain</span>
          <span className="text-xs text-muted-foreground">
            Profiles to try, in order, after a role's own model is unavailable - each gets a cooldown
            after failing so a subsequent call skips straight to the next one. Comma-separated profile
            names.
          </span>
          <Input
            value={draft.fallbackChain}
            onChange={(e) => setDraft({ ...draft, fallbackChain: e.target.value })}
            placeholder="cloud, local"
          />
        </Label>

        <div className="flex justify-end">
          <Button type="button" size="sm" disabled={updateRoles.isPending} onClick={handleSubmit}>
            Save per-role models
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
