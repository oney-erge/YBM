import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { ChannelSpec } from "@/lib/api"

/**
 * The ways to reach YBM, as a grid of cards.
 *
 * This replaced a single hardcoded "Also enable Telegram" toggle, which told a
 * new user that Telegram was the only thing that would ever exist and gave web
 * chat no visible status at all. The list comes from the backend catalog, so
 * adding a channel is a table row rather than a UI change - and planned
 * channels are shown greyed with a reason rather than hidden, because the
 * shape of the product is itself useful information.
 */

const STATUS: Record<ChannelSpec["status"], { label: string; tone: string }> = {
  ready: { label: "Available", tone: "text-muted-foreground" },
  manual: { label: "In Settings", tone: "text-muted-foreground" },
  planned: { label: "Coming soon", tone: "text-muted-foreground" },
}

export function ChannelGrid({
  channels,
  onConnect,
  showPlanned = false,
}: {
  channels: ChannelSpec[]
  onConnect: (key: string) => void
  /** Planned channels are hidden by default: on a first-run screen, five "Not
   * built yet" cards read as clutter and a promise, not as information. */
  showPlanned?: boolean
}) {
  const visible = showPlanned ? channels : channels.filter((channel) => channel.status !== "planned")
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {visible.map((channel) => {
        const selectable = channel.status === "ready" && !channel.connected && !channel.zero_setup
        return (
          <div
            key={channel.key}
            className={`flex flex-col gap-1.5 rounded-lg border p-3 transition-colors ${
              channel.connected
                ? "border-success/40 bg-success/5"
                : channel.status === "planned"
                  ? "border-border/60 bg-muted/20 opacity-70"
                  : "border-border hover:border-foreground/30"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">{channel.label}</span>
              {channel.connected ? (
                <Badge variant="secondary">Connected</Badge>
              ) : (
                <span className={`text-xs ${STATUS[channel.status].tone}`}>
                  {STATUS[channel.status].label}
                </span>
              )}
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">{channel.blurb}</p>
            {channel.note && (
              <p className="text-xs text-muted-foreground/80">{channel.note}</p>
            )}
            {selectable && (
              <Button
                size="sm"
                variant="outline"
                className="mt-1 self-start"
                onClick={() => onConnect(channel.key)}
              >
                Connect
              </Button>
            )}
          </div>
        )
      })}
    </div>
  )
}
