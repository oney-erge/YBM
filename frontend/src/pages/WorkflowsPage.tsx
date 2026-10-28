import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { Clock3, Play, Trash2, Workflow as WorkflowIcon } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { PageHeader } from "@/components/layout/PageHeader"
import { WorkflowSchedules } from "@/components/workflows/WorkflowSchedules"
import { ApiError, type TaskWorkflow } from "@/lib/api"
import { useDeleteWorkflow, useProjects, useRunWorkflow, useScheduleWorkflow, useWatchWorkflow, useWorkflows } from "@/lib/queries"

/**
 * Verified workflows (docs/ROADMAP.md "reusable verified workflows") - a
 * completed task's own replay plan, saved under a name and re-run against
 * new parameter values, through the identical approval/verification
 * pipeline replay already provides. Deliberately not a workflow designer:
 * saving happens from a task's trace page, and this page only lists,
 * runs, schedules, and deletes what's already been saved.
 */
export function WorkflowsPage() {
  const { data, isPending, isError } = useWorkflows()
  const [running, setRunning] = useState<TaskWorkflow | null>(null)
  const [deleting, setDeleting] = useState<TaskWorkflow | null>(null)
  const [scheduling, setScheduling] = useState<TaskWorkflow | null>(null)
  const deleteWorkflow = useDeleteWorkflow()

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageHeader
          eyebrow="Reusable"
          title="Workflows"
          description="Completed tasks saved as a named, reusable plan. Running one goes through the same approval and verification checks as any other task."
        />
        <WorkflowSchedules />

        {isPending && (
          <div className="flex flex-col gap-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        )}

        {isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load workflows</AlertTitle>
            <AlertDescription>Try again in a moment.</AlertDescription>
          </Alert>
        )}

        {data && data.workflows.length === 0 && (
          <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border py-12 text-center text-sm text-muted-foreground">
            <WorkflowIcon className="size-6" />
            <p>No workflows saved yet.</p>
            <p className="text-xs">
              Open a completed task&apos;s trace and choose &quot;Save as workflow&quot; to create one.
            </p>
          </div>
        )}

        {data && data.workflows.length > 0 && (
          <div className="flex flex-col gap-3">
            {data.workflows.map((workflow) => (
              <Card key={workflow.id}>
                <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                  <div>
                    <CardTitle className="text-base">{workflow.name}</CardTitle>
                    <CardDescription className="mt-1 [overflow-wrap:anywhere]">
                      {workflow.objective_template}
                    </CardDescription>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" size="sm" onClick={() => setRunning(workflow)}>
                      <Play className="size-3.5" />
                      Run
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => setScheduling(workflow)}>
                      <Clock3 className="size-3.5" /> Automate
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`Delete ${workflow.name}`}
                      onClick={() => setDeleting(workflow)}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                </CardHeader>
                <CardContent>
                  <p className="text-xs text-muted-foreground">
                    {workflow.plan.length} step{workflow.plan.length === 1 ? "" : "s"}
                    {workflow.parameters.length > 0 && (
                      <>
                        {" - "}parameters: {workflow.parameters.map((p) => `{{${p}}}`).join(", ")}
                      </>
                    )}
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>

      {running && <RunWorkflowDialog workflow={running} onOpenChange={(open) => !open && setRunning(null)} />}
      {scheduling && <ScheduleWorkflowDialog workflow={scheduling} onOpenChange={(open) => !open && setScheduling(null)} />}

      <ConfirmDialog
        open={deleting != null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Delete "${deleting?.name}"?`}
        description="This removes the saved workflow. It does not affect the task it was saved from, or any task already run from it."
        confirmLabel="Delete"
        pending={deleteWorkflow.isPending}
        onConfirm={() => {
          if (!deleting) return
          deleteWorkflow.mutate(deleting.id, {
            onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not delete this workflow."),
          })
        }}
      />
    </div>
  )
}

function ScheduleWorkflowDialog({ workflow, onOpenChange }: { workflow: TaskWorkflow; onOpenChange: (open: boolean) => void }) {
  const [values, setValues] = useState<Record<string, string>>(Object.fromEntries(workflow.parameters.map((p) => [p, ""])))
  const [cadence, setCadence] = useState("daily")
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC")
  const [projectId, setProjectId] = useState("")
  const [trigger, setTrigger] = useState("time")
  const [folder, setFolder] = useState("")
  const [fileParameter, setFileParameter] = useState(workflow.parameters[0] ?? "")
  const [suffixes, setSuffixes] = useState(".pdf,.txt,.csv,.json")
  const [settleSeconds, setSettleSeconds] = useState(10)
  const { data: projects } = useProjects()
  const schedule = useScheduleWorkflow()
  const watch = useWatchWorkflow()
  const missingValue = workflow.parameters.some((p) => !(trigger === "folder" && p === fileParameter) && !values[p]?.trim())
  const pending = schedule.isPending || watch.isPending
  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] max-w-md overflow-y-auto">
        <DialogHeader><DialogTitle>Automate &quot;{workflow.name}&quot;</DialogTitle><DialogDescription>YBM repeats this saved workflow when the selected trigger fires. Each run uses normal approvals and verification.</DialogDescription></DialogHeader>
        <div className="flex flex-col gap-1">
          <Label htmlFor="automation-trigger">Trigger</Label>
          <select id="automation-trigger" className="h-8 rounded-md border border-input bg-background px-2" value={trigger} onChange={(event) => setTrigger(event.target.value)}>
            <option value="time">On a schedule</option>
            <option value="folder" disabled={workflow.parameters.length === 0}>When a file changes</option>
          </select>
          {workflow.parameters.length === 0 && <p className="text-xs text-muted-foreground">To watch files, save a workflow with a parameter for its input file path.</p>}
        </div>
        {trigger === "time" ? <>
          <Label className="flex flex-col gap-1"><span>Cadence</span><Input value={cadence} onChange={(event) => setCadence(event.target.value)} placeholder="daily or every 2 hours" /></Label>
          <Label className="flex flex-col gap-1"><span>Timezone</span><Input value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="America/Chicago" /></Label>
        </> : <>
          <Label className="flex flex-col gap-1"><span>Watch folder</span><Input value={folder} onChange={(event) => setFolder(event.target.value)} placeholder="Absolute folder path" /></Label>
          <div className="flex flex-col gap-1">
            <Label htmlFor="changed-file-parameter">Changed file parameter</Label>
            <select id="changed-file-parameter" className="h-8 rounded-md border border-input bg-background px-2" value={fileParameter} onChange={(event) => setFileParameter(event.target.value)}>
              {workflow.parameters.map((param) => <option key={param} value={param}>{param}</option>)}
            </select>
          </div>
          <Label className="flex flex-col gap-1"><span>File extensions</span><Input value={suffixes} onChange={(event) => setSuffixes(event.target.value)} /></Label>
          <Label className="flex flex-col gap-1"><span>Wait for stable file (seconds)</span><Input type="number" min={1} max={300} value={settleSeconds} onChange={(event) => setSettleSeconds(Number(event.target.value))} /></Label>
          <p className="text-xs text-muted-foreground">Watches top-level files up to 10 MB. Existing files form the baseline. New or changed files run one at a time after settling, through normal approvals. Up to 500 entries per inbox.</p>
        </>}
        <div className="flex flex-col gap-1">
          <Label htmlFor="schedule-project">Project</Label>
          <select id="schedule-project" className="h-8 rounded-md border border-input bg-background px-2" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">General chat</option>
            {projects?.projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </div>
        {workflow.parameters.filter((param) => trigger !== "folder" || param !== fileParameter).map((param) => <Label key={param} className="flex flex-col gap-1"><span className="font-mono text-xs text-muted-foreground">{`{{${param}}}`}</span><Input value={values[param] ?? ""} onChange={(event) => setValues({ ...values, [param]: event.target.value })} /></Label>)}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="button" disabled={missingValue || pending || (trigger === "folder" ? !folder.trim() || !fileParameter || !suffixes.trim() || settleSeconds < 1 || settleSeconds > 300 : !cadence.trim() || !timezone.trim())} onClick={() => trigger === "folder" ? watch.mutate(
            { workflowId: workflow.id, values, folder, fileParameter, suffixes: suffixes.split(",").map((value) => value.trim()).filter(Boolean), settleSeconds, projectId: projectId || undefined },
            {
              onSuccess: () => { toast.success("Folder automation created. Waiting for new or changed files."); onOpenChange(false) },
              onError: (error) => toast.error(error.message),
            },
          ) : schedule.mutate(
            { workflowId: workflow.id, values, cadence, timezone, projectId: projectId || undefined },
            {
              onSuccess: (result) => { toast.success(`Automation scheduled for ${new Date(result.schedule.next_run_at).toLocaleString()}.`); onOpenChange(false) },
              onError: (error) => toast.error(error instanceof ApiError ? error.message : "Could not schedule this workflow."),
            },
          )}>{trigger === "folder" ? "Watch folder" : "Schedule"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RunWorkflowDialog({ workflow, onOpenChange }: { workflow: TaskWorkflow; onOpenChange: (open: boolean) => void }) {
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(workflow.parameters.map((p) => [p, ""])),
  )
  const run = useRunWorkflow()
  const navigate = useNavigate()

  function handleRun() {
    run.mutate(
      { workflowId: workflow.id, values },
      {
        onSuccess: (result) => {
          toast.success(`Started "${workflow.name}".`)
          onOpenChange(false)
          navigate(`/tasks/${result.task.id}`)
        },
        onError: (err) => {
          toast.error(err instanceof ApiError ? err.message : "Could not run this workflow.")
        },
      },
    )
  }

  const missingValue = workflow.parameters.some((p) => !values[p]?.trim())

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Run &quot;{workflow.name}&quot;</DialogTitle>
          <DialogDescription>
            {workflow.parameters.length > 0
              ? "Fill in a value for each parameter this workflow was saved with."
              : "This workflow has no parameters - it will run exactly as saved."}
          </DialogDescription>
        </DialogHeader>

        {workflow.parameters.length > 0 && (
          <div className="flex flex-col gap-3">
            {workflow.parameters.map((param) => (
              <Label key={param} className="flex flex-col gap-1">
                <span className="font-mono text-xs text-muted-foreground">{`{{${param}}}`}</span>
                <Input
                  value={values[param] ?? ""}
                  onChange={(e) => setValues({ ...values, [param]: e.target.value })}
                  autoFocus={param === workflow.parameters[0]}
                />
              </Label>
            ))}
          </div>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="button" disabled={missingValue || run.isPending} onClick={handleRun}>
            Run
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
