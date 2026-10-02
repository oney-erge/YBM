import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import {
  ApiError,
  fetchChannels,
  type ChannelSpec,
} from "@/lib/api"
import {
  useSelectLLMPreset,
  useSettingsSummary,
  useSetupDetect,
  useUpdateLLMConfig,
  useUpdateTelegramConfig,
} from "@/lib/queries"
import { ThemeToggle } from "@/components/layout/ThemeToggle"
import { ProviderPicker } from "@/components/onboarding/ProviderPicker"
import { ChannelGrid } from "@/components/onboarding/ChannelGrid"
import {
  TelegramConnectionGuide,
  type TelegramConnection,
} from "@/components/onboarding/TelegramConnectionGuide"

type Step = "brain" | "face" | "done"

/**
 * First-run wizard (docs/UI_REWRITE_PLAN.md §14) - shown when
 * `bootstrap.onboarding_complete` is false (backend: no LLM profile is
 * actually configured yet - `ybm setup` always creates config.yaml now, so
 * that alone can't be the signal), or when re-triggered manually from
 * Settings. Skippable at every step; every mutating step reuses an
 * existing config endpoint.
 *
 * Runs on real detection (`GET /api/setup/detect`, mirroring
 * bootstrap.py's own _prompt_llm_choice/_prompt_telegram_choice CLI
 * logic) rather than blind prompts: a real Ollama model list to click,
 * and "already configured" states for LocalDeploy/a cloud key/Telegram
 * that skip the question entirely instead of re-asking for something
 * that's already sitting in .env.
 */
