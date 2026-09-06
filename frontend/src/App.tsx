import { lazy, Suspense, useState } from "react"
import { Route, Routes } from "react-router"
import { AppShell } from "@/components/layout/AppShell"
import { ChatPage } from "@/pages/ChatPage"
import { TokenEntryScreen } from "@/components/onboarding/TokenEntryScreen"
import { useBootstrap } from "@/lib/queries"
import { getAdminToken } from "@/lib/api"

// Chat is the landing page and should stay light (docs/UI_REWRITE_PLAN.md
// §10: "a first-time user must get an answer without visiting any other
// screen") - lazy-loading Tasks/Trace/Access/Settings/the wizard keeps
// TanStack Table and React Flow (the two heaviest deps this rewrite
// added, and the reason the bundle crossed Vite's 500kB warning
// threshold) out of that first paint.
const TasksPage = lazy(() => import("@/pages/TasksPage").then((m) => ({ default: m.TasksPage })))
const TaskTracePage = lazy(() =>
  import("@/pages/TaskTracePage").then((m) => ({ default: m.TaskTracePage })),
)
const AccessPage = lazy(() => import("@/pages/AccessPage").then((m) => ({ default: m.AccessPage })))
const InsightsPage = lazy(() => import("@/pages/InsightsPage").then((m) => ({ default: m.InsightsPage })))
const MemoryPage = lazy(() => import("@/pages/MemoryPage").then((m) => ({ default: m.MemoryPage })))
const SkillsPage = lazy(() => import("@/pages/SkillsPage").then((m) => ({ default: m.SkillsPage })))
const ToolsPage = lazy(() => import("@/pages/ToolsPage").then((m) => ({ default: m.ToolsPage })))
const AgentHubPage = lazy(() => import("@/pages/AgentHubPage").then((m) => ({ default: m.AgentHubPage })))
const WorkflowsPage = lazy(() => import("@/pages/WorkflowsPage").then((m) => ({ default: m.WorkflowsPage })))
const SettingsPage = lazy(() =>
  import("@/pages/SettingsPage").then((m) => ({ default: m.SettingsPage })),
)
const OnboardingWizard = lazy(() =>
  import("@/components/onboarding/OnboardingWizard").then((m) => ({ default: m.OnboardingWizard })),
)

const SETUP_DISMISSED_KEY = "ybm.setup.dismissed"

// Route table matches docs/UI_REWRITE_PLAN.md §10's page table exactly.
function App() {
  const { data: bootstrap, isPending } = useBootstrap()
  const [manualWizard, setManualWizard] = useState(false)
  // The wizard calls itself "skippable at every step" but `showWizard` used to
  // be driven purely by `onboarding_complete`, which stays false when you skip
  // - so skipping re-rendered the wizard forever and the console was
  // unreachable. Dismissal is remembered per install so a skip actually skips;
  // the console then carries a persistent banner until setup is finished.
  const [dismissedSetup, setDismissedSetup] = useState(
    () => localStorage.getItem(SETUP_DISMISSED_KEY) === "1",
  )
  // Bumped after TokenEntryScreen verifies a manually-typed token, to force
  // this component to re-render past the token gate below (the token itself
  // lives outside React state in lib/api.ts, so setting it alone wouldn't
  // trigger a re-render).
  const [tokenVersion, setTokenVersion] = useState(0)

  // Bootstrap is the one request that must resolve before any real paint
  // (docs/UI_REWRITE_PLAN.md §9 Phase 0.3) - it decides wizard vs console.
  if (isPending) return null

  // A real, previously-missing gap (see TokenEntryScreen's own comment):
  // `run_setup()` always auto-generates AGENT_ADMIN_TOKEN, and every /api/*
  // route except bootstrap enforces it - without this gate, a fresh install
  // 401s on every page with no way to recover from the browser. The
  // ?token= URL case (same-machine `ybm start` auto-open) is captured and
  // stripped synchronously in lib/api.ts, before this ever renders.
  if (bootstrap?.token_required && !getAdminToken()) {
    return <TokenEntryScreen key={tokenVersion} onVerified={() => setTokenVersion((v) => v + 1)} />
  }

  const setupIncomplete = bootstrap ? !bootstrap.onboarding_complete : false
  const showWizard = manualWizard || (setupIncomplete && !dismissedSetup)
  if (showWizard) {
    return (
      <Suspense fallback={<PageFallback />}>
        <OnboardingWizard
          onDone={() => {
            setManualWizard(false)
            // Remember the dismissal even when setup is unfinished, or the
            // next render puts the wizard straight back.
            localStorage.setItem(SETUP_DISMISSED_KEY, "1")
            setDismissedSetup(true)
          }}
        />
      </Suspense>
    )
  }

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<ChatPage />} />
        <Route
          path="tasks"
          element={
            <Suspense fallback={<PageFallback />}>
              <TasksPage />
            </Suspense>
          }
        />
        <Route
          path="tasks/:taskId"
          element={
            <Suspense fallback={<PageFallback />}>
              <TaskTracePage />
            </Suspense>
          }
        />
        <Route
          path="access"
          element={
            <Suspense fallback={<PageFallback />}>
              <AccessPage />
            </Suspense>
          }
        />
        <Route
          path="insights"
          element={
            <Suspense fallback={<PageFallback />}>
              <InsightsPage />
            </Suspense>
          }
        />
        <Route
          path="agent"
          element={
            <Suspense fallback={<PageFallback />}>
              <AgentHubPage />
            </Suspense>
          }
        />
        <Route
          path="workflows"
          element={
            <Suspense fallback={<PageFallback />}>
              <WorkflowsPage />
            </Suspense>
          }
        />
        <Route
          path="memory"
          element={
            <Suspense fallback={<PageFallback />}>
              <MemoryPage />
            </Suspense>
          }
        />
        <Route
          path="skills"
          element={
            <Suspense fallback={<PageFallback />}>
              <SkillsPage />
            </Suspense>
          }
        />
        <Route
          path="tools"
          element={
            <Suspense fallback={<PageFallback />}>
              <ToolsPage />
            </Suspense>
          }
        />
        <Route
          path="settings"
          element={
            <Suspense fallback={<PageFallback />}>
              <SettingsPage onRerunWizard={() => setManualWizard(true)} />
            </Suspense>
          }
        />
      </Route>
    </Routes>
  )
}

function PageFallback() {
  return <div className="p-6 text-sm text-muted-foreground">Loading...</div>
}

export default App
