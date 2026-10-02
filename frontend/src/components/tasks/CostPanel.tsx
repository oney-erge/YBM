import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { TokenUsage } from "@/lib/api"

/** docs/archive/UI_REWRITE_PLAN.md §12.3 (D5) - per task, with the by_source split
 * (operator/auditor/subagent) that was already computed and barely
 * surfaced before this. */
export function CostPanel({ usage }: { usage: TokenUsage }) {
  const bySource = usage.by_source ?? {}
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Token usage</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-semibold">{usage.total_tokens ?? "-"}</span>
          <span className="text-muted-foreground">
            total over {usage.calls} call{usage.calls === 1 ? "" : "s"}
          </span>
        </div>
        {usage.last_model && (
          <p className="text-xs text-muted-foreground">last model: {usage.last_model}</p>
        )}
        {Object.keys(bySource).length > 0 && (
          <div className="mt-1 flex flex-col gap-1 border-t border-border pt-2">
            {Object.entries(bySource).map(([source, entry]) => (
              <div key={source} className="flex justify-between text-xs">
                <span className="text-muted-foreground">{source}</span>
                <span>
                  {entry.total_tokens ?? "-"} tokens · {entry.calls} call{entry.calls === 1 ? "" : "s"}
                </span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
