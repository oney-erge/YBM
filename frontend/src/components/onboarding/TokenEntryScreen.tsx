import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { ApiError, establishSession, getSummary, setAdminToken } from "@/lib/api"
import { ThemeToggle } from "@/components/layout/ThemeToggle"

/**
 * The fallback sign-in, for the rare browser that has neither the launch link
 * nor a session cookie (a different browser than the one YBM opened, cleared
 * site data, a link copied from another machine).
 *
 * Almost no one should ever see it. Every launcher, the tray icon and
 * `ybm admin-url` open the console with a one-time `?token=` link, and the
 * console turns that into a lasting session cookie - so the token is never
 * something a person has to find. When this does appear, it points at the
 * easy way back in first and only then at the token itself.
 */
export function TokenEntryScreen({ onVerified }: { onVerified: () => void }) {
  const [token, setToken] = useState("")
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    const trimmed = token.trim()
    if (!trimmed) return
    setChecking(true)
    setError(null)
    setAdminToken(trimmed)
    try {
      await getSummary(1)
      // Make it stick, so this is the last time this browser is asked.
      await establishSession()
      onVerified()
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "That token isn't right." : "Could not reach the backend.")
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="relative flex h-svh w-full items-center justify-center bg-background p-6">
      <div className="absolute top-4 right-4"><ThemeToggle /></div>
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>Sign in to YBM</CardTitle>
          <CardDescription>
            This browser isn&apos;t signed in yet. The easiest way back in is to open YBM from its
            shortcut (or run <code className="rounded bg-muted px-1 py-0.5">run.bat</code> /{" "}
            <code className="rounded bg-muted px-1 py-0.5">./run.sh</code> again); it opens this page
            already signed in.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="flex flex-col gap-3" onSubmit={handleSubmit}>
            <div className="flex flex-col gap-1">
              <Label htmlFor="admin-token" className="text-xs text-muted-foreground">
                Or paste the admin token
              </Label>
              <Input
                id="admin-token"
                type="password"
                autoFocus
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
              <p className="text-xs leading-relaxed text-muted-foreground">
                It is saved as <code className="rounded bg-muted px-1 py-0.5">AGENT_ADMIN_TOKEN</code> in{" "}
                <code className="rounded bg-muted px-1 py-0.5">.env</code>, and{" "}
                <code className="rounded bg-muted px-1 py-0.5">ybm admin-url</code> prints a signed-in link.
              </p>
            </div>
            {error && (
              <Alert variant="destructive">
                <AlertTitle>Couldn&apos;t verify that token</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <Button type="submit" disabled={checking || !token.trim()}>
              {checking ? "Checking..." : "Continue"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
