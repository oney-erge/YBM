import { Link } from "react-router"
import { ArrowRight, BrainCircuit, Sparkles, Workflow, Wrench } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { PageHeader } from "@/components/layout/PageHeader"
import { useMemoryFacts, useSettingsSummary, useSkills, useWorkflows } from "@/lib/queries"

/**
 * "What the agent is made of" in one place (docs/UI_UX_AUDIT.md Phase 11) -
 * Tools, Skills, and Memory were three separate, equally-weighted nav
 * entries, which is the exact scatter the operator feedback that prompted
 * this phase named directly: "these are basically agentic setup... should
 * live at one place." The three pages underneath are unchanged and still
 * directly linkable (bookmarks and deep links keep working) - this is a
 * landing hub in front of them, not a rewrite of any of them.
 */
export function AgentHubPage() {
  const memory = useMemoryFacts()
  const skills = useSkills()
  const settings = useSettingsSummary()
  const workflows = useWorkflows()

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageHeader
          eyebrow="Agentic setup"
          title="Agent"
          description="What YBM is made of: what it knows, what it can be told to do, and what it can act with."
        />

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <HubCard
            to="/memory"
            icon={BrainCircuit}
            title="Memory"
            description="Durable facts it remembers, with provenance."
            stat={memory.data ? `${memory.data.facts.length} remembered` : undefined}
          />
          <HubCard
            to="/skills"
            icon={Sparkles}
            title="Skills"
            description="Instructions it reads when relevant to a task."
            stat={skills.data ? `${skills.data.skills.length} installed` : undefined}
          />
          <HubCard
            to="/tools"
            icon={Wrench}
            title="Tools"
            description="What it can actually do, and whether it's allowed to."
            stat={
              settings.data
                ? `${settings.data.tool_registry.enabled} of ${settings.data.tool_registry.total} enabled`
                : undefined
            }
          />
          <HubCard
            to="/workflows"
            icon={Workflow}
            title="Workflows"
            description="Completed tasks saved as a reusable, re-runnable plan."
            stat={workflows.data ? `${workflows.data.workflows.length} saved` : undefined}
          />
        </div>
      </div>
    </div>
  )
}

function HubCard({
  to,
  icon: Icon,
  title,
  description,
  stat,
}: {
  to: string
  icon: typeof BrainCircuit
  title: string
  description: string
  stat?: string
}) {
  return (
    <Link to={to}>
      <Card className="h-full transition-colors hover:border-primary/40 hover:shadow-md">
        <CardContent className="flex flex-col gap-2 p-4">
          <span className="flex size-9 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <Icon className="size-4.5" />
          </span>
          <div className="flex items-center gap-1.5">
            <h3 className="font-semibold">{title}</h3>
            <ArrowRight className="size-3.5 text-muted-foreground" />
          </div>
          <p className="text-xs text-muted-foreground">{description}</p>
          {stat && <p className="mt-auto text-xs font-medium text-primary">{stat}</p>}
        </CardContent>
      </Card>
    </Link>
  )
}
