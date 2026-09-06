import { useState } from "react"
import { useNavigate, useParams } from "react-router"
import { toast } from "sonner"
import { Repeat, Save } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { PageBreadcrumb } from "@/components/layout/PageBreadcrumb"
import { StatusBadge } from "@/components/tasks/StatusBadge"
import { CostPanel } from "@/components/tasks/CostPanel"
import { DurationChart } from "@/components/tasks/DurationChart"
import { OperatorHistoryList } from "@/components/tasks/OperatorHistoryList"
import { SaveWorkflowDialog } from "@/components/tasks/SaveWorkflowDialog"
import { TraceGraph } from "@/components/tasks/TraceGraph"
import { TraceTimeline } from "@/components/tasks/TraceTimeline"
import { useReplayTask, useTaskSignal, useTaskTrace } from "@/lib/queries"
import { useAdvancedMode } from "@/lib/advanced-mode"
import { ApiError, tokenUsageOf, type EvidenceItem } from "@/lib/api"
import { isTerminal } from "@/lib/chat"
import { EFFECT_DISPLAY } from "@/lib/evidence"
import { CANCELLABLE, PAUSABLE, RESUMABLE } from "@/lib/task-signals"
import { cn } from "@/lib/utils"

type TraceView = "steps" | "timeline" | "duration" | "graph"

