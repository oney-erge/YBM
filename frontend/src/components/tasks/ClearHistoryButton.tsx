import { useState } from "react"
import { toast } from "sonner"
import { Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogClose,
} from "@/components/ui/dialog"
import { ApiError } from "@/lib/api"
import { useClearTaskHistory } from "@/lib/queries"

/**
 * DELETE /api/tasks already existed, audit-logged, with an include_active
 * flag - no UI called it (docs/archive/UI_UX_AUDIT.md Phase 9). Two distinct
 * destructive choices, not one generic "confirm", because completed-only
 * is the safe default and including active tasks is a meaningfully bigger
 * ask (it also cancels whatever is still running - see admin.py's
 * clear_history, which deletes the rows outright rather than signaling
 * cancel first, so this really does mean "gone", not "stopped cleanly").
 */
export function ClearHistoryButton() {
  const [open, setOpen] = useState(false)
  const clear = useClearTaskHistory()

  function run(includeActive: boolean) {
    clear.mutate(includeActive, {
      onSuccess: (result) => {
        toast.success(`Deleted ${result.deleted_tasks} task${result.deleted_tasks === 1 ? "" : "s"}.`)
        setOpen(false)
      },
      onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not clear task history."),
    })
  }

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <Trash2 className="size-4" />
        Clear history
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Clear task history?</DialogTitle>
            <DialogDescription>
              Permanently deletes tasks and everything attached to them - tool calls, approvals,
              artifacts, audit events. This can't be undone.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Button
              variant="outline"
              className="justify-start"
              disabled={clear.isPending}
              onClick={() => run(false)}
            >
              Completed, failed, cancelled, and blocked tasks only
            </Button>
            <Button
              variant="destructive"
              className="justify-start"
              disabled={clear.isPending}
              onClick={() => run(true)}
            >
              Everything, including tasks still in progress
            </Button>
          </div>
          <DialogFooter>
            <DialogClose render={<Button variant="ghost" />}>Cancel</DialogClose>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
