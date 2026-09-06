import { useState } from "react"
import { CheckCircle2, ChevronDown, ChevronRight, XCircle } from "lucide-react"
import { useAdapterReview } from "@/lib/queries"
import { cn } from "@/lib/utils"

/**
 * "I generated a connector. Here is exactly what it will access. Tests
 * pass. Install it?" (docs/ROADMAP.md "integration control plane") - shown
 * only for a pending adapter.factory promote_after_approval approval,
 * whose own tool_input carries just adapter_dir and approved=true
 * (adapter_factory.py's own docstring on why that call carries no code).
 * This is the review the plan actually asks for: the generated source and
 * a real sandbox test result, not a decision made from the tool name alone.
 */
export function AdapterReviewPanel({ adapterDir }: { adapterDir: string }) {
  const { data, isPending, isError } = useAdapterReview(adapterDir)
  const [expandedFile, setExpandedFile] = useState<string | null>("adapter.py")

  if (isPending) {
    return <p className="text-xs text-muted-foreground">Loading the generated adapter...</p>
  }
  if (isError || !data) {
    return <p className="text-xs text-destructive">Could not load this adapter's generated files.</p>
  }

  const manifest = data.manifest as { objective?: string; capability?: string; operations?: string[] }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Generated adapter</p>
        <div
          className={cn(
            "flex items-center gap-1 text-xs font-medium",
            data.test.passed ? "text-success" : "text-destructive",
          )}
        >
          {data.test.passed ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}
          {data.test.passed ? "Sandbox tests pass" : "Sandbox tests failed"}
        </div>
      </div>

      {manifest.objective && <p className="text-xs text-muted-foreground">{manifest.objective}</p>}
      {manifest.capability && (
        <p className="font-mono text-xs text-muted-foreground">
          capability: {manifest.capability}
          {manifest.operations && manifest.operations.length > 0 && ` · operations: ${manifest.operations.join(", ")}`}
        </p>
      )}

      <div className="flex flex-col gap-1.5">
        {Object.entries(data.files).map(([name, content]) => (
          <div key={name}>
            <button
              type="button"
              onClick={() => setExpandedFile(expandedFile === name ? null : name)}
              className="flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              {expandedFile === name ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
              {name}
            </button>
            {expandedFile === name && (
              <pre className="mt-1.5 max-h-64 overflow-auto rounded-md bg-muted p-3 font-mono text-xs [overflow-wrap:anywhere]">
                {content}
              </pre>
            )}
          </div>
        ))}
      </div>

      {!data.test.passed && (data.test.stdout || data.test.stderr) && (
        <div>
          <p className="text-xs font-medium text-destructive">Test output</p>
          <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-destructive/5 p-3 font-mono text-xs text-destructive [overflow-wrap:anywhere]">
            {data.test.stdout}
            {data.test.stderr}
          </pre>
        </div>
      )}
    </div>
  )
}
