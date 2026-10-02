import { useState } from "react"
import { toast } from "sonner"
import { Link } from "react-router"
import { Button } from "@/components/ui/button"
import { WorkFoldersCard } from "@/components/chat/WorkFoldersCard"
import { ApiError, type BlockedAccess, type CapabilityAccessMode } from "@/lib/api"
import { useUpdateAccessModes } from "@/lib/queries"

/**
 * What to do when a task was blocked because access is off.
 *
 * Every high-impact capability starts off, which is the right default and the
 * reason a first request often ends here. "No tool can do this" names nothing, so
 * the worker now records which access areas were off (backend access_hints.py) and
 * this turns it into a button: turn it on, and try the request again.
 *
 * Files need a folder to be useful, so they open the folder chooser; everything
 * else is a single click at the gentlest mode that lets the work happen (ask
 * before changing things). Wording follows the evidence: "needs" when a call was
 * actually refused, "may need" when the agent just gave up with access off.
 */
export function AccessHintCard({
  access,
  onRetry,
}: {
  access: BlockedAccess
  /** Ask the same thing again once access has been granted. */
  onRetry?: () => void
}) {
  const update = useUpdateAccessModes()
  const [granted, setGranted] = useState(false)
  const [choosingFolders, setChoosingFolders] = useState(false)
  const certain = access.evidence === "denied"
  const files = access.groups.find((group) => group.group === "filesystem")
  const others = access.groups.filter((group) => group.group !== "filesystem").slice(0, 2)

  function enable(group: string, label: string, mode: string) {
    update.mutate(
      { [group]: mode as CapabilityAccessMode },
      {
        onSuccess: () => {
          setGranted(true)
          toast.success(`${label} is on. YBM will ask before changing anything.`)
        },
        onError: (err) => toast.error(err instanceof ApiError ? err.message : `Could not turn on ${label}.`),
      },
    )
  }

  if (granted) {
    return (
      <div className="mt-3 flex items-center justify-between gap-2 rounded-xl border border-success/30 bg-success/5 px-3 py-2">
        <p className="text-xs">Access is on. Try your request again.</p>
        {onRetry && (
          <Button size="sm" className="h-7 px-2.5 text-xs" onClick={onRetry}>
            Try again
          </Button>
        )}
      </div>
    )
  }

  return (
    <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3">
      <p className="text-xs font-medium">{certain ? "This needs access that is off." : "This may need access that is off."}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {files && (
          <Button size="sm" className="h-7 px-2.5 text-xs" onClick={() => setChoosingFolders((open) => !open)}>
            Choose folders for YBM
          </Button>
        )}
        {others.map((group) => (
          <Button
            key={group.group}
            size="sm"
            variant="outline"
            className="h-7 px-2.5 text-xs"
            disabled={update.isPending}
            onClick={() => enable(group.group, group.label, group.recommended_mode)}
          >
            Turn on {group.label}
          </Button>
        ))}
        <Link to="/access" className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground">
          Open Access
        </Link>
      </div>
      {choosingFolders && (
        <WorkFoldersCard
          compact
          onDone={() => {
            setChoosingFolders(false)
            setGranted(true)
          }}
        />
      )}
    </div>
  )
}
