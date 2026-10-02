# Threat Model

YBM is a **single-operator, local** agent-control system. The supported deployment is one trusted
operator, loopback-bound services, and an OS account that protects local config and runtime data.
Internet exposure, untrusted local users, and multi-tenancy are out of scope.

This describes what's implemented. It is not a claim that model output or external content can be
made trustworthy.

## Trust boundary

The operator and local configuration are trusted. Everything crossing the dashed line is
**untrusted data, even when it looks like an instruction.**

```mermaid
flowchart TB
    subgraph trusted["Trusted"]
        OP["Operator - you"]
        CFG["config.yaml · .env · policy"]
    end
    subgraph enforce["Deterministic enforcement"]
        POL["Capability policy<br/>risk ceilings · scopes · allowlists"]
        APR["Approvals<br/>exact · expiring · one-shot"]
    end
    subgraph untrusted["Untrusted input"]
        LLM["LLM responses"]
        WEB["Web pages · HTTP · MCP results"]
        DOC["Files · documents · tool output"]
        MSG["Messages from non-allowlisted identities"]
        GEN["Generated code and adapters"]
        MEM["Memory derived from any of the above"]
    end
    OP --> APR
    CFG --> POL
    untrusted -.->|"cannot grant authority"| POL
    POL --> APR
    APR --> ACT["Tool execution"]
```

An enabled tool is not a trusted tool. Its result may be malicious, stale, malformed, or
controlled by someone else.

## Security invariants

Enforcement is deterministic - the model participates in none of it.

1. The **runtime tool definition** owns each tool's capability and minimum operation risk. A model
   cannot substitute a lower-risk capability or understate an operation's risk.
2. A capability must be **enabled**, in **scope**, and at or below its **risk ceiling**.
3. The **global approval floor** stays authoritative even when a capability's `requires_approval`
   is false.
4. Persistent and critical operations require approval **independently of any access-mode preset**
   - MCP server installation, generated-adapter promotion, active desktop/browser control,
   generated or unsandboxed code execution, and HTTP requests carrying vault secrets.
5. An approval is bound to one task, tool, capability, validated input, scope, risk, timeout, and
   approval requirement. It **expires** and is **atomically consumed** before a single dispatch.
   Changing any parameter or replaying it fails closed.
6. Access modes configure availability. They **do not grant or fabricate approvals** - including
   "Full Access".

Defaults keep terminal, filesystem writes, browser/desktop control, dependency installs, and Git
push disabled. Network requests require explicit host allowlisting.

## Principal threats

| Threat | What limits it | What it does *not* do |
|---|---|---|
| **Indirect prompt injection** - untrusted content redirects the model | Runtime-owned capabilities, risk levels, scopes, approvals bound the blast radius; content labeled `operation_content_trust` is fenced and labeled data-not-instruction in the Operator prompt itself | Does not make injected content safe, or guarantee the model ignores it |
| **Memory poisoning** - tool output persists into later context | Treat recalled content as untrusted; clear conversation state after processing known-bad content | Provenance tracking and automated poisoning detection remain open work |
| **Excessive agency** | Capability policy, operation risk, bounded retries/steps, exact approvals, kill switch | You remain responsible for approving the exact operation shown |
| **Generated / unsandboxed code** | Docker is the preferred boundary when enabled; local-subprocess fallback runs with the YBM account's authority and therefore requires approval | Docker is defense in depth, **not** equivalent to a separate host or VM |
| **MCP servers and generated adapters** | Both need an exact one-shot approval; review package, command, env vars, declared risks, and generated files first | Use a separate OS account or VM for anything untrusted |
| **Secret disclosure** | Secrets enter via env vars or vault refs; vault-secret HTTP calls need approval; CI scans full history with Gitleaks | Redaction is a safeguard, **not** a substitute for keeping secrets out of prompts and output |
| **Local control-plane exposure** | Services bind loopback by default; the admin API refuses cross-origin requests and fails closed if bound non-loopback without a token | Not hardened as a public or multi-user API |
| **Supply chain** | Actions pinned to commit SHAs, lockfiles committed, dependency audit in CI | Updates are reviewed manually |

Do not treat "Full Access" as a safe mode for browsing or processing untrusted content.

## Keep private

`.env`, `config/config.yaml`, `.agent_control/agent_control.db`, logs, screenshots, generated workspaces, and
everything under `.agent_control/`.

A Gitleaks finding requires **credential revocation and history cleanup** - not just deleting the
current file.

## Repository hardening

The repository is public. This is the state of its protections, checked against the GitHub API on
2026-10-02; re-check it after changing repository settings.

| Item | Status |
|---|---|
| Open-source license | MIT, in `LICENSE` |
| Deterministic test and quality suites | Run on every pull request: backend on Linux, Windows, and macOS, backend quality, frontend, WhatsApp bridge, VS Code extension, container, and launcher checks |
| Secret scanning | Gitleaks scans the full history in CI (`secrets` job) |
| Private vulnerability reporting | Enabled; see [SECURITY.md](../SECURITY.md) |
| `main` protection | Pull requests only; the nine core CI jobs are required and the branch must be up to date; no force pushes; no deletions; applies to administrators. No approving review is required, because there is one maintainer |
| Dependency updates | Dependabot opens pull requests; each is merged only when CI is green on its exact head |
| Private data in tracked files and release artifacts | Not automated beyond the secret scan; review release artifacts before publishing |

The launcher smoke jobs (`Launcher smoke`, `Launcher contract`) run on every pull request but are not yet
in the required list, so a failure there would not block a merge until they are added.

Visibility and release publication each take an explicit decision by the owner and are never a side
effect of a settings change.

## References

- [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [OWASP AI Agent Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html)
- [Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions)
