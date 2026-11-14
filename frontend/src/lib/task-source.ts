/**
 * Where a task came from and what the person actually said, for the task list.
 *
 * `task.objective` is what the concierge made of the request ("Navigate to http://..., create an account using ..."), which can drop
 * or reword what was asked; the person's own words are kept in `metadata.original_message_text`, and the channel and whether it was a
 * voice note are in the metadata too.
 */
export interface TaskSource {
  /** The person's own words, when they differ from the objective; otherwise null (nothing to add). */
  words: string | null
  /** "Telegram", "Web chat", ... or null when unknown. */
  channel: string | null
  voice: boolean
}

const CHANNEL_NAMES: Record<string, string> = { telegram: "Telegram", web: "Web chat", whatsapp: "WhatsApp" }

export function taskSource(task: { objective: string; metadata: Record<string, unknown> }): TaskSource {
  const said = typeof task.metadata.original_message_text === "string" ? task.metadata.original_message_text.trim() : ""
  const channel = typeof task.metadata.source_channel === "string" ? task.metadata.source_channel : ""
  const sameWords = said.replace(/\s+/g, " ").toLowerCase() === task.objective.replace(/\s+/g, " ").trim().toLowerCase()
  return {
    words: said && !sameWords ? said : null,
    channel: channel ? (CHANNEL_NAMES[channel] ?? channel) : null,
    voice: typeof task.metadata.voice_file_id === "string" && task.metadata.voice_file_id.length > 0,
  }
}
