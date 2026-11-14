import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Eye, EyeOff, KeyRound, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { ApiError } from "@/lib/api"
import { useDeleteSecret, useInitSecretVault, useRevealAccount, useSecrets, useSetSecret } from "@/lib/queries"

/** How long a shown password stays on the screen. */
const SHOWN_FOR_MS = 20_000

/**
 * A login YBM made for a website (service "accounts", the key is the site). The password is never in the list; pressing Show asks
 * the backend for it, shows it for a few seconds, and the audit trail records which site was shown.
 */
function AccountRow({ host, onDelete }: { host: string; onDelete: () => void }) {
  const reveal = useRevealAccount()
  const [login, setLogin] = useState<{ username: string; password: string } | null>(null)

  useEffect(() => {
    if (!login) return
    const timer = window.setTimeout(() => setLogin(null), SHOWN_FOR_MS)
    return () => window.clearTimeout(timer)
  }, [login])

  function show() {
    reveal.mutate(host, {
      onSuccess: (entry) => setLogin({ username: entry.username, password: entry.password }),
      onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not show that login."),
    })
  }

  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg bg-muted/60 px-3 py-2 text-sm">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="min-w-0 [overflow-wrap:anywhere]">
          <span className="font-medium">{host}</span> <span className="text-muted-foreground">(login YBM made)</span>
        </span>
        <span className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            aria-label={login ? `Hide the login for ${host}` : `Show the login for ${host}`}
            disabled={reveal.isPending}
            onClick={() => (login ? setLogin(null) : show())}
          >
            {login ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
            {login ? "Hide" : "Show"}
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label={`Delete the login for ${host}`} onClick={onDelete}>
            <Trash2 className="size-3.5" />
          </Button>
        </span>
      </div>
      {login && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono text-xs">
          <dt className="text-muted-foreground">Username</dt>
          <dd className="[overflow-wrap:anywhere]">{login.username || "(none saved)"}</dd>
          <dt className="text-muted-foreground">Password</dt>
          <dd className="[overflow-wrap:anywhere] select-all">{login.password}</dd>
        </dl>
      )}
    </div>
  )
}

/**
 * Secret vault (docs/archive/UI_REWRITE_PLAN.md §13) - list `service.key`, never
 * values (storage/secrets.py never returns them either), add, delete.
 * Ports Streamlit's admin_streamlit.py `_render_secrets_config`.
 */
export function SecretVaultCard() {
  const { data, isPending, isError, error } = useSecrets()
  const setSecret = useSetSecret()
  const deleteSecret = useDeleteSecret()
  const initVault = useInitSecretVault()
  const [service, setService] = useState("")
  const [key, setKey] = useState("")
  const [value, setValue] = useState("")
  const [pendingDelete, setPendingDelete] = useState<{ service: string; key: string } | null>(null)

  function handleDelete() {
    if (!pendingDelete) return
    const target = pendingDelete
    deleteSecret.mutate(target, {
      onSuccess: () => toast.success(`Deleted ${target.service}.${target.key}.`),
      onError: (err) => {
        toast.error(err instanceof ApiError ? err.message : "Could not delete the secret.")
      },
    })
  }

  return (
    <Card className="shadow-sm ring-border">
      <CardHeader className="flex flex-row items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
          <KeyRound className="size-4.5" />
        </span>
        <div>
        <CardTitle>Secret vault</CardTitle>
        <CardDescription>
          Store credentials here so http.request can inject them by reference - the value never
          appears in an LLM prompt or in this page&apos;s traffic.
        </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {isPending && <Skeleton className="h-16 w-full" />}

        {isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load secrets</AlertTitle>
            <AlertDescription>{error.message}</AlertDescription>
          </Alert>
        )}

        {data && !data.available && (
          <Alert variant="destructive">
            <AlertTitle>Vault not initialized</AlertTitle>
            <AlertDescription className="flex flex-col gap-2">
              <span>
                <span className="font-mono">{data.key_env}</span> is not set - generate one to start storing
                secrets. Takes effect immediately, no restart needed.
              </span>
              <Button
                variant="outline"
                size="sm"
                className="self-start"
                disabled={initVault.isPending}
                onClick={() => {
                  initVault.mutate(undefined, {
                    onSuccess: () => toast.success("Secret vault key generated."),
                    onError: (err) => {
                      toast.error(err instanceof ApiError ? err.message : "Could not generate the vault key.")
                    },
                  })
                }}
              >
                Generate key
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {data?.available && (
          <>
            {Object.keys(data.services).length === 0 && (
              <p className="text-sm text-muted-foreground">No secrets stored yet.</p>
            )}
            <div className="flex flex-col gap-1">
              {Object.entries(data.services).flatMap(([svc, keys]) =>
                keys.map((k) => svc === "accounts" ? (
                  <AccountRow key={`${svc}.${k}`} host={k} onDelete={() => setPendingDelete({ service: svc, key: k })} />
                ) : (
                  <div key={`${svc}.${k}`} className="flex min-w-0 items-center justify-between gap-2 rounded-lg bg-muted/60 px-3 py-2 text-sm">
                    <span className="min-w-0 font-mono [overflow-wrap:anywhere]">
                      {svc}.{k}
                    </span>
                    <Button variant="ghost" size="icon-sm" aria-label={`Delete ${svc}.${k}`} onClick={() => setPendingDelete({ service: svc, key: k })}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                )),
              )}
            </div>

            <form
              className="flex flex-col gap-2 border-t border-border pt-3"
              onSubmit={(event) => {
                event.preventDefault()
                const svc = service.trim()
                const k = key.trim()
                if (!svc || !k || !value.trim()) return
                setSecret.mutate(
                  { service: svc, key: k, value },
                  {
                    onSuccess: () => {
                      toast.success(`Saved ${svc}.${k}.`)
                      setService("")
                      setKey("")
                      setValue("")
                    },
                    onError: (err) => {
                      toast.error(err instanceof ApiError ? err.message : "Could not save the secret.")
                    },
                  },
                )
              }}
            >
              <p className="text-xs font-medium text-muted-foreground">Add / replace a secret</p>
              <div className="grid gap-2 sm:grid-cols-2">
                <Input
                  aria-label="Service"
                  placeholder="Service (e.g. openai)"
                  value={service}
                  onChange={(e) => setService(e.target.value)}
                />
                <Input aria-label="Key" placeholder="Key (e.g. api_key)" value={key} onChange={(e) => setKey(e.target.value)} />
              </div>
              <Input
                aria-label="Value"
                type="password"
                placeholder="Value"
                value={value}
                onChange={(e) => setValue(e.target.value)}
              />
              <Button
                type="submit"
                size="sm"
                className="self-start"
                disabled={setSecret.isPending || !service.trim() || !key.trim() || !value.trim()}
              >
                Save secret
              </Button>
            </form>
          </>
        )}
      </CardContent>

      <ConfirmDialog
        open={pendingDelete != null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null)
        }}
        title={pendingDelete ? `Delete ${pendingDelete.service}.${pendingDelete.key}?` : "Delete secret?"}
        description="Any tool configured to use this credential by reference will fail until it's re-added."
        confirmLabel="Delete"
        pending={deleteSecret.isPending}
        onConfirm={handleDelete}
      />
    </Card>
  )
}
