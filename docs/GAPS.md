# Known gaps

This is the current list of product and engineering limitations. Completed
plans and review snapshots remain available in Git history, not in the current
operator documentation.

## Security boundaries

- **Indirect prompt injection remains possible.** Tool results, web pages,
  documents, and MCP output are untrusted. Runtime capability policy,
  allowlists, workspace boundaries, and one-shot approvals limit impact.
  `ToolDefinition.operation_content_trust` labels which operations return
  content this machine does not control (`browser.open`, `browser.control`'s
  page-reading operations, `http.request`, `web.search`, `mcp.client`'s
  `call_tool`, `document.manage`'s read operations), and that label now
  reaches the Operator prompt itself: `worker.py` stamps `content_trust`
  onto the history entry it records, and `_format_history` wraps such an
  entry's output in an explicit `[UNTRUSTED CONTENT ...]` / `[END UNTRUSTED
  CONTENT]` delimiter pair, telling the model to read it as data and not
  follow anything it says. The content is neutralized first against a
  payload containing a literal copy of either marker, so it cannot forge a
  fake boundary and make the model think the fence closed early.
  This is a textual mitigation, not a guarantee: it makes the boundary
  visible and explicit to a model that reads instructions, but nothing
  mechanical stops a sufficiently persuasive payload from working anyway -
  the deterministic scenario suite covers the fence's own mechanics (it
  fences the right entries, preserves the content, resists marker-spoofing)
  but cannot prove a live model actually declines a given injection
  attempt; that remains a live-model question, same as elsewhere in this
  document. Re-recording the 2 of 16 scenario fixtures the prompt-text
  change affected turned out not to need a live LLM after all: since the
  fence only wraps existing content without altering the underlying
  decision, a migration reversed the fence transformation on the newly
  requested prompt to find the corresponding old-keyed fixture entry and
  copied its recorded response forward under the new key - see
  [THREAT_MODEL.md](THREAT_MODEL.md).
- **Redaction is pattern-based.** Known secret fields, configured secret
  values, and provider-token shapes are redacted. A novel high-entropy secret
  with no recognizable name or prefix may pass through. Entropy scanning has
  not been enabled because it would also hide hashes, UUIDs, and generated
  identifiers in user-facing results.
- **YBM is single-operator software.** It is not a hardened multi-tenant or
  Internet-facing control plane. Keep the backend and preview servers on
  loopback and use an admin token when the bind host is broader.

## Behavior still needing live validation

- The Auditor checks grounding and objective completion, but it does not
  reliably challenge an implausible value such as a zero total for a non-empty
  expense file. Improving that prompt requires a reviewed live fixture
  re-record.
- The built-in starter suggestions have deterministic UI and worker coverage,
  but the current wording has not been exercised against a configured live
  model profile.
- Voice failure paths and transcription APIs are tested with simulated audio
  and adapters. A real microphone recording and Telegram voice note have not
  been transcribed end to end in the release environment.
- WhatsApp's sidecar imports and health behavior are checked, but live QR
  pairing and send/receive need a real account. WhatsApp remains text-only:
  there are no buttons, voice messages, or artifact delivery.
- The credentialed live E2E suite is intentionally separate from deterministic
  CI and has not been run as part of this pre-public pass.

## Product limitations

- Desktop observation/control and computer-use actions are Windows-only.
- GitHub Copilot Chat panel responses cannot be captured directly through the
  VS Code API; the bridge and CLI-based coding-agent flow are supported.
- The Windows PowerShell supervisor and cross-platform `ybm` supervisor are
  separate implementations.
- ~~`mcp.client` supports configured servers, but the console does not yet
  offer a full add/edit/test form for MCP server definitions~~ - shipped
  (Settings' MCP servers card: add/edit/test/remove, env write-only and
  never echoed back). A pending `adapter.factory` `promote_after_approval`
  approval now also shows the actual generated source and sandbox test
  result, not just `adapter_dir`/`approved` - approving it used to be a
  decision made from the tool name alone.

## Maintainability

- `orchestration/worker.py`, `admin.py`, and the frontend API client remain
  large coordination modules. Shared path/text helpers have been extracted,
  but decomposing these files should be incremental and contract-tested rather
  than a release-blocking rewrite.

## Release validation

- CI now installs the MSI on a clean Windows runner, provisions the packaged
  runtime, checks backend health, stops it, and uninstalls it. The final visual
  interaction with the MSI dialogs and first-run browser wizard still needs a
  human clean-machine pass on Windows, macOS, and Linux before onboarding is
  called stable.
