import { useState } from "react"
import { toast } from "sonner"
import { Loader2, Plug, Plus, Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { ApiError, type MCPServerConfig, type MCPServerInput } from "@/lib/api"
import { useDeleteMCPServer, useSettingsSummary, useTestMCPServer, useUpsertMCPServer } from "@/lib/queries"

type Draft = {
  name: string
  enabled: boolean
  command: string
  args: string
  env: string
  cwd: string
  timeoutSeconds: string
  capability: string
  riskLevel: string
  disabledTools: string
}

const EMPTY_DRAFT: Draft = {
  name: "",
  enabled: true,
  command: "",
  args: "",
  env: "",
  cwd: "",
  timeoutSeconds: "30",
  capability: "terminal.run",
  riskLevel: "high",
  disabledTools: "",
}

function draftFromServer(name: string, server: MCPServerConfig): Draft {
  return {
    name,
    enabled: server.enabled,
    command: server.command,
    args: server.args.join(", "),
    env: "", // write-only: existing values never reach this client, only env_keys
    cwd: server.cwd ?? "",
    timeoutSeconds: String(server.timeout_seconds),
    capability: server.capability,
    riskLevel: server.risk_level,
    disabledTools: server.disabled_tools.join(", "),
  }
}

function parseList(value: string): string[] {
  return value.split(",").map((entry) => entry.trim()).filter(Boolean)
}

// "KEY=VALUE" per line - the same shape a .env file uses, which is exactly
// what most MCP server docs already show for configuring one.
function parseEnv(value: string): Record<string, string> {
  const env: Record<string, string> = {}
  for (const line of value.split("\n")) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const separator = trimmed.indexOf("=")
    if (separator <= 0) continue
    env[trimmed.slice(0, separator).trim()] = trimmed.slice(separator + 1).trim()
  }
  return env
}

/**
 * Add/edit/test/remove an MCP server (docs/ROADMAP.md "integration control
 * plane") - previously read-only, editing meant hand-editing config.yaml.
 * Env values are write-only from this client's point of view: an edit
 * starts with a blank env field (existing values never round-trip back),
 * and leaving it blank on save keeps whatever is already configured -
 * only a non-empty env field replaces it.
 */
