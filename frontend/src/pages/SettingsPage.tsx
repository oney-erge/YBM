import { LLMSettingsCard } from "@/components/settings/LLMSettingsCard"
import { LLMRolesCard } from "@/components/settings/LLMRolesCard"
import { VoiceSettingsCard } from "@/components/settings/VoiceSettingsCard"
import { TelegramSettingsCard } from "@/components/settings/TelegramSettingsCard"
import { VSCodeSettingsCard } from "@/components/settings/VSCodeSettingsCard"
import { WorkspaceSettingsCard } from "@/components/settings/WorkspaceSettingsCard"
import { ComputerUseSettingsCard } from "@/components/settings/ComputerUseSettingsCard"
import { MCPServersCard } from "@/components/settings/MCPServersCard"
import { DiagnosticsCard } from "@/components/settings/DiagnosticsCard"
import { AuditViewerCard } from "@/components/settings/AuditViewerCard"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { useAdvancedMode } from "@/lib/advanced-mode"
import { PageHeader } from "@/components/layout/PageHeader"

/**
 * docs/UI_REWRITE_PLAN.md §14. Level 1: LLM + Telegram. Level 2/Advanced
 * adds every adapter field, MCP servers (read-only), diagnostics, the audit
 * viewer, and per-role model routing (LLMRolesCard). **A2/A3 (per-role
 * prompt override, delegate presets) and D4 (OpenTelemetry export) are not
 * built** - see the plan doc for why (real new backend machinery, same
 * reasoning that scoped D2 out of Phase 2).
 */
export function SettingsPage({ onRerunWizard }: { onRerunWizard: () => void }) {
  const { advanced } = useAdvancedMode()

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
      <PageHeader
        eyebrow="Configuration"
        title="Settings"
        description="Connect the model, channels, editor bridge, and local adapters used by this YBM instance."
      />

      <LLMSettingsCard />
      <VoiceSettingsCard />
      <TelegramSettingsCard />

      {advanced && (
        <>
          <LLMRolesCard />
          <VSCodeSettingsCard />
          <WorkspaceSettingsCard />
          <ComputerUseSettingsCard />
          <MCPServersCard />
          <DiagnosticsCard />
          <AuditViewerCard />
        </>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Setup wizard</CardTitle>
          <CardDescription>Re-run guided model and channel setup.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" size="sm" onClick={onRerunWizard}>
            Re-run setup wizard
          </Button>
        </CardContent>
      </Card>
      </div>
    </div>
  )
}
