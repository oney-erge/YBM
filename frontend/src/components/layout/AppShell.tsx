import { useState } from "react"
import { NavLink, Outlet, useLocation } from "react-router"
import { ActivitySquare, Bot, ListTree, MessageSquare, Settings, ShieldCheck } from "lucide-react"
import { cn } from "@/lib/utils"
import { HealthIndicator } from "@/components/layout/HealthIndicator"
import { ApprovalBanner } from "@/components/approvals/ApprovalBanner"
import { ErrorBoundary } from "@/components/layout/ErrorBoundary"
import { SafetyTourBanner } from "@/components/layout/SafetyTourBanner"
import { SetupIncompleteBanner } from "@/components/layout/SetupIncompleteBanner"
import { useBootstrap } from "@/lib/queries"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { AdvancedModeContext, readAdvancedMode, writeAdvancedMode } from "@/lib/advanced-mode"
import { ThemeToggle } from "@/components/layout/ThemeToggle"

// Matches docs/UI_REWRITE_PLAN.md §10's table. Memory (Phase 4), Skills
// (Phase 5), and Tools (Phase 11) all live under one "Agent" entry rather
// than three separate top-level items - docs/UI_UX_AUDIT.md Phase 11's
// regroup, in response to direct feedback that they read as scattered
// ("these are basically agentic setup... should live at one place").
// Chat is the landing page/first route on purpose (plan: "a first-time
// user must get an answer without visiting any other screen").
// matchPaths: sibling routes that should also highlight this item even
// though they don't share its URL prefix (docs/UI_UX_AUDIT.md Phase 12) -
// Memory/Skills/Tools are reachable from the Agent hub but live at their
// own top-level paths (kept that way in Phase 11 so bookmarks and deep
// links didn't break), so NavLink's own prefix-based isActive never
// matched them. Fixed here instead of by nesting the routes.
const NAV_ITEMS = [
  { to: "/", label: "Chat", icon: MessageSquare, end: true, matchPaths: [] as string[] },
  { to: "/tasks", label: "Tasks", icon: ListTree, end: false, matchPaths: [] as string[] },
  { to: "/insights", label: "Insights", icon: ActivitySquare, end: false, matchPaths: [] as string[] },
  { to: "/access", label: "Access", icon: ShieldCheck, end: false, matchPaths: [] as string[] },
  { to: "/agent", label: "Agent", icon: Bot, end: false, matchPaths: ["/memory", "/skills", "/tools"] },
  { to: "/settings", label: "Settings", icon: Settings, end: false, matchPaths: [] as string[] },
] as const

function isNavItemActive(item: (typeof NAV_ITEMS)[number], pathname: string): boolean {
  if (item.end) return pathname === item.to
  if (pathname === item.to || pathname.startsWith(`${item.to}/`)) return true
  return item.matchPaths.some((path) => pathname === path || pathname.startsWith(`${path}/`))
}

