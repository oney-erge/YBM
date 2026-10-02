import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError, type SettingsSummary } from "@/lib/api"
import { useServerForm } from "@/lib/use-server-form"
import { useSettingsSummary, useUpdateTelegramConfig } from "@/lib/queries"
import { useAdvancedMode } from "@/lib/advanced-mode"
import {
  TelegramConnectionGuide,
  type TelegramConnection,
} from "@/components/onboarding/TelegramConnectionGuide"

type Draft = {
  enabled: boolean
  tokenEnv: string
  botToken: string
  allowedUserIds: string
  allowedChatIds: string
  polling: boolean
}

function deriveDraft(data: SettingsSummary): Draft {
  const telegram = data.config.channels.telegram
  return {
    enabled: telegram.enabled,
    tokenEnv: telegram.token_env,
    botToken: "",
    allowedUserIds: telegram.allowed_user_ids.join(", "),
    allowedChatIds: telegram.allowed_chat_ids.join(", "),
    polling: telegram.polling,
  }
}

function parseIds(value: string): number[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map(Number)
    .filter((n) => Number.isFinite(n))
}

/** Level 1 (docs/archive/UI_REWRITE_PLAN.md §14). Ports Streamlit's `_render_telegram_config`. */
export function TelegramSettingsCard() {
  const { data, isPending } = useSettingsSummary()
  const [draft, setDraft, resetDraft] = useServerForm(data, deriveDraft)
  const updateTelegram = useUpdateTelegramConfig()
  const { advanced } = useAdvancedMode()
  const [showGuide, setShowGuide] = useState(false)
  const [connection, setConnection] = useState<TelegramConnection | null>(null)

  if (isPending || !draft || !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Telegram</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    )
  }

  const telegram = data.config.channels.telegram
  const configured = telegram.enabled && telegram.token_present && telegram.allowed_user_ids.length > 0

  if (!advanced) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Telegram</CardTitle>
          <CardDescription>
            {configured
              ? `Connected and restricted to ${telegram.allowed_user_ids.length} allowed user${telegram.allowed_user_ids.length === 1 ? "" : "s"}.`
              : "Connect a bot without looking up numeric user or chat IDs."}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {(showGuide || !configured) && (
            <TelegramConnectionGuide tokenPresent={telegram.token_present} onLinked={setConnection} />
          )}
          {connection && (
            <Button
              type="button"
              size="sm"
              className="self-start"
              disabled={updateTelegram.isPending}
              onClick={() => {
                const userIds = connection.message.user_id
                  ? [...new Set([...telegram.allowed_user_ids, connection.message.user_id])]
                  : telegram.allowed_user_ids
                const chatIds = connection.message.chat_id
                  ? [...new Set([...telegram.allowed_chat_ids, connection.message.chat_id])]
                  : telegram.allowed_chat_ids
                updateTelegram.mutate(
                  {
                    enabled: true,
                    polling: true,
                    token_env: telegram.token_env,
                    bot_token: connection.botToken,
                    allowed_user_ids: userIds,
                    allowed_chat_ids: chatIds,
                  },
                  {
                    onSuccess: () => {
                      toast.success("Telegram connected. Restart polling to reload it.")
                      setConnection(null)
                      setShowGuide(false)
                    },
                    onError: (err) =>
                      toast.error(err instanceof ApiError ? err.message : "Could not save Telegram config."),
                  },
                )
              }}
            >
              Save and enable Telegram
            </Button>
          )}
          {configured && !showGuide && (
            <div className="flex flex-wrap gap-2">
              <Button type="button" size="sm" variant="outline" onClick={() => setShowGuide(true)}>
                Reconnect or add me
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={updateTelegram.isPending}
                onClick={() =>
                  updateTelegram.mutate(
                    { enabled: false },
                    {
                      onSuccess: () => toast.success("Telegram disabled. Restart polling to reload it."),
                      onError: (err) =>
                        toast.error(err instanceof ApiError ? err.message : "Could not disable Telegram."),
                    },
                  )
                }
              >
                Disable Telegram
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Telegram</CardTitle>
        <CardDescription>
          {data.config.channels.telegram.token_present ? "Bot token is set." : "No bot token set yet."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            updateTelegram.mutate(
              {
                enabled: draft.enabled,
                token_env: draft.tokenEnv,
                bot_token: draft.botToken || null,
                allowed_user_ids: parseIds(draft.allowedUserIds),
                allowed_chat_ids: parseIds(draft.allowedChatIds),
                polling: draft.polling,
              },
              {
                onSuccess: () => {
                  toast.success("Telegram config saved. Restart polling to reload it.")
                  resetDraft()
                },
                onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not save Telegram config."),
              },
            )
          }}
        >
          <div className="flex items-center gap-2">
            <Switch id="telegram-enabled" checked={draft.enabled} onCheckedChange={(v) => setDraft({ ...draft, enabled: v })} />
            <Label htmlFor="telegram-enabled" className="text-sm">Enabled</Label>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Token env">
              <Input value={draft.tokenEnv} onChange={(e) => setDraft({ ...draft, tokenEnv: e.target.value })} />
            </Field>
            <Field label="Replace bot token">
              <Input type="password" value={draft.botToken} onChange={(e) => setDraft({ ...draft, botToken: e.target.value })} />
            </Field>
            <Field label="Allowed user IDs">
              <Input value={draft.allowedUserIds} onChange={(e) => setDraft({ ...draft, allowedUserIds: e.target.value })} />
            </Field>
            <Field label="Allowed chat IDs">
              <Input value={draft.allowedChatIds} onChange={(e) => setDraft({ ...draft, allowedChatIds: e.target.value })} />
            </Field>
          </div>
          <div className="flex items-center gap-2">
            <Switch id="telegram-polling" checked={draft.polling} onCheckedChange={(v) => setDraft({ ...draft, polling: v })} />
            <Label htmlFor="telegram-polling" className="text-sm">Polling</Label>
          </div>
          <div className="flex justify-end">
            <Button type="submit" size="sm" disabled={updateTelegram.isPending}>
              Save Telegram config
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Label className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </Label>
  )
}
