import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useSummary } from "@/lib/queries"
import { ACTIVE_STATUSES, type Summary } from "@/lib/api"
import { cn } from "@/lib/utils"

interface HealthItem {
  label: string
  value: string
  ok: boolean
}

/** Mirrors admin_streamlit.py's `_health_items` - a per-domain glance
 * Streamlit showed as a persistent chip strip at the top of every page.
 * Kept as a hover tooltip on the existing compact nav-footer dot rather
 * than a chip strip above every screen (including the minimal Chat
 * landing page, docs/archive/UI_REWRITE_PLAN.md §10's "workbench test") or a
 * dropdown menu - Tooltip is already loaded unconditionally (main.tsx's
 * TooltipProvider), so this adds no bundle cost to Chat's first paint,
 * unlike the dropdown-menu primitive this replaced during review (+27kB
 * gzip on the always-loaded chunk for a click-to-open panel that
 * duplicates data already in Settings > Diagnostics). */
function healthItems(data: Summary): HealthItem[] {
  const workspace = data.config.adapters.workspace
  return [
    {
      label: "LLM",
      value: data.config.llm.default_profile || "missing",
      ok: data.integrations.llm.default_profile_configured,
    },
    {
      label: "Telegram",
      value: data.integrations.telegram.enabled && data.integrations.telegram.token_present
        ? "ready"
        : "needs token/config",
      ok: data.integrations.telegram.enabled && data.integrations.telegram.token_present,
    },
    {
      label: "VS Code",
      value: data.vscode.status,
      ok: data.vscode.connected,
    },
    {
      label: "Workspace",
      value: workspace.root_dir || "missing",
      ok: workspace.enabled !== false && Boolean(workspace.root_dir),
    },
    {
      label: "Database",
      value: data.database.path || data.database.database_url ? "ready" : "unknown",
      ok: Boolean(data.database.path || data.database.database_url),
    },
  ]
}

export function HealthIndicator() {
  const { data, isError, isPending } = useSummary()

  const state: "ok" | "error" | "loading" = isPending ? "loading" : isError ? "error" : "ok"
  const active = data ? data.tasks.filter((t) => ACTIVE_STATUSES.has(t.status)).length : null
  const items = data ? healthItems(data) : []

  const dot = (
    <div className="flex w-full items-center gap-2 rounded-md border border-border px-3 py-2 text-xs">
      <span
        className={cn("size-2 shrink-0 rounded-full", {
          "bg-muted-foreground/40": state === "loading",
          "bg-destructive": state === "error",
          "bg-success": state === "ok",
        })}
        aria-hidden
      />
      <span className="text-muted-foreground">
        {state === "loading" && "Connecting..."}
        {state === "error" && "Backend unreachable"}
        {state === "ok" && `${active} active task${active === 1 ? "" : "s"}`}
      </span>
    </div>
  )

  if (state !== "ok") return dot

  return (
    <Tooltip>
      <TooltipTrigger render={dot} />
      <TooltipContent side="top" align="start" className="flex flex-col gap-1 py-2">
        {items.map((item) => (
          <div key={item.label} className="flex items-center justify-between gap-3 text-xs">
            <span className="opacity-80">{item.label}</span>
            <span className="flex items-center gap-1.5 font-medium">
              <span
                className={cn("size-1.5 rounded-full", item.ok ? "bg-success" : "bg-destructive")}
                aria-hidden
              />
              {item.value}
            </span>
          </div>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}
