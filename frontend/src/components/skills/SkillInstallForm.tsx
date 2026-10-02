import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Card, CardContent } from "@/components/ui/card"
import { ApiError } from "@/lib/api"
import { useInstallSkill } from "@/lib/queries"

/**
 * Install a skill straight from the console (docs/archive/UI_UX_AUDIT.md Phase 5) -
 * no more "find adapters.skills.root_dir and hand-write YAML frontmatter".
 * Declaring tools here is optional; when left blank the backend infers
 * which tools the instructions reference by scanning the body against the
 * real tool registry - informational only, not an enforced permission.
 */
export function SkillInstallForm({ onDone, bare }: { onDone: () => void; bare?: boolean }) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [body, setBody] = useState("")
  const [tools, setTools] = useState("")
  const install = useInstallSkill()

  const valid = name.trim() && description.trim() && body.trim()

  function handleInstall() {
    if (!valid) return
    install.mutate(
      {
        name: name.trim(),
        description: description.trim(),
        body: body.trim(),
        tools: tools
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      },
      {
        onSuccess: () => {
          toast.success(`Installed ${name.trim()}.`)
          onDone()
        },
        onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not install the skill."),
      },
    )
  }

  const fields = (
    <>
      <div className="grid gap-2 sm:grid-cols-2">
        <Input
          aria-label="Skill name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Name (e.g. Invoice Extraction)"
          maxLength={80}
          autoFocus
        />
        <Input
          aria-label="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="One-line description"
          maxLength={400}
        />
      </div>
      <Textarea
        aria-label="Instructions"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Full instructions - what should YBM do when this skill is relevant?"
        rows={6}
      />
      <Input
        aria-label="Tools used"
        value={tools}
        onChange={(e) => setTools(e.target.value)}
        placeholder="Tools it uses, comma-separated (optional - inferred if left blank)"
        className="font-mono text-xs"
      />
      <div className="flex items-center gap-2 self-end">
        <Button variant="ghost" size="sm" onClick={onDone} disabled={install.isPending}>
          Cancel
        </Button>
        <Button size="sm" onClick={handleInstall} disabled={install.isPending || !valid}>
          Install
        </Button>
      </div>
    </>
  )

  if (bare) {
    return <div className="flex flex-col gap-2">{fields}</div>
  }

  return (
    <Card className="border-primary/40 shadow-sm ring-1 ring-primary/10">
      <CardContent className="flex flex-col gap-2">{fields}</CardContent>
    </Card>
  )
}
