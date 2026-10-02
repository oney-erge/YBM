import { useState } from "react"
import { Plus, Sparkles, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { PageBreadcrumb } from "@/components/layout/PageBreadcrumb"
import { PageHeader } from "@/components/layout/PageHeader"
import { SkillCard } from "@/components/skills/SkillCard"
import { SkillInstallForm } from "@/components/skills/SkillInstallForm"
import { SkillCatalogBrowser } from "@/components/skills/SkillCatalogBrowser"
import { useSkills } from "@/lib/queries"

/**
 * Skill catalog (docs/archive/UI_UX_AUDIT.md Phase 5, reworked Phase 12): install/
 * uninstall entirely from the console, with the tools each skill's
 * instructions reference shown up front - replacing "drop a markdown file
 * into adapters.skills.root_dir by hand". Those tool tags are
 * informational, not enforced permissions - see SkillCard's own docstring
 * for why.
 *
 * One entry point, not two: "Browse catalog" and "Install a skill" used to
 * be separate buttons opening separate stacked panels, and the empty state
 * then mentioned a third route (drop a file in manually) - three answers
 * to "how do I add a skill". Now it's one [+ Add a skill] button opening
 * one panel with two tabs.
 */
export function SkillsPage() {
  const { data, isPending, isError, error } = useSkills()
  const [adding, setAdding] = useState(false)

  const skills = data?.skills ?? []
  const installedNames = new Set(skills.map((s) => s.name))

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageBreadcrumb items={[{ label: "Agent", to: "/agent" }, { label: "Skills" }]} />
        <PageHeader
          eyebrow="Capability packs"
          title="Skills"
          description="Instructions YBM can read when they're relevant to a task - a runbook, a house style guide, anything that's expertise rather than an action. A skill can't execute anything on its own."
          actions={
            !adding && (
              <Button size="sm" onClick={() => setAdding(true)}>
                <Plus className="size-4" />
                Add a skill
              </Button>
            )
          }
        />

        {adding && (
          <Card>
            <CardContent className="flex flex-col gap-3">
              <div className="flex items-start justify-between gap-2">
                <Tabs defaultValue="catalog" className="min-w-0 flex-1">
                  <TabsList>
                    <TabsTrigger value="catalog">Catalog</TabsTrigger>
                    <TabsTrigger value="custom">Write your own</TabsTrigger>
                  </TabsList>
                  <TabsContent value="catalog" className="mt-3">
                    <SkillCatalogBrowser installedNames={installedNames} />
                  </TabsContent>
                  <TabsContent value="custom" className="mt-3">
                    <SkillInstallForm onDone={() => setAdding(false)} bare />
                  </TabsContent>
                </Tabs>
                <Button variant="ghost" size="icon-sm" aria-label="Close" onClick={() => setAdding(false)}>
                  <X className="size-4" />
                </Button>
              </div>
            </CardContent>
          </Card>
        )}

        {isPending && (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}

        {isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load skills</AlertTitle>
            <AlertDescription>{error?.message ?? "Unknown error"}</AlertDescription>
          </Alert>
        )}

        {!isPending && !isError && skills.length === 0 && (
          <Card>
            <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
              <span className="flex size-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
                <Sparkles className="size-5" />
              </span>
              <p className="text-sm font-medium">No skills installed yet.</p>
              <p className="max-w-sm text-xs text-muted-foreground">
                Click "Add a skill" above to browse the starter catalog or write your own. You can also drop a
                markdown file into {data?.root_dir ?? "the skills directory"} directly - it's available on the
                worker's next call either way.
              </p>
            </CardContent>
          </Card>
        )}

        {skills.length > 0 && (
          <div className="flex flex-col gap-3">
            {skills.map((skill) => (
              <SkillCard key={skill.name} skill={skill} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
