/**
 * Deliberately conservative: describes what a capability *is* (a factual
 * name→description lookup), never asserts whether a specific action is
 * "reversible" - claiming that with false confidence in a security UI is
 * worse than not claiming it at all (docs/UI_REWRITE_PLAN.md §11.2's
 * "Reversibility" field is scoped down to this on purpose). Unlisted
 * capabilities fall back to the raw name, not a guess.
 *
 * Keys are verified against agent_control.schemas.Capability's actual
 * string values, not guessed - keep in sync if that enum changes.
 */
const CAPABILITY_DESCRIPTIONS: Record<string, string> = {
  "agent.core": "can use YBM's built-in helpers (task status, skills, persona, notes search)",
  "telegram.receive": "can read incoming Telegram messages",
  "telegram.send": "can send Telegram messages",
  "llm.generate": "can call the configured LLM",
  "stt.transcribe": "can transcribe audio",
  "tts.synthesize": "can generate speech audio",
  "vscode.read_state": "can read VS Code editor/workspace state",
  "vscode.write_files": "can write files or run commands via the VS Code bridge",
  "terminal.run": "can execute commands or code on this machine",
  "filesystem.read": "can read files",
  "filesystem.write": "can create, modify, or delete files",
  "desktop.screenshot": "can capture screenshots",
  "desktop.control": "can control the mouse and keyboard",
  "browser.open": "can open and read web pages",
  "browser.control": "can control a browser session (click, fill forms, navigate)",
  "network.http": "can make outbound network requests",
  "schedule.manage": "can create or modify recurring scheduled tasks",
  "github.read": "can read from configured GitHub repositories",
  "github.push": "can push commits to configured GitHub repositories",
  "dependencies.install": "can install software packages",
}

export function describeCapability(capability: string): string {
  return CAPABILITY_DESCRIPTIONS[capability] ?? capability
}