export function OnboardingWizard({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<Step>("brain")
  const { data: detect, isPending: detectPending, refetch: refetchDetect } = useSetupDetect(true)
  const { data: settingsData } = useSettingsSummary()
  const selectPreset = useSelectLLMPreset()
  const updateLLM = useUpdateLLMConfig()
  const updateTelegram = useUpdateTelegramConfig()

  const [channels, setChannels] = useState<ChannelSpec[]>([])
  const [telegramEnabled, setTelegramEnabled] = useState(false)
  // The guided Telegram sequence. Enabling used to save a token and nothing
  // else, which produced a bot that ignored every message: the allowlist is
  // what makes it answer, and _authorization_decision fails closed on an empty
  // one. `linked` is the id learned from the user's own first message, so
  // nobody has to go and find a numeric id.
  const [telegramConnection, setTelegramConnection] = useState<TelegramConnection | null>(null)

  useEffect(() => {
    fetchChannels()
      .then((data) => setChannels(data.channels))
      // The grid is additive: if the catalog cannot be read the step still
      // works, it just shows no cards.
      .catch(() => setChannels([]))
  }, [])

  const presets = settingsData?.integrations.llm.presets ?? []

  return (
    <div className="relative flex h-svh w-full items-center justify-center bg-background p-6">
      <div className="absolute top-4 right-4"><ThemeToggle /></div>
      <Card className="w-full max-w-xl shadow-lg">
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-sm font-semibold text-primary-foreground">
              Y
            </div>
            <div>
              {/* CardTitle renders a div, and the wizard takes over the whole
                  page - so without this the first screen a new user meets has
                  no heading at all for a screen reader to navigate by. */}
              <CardTitle role="heading" aria-level={1}>Welcome to YBM</CardTitle>
              <CardDescription>
                Two quick questions. Skippable, and re-runnable later from Settings.
              </CardDescription>
            </div>
          </div>
          {/* A dot per step beats "Step 1 of 2" buried in the body text. */}
          {step !== "done" && (
            <div className="mt-3 flex items-center gap-1.5" aria-hidden>
              <span className="h-1.5 w-10 rounded-full bg-primary" />
              <span
                className={`h-1.5 w-10 rounded-full ${step === "face" ? "bg-primary" : "bg-border"}`}
              />
            </div>
          )}
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {detectPending && <Skeleton className="h-32 w-full" />}

          {!detectPending && detect && step === "brain" && (
            <>
              <p className="text-sm font-medium">Step 1 of 2 &middot; Which model should it use?</p>

              {/* Say what was found before offering choices. The list used to
                  be presented with equal confidence whether or not anything
                  behind it was reachable, and in a container none of the
                  loopback presets can work at all. */}
              {!detect.llm_configured && (
                <p className="text-xs text-muted-foreground">
                  {detect.ollama.available
                    ? `Found Ollama on this machine with ${detect.ollama.models.length} model(s) installed.`
                    : detect.ollama.reachable
                      ? "Ollama is running here but has no models installed yet."
                      : "No local model server found on this machine. A cloud API key is the quickest way to start."}
                </p>
              )}

              {!detect.llm_configured && (
                <p className="text-sm font-medium">Run a model on this computer - free and private</p>
              )}

              {detect.llm_configured ? (
                <div className="flex flex-col gap-2 rounded-md border border-border bg-muted/30 p-3">
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary">Already configured</Badge>
                    <span className="text-sm font-mono">{detect.current_llm_profile}</span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    This model is reachable and ready to use as-is.
                  </p>
                </div>
              ) : (
                <>
                  {detect.ollama.available && (
                    <div className="flex flex-col gap-2">
                      <p className="text-xs text-muted-foreground">
                        {detect.ollama.recommended
                          ? "Found a local Ollama server. The recommended model is first - pick it to continue."
                          : `Found a local Ollama server with ${detect.ollama.models.length} model(s):`}
                      </p>
                      {[...detect.ollama.models]
                        .sort((a, b) =>
                          a === detect.ollama.recommended ? -1 : b === detect.ollama.recommended ? 1 : 0,
                        )
                        .slice(0, 8)
                        .map((model) => (
                        <Button
                          key={model}
                          variant={model === detect.ollama.recommended ? "default" : "outline"}
                          className="justify-start font-mono"
                          disabled={updateLLM.isPending}
                          onClick={() => {
                            updateLLM.mutate(
                              {
                                profile_name: "onboard",
                                default_profile: "onboard",
                                provider: "openai_compatible",
                                model,
                                base_url: "http://127.0.0.1:11434/v1",
                                api_key_env: null,
                                timeout_seconds: 120,
                                max_tokens: 4096,
                                temperature: 0.2,
                                api_key_value: null,
                              },
                              {
                                onSuccess: () => {
                                  toast.success(`${model} selected.`)
                                  setStep("face")
                                },
                                onError: (err) => {
                                  toast.error(err instanceof ApiError ? err.message : "Could not select this model.")
                                },
                              },
                            )
                          }}
                        >
                          {model}
                          {model === detect.ollama.recommended && (
                            <span className="ml-2 text-xs opacity-80">recommended</span>
                          )}
                        </Button>
                      ))}
                    </div>
                  )}

                  {/* Ollama is running but nothing is pulled. Without this the
                      wizard looked identical to "no Ollama at all", and the
                      user had to leave, find a model name, and come back - the
                      only such point in onboarding. */}
                  {!detect.ollama.available && detect.ollama.reachable && (
                    <div className="flex flex-col gap-2 rounded-md border border-border bg-muted/30 p-3">
                      <Badge variant="secondary" className="w-fit">
                        Ollama running, no models yet
                      </Badge>
                      <p className="text-xs text-muted-foreground">
                        Pull one and reload this page - about 5 GB, a few minutes:
                      </p>
                      <code className="rounded bg-background px-2 py-1 font-mono text-xs">
                        ollama pull qwen3:8b
                      </code>
                      <Button size="sm" variant="outline" className="self-start" onClick={() => refetchDetect()}>
                        I've pulled a model - check again
                      </Button>
                    </div>
                  )}

                  {!detect.ollama.available && !detect.ollama.reachable && detect.localdeploy_root_present && (
                    <div className="flex flex-col gap-2 rounded-md border border-border bg-muted/30 p-3">
                      <Badge variant="secondary" className="w-fit">
                        LocalDeploy detected
                      </Badge>
                      <p className="text-xs text-muted-foreground">
                        YBM_LOCALDEPLOY_ROOT is set - keeping the shipped LocalDeploy profile.
                      </p>
                      <Button size="sm" className="self-start" onClick={() => setStep("face")}>
                        Continue
                      </Button>
                    </div>
                  )}

                  {!detect.ollama.available && !detect.localdeploy_root_present && detect.openai_key_present && (
                    <div className="flex flex-col gap-2 rounded-md border border-border bg-muted/30 p-3">
                      <Badge variant="secondary" className="w-fit">
                        API key found
                      </Badge>
                      <p className="text-xs text-muted-foreground">
                        OPENAI_API_KEY is already set - keeping the shipped cloud profile that uses it.
                      </p>
                      <Button size="sm" className="self-start" onClick={() => setStep("face")}>
                        Continue
                      </Button>
                    </div>
                  )}

                  {presets.length > 0 && (
                    <div className="flex flex-col gap-2">
                      {presets.map((preset) => (
                        <Button
                          key={preset.key}
                          variant="outline"
                          // A model that cannot fit is not offered as a choice
                          // that fails later; it is shown greyed with the two
                          // numbers that make the reason obvious.
                          disabled={preset.fit?.status === "too_big" || selectPreset.isPending}
                          title={preset.fit?.reason ?? undefined}
                          // whitespace-normal overrides the Button base's
                          // whitespace-nowrap. Without it the label is clipped
                          // at the card's edge on a phone, and "(~3 GB
                          // download)" - the number the choice actually turns
                          // on - is the part that disappears.
                          className="h-auto w-full flex-col items-start gap-0.5 py-2 text-left whitespace-normal"
                          onClick={() => {
                            selectPreset.mutate(preset.key, {
                              onSuccess: () => {
                                toast.success(`${preset.label ?? preset.key} selected.`)
                                setStep("face")
                              },
                              onError: (err) => {
                                toast.error(err instanceof ApiError ? err.message : "Could not select this preset.")
                              },
                            })
                          }}
                        >
                          <span className="font-medium">{preset.label ?? preset.key}</span>
                          {preset.fit?.reason && (
                            <span
                              className={`text-xs font-normal ${
                                preset.fit.status === "too_big" ? "text-destructive" : "text-muted-foreground"
                              }`}
                            >
                              {preset.fit.reason}
                            </span>
                          )}
                        </Button>
                      ))}
                    </div>
                  )}

                  {/* Bring your own key. The old version of this block sent
                      base_url: null, so it could never actually reach a cloud
                      provider - and it offered no provider choice at all.
                      ProviderPicker verifies the key and asks the provider
                      what models it has before saving anything. */}
                  {/* 2b: downloading several GB is not something to discover
                      after clicking. Say what will happen, before it happens. */}
                  <p className="rounded-md border border-border bg-muted/30 p-2 text-xs text-muted-foreground">
                    Picking one of these installs LocalDeploy if it is missing, downloads the model,
                    and runs it on this computer. It costs nothing to use and nothing you type leaves
                    the machine.
                  </p>

                  <details className="text-sm" open={!detect.ollama.available}>
                    <summary className="cursor-pointer font-medium">
                      Or use an API key - Anthropic, OpenAI, and 11 others
                    </summary>
                    <div className="mt-2">
                      <ProviderPicker onConfigured={() => setStep("face")} />
                    </div>
                  </details>
                </>
              )}

              {!detect.llm_configured && (
                <p className="text-xs text-muted-foreground">
                  Skipping is fine - you can pick a model later in Settings. Until you do, YBM has nothing to think
                  with and cannot answer.
                </p>
              )}
              <div className="flex justify-between">
                <Button variant="ghost" size="sm" onClick={() => setStep("face")}>
                  Skip
                </Button>
                {detect.llm_configured && (
                  <Button size="sm" onClick={() => setStep("face")}>
                    Continue
                  </Button>
                )}
              </div>
            </>
          )}

          {!detectPending && detect && step === "face" && (
            <>
              <p className="text-sm font-medium">Step 2 of 2 &middot; Where can you reach it?</p>
              <p className="text-sm text-muted-foreground">
                Web chat already works. Connect a messaging app to reach YBM from your phone.
              </p>

              {/* The catalog, not a hardcoded toggle - a single "Also enable
                  Telegram" switch implied Telegram was the only thing that
                  would ever exist. */}
              {channels.length > 0 && (
                <ChannelGrid
                  channels={channels}
                  onConnect={(key) => {
                    if (key === "telegram") setTelegramEnabled(true)
                  }}
                />
              )}

              {telegramEnabled && (
                <TelegramConnectionGuide
                  tokenPresent={detect.telegram_token_present}
                  onLinked={setTelegramConnection}
                />
              )}

              {telegramEnabled && (
                <p className="text-xs text-muted-foreground">
                  One more step after this: Settings → Telegram → <b>Find me</b>, to say who is
                  allowed to message the bot. Until then it refuses everyone, including you.
                </p>
              )}

              <div className="flex justify-between">
                <div className="flex gap-1">
                  <Button variant="ghost" size="sm" onClick={() => setStep("brain")}>
                    Back
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setStep("done")}>
                    Skip
                  </Button>
                </div>
                <Button
                  size="sm"
                  disabled={
                    updateTelegram.isPending ||
                    // Requiring the link is the point: enabling Telegram
                    // without an allowlist produces a bot that silently
                    // ignores its owner.
                    (telegramEnabled && !telegramConnection)
                  }
                  onClick={() => {
                    if (!telegramEnabled) {
                      setStep("done")
                      return
                    }
                    updateTelegram.mutate(
                      {
                        enabled: true,
                        token_env: "TELEGRAM_BOT_TOKEN",
                        bot_token: telegramConnection?.botToken ?? null,
                        // Without these the bot ignores every message:
                        // _authorization_decision fails closed on an empty
                        // allowlist. Learned from the user's own first message
                        // rather than asking them to find a numeric id.
                        ...(telegramConnection?.message.user_id
                          ? { allowed_user_ids: [telegramConnection.message.user_id] }
                          : {}),
                        ...(telegramConnection?.message.chat_id
                          ? { allowed_chat_ids: [telegramConnection.message.chat_id] }
                          : {}),
                      },
                      {
                        onSuccess: () => {
                          // Deliberately not "Telegram enabled." - this step
                          // writes no allowlist, and an empty allowlist denies
                          // every message. Reporting plain success here is what
                          // sent people off to message a bot that would never
                          // answer, with nothing anywhere saying why.
                          toast.success("Token saved - now pick who may use it.")
                          setStep("done")
                        },
                        onError: (err) => {
                          toast.error(err instanceof ApiError ? err.message : "Could not save Telegram config.")
                        },
                      },
                    )
                  }}
                >
                  Continue
                </Button>
              </div>
            </>
          )}

          {step === "done" && (
            <>
              <p className="text-sm font-medium">
                {detect?.llm_configured ? "You're set." : "Almost there - YBM still needs a model."}
              </p>
              {!detect?.llm_configured && (
                <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3">
                  <p className="text-sm">
                    Without a model YBM has nothing to think with, so it cannot answer yet. Choose one now, or any
                    time in Settings.
                  </p>
                  <Button size="sm" variant="outline" className="mt-2" onClick={() => setStep("brain")}>
                    Choose a model
                  </Button>
                </div>
              )}
              {telegramEnabled && (
                <div className="rounded-md border border-border bg-muted/30 p-3">
                  <p className="text-sm font-medium">One thing left for Telegram</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Go to <b>Settings → Telegram</b>, message your bot, and press <b>Find me</b>.
                    Nobody is on the allowlist yet, so the bot refuses every message until then.
                  </p>
                </div>
              )}
              <p className="text-sm text-muted-foreground">
                High-impact capabilities are off by default - YBM will ask before it touches anything. You can
                turn capabilities on in Access whenever you need them.
              </p>
              {/* Ending on an empty chat box leaves the user to invent a first
                  request. Suggesting one makes the first act a working round
                  trip instead. */}
              <p className="text-xs text-muted-foreground">Try asking it something like:</p>
              <code className="rounded bg-muted px-2 py-1 text-xs">What can you do?</code>
              <div className="flex justify-between">
                <Button variant="ghost" size="sm" onClick={() => setStep("face")}>
                  Back
                </Button>
                <Button size="sm" onClick={onDone}>
                  Start chatting
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
