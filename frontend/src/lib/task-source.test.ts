import { describe, expect, it } from "vitest"
import { taskSource } from "@/lib/task-source"

describe("taskSource", () => {
  it("keeps the person's own words when the concierge reworded them", () => {
    const source = taskSource({
      objective: "Navigate to http://x/signup and create an account",
      metadata: { original_message_text: "Go to http://x/signup and make me an account", source_channel: "telegram" },
    })
    expect(source.words).toBe("Go to http://x/signup and make me an account")
    expect(source.channel).toBe("Telegram")
    expect(source.voice).toBe(false)
  })

  it("adds nothing when the objective is the person's own words, apart from spacing and case", () => {
    expect(taskSource({ objective: "How many   files?", metadata: { original_message_text: "how many files?" } }).words).toBeNull()
  })

  it("marks a task that started as a voice note", () => {
    expect(taskSource({ objective: "x", metadata: { voice_file_id: "AwACAgQ", source_channel: "telegram" } }).voice).toBe(true)
    expect(taskSource({ objective: "x", metadata: { voice_file_id: "" } }).voice).toBe(false)
  })

  it("copes with no metadata and with channels it does not know", () => {
    expect(taskSource({ objective: "x", metadata: {} })).toEqual({ words: null, channel: null, voice: false })
    expect(taskSource({ objective: "x", metadata: { source_channel: "signal" } }).channel).toBe("signal")
  })
})
