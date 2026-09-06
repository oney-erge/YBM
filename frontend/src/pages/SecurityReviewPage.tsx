import { useState } from "react"
import { Globe, Lock, ShieldAlert, ShieldCheck, Terminal } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { PageHeader } from "@/components/layout/PageHeader"
import { useSecurityReview } from "@/lib/queries"
import { cn } from "@/lib/utils"

const WINDOW_OPTIONS = [7, 30] as const

/**
 * A page, not a CLI command (docs/ROADMAP.md "Finish the Proof"): this
 * machine's actual exposure, in the surface a first-time user already
 * opens rather than a terminal flag they'd have to know exists. Every
 * fact here is read from configuration or a recorded row - nothing is
 * probed live, so this loads as fast as any other page and never blocks
 * on the network or Docker.
 */
export function SecurityReviewPage() {
  const [windowDays, setWindowDays] = useState<(typeof WINDOW_OPTIONS)[number]>(7)
  const { data, isPending, isError } = useSecurityReview(windowDays)

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
        <PageHeader
          eyebrow="Exposure"
          title="Security review"
          description="What this machine is actually exposed to right now - read from configuration and recorded activity, not a live scan."
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
          <div className="flex flex-col gap-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-24 w-full" />
            ))}
          </div>
        )}

        {isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load the security review</AlertTitle>
            <AlertDescription>Try again in a moment.</AlertDescription>
          </Alert>
        )}

        {data && (
          <>
            <Card className={cn(data.network.reachable_beyond_this_machine ? "border-destructive/40" : "border-success/30")}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  {data.network.reachable_beyond_this_machine ? (
                    <ShieldAlert className="size-4.5 text-destructive" />
                  ) : (
                    <ShieldCheck className="size-4.5 text-success" />
                  )}
                  Network
                </CardTitle>
                <CardDescription>
                  {data.network.reachable_beyond_this_machine
                    ? `Bound to ${data.network.host}:${data.network.port} - reachable beyond this machine.`
                    : `Bound to ${data.network.host}:${data.network.port} - loopback only, not reachable from the network.`}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-wrap gap-2">
                <Badge variant="outline" className={cn(!data.network.admin_enabled && "text-muted-foreground")}>
                  Admin console: {data.network.admin_enabled ? "enabled" : "disabled"}
                </Badge>
                <Badge
                  variant="outline"
                  className={cn(
                    !data.network.admin_token_set &&
                      data.network.reachable_beyond_this_machine &&
                      "border-destructive/40 text-destructive",
                  )}
                >
                  <Lock className="size-3" />
                  Admin token: {data.network.admin_token_set ? "set" : "not set"}
                </Badge>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">Active approval grants</CardTitle>
                <CardDescription>
                  &quot;Allow for this task&quot; grants currently usable, across every task.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {data.active_grants.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No active grants right now.</p>
                ) : (
                  <div className="overflow-x-auto">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Tool</TableHead>
                          <TableHead>Capability</TableHead>
                          <TableHead>Scope</TableHead>
                          <TableHead className="text-right">Used</TableHead>
                          <TableHead>Expires</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.active_grants.map((grant) => (
                          <TableRow key={grant.id}>
                            <TableCell className="font-mono text-xs">{grant.tool_name}</TableCell>
                            <TableCell className="font-mono text-xs">{grant.capability}</TableCell>
                            <TableCell className="font-mono text-xs">{grant.scope ?? "(unscoped)"}</TableCell>
                            <TableCell className="text-right tabular-nums">
                              {grant.operations_used}
                              {grant.max_operations != null && ` / ${grant.max_operations}`}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {new Date(grant.expires_at).toLocaleString()}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </CardContent>
            </Card>

            <Card
              className={cn(data.capability_access.no_approval_required.length > 0 && "border-warning/40")}
            >
              <CardHeader>
                <CardTitle className="text-base">Capabilities that act without asking first</CardTitle>
                <CardDescription>
                  {data.capability_access.enabled} capabilit{data.capability_access.enabled === 1 ? "y" : "ies"}{" "}
                  enabled in total.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {data.capability_access.no_approval_required.length === 0 ? (
                  <p className="text-sm text-muted-foreground">Every enabled capability requires approval.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {data.capability_access.no_approval_required.map((capability) => (
                      <Badge key={capability} variant="outline" className="border-warning/40 font-mono text-xs text-warning">
                        {capability}
                      </Badge>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">MCP servers</CardTitle>
                <CardDescription>External tool servers configured for this machine to reach.</CardDescription>
              </CardHeader>
              <CardContent>
                {data.mcp_servers.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No MCP servers configured.</p>
                ) : (
                  <ul className="flex flex-col gap-1.5 text-sm">
                    {data.mcp_servers.map((server) => (
                      <li key={server.name} className="flex items-center gap-2">
                        <Terminal className="size-3.5 shrink-0 text-muted-foreground" />
                        <span className="font-mono text-xs">{server.name}</span>
                        <span className="text-xs text-muted-foreground">{server.command}</span>
                        <Badge variant={server.enabled ? "outline" : "secondary"} className="ml-auto text-[10px]">
                          {server.enabled ? "enabled" : "disabled"}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">External hosts contacted</CardTitle>
                  <CardDescription>In the last {windowDays} days, across every task.</CardDescription>
                </CardHeader>
                <CardContent>
                  {data.external_hosts_contacted.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No external hosts recorded in this window.</p>
                  ) : (
                    <ul className="flex flex-col gap-1 text-xs">
                      {data.external_hosts_contacted.map((host) => (
                        <li key={host} className="flex items-center gap-1.5">
                          <Globe className="size-3 shrink-0 text-muted-foreground" />
                          <span className="font-mono [overflow-wrap:anywhere]">{host}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>

              <Card className={cn(data.code_execution.unsandboxed_runs > 0 && "border-warning/40")}>
                <CardHeader>
                  <CardTitle className="text-base">Code execution</CardTitle>
                  <CardDescription>In the last {windowDays} days.</CardDescription>
                </CardHeader>
                <CardContent className="flex gap-4 text-sm">
                  <span>
                    <span className="font-semibold tabular-nums text-success">{data.code_execution.sandboxed_runs}</span>{" "}
                    sandboxed
                  </span>
                  <span>
                    <span
                      className={cn(
                        "font-semibold tabular-nums",
                        data.code_execution.unsandboxed_runs > 0 ? "text-warning" : "text-muted-foreground",
                      )}
                    >
                      {data.code_execution.unsandboxed_runs}
                    </span>{" "}
                    unsandboxed
                  </span>
                </CardContent>
              </Card>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
