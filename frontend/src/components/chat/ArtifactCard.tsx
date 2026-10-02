import { useState } from "react"
import { toast } from "sonner"
import { Check, Copy, Download, ExternalLink, File, FileJson, FileText, Image, Mic } from "lucide-react"
import { Button } from "@/components/ui/button"
import { createArtifactDownloadGrant, type Artifact } from "@/lib/api"

const TYPE_ICON: Record<string, typeof File> = {
  text_log: FileText,
  json: FileJson,
  screenshot: Image,
  voice: Mic,
  transcript: FileText,
  generated_file: File,
  document: FileText,
  external_link: ExternalLink,
}

const TYPE_LABEL: Record<string, string> = {
  text_log: "Log",
  json: "JSON",
  screenshot: "Screenshot",
  voice: "Voice",
  transcript: "Transcript",
  generated_file: "Generated file",
  document: "Document",
  external_link: "Link",
}

function basename(uri: string): string {
  const cleaned = uri.split(/[?#]/)[0]
  const parts = cleaned.split(/[/\\]/)
  return parts[parts.length - 1] || uri
}

/**
 * A file/output a task produced (docs/archive/UI_UX_AUDIT.md Phase 1). `uri` is a
 * local filesystem path for most artifact types - Phase 8 added a real
 * download endpoint, so a local artifact now gets Open / Download / Copy
 * path instead of only showing its path as inert text. ("Show in folder"
 * was also requested but deliberately deferred: it would mean a new
 * backend endpoint that launches Explorer from a stored path, which is a
 * real desktop-control-shaped capability this pass didn't scope a policy
 * for - not something to add as a side effect of a download button.)
 */
export function ArtifactCard({ artifact }: { artifact: Artifact }) {
  const Icon = TYPE_ICON[artifact.type] ?? File
  const label = TYPE_LABEL[artifact.type] ?? artifact.type
  const isWebLink = artifact.uri != null && /^https?:\/\//.test(artifact.uri)
  const isLocalFile = artifact.uri != null && !isWebLink
  const name = artifact.uri ? basename(artifact.uri) : label

  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-border bg-muted/40 px-3 py-2">
      <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md bg-background text-muted-foreground">
        <Icon className="size-3.5" />
      </span>
      <div className="min-w-0 flex-1">
        {isWebLink ? (
          <a
            href={artifact.uri!}
            target="_blank"
            rel="noopener noreferrer"
            className="block truncate text-xs font-medium text-primary underline underline-offset-2 hover:no-underline"
          >
            {name}
          </a>
        ) : (
          <p className="truncate text-xs font-medium" title={artifact.uri ?? undefined}>
            {name}
          </p>
        )}
        <p className="text-[11px] text-muted-foreground">
          {label}
          {artifact.content_preview && ` · ${artifact.content_preview}`}
        </p>
        {isLocalFile && <ArtifactActions artifact={artifact} />}
      </div>
    </div>
  )
}

function ArtifactActions({ artifact }: { artifact: Artifact }) {
  const [copied, setCopied] = useState(false)
  const [loadingAction, setLoadingAction] = useState<"open" | "download" | null>(null)

  async function copyPath() {
    if (!artifact.uri) return
    try {
      await navigator.clipboard.writeText(artifact.uri)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error("Could not copy the path - your browser blocked clipboard access.")
    }
  }

  async function openArtifact() {
    const preview = window.open("", "_blank")
    if (preview) preview.opener = null
    setLoadingAction("open")
    try {
      const { url } = await createArtifactDownloadGrant(artifact.id, { inline: true })
      if (preview) {
        preview.location.replace(url)
      } else {
        window.location.assign(url)
      }
    } catch {
      preview?.close()
      toast.error("Could not open this artifact.")
    } finally {
      setLoadingAction(null)
    }
  }

  async function downloadArtifact() {
    setLoadingAction("download")
    try {
      const { url } = await createArtifactDownloadGrant(artifact.id)
      const link = document.createElement("a")
      link.href = url
      link.download = artifact.uri ? basename(artifact.uri) : "artifact"
      document.body.appendChild(link)
      link.click()
      link.remove()
    } catch {
      toast.error("Could not download this artifact.")
    } finally {
      setLoadingAction(null)
    }
  }

  return (
    <div className="mt-1 flex items-center gap-2">
      <Button
        variant="ghost"
        size="sm"
        className="h-5 gap-1 px-1.5 text-[11px] text-muted-foreground hover:text-foreground"
        onClick={openArtifact}
        disabled={loadingAction !== null}
      >
        <ExternalLink className="size-3" />
        {loadingAction === "open" ? "Opening..." : "Open"}
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="h-5 gap-1 px-1.5 text-[11px] text-muted-foreground hover:text-foreground"
        onClick={downloadArtifact}
        disabled={loadingAction !== null}
      >
        <Download className="size-3" />
        {loadingAction === "download" ? "Downloading..." : "Download"}
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="h-5 gap-1 px-1.5 text-[11px] text-muted-foreground hover:text-foreground"
        onClick={copyPath}
      >
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
        {copied ? "Copied" : "Copy path"}
      </Button>
    </div>
  )
}
