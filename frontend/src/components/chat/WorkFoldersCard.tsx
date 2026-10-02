import { useState } from "react"
import { toast } from "sonner"
import { FolderCheck, FolderOpen, ShieldCheck } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ApiError, type SetupFolders } from "@/lib/api"
import { useSaveWorkFolders, useSetupFolders } from "@/lib/queries"
import { cn } from "@/lib/utils"

type Mode = "write_access" | "read_only"

const MODE_OPTIONS: { value: Mode; title: string; detail: string }[] = [
  {
    value: "write_access",
    title: "Ask before changing anything",
    detail: "YBM can look at everything here and shows you each move or edit first. Recommended.",
  },
  {
    value: "read_only",
    title: "Look only",
    detail: "YBM can read and search these folders but never change them.",
  },
]

/**
 * "Which folders may YBM work in?" - the step a first task needs and the first
 * run never asked.
 *
 * File access is off on a fresh install (the right default), and the only folder
 * the agent could touch was its own scratch workspace, so the headline request -
 * "organize my Downloads" - ended in a blocked task. Granting access meant finding
 * the Access page, choosing a mode, and separately editing a list of allowed
 * folders in a config file. This does all of that in one action, at the moment the
 * person is looking at the thing that needs it.
 *
 * Only the two gentle modes are offered. Full autonomy is a deliberate choice on
 * the Access page, not something a convenience shortcut should hand out.
 */
export function WorkFoldersCard({
  onDone,
  compact = false,
}: {
  /** Called after access is granted, e.g. to retry the request that was blocked. */
  onDone?: (state: SetupFolders) => void
  compact?: boolean
}) {
  const { data, isPending } = useSetupFolders()
  const save = useSaveWorkFolders()
  const [picked, setPicked] = useState<string[] | null>(null)
  const [custom, setCustom] = useState("")
  const [mode, setMode] = useState<Mode>("write_access")
  const [error, setError] = useState<string | null>(null)

  if (isPending || !data) return null

  // Downloads is the folder the headline task names, so it starts ticked.
  const suggestedPaths = data.suggested.map((folder) => folder.path)
  const defaultPick = data.suggested.filter((folder) => folder.name === "Downloads").map((folder) => folder.path)
  const chosen = picked ?? defaultPick
  const customTrimmed = custom.trim()
  const folders = customTrimmed ? [...chosen, customTrimmed] : chosen
  const alreadyGranted = data.file_access !== "off" && data.work_folders.length > 0

  function toggle(path: string) {
    setPicked((current) => {
      const base = current ?? defaultPick
      return base.includes(path) ? base.filter((item) => item !== path) : [...base, path]
    })
  }

  function handleAllow() {
    setError(null)
    save.mutate(
      { folders, mode },
      {
        onSuccess: (state) => {
          const names = state.work_folders.map((path) => path.split(/[\\/]/).filter(Boolean).pop() ?? path)
          toast.success(
            mode === "write_access"
              ? `YBM can now work in ${names.join(", ")}. It will ask before changing anything.`
              : `YBM can now read ${names.join(", ")}.`,
          )
          // Back to the untouched state, so once the server reports the grant the
          // card can go away (it stays only while someone is still choosing).
          setPicked(null)
          setCustom("")
          onDone?.(state)
        },
        onError: (err) => {
          setError(err instanceof ApiError ? err.message : "Could not save that. Try again.")
        },
      },
    )
  }

  // Nothing left to ask once folders are granted: the full card disappears from
  // the empty chat, and the compact form (used inside other cards) says so.
  if (alreadyGranted && !picked && !custom) {
    if (!compact) return null
    return (
      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        <FolderCheck className="size-3.5 text-success" />
        YBM can work in {data.work_folders.length} folder{data.work_folders.length === 1 ? "" : "s"}.
      </p>
    )
  }

  return (
    <section
      aria-label="Choose folders YBM may work in"
      className={cn(
        "w-full rounded-2xl border border-border bg-card p-4 text-left shadow-sm",
        compact ? "" : "mt-6",
      )}
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
          <FolderOpen className="size-4" />
        </span>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">Where may YBM work?</h3>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
            YBM only touches folders you choose here. Nothing outside them is reachable, and you can change this any time
            on the Access page.
          </p>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label="Folders">
        {data.suggested.map((folder) => {
          const on = chosen.includes(folder.path)
          return (
            <button
              key={folder.path}
              type="button"
              aria-pressed={on}
              title={folder.path}
              onClick={() => toggle(folder.path)}
              className={cn(
                "rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
                on
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-background text-muted-foreground hover:text-foreground",
              )}
            >
              {folder.name}
            </button>
          )
        })}
        {data.suggested.length === 0 && (
          <p className="text-xs text-muted-foreground">
            No standard folders were found. Type the full path of one below.
          </p>
        )}
      </div>

      <div className="mt-3">
        <label htmlFor="work-folder-custom" className="text-xs text-muted-foreground">
          Or another folder (full path)
        </label>
        <Input
          id="work-folder-custom"
          value={custom}
          onChange={(event) => {
            setCustom(event.target.value)
            setError(null)
          }}
          placeholder={suggestedPaths[0] ?? "C:\\Users\\you\\Projects"}
          className="mt-1 h-8 text-xs"
        />
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="How careful YBM should be">
        {MODE_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={mode === option.value}
            onClick={() => setMode(option.value)}
            className={cn(
              "rounded-xl border p-3 text-left transition-colors",
              mode === option.value ? "border-primary bg-primary/5" : "border-border hover:border-foreground/30",
            )}
          >
            <span className="flex items-center gap-1.5 text-xs font-medium">
              {option.value === "write_access" && <ShieldCheck className="size-3.5 text-success" />}
              {option.title}
            </span>
            <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">{option.detail}</span>
          </button>
        ))}
      </div>

      {error && (
        <p role="alert" className="mt-3 text-xs text-destructive">
          {error}
        </p>
      )}

      <div className="mt-3 flex items-center justify-between gap-2">
        <p className="text-[11px] text-muted-foreground">
          {folders.length === 0 ? "Choose at least one folder." : `${folders.length} folder${folders.length === 1 ? "" : "s"} selected.`}
        </p>
        <Button type="button" size="sm" disabled={folders.length === 0 || save.isPending} onClick={handleAllow}>
          {save.isPending ? "Saving..." : "Allow"}
        </Button>
      </div>
    </section>
  )
}
