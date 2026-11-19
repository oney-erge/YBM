import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ApiError, fetchInitiativeConfig, updateInitiativeConfig } from "@/lib/api"

const LEVELS = [
  { value: "1", label: "1 - Ask", detail: "Ask before meaningful implementation choices." },
  { value: "2", label: "2 - Balanced", detail: "Choose routine details and ask only when a missing fact blocks the work." },
  { value: "3", label: "3 - Autonomous", detail: "Do not ask: work out what is meant, say what was assumed, choose methods, grant routine approvals. Payments and anything unrecoverable still wait for you." },
]

export function InitiativeSettingsCard() {
  const [level, setLevel] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    fetchInitiativeConfig().then((data) => setLevel(String(data.level))).catch((err) => {
      setError(err instanceof ApiError ? err.message : "Could not load the decision preference.")
    })
  }, [])

  async function save() {
    if (!level) return
    setSaving(true)
    try {
      await updateInitiativeConfig(Number(level))
      const effective = await fetchInitiativeConfig()
      setLevel(String(effective.level))
      toast.success(effective.level === Number(level) ? "Decision preference saved." : "Saved, but another configuration source takes precedence.")
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not save the decision preference.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Decision preference</CardTitle>
        <CardDescription>Choose how much YBM does without asking. Tool access stays as configured; payments always wait for your yes.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {error ? <p role="alert">{error}</p> : level && (
          <>
            <Label htmlFor="initiative-level">How should YBM handle routine choices?</Label>
            <Select value={level} onValueChange={(value) => value && setLevel(value)}>
              <SelectTrigger id="initiative-level"><SelectValue>{LEVELS.find((item) => item.value === level)?.label}</SelectValue></SelectTrigger>
              <SelectContent>{LEVELS.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground">{LEVELS.find((item) => item.value === level)?.detail} This guides the model; it cannot guarantee every choice.</p>
            <Button type="button" onClick={save} disabled={saving}>{saving ? "Saving..." : "Save preference"}</Button>
          </>
        )}
      </CardContent>
    </Card>
  )
}
