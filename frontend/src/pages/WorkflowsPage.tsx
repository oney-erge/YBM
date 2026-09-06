import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { Play, Trash2, Workflow as WorkflowIcon } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { PageHeader } from "@/components/layout/PageHeader"
import { ApiError, type TaskWorkflow } from "@/lib/api"
import { useDeleteWorkflow, useRunWorkflow, useWorkflows } from "@/lib/queries"

/**
 * Verified workflows (docs/ROADMAP.md "reusable verified workflows") - a
 * completed task's own replay plan, saved under a name and re-run against
 * new parameter values, through the identical approval/verification
 * pipeline replay already provides. Deliberately not a workflow designer:
 * saving happens from a task's trace page, and this page only lists,
 * runs, and deletes what's already been saved.
 */
export function WorkflowsPage() {
  const { data, isPending, isError } = useWorkflows()
  const [running, setRunning] = useState<TaskWorkflow | null>(null)
  const [deleting, setDeleting] = useState<TaskWorkflow | null>(null)
  const deleteWorkflow = useDeleteWorkflow()

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageHeader
          eyebrow="Reusable"
          title="Workflows"
          description="Completed tasks saved as a named, reusable plan. Running one goes through the same approval and verification checks as any other task."
        />

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
                  <div className="flex shrink-0 gap-2">
                    <Button variant="outline" size="sm" onClick={() => setRunning(workflow)}>
                      <Play className="size-3.5" />
                      Run
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
