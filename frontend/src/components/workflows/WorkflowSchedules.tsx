import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { useSchedules, useUpdateScheduleStatus } from "@/lib/queries"

export function WorkflowSchedules() {
  const { data, error, isPending } = useSchedules()
  const update = useUpdateScheduleStatus()
  const [deleting, setDeleting] = useState<string | null>(null)
  function change(id: string, status: "enabled" | "paused" | "deleted") {
    update.mutate({ id, status }, {
      onSuccess: () => setDeleting(null),
      onError: (err) => toast.error(err.message),
    })
  }
  return (
    <Card>
      <CardHeader><CardTitle className="text-base">Automations</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        {isPending && <p role="status" className="text-sm">Loading automations...</p>}
        {error && <p role="alert" className="text-sm text-destructive">Could not load automations: {error.message}</p>}
        {data && !data.enabled && <p className="text-sm text-muted-foreground">The scheduler is disabled. Automations will not run until it is enabled.</p>}
        {data?.schedules.length === 0 && <p className="text-sm text-muted-foreground">Choose Automate on a saved workflow to keep it running on a schedule.</p>}
        {data?.schedules.map((item) => (
          <div key={item.id} className="flex flex-col gap-2 rounded-lg border p-3 text-sm [overflow-wrap:anywhere]">
            <p className="font-medium">{item.objective}</p>
            <p className="text-xs text-muted-foreground">{item.status} - {item.cadence}{!item.metadata.folder_trigger && ` - ${item.timezone}`}</p>
            {item.metadata.folder_trigger ? <>
              <p className="text-xs text-muted-foreground">Folder: {item.metadata.folder_trigger.folder}</p>
              <p className="text-xs text-muted-foreground">{item.metadata.folder_trigger.suffixes.join(", ")} - {item.metadata.folder_trigger.settle_seconds}s stable - parameter: {item.metadata.folder_trigger.file_parameter}</p>
              {item.status === "enabled" && !item.metadata.watch_error && <p className="text-xs text-muted-foreground">Watching for new or changed files. Runs one file at a time.</p>}
            </> : item.status === "enabled" && <p className="text-xs text-muted-foreground">Next due: {new Date(item.next_run_at).toLocaleString()}</p>}
            {item.metadata.watch_error && <p role="alert" className="text-xs text-destructive">Folder observation stopped: {item.metadata.watch_error}</p>}
            {Number(item.metadata.consecutive_failures) > 0 && <p className="text-xs text-destructive">{Number(item.metadata.consecutive_failures)} consecutive failures. Review the last run before resuming.</p>}
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" disabled={update.isPending || (!data.enabled && item.status === "paused")} onClick={() => change(item.id, item.status === "enabled" ? "paused" : "enabled")}>{item.status === "enabled" ? "Pause" : "Resume"}</Button>
              <Button size="sm" variant="ghost" disabled={update.isPending} onClick={() => setDeleting(item.id)}>Delete automation</Button>
              {item.last_task_id && <Link to={`/tasks/${item.last_task_id}`} className="text-primary underline">Last run and receipt</Link>}
            </div>
          </div>
        ))}
      </CardContent>
      <ConfirmDialog open={deleting != null} onOpenChange={(open) => !open && setDeleting(null)} title="Delete automation?" description="Future runs will stop. Tasks already created keep their current state." confirmLabel="Delete" pending={update.isPending} onConfirm={() => deleting && change(deleting, "deleted")} />
    </Card>
  )
}