export function TaskTracePage() {
  const { taskId } = useParams<{ taskId: string }>()
  const { data: trace, isPending, isError, error } = useTaskTrace(taskId)
  const { advanced } = useAdvancedMode()
  const signal = useTaskSignal()
  const replay = useReplayTask()
  const navigate = useNavigate()
  const [view, setView] = useState<TraceView>("steps")
  const [savingWorkflow, setSavingWorkflow] = useState(false)

  if (isPending) {
    return (
      <div className="flex flex-col gap-4 p-6">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }
  if (isError || !trace) {
    return (
      <div className="p-6">
        <Alert variant="destructive">
          <AlertTitle>Couldn't load this task</AlertTitle>
          <AlertDescription>{error?.message ?? "Not found"}</AlertDescription>
        </Alert>
      </div>
    )
  }

  const task = trace.task
  const usage = tokenUsageOf(task)

  function handleSignal(name: "pause" | "resume" | "cancel") {
    if (!taskId) return
    signal.mutate(
      { taskId, signal: name },
      {
        onError: (err) => {
          toast.error(err instanceof ApiError ? err.message : `Could not ${name} the task.`)
        },
      },
    )
  }

  function handleReplay() {
    if (!taskId) return
    replay.mutate(taskId, {
      onSuccess: (result) => {
        toast.success("Replay started from this task's successful steps.")
        navigate(`/tasks/${result.task.id}`)
      },
      onError: (err) => {
        toast.error(err instanceof ApiError ? err.message : "Could not replay this task.")
      },
    })
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-6xl flex-col gap-5 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
      <div>
        <PageBreadcrumb items={[{ label: "Tasks", to: "/tasks" }, { label: task.objective }]} />
        <div className="mt-3 flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="mb-1 text-xs font-semibold uppercase tracking-[0.14em] text-primary">Task trace</p>
            <h1 className="text-xl font-semibold tracking-tight [overflow-wrap:anywhere]">{task.objective}</h1>
            <p className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">
              {task.id} · created {new Date(task.created_at).toLocaleString()}
            </p>
          </div>
          <StatusBadge status={task.status} />
        </div>
      </div>

      <div className="flex gap-2">
        {PAUSABLE.has(task.status) && (
          <Button variant="outline" size="sm" disabled={signal.isPending} onClick={() => handleSignal("pause")}>
            Pause
          </Button>
        )}
        {RESUMABLE.has(task.status) && (
          <Button variant="outline" size="sm" disabled={signal.isPending} onClick={() => handleSignal("resume")}>
            Resume
          </Button>
        )}
        {CANCELLABLE.has(task.status) && (
          <Button variant="outline" size="sm" disabled={signal.isPending} onClick={() => handleSignal("cancel")}>
            Cancel
          </Button>
        )}
        {task.status === "completed" && (
          <Button variant="outline" size="sm" disabled={replay.isPending} onClick={handleReplay}>
            <Repeat className="size-3.5" />
            Replay
          </Button>
        )}
        {task.status === "completed" && (
          <Button variant="outline" size="sm" onClick={() => setSavingWorkflow(true)}>
            <Save className="size-3.5" />
            Save as workflow
          </Button>
        )}
      </div>

      {taskId && (
        <SaveWorkflowDialog taskId={taskId} open={savingWorkflow} onOpenChange={setSavingWorkflow} />
      )}

      {typeof task.metadata.synthesized_answer === "string" && (
        <div className="whitespace-pre-wrap rounded-xl border border-primary/15 bg-primary/5 p-4 text-sm leading-6 [overflow-wrap:anywhere]">
          {task.metadata.synthesized_answer}
        </div>
      )}

      {usage && <CostPanel usage={usage} />}

      <div>
        <div className="mb-2 flex items-center gap-1">
          <ViewTab label="Steps" active={view === "steps"} onClick={() => setView("steps")} />
          <ViewTab label="Timeline" active={view === "timeline"} onClick={() => setView("timeline")} />
          <ViewTab label="Duration" active={view === "duration"} onClick={() => setView("duration")} />
          {advanced && <ViewTab label="Graph" active={view === "graph"} onClick={() => setView("graph")} />}
        </div>
        {view === "graph" && advanced ? (
          <TraceGraph trace={trace} />
        ) : view === "timeline" ? (
          <TraceTimeline items={trace.timeline} />
        ) : view === "duration" ? (
          <DurationChart trace={trace} />
        ) : (
          <OperatorHistoryList entries={trace.operator_history} />
        )}
      </div>

      {advanced && (
        <>
          <EvidenceSection
            files={trace.evidence.files}
            urls={trace.evidence.urls}
            commands={trace.evidence.commands}
          />
          <RawSection title={`Approvals (${trace.approvals.length})`} data={trace.approvals} />
          <RawSection title={`Artifacts (${trace.artifacts.length})`} data={trace.artifacts} />
          <RawSection title={`Audit events (${trace.audit.length})`} data={trace.audit} />
        </>
      )}

      {!isTerminal(task.status) && (
        <p className="text-xs text-muted-foreground">This task is still in progress - updating live.</p>
      )}
      </div>
    </div>
  )
}

function EvidenceSection({
  files,
  urls,
  commands,
}: {
  files: EvidenceItem[]
  urls: EvidenceItem[]
  commands: EvidenceItem[]
}) {
  if (files.length === 0 && urls.length === 0 && commands.length === 0) return null
  const items = [...files, ...urls, ...commands]
  return (
    <div>
      <h2 className="mb-2 text-sm font-medium">What this task touched</h2>
      <div className="flex flex-col gap-1 text-xs">
        {items.map((item) => {
          const effect = EFFECT_DISPLAY[item.effect]
          const Icon = effect.icon
          return (
            <div key={item.value} className="flex items-start gap-1.5">
              <Icon className={cn("mt-0.5 size-3 shrink-0", effect.className)} />
              <span className="[overflow-wrap:anywhere]">
                <span className={cn("font-medium", effect.className)}>{effect.label}</span>{" "}
                <span className="font-mono">{item.value}</span>
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ViewTab({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-lg px-2.5 py-1 text-xs font-medium transition-colors",
        active ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted",
      )}
    >
      {label}
    </button>
  )
}

function RawSection({ title, data }: { title: string; data: unknown[] }) {
  if (data.length === 0) return null
  return (
    <details className="text-sm">
      <summary className="cursor-pointer font-medium">{title}</summary>
      <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-muted p-3 font-mono text-xs">
        {JSON.stringify(data, null, 2)}
      </pre>
    </details>
  )
}
