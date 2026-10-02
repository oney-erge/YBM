import { Link } from "react-router"
import { Cpu } from "lucide-react"
import { type ActiveModel } from "@/lib/api"

/**
 * Says which model is answering, in the chat header.
 *
 * First run now picks a model without asking - your own LocalDeploy, a local
 * Ollama, or a provider key already in your environment - so the console owes
 * you a plain statement of what it picked, in the place you are looking. When
 * the pick came from an API key in your environment (which can bill you), the
 * chip names the variable it read, never its value, and links to where to change
 * it. Renders nothing until the backend reports a model, so an older backend
 * just looks like it always did.
 */
export function ModelChip({ model }: { model: ActiveModel | undefined }) {
  if (!model?.configured || !model.model) return null

  const where = model.local ? "on this computer" : model.provider
  const detail = model.key_env
    ? `${model.provider} (${model.model}), using ${model.key_env} from your environment. Change it in Settings.`
    : `${model.provider} (${model.model}). Change it in Settings.`

  return (
    <Link
      to="/settings"
      title={detail}
      aria-label={`Model: ${model.model}. ${detail}`}
      className="flex max-w-[16rem] items-center gap-1.5 rounded-full bg-muted px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
    >
      <Cpu className="size-3.5 shrink-0" />
      <span className="truncate">
        {model.model}
        {where ? <span className="font-normal"> · {where}</span> : null}
      </span>
    </Link>
  )
}
