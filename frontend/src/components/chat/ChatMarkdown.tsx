import { useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import rehypeSanitize from "rehype-sanitize"
import { Check, Copy } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * Renders an assistant answer as sanitized Markdown (docs/archive/UI_UX_AUDIT.md
 * Phase 1 "safe Markdown" item). rehype-sanitize's default schema strips
 * script tags, event handlers, and javascript: URIs - answer text can
 * embed tool output (file contents, command stdout, web page text), which
 * is untrusted with respect to this rendering surface even though it's
 * "our own" data by the time it reaches the browser.
 *
 * Plain text (no Markdown syntax) renders identically to before: a single
 * paragraph, same font/size/wrapping - this isn't a visual reset for the
 * common case, just an upgrade for the cases that have code/tables/links.
 */
export function ChatMarkdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn("chat-markdown", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={MARKDOWN_COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  )
}

const MARKDOWN_COMPONENTS = {
  p: ({ children }: { children?: React.ReactNode }) => (
    <p className="whitespace-pre-wrap [overflow-wrap:anywhere] first:mt-0 last:mb-0 mb-2">{children}</p>
  ),
  a: ({ href, children }: { href?: string; children?: React.ReactNode }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary underline underline-offset-2 hover:no-underline [overflow-wrap:anywhere]"
    >
      {children}
    </a>
  ),
  ul: ({ children }: { children?: React.ReactNode }) => (
    <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>
  ),
  ol: ({ children }: { children?: React.ReactNode }) => (
    <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>
  ),
  li: ({ children }: { children?: React.ReactNode }) => <li className="[overflow-wrap:anywhere]">{children}</li>,
  h1: ({ children }: { children?: React.ReactNode }) => <h3 className="mb-1.5 mt-3 text-base font-semibold first:mt-0">{children}</h3>,
  h2: ({ children }: { children?: React.ReactNode }) => <h3 className="mb-1.5 mt-3 text-base font-semibold first:mt-0">{children}</h3>,
  h3: ({ children }: { children?: React.ReactNode }) => <h4 className="mb-1 mt-2.5 text-sm font-semibold first:mt-0">{children}</h4>,
  blockquote: ({ children }: { children?: React.ReactNode }) => (
    <blockquote className="my-2 border-l-2 border-border pl-3 text-muted-foreground">{children}</blockquote>
  ),
  table: ({ children }: { children?: React.ReactNode }) => (
    <div className="my-2 max-w-full overflow-x-auto rounded-md border border-border">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  thead: ({ children }: { children?: React.ReactNode }) => <thead className="bg-muted/60">{children}</thead>,
  th: ({ children }: { children?: React.ReactNode }) => (
    <th className="border-b border-border px-2.5 py-1.5 text-left font-medium">{children}</th>
  ),
  td: ({ children }: { children?: React.ReactNode }) => (
    <td className="border-b border-border/60 px-2.5 py-1.5 align-top [overflow-wrap:anywhere]">{children}</td>
  ),
  hr: () => <hr className="my-3 border-border" />,
  code: ({ className, children }: { className?: string; children?: React.ReactNode }) => {
    const isBlock = /language-/.test(className || "")
    if (!isBlock) {
      return (
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] [overflow-wrap:anywhere]">
          {children}
        </code>
      )
    }
    return <code className={className}>{children}</code>
  },
  pre: ({ children }: { children?: React.ReactNode }) => <CodeBlock>{children}</CodeBlock>,
}

function CodeBlock({ children }: { children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false)

  function handleCopy(event: React.MouseEvent<HTMLButtonElement>) {
    const pre = event.currentTarget.closest("div")?.querySelector("pre")
    const text = pre?.textContent ?? ""
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div className="group relative my-2">
      <pre className="max-w-full overflow-x-auto rounded-lg bg-muted p-3 font-mono text-xs leading-5">{children}</pre>
      <button
        type="button"
        onClick={handleCopy}
        aria-label="Copy code"
        className="absolute right-2 top-2 flex size-6 items-center justify-center rounded-md border border-border bg-card text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100 focus-visible:opacity-100 group-focus-within:opacity-100"
      >
        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </button>
    </div>
  )
}
