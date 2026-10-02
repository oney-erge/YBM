import { useEffect, useState } from "react"
import { toast } from "sonner"
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  RotateCw,
  Stethoscope,
  XCircle,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog"
import { useRunDoctor, useServiceLog, useSettingsSummary } from "@/lib/queries"
import { ApiError, type DoctorCheck } from "@/lib/api"
import { formatRelativeTime } from "@/lib/time"
import { cn } from "@/lib/utils"

/**
 * Level 2/Advanced, read-only except for two explicitly-triggered actions
 * (Run doctor, view a service log). Rebuilt in docs/archive/UI_UX_AUDIT.md Phase 9:
 * the backend (and this file's own Zod schemas) already carried
 * restart_count, last_exit_code, child_pid, and age_seconds per service -
 * none of it was rendered, just a name/status/message line. Still
 * deliberately scoped down from Streamlit's Diagnostics tab (full tool
 * registry table, raw JSON dump) - deep JSON inspection belongs to
 * Effective Config, not here.
 */
export function DiagnosticsCard() {
  const { data, isPending } = useSettingsSummary()
  const doctor = useRunDoctor()
  const [logService, setLogService] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  if (isPending || !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Diagnostics</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    )
  }

  async function copyBundle() {
    const bundle = {
      generated_at: new Date().toISOString(),
      config_file: data!.admin.config_file,
      warnings: data!.warnings,
      services: data!.services.items,
      database: data!.database,
      doctor_checks: doctor.data?.checks ?? "not run this session",
    }
    try {
      await navigator.clipboard.writeText(JSON.stringify(bundle, null, 2))
      setCopied(true)
      toast.success("Diagnostics copied to clipboard.")
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error("Could not copy - your browser blocked clipboard access.")
    }
  }

  const maxTableCount = Math.max(1, ...Object.values(data.database.table_counts))

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div>
          <CardTitle>Diagnostics</CardTitle>
          <CardDescription>Config file: {data.admin.config_file}</CardDescription>
        </div>
        <Button variant="outline" size="sm" onClick={copyBundle}>
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          Copy bundle
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
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

        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Services</p>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {data.services.items.map((item) => (
              <div key={item.name} className="rounded-lg border border-border p-2.5 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{item.name.replace(/_/g, " ")}</span>
                  <Badge variant={!item.expected ? "outline" : item.ok ? "secondary" : "destructive"}>
                    {!item.expected ? "not expected" : item.status}
                  </Badge>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-muted-foreground">
                  {item.child_pid != null && <span>pid {item.child_pid}</span>}
                  {item.updated_at != null && <span>{formatRelativeTime(item.updated_at)}</span>}
                  {item.restart_count != null && item.restart_count > 0 && (
                    <span className="flex items-center gap-0.5 text-warning">
                      <RotateCw className="size-3" />
                      {item.restart_count} restart{item.restart_count === 1 ? "" : "s"}
                    </span>
                  )}
                  {item.last_exit_code != null && item.last_exit_code !== 0 && (
                    <span className="text-destructive">exit {item.last_exit_code}</span>
                  )}
                </div>
                {item.message && <p className="mt-1 text-muted-foreground">{item.message}</p>}
                <button
                  type="button"
                  onClick={() => setLogService(item.name)}
                  className="mt-1.5 flex items-center gap-1 text-primary hover:underline"
                >
                  <FileText className="size-3" />
                  View log
                </button>
              </div>
            ))}
          </div>
        </div>

        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Database</p>
          <p className="font-mono text-xs text-muted-foreground">{data.database.database_url}</p>
          <div className="mt-2 flex flex-col gap-1">
            {Object.entries(data.database.table_counts).map(([table, count]) => (
              <div key={table} className="flex items-center gap-2 text-xs">
                <span className="w-36 shrink-0 truncate text-muted-foreground">{table}</span>
                <div className="h-3 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-primary/50"
                    style={{ width: `${Math.max(2, (count / maxTableCount) * 100)}%` }}
                  />
                </div>
                <span className="w-10 shrink-0 text-right font-medium">{count}</span>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Environment checks</p>
            <Button
              variant="outline"
              size="sm"
              disabled={doctor.isPending}
              onClick={() => {
                doctor.mutate(undefined, {
                  onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not run doctor checks."),
                })
              }}
            >
              <Stethoscope className="size-3.5" />
              {doctor.isPending ? "Running..." : "Run doctor"}
            </Button>
          </div>
          {doctor.data ? (
            <div className="flex flex-col gap-1">
              {doctor.data.checks.map((check) => (
                <DoctorCheckRow key={check.name} check={check} />
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Not run this session - checks Python/venv, config, the database, LocalDeploy, Telegram, the
              secret vault, and ports.
            </p>
          )}
        </div>
      </CardContent>

      <ServiceLogDialog
        service={logService}
        onOpenChange={(open) => {
          if (!open) setLogService(null)
        }}
      />
    </Card>
  )
}

function DoctorCheckRow({ check }: { check: DoctorCheck }) {
  const Icon = check.status === "ok" ? CheckCircle2 : check.status === "warn" ? AlertTriangle : XCircle
  const tone = check.status === "ok" ? "text-success" : check.status === "warn" ? "text-warning" : "text-destructive"
  return (
    <div className="flex items-start gap-2 text-xs">
      <Icon className={cn("mt-0.5 size-3.5 shrink-0", tone)} />
      <div className="min-w-0">
        <span className="font-medium">{check.name}</span>
        {check.detail && <span className="text-muted-foreground"> - {check.detail}</span>}
      </div>
    </div>
  )
}

function ServiceLogDialog({ service, onOpenChange }: { service: string | null; onOpenChange: (open: boolean) => void }) {
  const log = useServiceLog()
  const [expanded, setExpanded] = useState(true)
  const { mutate: fetchLog } = log

  // Fires the fetch the moment a service is picked, not on a separate
  // "load" click - the dialog opening already IS the request to see it.
  // An effect, not a call during render: mutate() triggers a state update,
  // which React disallows synchronously mid-render.
  useEffect(() => {
    if (service) fetchLog(service)
  }, [service, fetchLog])

  return (
    <Dialog
      open={service != null}
      onOpenChange={(open) => {
        onOpenChange(open)
        if (!open) log.reset()
      }}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{service?.replace(/_/g, " ")} log</DialogTitle>
          <DialogDescription>
            {log.data?.log_path ?? "Last 200 lines"}
          </DialogDescription>
        </DialogHeader>
        {log.isPending && <Skeleton className="h-48 w-full" />}
        {log.data && log.data.lines.length === 0 && (
          <p className="text-sm text-muted-foreground">No log file yet - this service hasn't run.</p>
        )}
        {log.data && log.data.lines.length > 0 && (
          <>
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="flex w-fit items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
              {log.data.lines.length} lines
            </button>
            {expanded && (
              <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-[11px] whitespace-pre-wrap">
                {log.data.lines.join("\n")}
              </pre>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
