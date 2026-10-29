import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  ApiError,
  fetchDecisionConfig,
  testDecision,
  updateDecisionConfig,
  type DecisionMode,
  type DecisionSettings,
  type DecisionTest,
} from "@/lib/api"

const MODES: { value: DecisionMode; label: string; detail: (sure: string) => string }[] = [
  {
    value: "off",
    label: "LLM only",
    detail: () => "The LLM reads every message and decides what it is. On a local 8B model that takes several seconds.",
  },
  {
    value: "shadow",
    label: "LLM decides, decision model compared",
    detail: () =>
      "Nothing changes for you. The decision model answers too, and both answers are recorded so you can see how often they agree.",
  },
  {
    value: "cascade",
    label: "Decision model for clear task routes",
    detail: (sure) =>
      `The decision model decides alone when it is at least ${sure} sure about a file, desktop, document, script, workspace, new-tool or website request. Everything else goes to the LLM.`,
  },
  {
    value: "decision",
    label: "Decision model first",
    detail: (sure) =>
      `The decision model decides every message it is at least ${sure} sure about, chat and status included; the LLM still writes chat replies. Coding-agent requests and unsure messages go to the LLM.`,
  },
]

/**
 * Choose what classifies incoming messages: the LLM concierge, a small local
 * decision model served by LocalDeploy, or both.
 *
 * The choice is only worth making with evidence, so the card shows what the
 * recorded answers say (how often the two agreed) and lets a message be tried
 * against both before anything is switched.
 */
export function DecisionSettingsCard() {
  const [settings, setSettings] = useState<DecisionSettings | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [draft, setDraft] = useState({ mode: "off" as DecisionMode, profile: "", minConfidence: "0.9" })
  const [saving, setSaving] = useState(false)
  const [sample, setSample] = useState("")
  const [includeLlm, setIncludeLlm] = useState(false)
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<DecisionTest | null>(null)

  function load() {
    fetchDecisionConfig()
      .then((loaded) => {
        setSettings(loaded)
        setDraft({
          mode: loaded.decision.mode,
          profile: loaded.decision.profile ?? "",
          minConfidence: String(loaded.decision.min_confidence),
        })
      })
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : "Could not load message routing."))
  }

  useEffect(load, [])

  async function save() {
    const minConfidence = Number(draft.minConfidence)
    if (!(minConfidence >= 0.5 && minConfidence <= 1)) {
      toast.error("The confidence needs to be between 0.5 and 1.")
      return
    }
    setSaving(true)
    try {
      await updateDecisionConfig({ mode: draft.mode, profile: draft.profile.trim() || null, min_confidence: minConfidence })
      toast.success("Message routing saved. Running channels pick it up on their next message.")
      load()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not save that.")
    } finally {
      setSaving(false)
    }
  }

  async function runTest() {
    if (!sample.trim()) return
    setTesting(true)
    setResult(null)
    try {
      setResult(await testDecision(sample.trim(), includeLlm))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not run the test.")
    } finally {
      setTesting(false)
    }
  }

  if (loadError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Message routing</CardTitle>
          <CardDescription>{loadError}</CardDescription>
        </CardHeader>
      </Card>
    )
  }

  if (!settings) return null

  const sure = `${Math.round(Number(draft.minConfidence) * 100) || 90}%`
  const chosen = MODES.find((mode) => mode.value === draft.mode) ?? MODES[0]
  const stats = settings.stats

  return (
    <Card>
      <CardHeader>
        <CardTitle>Message routing</CardTitle>
        <CardDescription>
          What decides whether a message is chat, a status question, or a task, and which tool it needs. A small
          decision model answers in under a second on the CPU; it needs LocalDeploy with a decision profile such
          as <code>d3_lite_decision</code> (see docs/INSTALL.md).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Label className="flex flex-col gap-1">
          <span className="text-xs font-medium">Classifier</span>
          <Select value={draft.mode} onValueChange={(value) => value && setDraft({ ...draft, mode: value as DecisionMode })}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {MODES.map((mode) => (
                <SelectItem key={mode.value} value={mode.value}>
                  {mode.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground">{chosen.detail(sure)}</span>
        </Label>

        {draft.mode !== "off" && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Label className="flex flex-col gap-1">
              <span className="text-xs font-medium">LocalDeploy decision profile</span>
              <Input
                value={draft.profile}
                placeholder="LocalDeploy's default decision profile"
                onChange={(e) => setDraft({ ...draft, profile: e.target.value })}
              />
            </Label>
            <Label className="flex flex-col gap-1">
              <span className="text-xs font-medium">Decide alone at or above</span>
              <Input
                type="number"
                min="0.5"
                max="1"
                step="0.05"
                value={draft.minConfidence}
                onChange={(e) => setDraft({ ...draft, minConfidence: e.target.value })}
              />
            </Label>
          </div>
        )}

        <div>
          <Button size="sm" onClick={save} disabled={saving}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </div>

        <div className="flex flex-col gap-1 border-t border-border pt-3 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">What has been recorded</span>
          {stats.messages === 0 ? (
            <span>Nothing yet. Any choice other than LLM only records the decision model's answers.</span>
          ) : (
            <>
              <span>
                Last {stats.messages} messages: the decision model decided {stats.decided_alone} alone
                {stats.median_ms !== null ? `, answering in ${stats.median_ms} ms (median)` : ""}.
                {stats.unanswered > 0 ? ` It gave no answer for ${stats.unanswered}.` : ""}
              </span>
              {stats.agreement !== null && (
                <span>
                  Where the LLM decided, they agreed on {stats.agreed} of {stats.compared} (
                  {Math.round(stats.agreement * 100)}%).
                </span>
              )}
              {stats.recent_disagreements.map((item, index) => (
                <span key={index} className="truncate">
                  "{item.text}": decision model {item.decision_route}
                  {item.decision_confidence !== null ? ` (${item.decision_confidence.toFixed(2)})` : ""}, LLM{" "}
                  {item.llm_route}
                </span>
              ))}
            </>
          )}
        </div>

        <div className="flex flex-col gap-2 border-t border-border pt-3">
          <span className="text-xs font-medium">Try a message</span>
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              value={sample}
              placeholder="what's in my Downloads folder?"
              onChange={(e) => setSample(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void runTest()}
            />
            <Button size="sm" variant="outline" onClick={runTest} disabled={testing || !sample.trim()}>
              {testing ? "Asking..." : "Try"}
            </Button>
          </div>
          <div className="flex items-center gap-2">
            <Switch id="decision-include-llm" checked={includeLlm} onCheckedChange={setIncludeLlm} />
            <Label htmlFor="decision-include-llm" className="text-xs">
              Also ask the LLM (slower)
            </Label>
          </div>
          {result && (
            <div className="flex flex-col gap-1 text-xs">
              {"error" in result.decision ? (
                <span className="text-destructive">Decision model: {result.decision.error}</span>
              ) : (
                <span>
                  Decision model: <code>{result.decision.route}</code> at {result.decision.confidence.toFixed(2)} in{" "}
                  {result.decision.latency_ms} ms.{" "}
                  {result.decision.decides_alone
                    ? "With the saved setting it would decide this alone."
                    : "With the saved setting the LLM would decide this."}
                </span>
              )}
              {result.llm &&
                ("error" in result.llm ? (
                  <span className="text-destructive">LLM: {result.llm.error}</span>
                ) : (
                  <span>
                    LLM: <code>{result.llm.route}</code> in {(result.llm.latency_ms / 1000).toFixed(1)} s.
                  </span>
                ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
