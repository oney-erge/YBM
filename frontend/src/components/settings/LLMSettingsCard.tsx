import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError, type LLMConfigInput, type SettingsSummary } from "@/lib/api"
import { useServerForm } from "@/lib/use-server-form"
import { useSelectLLMPreset, useSettingsSummary, useTestLLM, useUpdateLLMConfig } from "@/lib/queries"
import { useAdvancedMode } from "@/lib/advanced-mode"
import { ProviderPicker } from "@/components/onboarding/ProviderPicker"

type Draft = {
  profileName: string
  defaultProfile: string
  provider: string
  model: string
  baseUrl: string
  apiKeyEnv: string
  apiKeyValue: string
  timeoutSeconds: string
  maxTokens: string
  temperature: string
}

function deriveDraft(data: SettingsSummary): Draft {
  const name = data.config.llm.default_profile
  const profile = data.config.llm.profiles[name]
  return {
    profileName: name,
    defaultProfile: name,
    provider: profile?.provider ?? "openai_compatible",
    model: profile?.model ?? "",
    baseUrl: profile?.base_url ?? "",
    apiKeyEnv: profile?.api_key_env ?? "",
    apiKeyValue: "",
    timeoutSeconds: String(profile?.timeout_seconds ?? 60),
    maxTokens: String(profile?.max_tokens ?? 4096),
    temperature: String(profile?.temperature ?? 0.2),
  }
}

/**
 * Level 1 (docs/archive/UI_REWRITE_PLAN.md §14): model picker via presets + a
 * "Test connection" button, plus the full manual profile form. Ports
 * Streamlit's `_render_llm_config`.
 */
