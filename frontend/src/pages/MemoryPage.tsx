import { useState } from "react"
import { toast } from "sonner"
import { BrainCircuit, Plus } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Card, CardContent } from "@/components/ui/card"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { PageBreadcrumb } from "@/components/layout/PageBreadcrumb"
import { PageHeader } from "@/components/layout/PageHeader"
import { MemoryFactCard } from "@/components/memory/MemoryFactCard"
import { ApiError } from "@/lib/api"
import { useCreateMemoryFact, useMemoryFacts } from "@/lib/queries"

/**
 * Structured memory (docs/archive/UI_UX_AUDIT.md Phase 4): every durable fact YBM
 * currently believes, where it came from, and remember/edit/forget
 * controls - replacing "trust the rolling summary blob" with something
 * inspectable and correctable. Distinct from the rolling per-conversation
 * summary (channels/memory.py), which still runs underneath this.
 */
export function MemoryPage() {
  const [q, setQ] = useState("")
  const { data, isPending, isError, error } = useMemoryFacts({ q: q.trim() || undefined })
  const create = useCreateMemoryFact()
  const [category, setCategory] = useState("")
  const [content, setContent] = useState("")
  const [adding, setAdding] = useState(false)

  const facts = data?.facts ?? []

  function handleCreate() {
    const c = category.trim()
    const body = content.trim()
    if (!c || !body) return
    create.mutate(
      { category: c, content: body },
      {
        onSuccess: () => {
          setCategory("")
          setContent("")
          setAdding(false)
          toast.success("Remembered.")
        },
        onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not save the fact."),
      },
    )
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageBreadcrumb items={[{ label: "Agent", to: "/agent" }, { label: "Memory" }]} />
        <PageHeader
          eyebrow="Structured memory"
          title="Memory"
          description="What YBM currently believes about you and your setup, and where each fact came from. Correct or remove anything wrong - the correction sticks."
          actions={
            !adding && (
              <Button size="sm" onClick={() => setAdding(true)}>
                <Plus className="size-4" />
                Remember something
              </Button>
            )
          }
        />

        {adding && (
          <Card className="border-primary/40 shadow-sm ring-1 ring-primary/10">
            <CardContent className="flex flex-col gap-2">
              <Input
                aria-label="Category"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                placeholder="Category (e.g. preference, project)"
                maxLength={60}
                autoFocus
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
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setAdding(false)
                    setCategory("")
                    setContent("")
                  }}
                  disabled={create.isPending}
                >
                  Cancel
                </Button>
                <Button size="sm" onClick={handleCreate} disabled={create.isPending || !category.trim() || !content.trim()}>
                  Remember
                </Button>
              </div>
            </CardContent>
          </Card>
        )}

        <Input
          aria-label="Search remembered facts"
          placeholder="Search remembered facts"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="max-w-sm"
        />

        {isPending && (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        )}

        {isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load memory</AlertTitle>
            <AlertDescription>{error?.message ?? "Unknown error"}</AlertDescription>
          </Alert>
        )}

        {!isPending && !isError && facts.length === 0 && (
          <Card>
            <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
              <span className="flex size-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
                <BrainCircuit className="size-5" />
              </span>
              <p className="text-sm font-medium">
                {q ? "No facts match that search." : "YBM hasn't remembered anything yet."}
              </p>
              <p className="max-w-sm text-xs text-muted-foreground">
                Facts show up here once you tell it something worth keeping, or once it saves something durable
                while working on a task.
              </p>
            </CardContent>
          </Card>
        )}

        {facts.length > 0 && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {facts.map((fact) => (
              <MemoryFactCard key={fact.id} fact={fact} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