export function MCPServersCard() {
  const { data, isPending } = useSettingsSummary()
  const upsert = useUpsertMCPServer()
  const deleteServer = useDeleteMCPServer()
  const testServer = useTestMCPServer()
  const [editing, setEditing] = useState<Draft | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [testingName, setTestingName] = useState<string | null>(null)

  if (isPending || !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>MCP servers</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-16 w-full" />
        </CardContent>
      </Card>
    )
  }

  const mcp = data.config.mcp
  const servers = Object.entries(mcp.servers)
  const isNewServer = editing != null && !(editing.name in mcp.servers)

  function handleTest(name: string) {
    setTestingName(name)
    testServer.mutate(name, {
      onSuccess: (result) => {
        toast[result.healthy ? "success" : "error"](
          result.healthy
            ? `${name}: reachable, ${result.tool_count} tool(s) - ${result.tools.join(", ") || "none"}`
            : `${name}: ${result.error ?? "unreachable"}`,
        )
      },
      onError: (err) => toast.error(err instanceof ApiError ? err.message : `Could not test ${name}.`),
      onSettled: () => setTestingName(null),
    })
  }

  function handleDelete() {
    if (!pendingDelete) return
    const name = pendingDelete
    deleteServer.mutate(name, {
      onSuccess: () => toast.success(`Removed ${name}.`),
      onError: (err) => toast.error(err instanceof ApiError ? err.message : `Could not remove ${name}.`),
    })
    setPendingDelete(null)
  }

  function handleSave() {
    if (!editing) return
    const draft = editing
    if (!draft.name.trim() || !draft.command.trim()) {
      toast.error("Name and command are required.")
      return
    }
    const input: MCPServerInput = {
      name: draft.name.trim(),
      enabled: draft.enabled,
      command: draft.command.trim(),
      args: parseList(draft.args),
      env: parseEnv(draft.env),
      cwd: draft.cwd.trim() || null,
      timeout_seconds: Number(draft.timeoutSeconds) || 30,
      capability: draft.capability.trim(),
      risk_level: draft.riskLevel,
      disabled_tools: parseList(draft.disabledTools),
    }
    upsert.mutate(input, {
      onSuccess: () => {
        toast.success(`Saved ${input.name}. Restart long-running processes to pick it up.`)
        setEditing(null)
      },
      onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not save the MCP server."),
    })
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div>
          <CardTitle>MCP servers</CardTitle>
          <CardDescription>
            {mcp.enabled ? "Enabled" : "Disabled"} - configured servers the Operator can discover and
            call tools from.
          </CardDescription>
        </div>
        <Button type="button" variant="outline" size="sm" className="gap-1" onClick={() => setEditing({ ...EMPTY_DRAFT })}>
          <Plus className="size-3.5" /> Add server
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {servers.length === 0 && <p className="text-sm text-muted-foreground">No MCP servers configured.</p>}
        {servers.map(([name, server]) => (
          <div key={name} className="flex flex-col gap-1 rounded-md border border-border p-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{name}</span>
              <Badge variant={server.enabled ? "secondary" : "outline"}>{server.enabled ? "enabled" : "disabled"}</Badge>
              <Badge variant="outline">{server.risk_level}</Badge>
              <div className="ml-auto flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-7 gap-1 px-2 text-xs"
                  disabled={testingName === name}
                  onClick={() => handleTest(name)}
                >
                  {testingName === name ? <Loader2 className="size-3 animate-spin" /> : <Plug className="size-3" />}
                  Test
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  onClick={() => setEditing(draftFromServer(name, server))}
                >
                  Edit
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  className="text-destructive hover:text-destructive"
                  onClick={() => setPendingDelete(name)}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </div>
            </div>
            <p className="font-mono text-xs text-muted-foreground">
              {server.command} {server.args.join(" ")}
            </p>
            <p className="text-xs text-muted-foreground">
              capability: {server.capability}
              {server.env_keys.length > 0 && ` · env: ${server.env_keys.join(", ")}`}
              {server.disabled_tools.length > 0 && ` · disabled tools: ${server.disabled_tools.join(", ")}`}
            </p>
          </div>
        ))}
      </CardContent>

      <Dialog open={editing != null} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{isNewServer ? "Add MCP server" : `Edit ${editing?.name}`}</DialogTitle>
            <DialogDescription>
              Runs as its own process on future calls - review the command and env before saving.
            </DialogDescription>
          </DialogHeader>
          {editing && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="Name">
                <Input
                  value={editing.name}
                  disabled={!isNewServer}
                  onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                />
              </Field>
              <Label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Enabled</span>
                <Switch checked={editing.enabled} onCheckedChange={(checked) => setEditing({ ...editing, enabled: checked })} />
              </Label>
              <Field label="Command">
                <Input value={editing.command} onChange={(e) => setEditing({ ...editing, command: e.target.value })} />
              </Field>
              <Field label="Args (comma-separated)">
                <Input value={editing.args} onChange={(e) => setEditing({ ...editing, args: e.target.value })} />
              </Field>
              <Field label="Working directory (optional)">
                <Input value={editing.cwd} onChange={(e) => setEditing({ ...editing, cwd: e.target.value })} />
              </Field>
              <Field label="Timeout (s)">
                <Input
                  type="number"
                  value={editing.timeoutSeconds}
                  onChange={(e) => setEditing({ ...editing, timeoutSeconds: e.target.value })}
                />
              </Field>
              <Field label="Capability">
                <Input value={editing.capability} onChange={(e) => setEditing({ ...editing, capability: e.target.value })} />
              </Field>
              <Label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Risk level</span>
                <Select value={editing.riskLevel} onValueChange={(v) => v && setEditing({ ...editing, riskLevel: v })}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {["low", "medium", "high", "critical"].map((level) => (
                      <SelectItem key={level} value={level}>{level}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>
              <Field label="Disabled tools (comma-separated)">
                <Input value={editing.disabledTools} onChange={(e) => setEditing({ ...editing, disabledTools: e.target.value })} />
              </Field>
              <Label className="col-span-full flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">
                  Env (one KEY=VALUE per line{isNewServer ? "" : " - leave blank to keep the current values"})
                </span>
                <Textarea
                  rows={3}
                  value={editing.env}
                  onChange={(e) => setEditing({ ...editing, env: e.target.value })}
                  placeholder="API_KEY=..."
                />
              </Label>
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button type="button" disabled={upsert.isPending} onClick={handleSave}>
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={pendingDelete != null}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title={`Remove ${pendingDelete}?`}
        description="The Operator will no longer be able to discover or call this server's tools."
        confirmLabel="Remove"
        pending={deleteServer.isPending}
        onConfirm={handleDelete}
      />
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
