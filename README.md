<div align="center">

<img src="docs/brand/ybm-mark.svg" alt="YBM logo" width="112" />

# YBM

**A local AI agent you can reach from the web, Telegram, and WhatsApp.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](backend/pyproject.toml)
[![Docker ready](https://img.shields.io/badge/docker-ready-2496ED.svg)](docker-compose.yml)
[![12 model providers](https://img.shields.io/badge/models-12%20providers-6E56CF.svg)](#choose-a-model)

![Asking YBM to organize a Downloads folder: it plans the work, asks for approval before moving any file, then reports exactly what it moved](docs/screenshots/demo.gif)

</div>

YBM runs on your machine and turns a message into either a direct reply or a traceable task. Tasks can
use the tools you enable: files, terminal commands, Chrome, VS Code, desktop control, scheduled work, MCP
servers, and coding agents.

## The question YBM answers

Plenty of agents can write you a paragraph. The hard question is whether you can let one **touch your
actual computer**. Every task runs the same five stages, and you can stop it at any of them:

```text
Plan  ->  Approve  ->  Execute  ->  Verify  ->  Receipt
 |         |            |            |           |
 |         |            |            |           +-- what it touched and how, what it re-checked on
 |         |            |            |               disk (and an admission when it checked nothing),
 |         |            |            |               and what the model calls cost
 |         |            |            +-- the answer is checked against the tool output, not just
 |         |            |                asserted, for tasks that read or produce content
 |         |            +-- policy is enforced on every tool call, not once at the start
 |         +-- anything consequential stops and asks; related changes are asked for once, as a list
 +-- large or destructive work is listed up front, before any of it runs
```

High-impact capabilities are off until you turn them on, file access is limited to folders you choose,
and approvals are exact, expiring, and used once. It is built for three jobs in particular:

1. **Safely organise and change files on your actual computer.**
2. **Do browser and desktop work for you, asking before anything consequential.**
3. **Run a long multi-step task and prove afterwards exactly what happened.**

> YBM is alpha software. Start with a test folder, review the access settings, and keep the admin console
> bound to localhost unless you understand the authentication and network implications.

## Is YBM for you?

**A good fit:**

- You want an agent to work on your own files, browser, or desktop, and you want to approve the
  consequential steps and keep a record of what it did.
- You want it to run on your machine, with a local model (Ollama, LM Studio, LocalDeploy) or an API key you
  already have.
- You use Windows, where YBM is tested most heavily and where desktop control works.

**Probably not a good fit:**

- You want one assistant across many chat apps or native phone apps. YBM has web chat, Telegram, and
  text-only WhatsApp. [OpenClaw](https://github.com/openclaw/openclaw) lists more than twenty channels and
  native apps.
- You want fully unattended automation. By design, anything consequential stops and asks.
- You need a hardened production service. YBM is alpha software.

## Install

Nothing needs configuring first. The installer gets everything YBM needs, starts it, picks a model from
what is already on your machine, and opens the console signed in. The first start takes 2-5 minutes while
Python and the runtime download; later starts take seconds.

**Windows**

1. **[Download YBM-Setup.msi](https://github.com/oney-erge/YBM/releases/latest/download/YBM-Setup.msi),**
   open it, and leave **Launch YBM now** selected.
2. Or, without an MSI, **[download Install-YBM.bat](https://github.com/oney-erge/YBM/releases/latest/download/Install-YBM.bat)**
   and double-click it. It needs no administrator access or script signing.

**macOS and Linux:** open Terminal and paste

```bash
curl -fsSL https://raw.githubusercontent.com/oney-erge/YBM/main/scripts/install.sh | bash
```

Run `~/ybm/ybm.sh` next time. **From a source checkout** (needs Node.js 22.22+ to build the console) run
`run.bat` (Windows), `./run.command` (macOS), or `./run.sh` (Linux). **Headless server:** `./run.sh docker` starts the published container and
opens its console. Every option, with checksums and verification, is in [INSTALL.md](docs/INSTALL.md).

## Your first ten minutes

1. YBM opens in your browser, already signed in. It picks a model it finds (your own LocalDeploy, a
   running Ollama, or a provider API key in your environment or `.env`) and tells you which. It never
   calls a paid API to choose. If none is found, a short wizard offers a local model or a cloud provider.
2. File access starts off. Chat offers your Downloads, Documents, and Desktop folders: pick one and
   whether YBM may only read, or may change things after asking you.
3. With write access, try **"Organize my Downloads folder by type"**. It shows what it will move and waits
   for your approval. Nothing moves until you say so, and the finished task keeps a receipt.

[USING.md](docs/USING.md) tours the console and covers approvals and troubleshooting.

## Choose a model

| Local, no API key | Cloud API key |
|---|---|
| Ollama, LM Studio, LocalDeploy | Anthropic, OpenAI, OpenRouter, Google Gemini, Groq, DeepSeek, Mistral, xAI, Together AI |

The picker also accepts a custom OpenAI-compatible endpoint. Anthropic uses its native SDK; the other
providers use their OpenAI-compatible APIs. A local model keeps prompts and completions on your machine;
other enabled tools, such as web search or HTTP requests, can still contact external services.

## Channels

- **Web chat:** in the console, ready as soon as a model is configured.
- **Telegram:** optional bot with user and chat allowlists. Text, commands, approvals, voice transcription,
  and file delivery.
- **WhatsApp:** optional and text-only, through the unofficial Baileys client. Needs Node.js, QR linking,
  and a phone-number allowlist, and is configured in `config/config.yaml`.

An empty Telegram or WhatsApp allowlist denies every incoming message.

## How it works

```text
Telegram  \
WhatsApp  +--> Concierge --> Operator <--> Policy <--> Tools
Web chat  /         |            |
                    +--> reply    +--> Auditor --> result and trace
```

A FastAPI backend owns orchestration, policy, persistence, and the channel adapters; the React console
talks to it through `/admin/api/*`. The Concierge decides whether a message is chat or a task, the
Operator does the work one tool call at a time under the policy engine, and the Auditor checks the
result. See [ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Safety

High-impact capabilities start disabled: terminal, file writes, browser and desktop control, dependency
installs, and Git pushes are each gated separately. Secrets are redacted from logs and task output, and
every task keeps an audit trail. Read the [threat model](docs/THREAT_MODEL.md) before granting access to
important files or accounts, and see [SECURITY.md](SECURITY.md) to report a vulnerability.

## Limitations

- Alpha, and tested most heavily on Windows.
- Desktop observation and control are Windows-only.
- WhatsApp is text-only and uses an unofficial client, which carries account risk.
- Voice transcription is off by default and needs the `voice` extra.
- Docker cannot reach the host desktop or editor, and sees only mounted paths.

The full list is under [Known limits](docs/ROADMAP.md#known-limits).

## Documentation

| Guide | Contents |
|---|---|
| [Install](docs/INSTALL.md) | Every install path, runtime commands, local data, linking WhatsApp |
| [Using YBM](docs/USING.md) | First run, the console, approvals, checking it works, troubleshooting |
| [Architecture](docs/ARCHITECTURE.md) | The three roles, the Operator loop, task states, components |
| [Capabilities](docs/CAPABILITIES.md) | The tool catalog and the access each tool needs |
| [Threat model](docs/THREAT_MODEL.md) | Trust boundaries, protections, residual risk |
| [Roadmap](docs/ROADMAP.md) | What is built, what is next, and the known limits |
| [Database inspection](docs/DATABASE_INSPECTION.md) | Inspect, prune, reset, and trace local state |
| [Contributing](CONTRIBUTING.md) | Development setup and verification commands |

Design rationale and finished plans are kept in [docs/archive/](docs/archive/HISTORY.md); they describe
decisions at the time, not current behavior.

MIT licensed.
