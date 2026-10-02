import { toast } from "sonner"
import { Check, Download } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError, type Skill } from "@/lib/api"
import { useInstallSkill, useSkillsCatalog } from "@/lib/queries"

/**
 * The bundled starter catalog (docs/archive/UI_UX_AUDIT.md Phase 11) -
 * skills/starter/ in the repo, committed (unlike adapters.skills.root_dir,
 * which is generated and starts empty on every fresh checkout). Installing
 * a catalog entry reuses the exact same installSkill() call the manual
 * form uses, with this entry's own name/description/body/tools - there is
 * no separate "install from catalog" code path on the backend.
 */
export function SkillCatalogBrowser({ installedNames }: { installedNames: Set<string> }) {
  const { data, isPending, isError } = useSkillsCatalog()
  const install = useInstallSkill()

  if (isPending) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    )
  }
  if (isError || !data) {
    return <p className="text-sm text-muted-foreground">Couldn&apos;t load the starter catalog.</p>
  }
  if (data.skills.length === 0) {
    return <p className="text-sm text-muted-foreground">No starter skills bundled with this checkout.</p>
  }

  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {data.skills.map((entry) => (
        <CatalogEntry
          key={entry.name}
          entry={entry}
          installed={installedNames.has(entry.name)}
          installing={install.isPending}
          onInstall={() => {
            install.mutate(
              { name: entry.name, description: entry.description, body: entry.body, version: entry.version, tools: entry.tools },
              {
                onSuccess: () => toast.success(`Installed ${entry.name}.`),
                onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not install the skill."),
              },
            )
          }}
        />
      ))}
    </div>
  )
}

function CatalogEntry({
  entry,
  installed,
  installing,
  onInstall,
}: {
  entry: Skill
  installed: boolean
  installing: boolean
  onInstall: () => void
}) {
  return (
    <Card className="py-0">
      <CardContent className="flex flex-col gap-1.5 p-3">
        <div className="flex items-start justify-between gap-2">
          <h4 className="text-sm font-semibold">{entry.name}</h4>
          {installed ? (
            <Badge variant="secondary" className="shrink-0 gap-1">
              <Check className="size-3" />
              Installed
            </Badge>
          ) : (
            <Button variant="outline" size="sm" className="h-6 shrink-0 px-2 text-xs" disabled={installing} onClick={onInstall}>
              <Download className="size-3" />
              Install
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground">{entry.description}</p>
        {entry.tools.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {entry.tools.map((tool) => (
              <Badge key={tool} variant="outline" className="font-mono text-[10px]">
                {tool}
              </Badge>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