export function AppShell() {
  const { data: bootstrap } = useBootstrap()
  const [advanced, setAdvancedState] = useState(readAdvancedMode)
  const location = useLocation()
  const setAdvanced = (value: boolean) => {
    setAdvancedState(value)
    writeAdvancedMode(value)
  }

  return (
    <AdvancedModeContext.Provider value={{ advanced, setAdvanced }}>
      <div className="flex h-svh w-full flex-col overflow-hidden bg-background text-foreground">
        {/* Full-width, above both nav and content - "persistent, unmissable,
            on every route" (docs/UI_REWRITE_PLAN.md §11.1). Renders nothing
            when there are no pending approvals. */}
        <ApprovalBanner />
        {/* While no model is configured the console cannot answer anything.
            Skipping the wizard is allowed now, so this is what stops that
            being a silent dead end. */}
        {bootstrap && !bootstrap.onboarding_complete && (
          <SetupIncompleteBanner
            onResume={() => {
              localStorage.removeItem("ybm.setup.dismissed")
              window.location.reload()
            }}
          />
        )}
        {/* Setup already explains the blocking condition and route back. Do
            not stack a second banner above every mobile screen until setup is
            complete. */}
        {bootstrap?.onboarding_complete && <SafetyTourBanner />}
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-sidebar-border bg-sidebar px-4 md:hidden">
          <Brand />
          <ThemeToggle />
        </header>
        <div className="flex min-h-0 flex-1">
          <nav className="hidden w-64 shrink-0 flex-col justify-between border-r border-sidebar-border bg-sidebar p-4 md:flex">
            <div>
              <div className="mb-7 px-2">
                <Brand />
              </div>
              <ul className="space-y-1.5">
                {NAV_ITEMS.map((item) => {
                  const { to, label, icon: Icon, end } = item
                  const active = isNavItemActive(item, location.pathname)
                  return (
                    <li key={to}>
                      <NavLink
                        to={to}
                        end={end}
                        aria-current={active ? "page" : undefined}
                        className={cn(
                          "group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors",
                          active
                            ? "bg-sidebar-accent text-sidebar-accent-foreground shadow-sm"
                            : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                        )}
                      >
                        <Icon className="size-4.5 transition-transform group-hover:scale-105" />
                        {label}
                      </NavLink>
                    </li>
                  )
                })}
              </ul>
            </div>
            <div className="flex flex-col gap-3">
              {/* One switch, reveals Level 2 everywhere (docs/UI_REWRITE_PLAN.md
                  §6) - not a per-panel disclosure to hunt for on each screen. */}
              <div className="flex items-center justify-between gap-2 rounded-xl px-2 py-1">
                <Label htmlFor="advanced-mode" className="text-xs text-muted-foreground">
                  Advanced mode
                </Label>
                <Switch id="advanced-mode" checked={advanced} onCheckedChange={setAdvanced} />
              </div>
              <HealthIndicator />
              <div className="flex items-center justify-between border-t border-sidebar-border pt-2 pl-2">
                <span className="text-xs text-muted-foreground">Appearance</span>
                <ThemeToggle />
              </div>
            </div>
          </nav>
          <main className="min-w-0 flex-1 overflow-hidden pb-17 md:pb-0">
            {/* Keyed on pathname so navigating to a different page after a
                crash clears the boundary's error state instead of the new
                page inheriting the old page's fallback (a class component
                doesn't reset state just because its children prop changes). */}
            <ErrorBoundary key={location.pathname} variant="page">
              <Outlet />
            </ErrorBoundary>
          </main>
        </div>
        <nav className="fixed inset-x-0 bottom-0 z-40 grid h-17 grid-cols-5 border-t border-sidebar-border bg-sidebar/95 px-2 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden">
          {NAV_ITEMS.map((item) => {
            const { to, label, icon: Icon, end } = item
            const active = isNavItemActive(item, location.pathname)
            return (
              <NavLink
                key={to}
                to={to}
                end={end}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex min-w-0 flex-col items-center justify-center gap-1 rounded-lg text-[11px] font-medium transition-colors",
                  active ? "text-primary" : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className="size-5" />
                <span className="truncate">{label}</span>
              </NavLink>
            )
          })}
        </nav>
      </div>
    </AdvancedModeContext.Provider>
  )
}

function Brand() {
  // The same mark as the browser tab (public/favicon.svg), not a Lucide
  // Bot glyph in a colored box (docs/UI_UX_AUDIT.md Phase 10) - the two
  // disagreeing was the actual gap, not a missing logo; a real one already
  // existed and just wasn't reused here. BASE_URL-prefixed the same way
  // main.tsx's router basename is, since this app is served at /admin in
  // production, not domain root.
  return (
    <div className="flex items-center gap-2.5">
      <img src={`${import.meta.env.BASE_URL}favicon.svg`} alt="" className="size-8" />
      <span className="text-base font-semibold tracking-tight text-sidebar-foreground">YBM</span>
    </div>
  )
}
