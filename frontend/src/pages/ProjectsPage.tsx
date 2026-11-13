import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"
import { FolderKanban, Pencil, Plus, Trash2 } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { ConfirmDialog } from "@/components/access/ConfirmDialog"
import { PageHeader } from "@/components/layout/PageHeader"
import { ApiError, type Project } from "@/lib/api"
import { useCreateProject, useDeleteProject, useProjects, useUpdateProject } from "@/lib/queries"

export function ProjectsPage() {
  const { data, isPending, error } = useProjects()
  const create = useCreateProject()
  const update = useUpdateProject()
  const remove = useDeleteProject()
  const [editing, setEditing] = useState<Project | null>(null)
  const [deleting, setDeleting] = useState<Project | null>(null)
  const [name, setName] = useState("")
  const [instructions, setInstructions] = useState("")
  const [roots, setRoots] = useState("")
  const [criteria, setCriteria] = useState("")
  const saving = create.isPending || update.isPending

  function edit(project: Project | null) {
    setEditing(project)
    setName(project?.name ?? "")
    setInstructions(project?.instructions ?? "")
    setRoots(project?.root_paths.join("\n") ?? "")
    setCriteria(project?.criteria.join("\n") ?? "")
  }

  function save() {
    if (!name.trim() || saving) return
    const input = {
      name: name.trim(), instructions: instructions.trim(),
      root_paths: roots.split("\n").map((value) => value.trim()).filter(Boolean),
      criteria: criteria.split("\n").map((value) => value.trim()).filter(Boolean),
    }
    const callbacks = {
      onSuccess: () => {
        toast.success(editing ? "Project updated. New tasks will use these instructions." : "Project created.")
        edit(null)
      },
      onError: (err: Error) => toast.error(err instanceof ApiError ? err.message : "Could not save project."),
    }
    if (editing) update.mutate({ projectId: editing.id, input }, callbacks)
    else create.mutate(input, callbacks)
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
        <PageHeader eyebrow="Persistent context" title="Projects" description="Keep folders, instructions, corrections, and success criteria ready for your next task." />
        {error && <Alert variant="destructive"><AlertTitle>Could not load projects</AlertTitle><AlertDescription>{error.message}</AlertDescription></Alert>}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{editing ? `Edit ${editing.name}` : "New project"}</CardTitle>
            <CardDescription>One folder or criterion per line. Folder access is managed separately in Access.</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); save() }}>
              <Label htmlFor="project-name">Project name</Label>
              <Input id="project-name" value={name} maxLength={120} required onChange={(event) => setName(event.target.value)} />
              <Label htmlFor="project-instructions">Instructions and corrections</Label>
              <Textarea id="project-instructions" value={instructions} maxLength={8000} onChange={(event) => setInstructions(event.target.value)} placeholder="What should YBM optimize for? What should it do differently next time?" />
              <Label htmlFor="project-roots">Project folders</Label>
              <Textarea id="project-roots" value={roots} onChange={(event) => setRoots(event.target.value)} placeholder="One absolute folder path per line" />
              <Label htmlFor="project-criteria">Success criteria</Label>
              <Textarea id="project-criteria" value={criteria} onChange={(event) => setCriteria(event.target.value)} placeholder="One criterion per line" />
              <div className="flex gap-2">
                <Button type="submit" disabled={!name.trim() || saving}><Plus className="size-4" />{editing ? "Save changes" : "Create project"}</Button>
                {editing && <Button type="button" variant="outline" disabled={saving} onClick={() => edit(null)}>Cancel edit</Button>}
              </div>
            </form>
          </CardContent>
        </Card>
        {isPending && <p role="status">Loading projects...</p>}
        {!isPending && data?.projects.length === 0 && <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed py-10 text-sm text-muted-foreground"><FolderKanban className="size-6" />No projects yet.</div>}
        <div className="grid gap-3">
          {data?.projects.map((project) => (
            <Card key={project.id}>
              <CardHeader>
                <CardTitle className="text-base">{project.name}</CardTitle>
                <CardDescription className="whitespace-pre-wrap [overflow-wrap:anywhere]">{project.instructions || "No instructions yet."}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-3 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                {project.root_paths.length > 0 && <p><span className="font-medium text-foreground">Folders: </span>{project.root_paths.join("; ")}</p>}
                {project.criteria.length > 0 && <p><span className="font-medium text-foreground">Criteria: </span>{project.criteria.join("; ")}</p>}
                <div className="flex flex-wrap items-center gap-3">
                  <Link to={`/?project=${encodeURIComponent(project.id)}`} className="text-sm font-medium text-primary underline">Open project chat and history</Link>
                  <Button variant="outline" size="sm" disabled={saving} onClick={() => edit(project)}><Pencil className="size-3.5" />Edit {project.name}</Button>
                  <Button variant="ghost" size="icon" aria-label={`Delete ${project.name}`} onClick={() => setDeleting(project)}><Trash2 className="size-4" /></Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
      <ConfirmDialog
        open={deleting != null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Delete "${deleting?.name}"?`}
        description="Removes the saved project. Existing tasks and schedules keep their recorded context and remain in Tasks and Workflows."
        confirmLabel="Delete"
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return
          remove.mutate(deleting.id, {
            onSuccess: () => { if (editing?.id === deleting.id) edit(null); setDeleting(null) },
            onError: (err) => toast.error(err instanceof ApiError ? err.message : "Could not delete project."),
          })
        }}
      />
    </div>
  )
}