export function LLMSettingsCard() {
  const { data, isPending } = useSettingsSummary()
  const [draft, setDraft, resetDraft] = useServerForm(data, deriveDraft)
  const updateLLM = useUpdateLLMConfig()
  const selectPreset = useSelectLLMPreset()
  const testLLM = useTestLLM()
  const { advanced } = useAdvancedMode()

  const presets = data?.integrations.llm.presets ?? []

  function handlePreset(key: string) {
    selectPreset.mutate(key, {
      onSuccess: () => {
        toast.success("Model saved. New tasks and chat replies use it right away.")
        resetDraft()
      },
      onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not apply the preset."),
    })
  }

  function handleTest() {
    testLLM.mutate(undefined, {
      onSuccess: (result) => toast.success(result.output_preview || "LLM responded."),
      onError: (err) => toast.error(err instanceof ApiError ? err.message : "LLM test failed."),
    })
  }

  if (isPending || !draft || !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>LLM</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    )
  }

  const input: LLMConfigInput = {
    profile_name: draft.profileName,
    default_profile: draft.defaultProfile,
    provider: draft.provider,
    model: draft.model,
    base_url: draft.baseUrl || null,
    api_key_env: draft.apiKeyEnv || null,
    timeout_seconds: Number(draft.timeoutSeconds) || 60,
    max_tokens: Number(draft.maxTokens) || 4096,
    temperature: Number(draft.temperature) || 0.2,
    api_key_value: draft.apiKeyValue || null,
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Model</CardTitle>
        {/* "The model the Concierge, Operator, and Auditor all currently share"
            named three internal components at a first-time user. What they
            care about is what happens to their request. */}
        <CardDescription>
          The model YBM uses to understand requests, do the work, and check the result.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {presets.length > 0 && (
          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">Run a model on this computer - free and private</p>
            <p className="text-xs text-muted-foreground">
              Installs LocalDeploy if it is missing, downloads the model, and runs it here. Nothing
              you type leaves the machine.
            </p>
          <div className="flex flex-wrap gap-2">
            {presets.map((preset) => (
              <Button
                key={preset.key}
                type="button"
                size="sm"
                variant={preset.active ? "default" : "outline"}
                disabled={preset.active || selectPreset.isPending}
                onClick={() => handlePreset(preset.key)}
              >
                {preset.label ?? preset.key}
              </Button>
            ))}
          </div>
          </div>
        )}

        <Button type="button" variant="outline" size="sm" className="self-start" disabled={testLLM.isPending} onClick={handleTest}>
          {testLLM.isPending ? "Testing..." : "Send a test message to the current model"}
        </Button>

        {/* There was no way to reach the 13 providers the first-run wizard
            offers, so someone who set up Anthropic during onboarding could not
            change it without editing YAML - and "how do I add a remote API?"
            had no answer on this page at all. Same component as the wizard, so
            the key is verified and the model must actually answer before
            anything is saved. */}
        <details className="border-t border-border pt-3">
          <summary className="cursor-pointer text-sm font-medium">
            Use an API key instead - Anthropic, OpenAI, and 11 others
          </summary>
          <div className="mt-3">
            <ProviderPicker onConfigured={() => window.location.reload()} />
          </div>
        </details>

        {/* Profile name, provider, base URL, API key env, timeout, max tokens
            and temperature were shown to everyone. For the audience the
            installer now targets - no Python, no terminal - the preset row
            above is the whole useful control, and the rest is expert surface
            presented as if it needed attention. Advanced mode already exists
            in the sidebar; this is what it is for. */}
        {!advanced && (
          <p className="border-t border-border pt-3 text-xs text-muted-foreground">
            Pick a model above, or turn on <span className="font-medium">Advanced mode</span> in the sidebar to edit the
            profile directly.
          </p>
        )}

        {advanced && (
        <form
          className="grid grid-cols-1 gap-3 border-t border-border pt-3 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault()
            updateLLM.mutate(input, {
              onSuccess: () => {
                toast.success("Model saved. New tasks and chat replies use it right away.")
                resetDraft()
              },
              onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not save the LLM config."),
            })
          }}
        >
          <Field label="Profile name">
            <Input value={draft.profileName} onChange={(e) => setDraft({ ...draft, profileName: e.target.value })} />
          </Field>
          <Field label="Default profile">
            <Input value={draft.defaultProfile} onChange={(e) => setDraft({ ...draft, defaultProfile: e.target.value })} />
          </Field>
          <Field label="Provider">
            <Input value={draft.provider} onChange={(e) => setDraft({ ...draft, provider: e.target.value })} />
          </Field>
          <Field label="Model">
            <Input value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.target.value })} />
          </Field>
          <Field label="Base URL">
            <Input value={draft.baseUrl} onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })} />
          </Field>
          <Field label="API key env">
            <Input value={draft.apiKeyEnv} onChange={(e) => setDraft({ ...draft, apiKeyEnv: e.target.value })} />
          </Field>
          <Field label="Replace API key">
            <Input
              type="password"
              value={draft.apiKeyValue}
              onChange={(e) => setDraft({ ...draft, apiKeyValue: e.target.value })}
              placeholder={data.config.llm.profiles[draft.profileName]?.api_key_present ? "already set" : "not set"}
            />
          </Field>
          <Field label="Timeout (s)">
            <Input
              type="number"
              value={draft.timeoutSeconds}
              onChange={(e) => setDraft({ ...draft, timeoutSeconds: e.target.value })}
            />
          </Field>
          <Field label="Max tokens">
            <Input type="number" value={draft.maxTokens} onChange={(e) => setDraft({ ...draft, maxTokens: e.target.value })} />
          </Field>
          <Field label="Temperature">
            <Input
              type="number"
              step="0.1"
              value={draft.temperature}
              onChange={(e) => setDraft({ ...draft, temperature: e.target.value })}
            />
          </Field>
          <div className="col-span-full flex justify-end">
            <Button type="submit" size="sm" disabled={updateLLM.isPending}>
              Save LLM config
            </Button>
          </div>
        </form>
        )}
      </CardContent>
    </Card>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Label className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </Label>
  )
}
