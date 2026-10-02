import { toast } from "sonner"
import { Link } from "react-router"
import { Button } from "@/components/ui/button"
import { StatusBadge } from "@/components/tasks/StatusBadge"
import { ACTIVE_STATUSES, ApiError, type TaskRecord } from "@/lib/api"
import { extractLastOutput } from "@/lib/chat"
import { formatRelativeTime } from "@/lib/time"
import { CANCELLABLE, PAUSABLE } from "@/lib/task-signals"
import { useTaskSignal, useTasks } from "@/lib/queries"

/**
 * "Live Activity" (docs/archive/UI_REWRITE_PLAN.md §19 parity check) - ports
 * Streamlit's `_render_live_activity`: currently-running tasks, inline
 * pause/cancel, without opening a trace. Lives at the top of Tasks rather
 * than on the Chat landing page - Chat is scoped to the local web-chat
 * conversation, while this (like Streamlit's version) shows every active
 * task system-wide, including ones spawned from Telegram or a schedule.
 * Reuses useTasks/useTaskSignal - no new backend endpoint.
 */
export function ActiveTasksPanel() {
  const { data } = useTasks(100)
  const signal = useTaskSignal()

  const tasks = data?.tasks ?? []
  const active = tasks.filter((task) => ACTIVE_STATUSES.has(task.status))

  if (active.length === 0) return null

  function handleSignal(taskId: string, name: "pause" | "cancel") {
    signal.mutate(
      { taskId, signal: name },
      {
        onError: (err) => {
          toast.error(err instanceof ApiError ? err.message : `Could not ${name} the task.`)
        },
      },
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-2xl border border-info/20 bg-info/5 p-4">
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <span className="size-2 animate-pulse rounded-full bg-info" /> Active now
      </h2>
      {active.map((task) => (
        <ActiveTaskRow key={task.id} task={task} onSignal={handleSignal} pending={signal.isPending} />
      ))}
    </div>
  )
}

function ActiveTaskRow({
  task,
  onSignal,
  pending,
}: {
  task: TaskRecord
  onSignal: (taskId: string, name: "pause" | "cancel") => void
  pending: boolean
}) {
  const lastOutput = extractLastOutput(task)
  const history = task.metadata.operator_history
  const stepsTaken = Array.isArray(history) ? history.length : 0

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-3 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <Link to={`/tasks/${task.id}`} className="min-w-0 flex-1 hover:underline">
          <p className="truncate text-sm font-medium">{task.objective}</p>
        </Link>
        <StatusBadge status={task.status} />
      </div>
      <div className="flex gap-4 text-xs text-muted-foreground">
        <span>Updated {formatRelativeTime(task.updated_at)}</span>
        <span>{stepsTaken} step{stepsTaken === 1 ? "" : "s"}</span>
      </div>
      {lastOutput && <p className="line-clamp-2 text-xs text-muted-foreground">{lastOutput}</p>}
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={pending || !PAUSABLE.has(task.status)}
          onClick={() => onSignal(task.id, "pause")}
        >
          Pause
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={pending || !CANCELLABLE.has(task.status)}
          onClick={() => onSignal(task.id, "cancel")}
        >
          Cancel
        </Button>
      </div>
    </div>
  )
}
