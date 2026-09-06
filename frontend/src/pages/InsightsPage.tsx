import { useState } from "react"
import { AlertTriangle, ShieldCheck } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader } from "@/components/layout/PageHeader"
import { formatDuration } from "@/lib/time"
import { useReliabilityDashboard } from "@/lib/queries"
import { cn } from "@/lib/utils"

const WINDOW_OPTIONS = [7, 30] as const

/**
 * Cross-task reliability dashboard (docs/ROADMAP.md "reliability
 * dashboard") - the metric that distinguishes this from "completed": a
 * verified-success rate, computed from the same mechanical
 * ToolVerification data a task receipt already claims, not a count of
 * tasks the model said it finished.
 */
export function InsightsPage() {
  const [windowDays, setWindowDays] = useState<(typeof WINDOW_OPTIONS)[number]>(7)
  const { data, isPending, isError } = useReliabilityDashboard(windowDays)

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageHeader
          eyebrow="Reliability"
          title="Insights"
          description="How often work actually finishes verified, not just how often the model said it did."
          actions={
            <div className="flex items-center gap-1 rounded-lg border border-border p-0.5">
              {WINDOW_OPTIONS.map((days) => (
                <Button
                  key={days}
                  type="button"
                  size="sm"
                  variant={windowDays === days ? "default" : "ghost"}
                  className="h-7 px-3 text-xs"
                  onClick={() => setWindowDays(days)}
                >
                  {days}d
                </Button>
              ))}
            </div>
          }
        />

        {isPending && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-24 w-full" />
            ))}
          </div>
        )}

        {isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load the dashboard</AlertTitle>
            <AlertDescription>Try again in a moment.</AlertDescription>
          </Alert>
        )}

        {data && (
          <>
            {data.tasks_attempted === 0 ? (
              <p className="text-sm text-muted-foreground">No tasks in the last {windowDays} days.</p>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <StatTile label="Tasks attempted" value={String(data.tasks_attempted)} />
                  <StatTile
                    label="Completed"
                    value={`${data.completed_pct}%`}
                    sub={`${data.completed}/${data.tasks_attempted}`}
                  />
                  <StatTile
                    label="Verified completed"
                    value={`${data.verified_completed_pct}%`}
                    sub={`${data.verified_completed}/${data.tasks_attempted}`}
                    tone="success"
                    icon={<ShieldCheck className="size-3.5" />}
                  />
                  <StatTile
                    label="Failed"
                    value={`${data.failed_pct}%`}
                    sub={`${data.failed}/${data.tasks_attempted}`}
                    tone={data.failed > 0 ? "destructive" : undefined}
                  />
                  <StatTile label="Mean retries" value={data.mean_retries.toFixed(2)} sub={`${data.tasks_with_retries} task(s) retried`} />
                  <StatTile
                    label="Avg. duration"
                    value={data.avg_task_duration_seconds != null ? formatDuration(data.avg_task_duration_seconds) : "-"}
                  />
                  <StatTile label="Tokens used" value={data.total_tokens.toLocaleString()} />
                  <StatTile
                    label="Fallback used"
                    value={String(data.fallback_tasks)}
                    sub={data.fallback_tasks > 0 ? "task(s) hit a secondary model" : undefined}
                    tone={data.fallback_tasks > 0 ? "warning" : undefined}
                  />
                </div>

                {data.most_unreliable_tool && (
                  <Alert variant="destructive">
                    <AlertTriangle className="size-4" />
                    <AlertTitle>Least reliable tool: {data.most_unreliable_tool}</AlertTitle>
                    <AlertDescription>
                      Highest failure rate among tools called at least 3 times in this window.
                    </AlertDescription>
                  </Alert>
                )}

                <Card>
                  <CardHeader>
                    <CardTitle>Tool reliability</CardTitle>
                    <CardDescription>Every tool called in this window, most active first.</CardDescription>
                  </CardHeader>
                  <CardContent className="overflow-x-auto">
                    {data.tools.length === 0 ? (
                      <p className="text-sm text-muted-foreground">No tool calls in this window.</p>
                    ) : (
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>Tool</TableHead>
                            <TableHead className="text-right">Calls</TableHead>
                            <TableHead className="text-right">Succeeded</TableHead>
                            <TableHead className="text-right">Failed</TableHead>
                            <TableHead className="text-right">Failure rate</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {data.tools.map((tool) => (
                            <TableRow key={tool.tool_name}>
                              <TableCell className="font-mono text-xs">{tool.tool_name}</TableCell>
                              <TableCell className="text-right tabular-nums">{tool.calls}</TableCell>
                              <TableCell className="text-right tabular-nums">{tool.succeeded}</TableCell>
                              <TableCell className="text-right tabular-nums">{tool.failed}</TableCell>
                              <TableCell
                                className={cn(
                                  "text-right tabular-nums",
                                  tool.failure_rate_pct > 0 && "text-destructive",
                                )}
                              >
                                {tool.failure_rate_pct}%
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    )}
                  </CardContent>
                </Card>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-sm">By model</CardTitle>
                    </CardHeader>
                    <CardContent className="overflow-x-auto">
                      {data.by_model.length === 0 ? (
                        <p className="text-sm text-muted-foreground">No model usage recorded.</p>
                      ) : (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>Model</TableHead>
                              <TableHead className="text-right">Tasks</TableHead>
                              <TableHead className="text-right">Completed</TableHead>
                              <TableHead className="text-right">Tokens</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {data.by_model.map((entry) => (
                              <TableRow key={entry.model}>
                                <TableCell className="font-mono text-xs">{entry.model}</TableCell>
                                <TableCell className="text-right tabular-nums">{entry.tasks}</TableCell>
                                <TableCell className="text-right tabular-nums">{entry.completed}</TableCell>
                                <TableCell className="text-right tabular-nums">{entry.total_tokens.toLocaleString()}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )}
                    </CardContent>
                  </Card>

                  <Card>
                    <CardHeader>
                      <CardTitle className="text-sm">By task type</CardTitle>
                    </CardHeader>
                    <CardContent className="overflow-x-auto">
                      {data.by_task_type.length === 0 ? (
                        <p className="text-sm text-muted-foreground">No classified task types recorded.</p>
                      ) : (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>Type</TableHead>
                              <TableHead className="text-right">Tasks</TableHead>
                              <TableHead className="text-right">Completed</TableHead>
                              <TableHead className="text-right">Failed</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {data.by_task_type.map((entry) => (
                              <TableRow key={entry.task_type}>
                                <TableCell className="font-mono text-xs">{entry.task_type}</TableCell>
                                <TableCell className="text-right tabular-nums">{entry.tasks}</TableCell>
                                <TableCell className="text-right tabular-nums">{entry.completed}</TableCell>
                                <TableCell className="text-right tabular-nums">{entry.failed}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )}
                    </CardContent>
                  </Card>
                </div>
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function StatTile({
  label,
  value,
  sub,
  tone,
  icon,
}: {
  label: string
  value: string
  sub?: string
  tone?: "success" | "destructive" | "warning"
  icon?: React.ReactNode
}) {
  const toneClass = tone === "success" ? "text-success" : tone === "destructive" ? "text-destructive" : tone === "warning" ? "text-warning" : "text-foreground"
  return (
    <Card className="shadow-sm">
      <CardContent className="flex flex-col gap-1 p-4">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</span>
        <span className={cn("flex items-center gap-1.5 text-2xl font-semibold tabular-nums", toneClass)}>
          {icon}
          {value}
        </span>
        {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
      </CardContent>
    </Card>
  )
}
