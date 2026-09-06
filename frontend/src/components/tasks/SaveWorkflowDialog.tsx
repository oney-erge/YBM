import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"
import { Plus, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useSaveWorkflow } from "@/lib/queries"
import { ApiError } from "@/lib/api"

/**
 * "Save as workflow" (docs/ROADMAP.md "reusable verified workflows") - a
 * completed task's own replay plan, saved under a name, with specific
 * literal values swapped for named parameters so it can run again later
 * against different inputs. Deliberately not a workflow designer: the
 * only editing surface is naming values already present in the plan, not
 * authoring new steps.
 *
 * The backend refuses (400) when the plan can't be fully captured -
 * a delegated sub-task or a parallel-batch member the replay plan itself
 * would drop - and names exactly which steps, surfaced here as a plain
 * error rather than silently saving a workflow missing part of what the
 * source task actually did.
 */
export function SaveWorkflowDialog({
  taskId,
  open,
  onOpenChange,
}: {
  taskId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [name, setName] = useState("")
  const [rows, setRows] = useState<{ value: string; param: string }[]>([{ value: "", param: "" }])
  const save = useSaveWorkflow()
  const navigate = useNavigate()

  function reset() {
    setName("")
    setRows([{ value: "", param: "" }])
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset()
    onOpenChange(next)
  }

  function handleSave() {
    const parameters: Record<string, string> = {}
    for (const row of rows) {
      if (row.value.trim() && row.param.trim()) parameters[row.value.trim()] = row.param.trim()
    }
    save.mutate(
      { taskId, name: name.trim(), parameters },
      {
        onSuccess: () => {
          toast.success(`Saved as workflow "${name.trim()}".`)
          handleOpenChange(false)
          navigate("/workflows")
        },
        onError: (err) => {
          toast.error(err instanceof ApiError ? err.message : "Could not save this task as a workflow.")
        },
      },
    )
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Save as workflow</DialogTitle>
          <DialogDescription>
            Saves this task's own successful steps so you can run them again later against different
            values, through the same approval and verification checks.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-muted-foreground">Name</span>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Organize invoices" autoFocus />
          </Label>

          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-muted-foreground">
              Turn a value into a parameter (optional)
            </span>
            {rows.map((row, index) => (
              <div key={index} className="flex items-center gap-2">
                <Input
                  value={row.value}
                  onChange={(e) => {
                    const next = [...rows]
                    next[index] = { ...next[index], value: e.target.value }
                    setRows(next)
                  }}
                  placeholder="Exact value, e.g. C:/Users/sam/Downloads"
                  className="flex-1 font-mono text-xs"
                />
                <Input
                  value={row.param}
                  onChange={(e) => {
                    const next = [...rows]
                    next[index] = { ...next[index], param: e.target.value }
                    setRows(next)
                  }}
                  placeholder="folder"
                  className="w-28 font-mono text-xs"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="shrink-0"
                  disabled={rows.length === 1}
                  onClick={() => setRows(rows.filter((_, i) => i !== index))}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="self-start"
              onClick={() => setRows([...rows, { value: "", param: "" }])}
            >
              <Plus className="size-3.5" /> Add another value
            </Button>
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button type="button" disabled={!name.trim() || save.isPending} onClick={handleSave}>
            Save workflow
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
