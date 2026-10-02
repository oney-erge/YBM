import { useState } from "react"
import { ChevronRight, Folder, FolderOpen, Home } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogClose,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { useFolders } from "@/lib/queries"

/**
 * Server-side folder browsing for the chat composer (docs/archive/UI_UX_AUDIT.md
 * Phase 13) - a browser directory picker cannot yield a usable absolute
 * path, which is why this was deferred back in Phase 1. Selecting a
 * folder inserts its absolute path into the draft text; there is no new
 * "folder reference" concept, it's the same plain path
 * filesystem.manage's own alias resolution already understands.
 */
export function FolderPicker({ onSelect }: { onSelect: (path: string) => void }) {
  const [open, setOpen] = useState(false)
  const [path, setPath] = useState<string | undefined>(undefined)
  const { data, isPending, isError, error } = useFolders(path, open)

  function handleOpenChange(next: boolean) {
    setOpen(next)
    if (!next) setPath(undefined)
  }

  function handleSelect(folderPath: string) {
    onSelect(folderPath)
    handleOpenChange(false)
  }

  const currentName = data?.current_path ? data.current_path.split(/[/\\]/).filter(Boolean).pop() : undefined

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-9 shrink-0 rounded-xl text-muted-foreground"
        aria-label="Attach a folder"
        onClick={() => handleOpenChange(true)}
      >
        <FolderOpen className="size-4" />
      </Button>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Attach a folder</DialogTitle>
            <DialogDescription>
              Browse folders YBM is allowed to work in. Selecting one inserts its path into your
              message.
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-2">
            {data?.current_path && (
              <div className="flex items-center gap-2 rounded-lg bg-muted/60 px-2.5 py-1.5 text-xs">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Back to allowed folders"
                  onClick={() => setPath(undefined)}
                >
                  <Home className="size-3.5" />
                </Button>
                <span className="min-w-0 flex-1 truncate font-mono" title={data.current_path}>
                  {data.current_path}
                </span>
                {data.parent_path !== null && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-6 shrink-0 px-2 text-[11px]"
                    onClick={() => setPath(data.parent_path ?? undefined)}
                  >
                    Up
                  </Button>
                )}
              </div>
            )}

            {isPending && (
              <div className="flex flex-col gap-1.5">
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
              </div>
            )}

            {isError && (
              <Alert variant="destructive">
                <AlertTitle>Couldn&apos;t browse folders</AlertTitle>
                <AlertDescription>{error?.message ?? "Unknown error"}</AlertDescription>
              </Alert>
            )}

            {!isPending && !isError && data && data.roots.length === 0 && (
              <p className="px-1 text-sm text-muted-foreground">
                No folders are configured for YBM to work in. Set{" "}
                <code className="font-mono text-xs">adapters.computer_use.allowed_roots</code> in
                config.yaml to enable this.
              </p>
            )}

            {!isPending && !isError && data && data.roots.length > 0 && data.entries.length === 0 && (
              <p className="px-1 text-sm text-muted-foreground">This folder has no subfolders.</p>
            )}

            {!isPending && !isError && data && data.entries.length > 0 && (
              <div className="flex max-h-72 flex-col gap-0.5 overflow-y-auto">
                {data.entries.map((entry) => (
                  <div
                    key={entry.path}
                    className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 hover:bg-muted/60"
                  >
                    <button
                      type="button"
                      className="flex min-w-0 flex-1 items-center gap-2 text-left text-sm"
                      onClick={() => setPath(entry.path)}
                    >
                      <Folder className="size-4 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 truncate">{entry.name}</span>
                      <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
                    </button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-6 shrink-0 px-2 text-[11px]"
                      onClick={() => handleSelect(entry.path)}
                    >
                      Select
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <DialogFooter>
            <DialogClose render={<Button variant="ghost" />}>Cancel</DialogClose>
            {data?.current_path && (
              <Button type="button" onClick={() => handleSelect(data.current_path!)}>
                Select {currentName}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
