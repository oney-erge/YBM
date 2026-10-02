import { useState } from "react"
import { toast } from "sonner"
import { Check, Pencil, Trash2, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { ApiError, type MemoryFact } from "@/lib/api"
import { useDeleteMemoryFact, useUpdateMemoryFact } from "@/lib/queries"
import { formatRelativeTime } from "@/lib/time"

const SOURCE_LABEL: Record<MemoryFact["source"], string> = {
  user_stated: "You told it",
  task_derived: "Learned while working",
  operator_admin: "Added here",
}

const SOURCE_TONE: Record<MemoryFact["source"], "outline" | "secondary" | "default"> = {
  user_stated: "outline",
  task_derived: "secondary",
  operator_admin: "outline",
}

/**
 * One remembered fact (docs/archive/UI_UX_AUDIT.md Phase 4): category, content,
 * where it came from, edit-in-place, forget. Source is read-only - it's
 * provenance the backend stamped (memory_manage.py never accepts one from
 * the model, admin.py always stamps operator_admin), not something a
 * person should be able to relabel into a false trust level.
 */
export function MemoryFactCard({ fact }: { fact: MemoryFact }) {
  const [editing, setEditing] = useState(false)
  const [category, setCategory] = useState(fact.category)
  const [content, setContent] = useState(fact.content)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const update = useUpdateMemoryFact()
  const del = useDeleteMemoryFact()

  function cancel() {
    setCategory(fact.category)
    setContent(fact.content)
    setEditing(false)
  }

  function save() {
    const c = category.trim()
    const body = content.trim()
    if (!c || !body) return
    update.mutate(
      { factId: fact.id, category: c, content: body },
      {
        onSuccess: () => {
          setEditing(false)
          toast.success("Fact updated.")
        },
        onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not update the fact."),
      },
    )
  }

  if (editing) {
    return (
      <div className="flex flex-col gap-2 rounded-xl border border-primary/40 bg-card p-3 shadow-sm ring-1 ring-primary/10">
        <Input
          aria-label="Category"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          placeholder="Category"
          maxLength={60}
          className="max-w-xs font-medium"
        />
        <Textarea
          aria-label="Fact content"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="What should YBM remember?"
          maxLength={2000}
          rows={3}
        />
        <div className="flex items-center gap-2 self-end">
          <Button variant="ghost" size="sm" onClick={cancel} disabled={update.isPending}>
            <X className="size-3.5" />
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={save}
            disabled={update.isPending || !category.trim() || !content.trim()}
          >
            <Check className="size-3.5" />
            Save
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="group flex flex-col gap-1.5 rounded-xl border border-border bg-card p-3 shadow-sm transition-colors hover:border-primary/30">
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="secondary" className="font-medium">
            {fact.category}
          </Badge>
          <Badge variant={SOURCE_TONE[fact.source]}>{SOURCE_LABEL[fact.source]}</Badge>
        </div>
        <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
          <Button variant="ghost" size="icon-sm" aria-label="Edit fact" onClick={() => setEditing(true)}>
            <Pencil className="size-3.5" />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="Forget fact" onClick={() => setConfirmDelete(true)}>
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </div>
      <p className="text-sm leading-6 whitespace-pre-wrap [overflow-wrap:anywhere]">{fact.content}</p>
      <p className="text-xs text-muted-foreground">
        Updated {formatRelativeTime(fact.updated_at)}
        {fact.confidence < 1 && ` · ${Math.round(fact.confidence * 100)}% confident`}
      </p>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Forget this fact?"
        description={`YBM will no longer be told "${fact.content}" in future tasks. This can't be undone.`}
        confirmLabel="Forget"
        pending={del.isPending}
        onConfirm={() => {
          del.mutate(fact.id, {
            onSuccess: () => toast.success("Fact forgotten."),
            onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not forget the fact."),
          })
        }}
      />
    </div>
  )
}
