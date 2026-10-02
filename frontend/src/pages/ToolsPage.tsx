import { useMemo, useState } from "react"
import { Link } from "react-router"
import { ArrowRight, Lock, Search, Wrench } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { PageBreadcrumb } from "@/components/layout/PageBreadcrumb"
import { PageHeader } from "@/components/layout/PageHeader"
import { useSettingsSummary } from "@/lib/queries"
import type { RiskLevel, ToolItem } from "@/lib/api"

const RISK_TONE: Record<RiskLevel, string> = {
  low: "bg-info/10 text-info",
  medium: "bg-warning/10 text-warning",
  high: "bg-destructive/10 text-destructive",
  critical: "bg-destructive/15 text-destructive",
}

const GROUP_LABEL: Record<string, string> = {
  filesystem: "Filesystem",
  documents: "Documents",
  browser: "Browser",
  desktop: "Desktop",
  coding_agents: "Coding agents",
  schedules: "Scheduling",
  artifacts: "Artifacts",
  adapters: "Adapter factory",
}

/**
 * What YBM can do, and whether it's currently allowed to (docs/archive/UI_UX_AUDIT.md
 * Phase 11) - _tool_registry_summary already computed this (every
 * registered tool's group, capability, enabled state, operations, and
 * effective risk); it just had no page of its own, only internal use by
 * Diagnostics' service summary. Read-only here on purpose: enabling or
 * disabling a tool means changing its underlying capability, which Access
 * already owns as the one place that does it - this page answers "what
 * exists and can it currently run", not a second control surface for the
 * same toggle.
 */
export function ToolsPage() {
  const { data, isPending, isError, error } = useSettingsSummary()
  const [search, setSearch] = useState("")

  const tools = useMemo(() => data?.tool_registry.tools ?? [], [data?.tool_registry.tools])
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return tools
    return tools.filter(
      (t) => t.name.toLowerCase().includes(q) || t.capability.toLowerCase().includes(q) || t.group.toLowerCase().includes(q),
    )
  }, [tools, search])

  const groups = useMemo(() => {
    const byGroup = new Map<string, ToolItem[]>()
    for (const tool of filtered) {
      const list = byGroup.get(tool.group) ?? []
      list.push(tool)
      byGroup.set(tool.group, list)
    }
    return [...byGroup.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [filtered])

  return (
    <div className="h-full overflow-y-auto">
      {/* max-w-6xl to match Tasks and Settings. At 4xl this inventory of 22
          tools used about 58% of a 1440px window, and every single-tool group
          left half a row empty. */}
      <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageBreadcrumb items={[{ label: "Agent", to: "/agent" }, { label: "Tools" }]} />
        <PageHeader
          eyebrow="Agentic setup · read-only inventory"
          title="Tools"
          description="Every capability YBM's registry knows about, grouped by domain, and whether it's currently allowed to run. This page is the inventory, not the switchboard - enabling or disabling a capability always happens on Access."
          actions={
            data && (
              <div className="flex items-center gap-2 rounded-xl bg-card px-3 py-2 text-sm shadow-sm ring-1 ring-border">
                <Wrench className="size-4 text-primary" />
                <span className="font-semibold">{data.tool_registry.enabled}</span>
                <span className="text-muted-foreground">of {data.tool_registry.total} enabled</span>
              </div>
            )
          }
        />

        {isPending && (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}

        {isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load the tool registry</AlertTitle>
            <AlertDescription>{error?.message ?? "Unknown error"}</AlertDescription>
          </Alert>
        )}

        {!isPending && !isError && (
          <>
            <div className="relative max-w-sm">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                aria-label="Search tools, capabilities, or groups"
                placeholder="Search tools, capabilities, or groups..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-10 bg-card pl-9 shadow-sm"
              />
            </div>

            {groups.length === 0 && <p className="text-sm text-muted-foreground">No tools match that search.</p>}

            {groups.map(([group, groupTools]) => (
              <section key={group}>
                <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {GROUP_LABEL[group] ?? group.replace(/_/g, " ")} ({groupTools.length})
                </h2>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
                  {groupTools.map((tool) => (
                    <ToolRow key={tool.name} tool={tool} />
                  ))}
                </div>
              </section>
            ))}
          </>
        )}
      </div>
    </div>
  )
}

function ToolRow({ tool }: { tool: ToolItem }) {
  return (
    <Card className={tool.enabled ? "py-0" : "py-0 opacity-75"}>
      <CardContent className="flex flex-col gap-1.5 p-3">
        <div className="flex items-start justify-between gap-2">
          <span className="truncate font-mono text-sm font-medium">{tool.name}</span>
          <Badge variant={tool.enabled ? "secondary" : "outline"} className="shrink-0 gap-1">
            {!tool.enabled && <Lock className="size-2.5" />}
            {tool.enabled ? "enabled" : "disabled"}
          </Badge>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className={`rounded-full px-1.5 py-0.5 font-medium ${RISK_TONE[tool.risk_level]}`}>
            {tool.risk_level} risk
          </span>
          <span className="text-muted-foreground">{tool.operations.length} operation{tool.operations.length === 1 ? "" : "s"}</span>
        </div>
        <p className="truncate text-[11px] text-muted-foreground" title={tool.operations.join(", ")}>
          {tool.operations.join(", ")}
        </p>
        {tool.enabled ? (
          <Link to="/access" className="text-[11px] text-muted-foreground hover:text-primary hover:underline">
            {tool.capability} · manage in Access
          </Link>
        ) : (
          <Link
            to="/access"
            className="flex items-center gap-1 text-[11px] font-medium text-warning hover:underline"
          >
            Disabled - enable {tool.capability} in Access
            <ArrowRight className="size-3" />
          </Link>
        )}
      </CardContent>
    </Card>
  )
}
