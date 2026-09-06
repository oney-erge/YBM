import { toast } from "sonner"
import { ShieldOff, Timer } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { useState } from "react"
import { ApiError, type ActiveGrantItem } from "@/lib/api"
import { useActiveGrants, useRevokeGrant } from "@/lib/queries"
import { formatCountdown, useCountdown } from "@/lib/time"

/**
 * "Allow for this task" grants, visible and revocable (docs/ROADMAP.md
 * "scoped temporary authority") - previously created silently and only
 * ever readable by querying the database directly. Every field a grant
 * actually carries is shown: what it covers, where it's narrowed to (if
 * anywhere), how much of its operation budget is used, and how long is
 * left.
 */
export function ActiveGrantsCard() {
  const { data, isPending } = useActiveGrants()
  const revoke = useRevokeGrant()
  const [pendingRevoke, setPendingRevoke] = useState<ActiveGrantItem | null>(null)

  const grants = data?.grants ?? []

  function handleRevoke() {
    if (!pendingRevoke) return
    const target = pendingRevoke
    revoke.mutate(target.grant.id, {
      onSuccess: () => toast.success(`Revoked ${target.grant.tool_name} for "${target.task_objective ?? target.grant.task_id}".`),
      onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not revoke the grant."),
    })
    setPendingRevoke(null)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Active grants</CardTitle>
        <CardDescription>
          Every live &quot;Allow for this task&quot; decision - each is bound to one task, one tool,
          and an operation cap, and expires with the task. Revoke one early if you change your mind.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isPending && <Skeleton className="h-16 w-full" />}
        {!isPending && grants.length === 0 && (
          <p className="text-sm text-muted-foreground">No active grants right now.</p>
        )}
        {!isPending && grants.length > 0 && (
          <ul className="flex flex-col gap-2">
            {grants.map((item) => (
              <GrantRow key={item.grant.id} item={item} onRevoke={() => setPendingRevoke(item)} />
            ))}
          </ul>
        )}
      </CardContent>

      <ConfirmDialog
        open={pendingRevoke != null}
        onOpenChange={(open) => !open && setPendingRevoke(null)}
        title="Revoke this grant?"
        description={
          <>
            <span className="font-mono">{pendingRevoke?.grant.tool_name}</span> will need approval again
            on its very next call in this task.
          </>
        }
        confirmLabel="Revoke"
        pending={revoke.isPending}
        onConfirm={handleRevoke}
      />
    </Card>
  )
}

function GrantRow({ item, onRevoke }: { item: ActiveGrantItem; onRevoke: () => void }) {
  const { grant, task_objective: taskObjective } = item
  const remaining = useCountdown(grant.expires_at)
  const usageLabel = grant.max_operations != null ? `${grant.operations_used}/${grant.max_operations} used` : `${grant.operations_used} used`

  return (
    <li className="flex flex-col gap-1.5 rounded-lg border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="font-mono text-xs">{grant.tool_name}</span>
          <Badge variant="secondary" className="text-[10px]">{grant.capability}</Badge>
        </div>
        <Button type="button" variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs" onClick={onRevoke}>
          <ShieldOff className="size-3" /> Revoke
        </Button>
      </div>
      <p className="truncate text-xs text-muted-foreground" title={taskObjective ?? undefined}>
        {taskObjective ?? grant.task_id}
      </p>
      <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
        <span className="flex items-center gap-1">
          <Timer className="size-3" /> Expires in {formatCountdown(remaining)}
        </span>
        <span>{usageLabel}</span>
        {grant.scope && (
          <span className="min-w-0 truncate font-mono" title={grant.scope}>
            scope: {grant.scope}
          </span>
        )}
      </div>
    </li>
  )
}
