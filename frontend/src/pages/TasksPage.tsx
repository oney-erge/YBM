import { useMemo, useState } from "react"
import { useNavigate } from "react-router"
import { ListTree, Search } from "lucide-react"
import { flexRender, getCoreRowModel, useReactTable, createColumnHelper } from "@tanstack/react-table"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { StatusBadge } from "@/components/tasks/StatusBadge"
import { ActiveTasksPanel } from "@/components/tasks/ActiveTasksPanel"
import { TaskOutcomeCell } from "@/components/tasks/TaskOutcomeCell"
import { ClearHistoryButton } from "@/components/tasks/ClearHistoryButton"
import { PageHeader } from "@/components/layout/PageHeader"
import { Card, CardContent } from "@/components/ui/card"
import { useTasks } from "@/lib/queries"
import { cn } from "@/lib/utils"
import type { TaskRecord, TaskStatus } from "@/lib/api"

const STATUS_OPTIONS: (TaskStatus | "all")[] = [
  "all", "received", "interpreting", "clarifying", "planned", "awaiting_approval",
  "awaiting_external", "running", "paused", "retrying", "blocked", "completed",
  "cancelled", "failed",
]

const columnHelper = createColumnHelper<TaskRecord>()

export function TasksPage() {
  const { data, isPending, isError, error } = useTasks(100)
  const navigate = useNavigate()
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "all">("all")
  const [search, setSearch] = useState("")

  // Client-side filter/search: the backend's /api/tasks has no status/query
  // params today (docs/archive/UI_REWRITE_PLAN.md §12.1), and a personal-scale task
  // history (dozens to low hundreds) makes filtering the fetched page
  // cheap enough that adding server-side filtering would be solving a
  // problem that doesn't exist yet - the same reasoning knowledge_base.py
  // already applies to its own re-index-on-every-call design.
  const filtered = useMemo(() => {
    const tasks = data?.tasks ?? []
    const bySearch = search.trim()
      ? tasks.filter((t) => t.objective.toLowerCase().includes(search.trim().toLowerCase()))
      : tasks
    return statusFilter === "all" ? bySearch : bySearch.filter((t) => t.status === statusFilter)
  }, [data, statusFilter, search])

  // Widths are declared, and the table below is `table-fixed`, because an
  // auto-layout table sizes each column to its longest unwrapped line. The
  // cells already carried line-clamp-1, but that clamps vertically - it stops
  // the text wrapping without constraining the box - so a long objective or
  // outcome grew the column instead of being truncated. Measured before this
  // change: the Objective cell was 3,321px and Outcome 26,618px, for a table
  // 30,525px wide inside a 1,440px window.
  const columns = useMemo(
    () => [
      columnHelper.accessor("objective", {
        header: "Objective",
        meta: { headClassName: "w-[38%]", cellClassName: "whitespace-normal" },
        cell: (info) => (
          <span className="line-clamp-2 [overflow-wrap:anywhere]" title={info.getValue()}>
            {info.getValue()}
          </span>
        ),
      }),
      columnHelper.accessor("status", {
        header: "Status",
        meta: { headClassName: "w-[132px]" },
        cell: (info) => <StatusBadge status={info.getValue()} />,
      }),
      columnHelper.display({
        id: "outcome",
        header: "Outcome",
        meta: { headClassName: "w-auto", cellClassName: "whitespace-normal" },
        cell: (info) => <TaskOutcomeCell task={info.row.original} />,
      }),
      columnHelper.accessor("created_at", {
        header: "Created",
        meta: { headClassName: "w-[168px]" },
        cell: (info) => (
          <span className="text-sm whitespace-nowrap text-muted-foreground">
            {new Date(info.getValue()).toLocaleString()}
          </span>
        ),
      }),
    ],
    [],
  )

  const table = useReactTable({
    data: filtered,
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8 [&>*]:shrink-0">
      <PageHeader
        eyebrow="Activity"
        title="Tasks"
        description="Monitor every request, open its evidence trail, and quickly find failures or work still in progress."
        actions={
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2 rounded-xl bg-card px-3 py-2 text-sm shadow-sm ring-1 ring-border">
              <ListTree className="size-4 text-primary" />
              <span className="font-semibold">{data?.pagination.total ?? 0}</span>
              <span className="text-muted-foreground">total</span>
            </div>
            <ClearHistoryButton />
          </div>
        }
      />
      <ActiveTasksPanel />

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative min-w-0 flex-1 sm:max-w-md">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            aria-label="Search objectives"
            placeholder="Search objectives..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-10 bg-card pl-9 shadow-sm"
          />
        </div>
        <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as TaskStatus | "all")}>
          <SelectTrigger className="h-10 w-full bg-card shadow-sm sm:w-48">
            <SelectValue>{statusFilter === "all" ? "All statuses" : statusFilter.replace(/_/g, " ")}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((s) => (
              <SelectItem key={s} value={s}>
                {s === "all" ? "All statuses" : s.replace(/_/g, " ")}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {isError && (
        <Alert variant="destructive">
          <AlertTitle>Couldn't load tasks</AlertTitle>
          <AlertDescription>{error.message}</AlertDescription>
        </Alert>
      )}

      {isPending && <Skeleton className="h-64 w-full" />}

      {!isPending && !isError && filtered.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {data?.tasks.length ? "No tasks match this filter." : "No tasks yet."}
        </p>
      )}

      {!isPending && filtered.length > 0 && (
        <>
        <div className="flex flex-col gap-2 sm:hidden">
          {filtered.map((task) => (
            <button
              key={task.id}
              type="button"
              onClick={() => navigate(`/tasks/${task.id}`)}
              className="flex min-w-0 flex-col gap-2 rounded-xl bg-card p-3 text-left shadow-sm ring-1 ring-border transition-colors hover:bg-muted/40"
            >
              <div className="flex min-w-0 items-start justify-between gap-2">
                <span className="line-clamp-2 min-w-0 text-sm font-medium [overflow-wrap:anywhere]">{task.objective}</span>
                <StatusBadge status={task.status} />
              </div>
              <TaskOutcomeCell task={task} />
              <span className="text-xs text-muted-foreground">{new Date(task.created_at).toLocaleString()}</span>
            </button>
          ))}
        </div>
        <Card className="hidden py-0 shadow-sm ring-border sm:block">
          <CardContent className="px-0">
          <Table className="table-fixed">
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <TableHead
                    key={header.id}
                    className={(header.column.columnDef.meta as { headClassName?: string } | undefined)?.headClassName}
                  >
                    {flexRender(header.column.columnDef.header, header.getContext())}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.map((row) => (
              <TableRow
                key={row.id}
                className="cursor-pointer transition-colors hover:bg-muted/60"
                onClick={() => navigate(`/tasks/${row.original.id}`)}
              >
                {row.getVisibleCells().map((cell) => (
                  <TableCell
                    key={cell.id}
                    className={cn(
                      "align-top",
                      (cell.column.columnDef.meta as { cellClassName?: string } | undefined)?.cellClassName,
                    )}
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
          </CardContent>
        </Card>
        </>
      )}
      </div>
    </div>
  )
}
