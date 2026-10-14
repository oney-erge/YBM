<div align="center">

<img src="docs/brand/ybm-mark.svg" alt="YBM logo" width="112" />

# YBM

**A local AI agent that asks before it changes your computer.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](backend/pyproject.toml)
[![Docker ready](https://img.shields.io/badge/docker-ready-2496ED.svg)](docker-compose.yml)
[![12 model providers](https://img.shields.io/badge/models-12%20providers-6E56CF.svg)](#choose-a-model-and-channel)

![Asking YBM to organize a Downloads folder: it plans the work, asks for approval before moving any file, then reports exactly what it moved](docs/screenshots/demo.gif)

</div>

YBM runs on your machine. You can talk to it through the web console, Telegram,
or WhatsApp, then let it use only the tools you enable: files, terminal
commands, Chrome, VS Code, desktop control, schedules, MCP servers, and coding
agents.

Its core workflow is simple:

```text
Plan -> Approve -> Execute -> Verify -> Receipt
```

For consequential work, YBM shows the plan, waits for approval, enforces policy
on each tool call, and keeps a record of what happened. High-impact capabilities
start disabled, and file access is limited to folders you choose.

> YBM is alpha software. Start with a test folder, review access settings before
> granting them, and keep the admin console on localhost unless you understand
> the authentication and network implications.

## Install

The first start installs what YBM needs, opens the console, and finds a model
already available on your machine. It takes about 2-5 minutes; later starts
take seconds.

**Windows**

1. [Download YBM-Setup.msi](https://github.com/oney-erge/YBM/releases/latest/download/YBM-Setup.msi)
   and leave **Launch YBM now** selected.
2. Or [download Install-YBM.bat](https://github.com/oney-erge/YBM/releases/latest/download/Install-YBM.bat)
   and double-click it. It needs no administrator access or script signing.

**macOS and Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/oney-erge/YBM/main/scripts/install.sh | bash
```

Run `~/ybm/ybm.sh` later. For a source checkout, use `run.bat` on Windows,
`./run.command` on macOS, or `./run.sh` on Linux. Every install route and the
headless Docker option are in [Install](docs/INSTALL.md).

## Your first task

1. YBM opens in your browser and shows the model it found. It can use a local
   model through Ollama, LM Studio, or LocalDeploy, or a provider API key you
   already have. It does not call a paid API just to choose a model.
2. File access starts off. Choose a folder and whether YBM may read it or may
   change it after approval.
3. Try: **"Organize my Downloads folder by type."** YBM shows what it intends
   to move, waits for approval, then leaves a receipt.

[Using YBM](docs/USING.md) explains the console, approvals, and troubleshooting.

## Choose a model and channel

| Local, no API key | Cloud API key |
|---|---|
| Ollama, LM Studio, LocalDeploy | Anthropic, OpenAI, OpenRouter, Google Gemini, Groq, DeepSeek, Mistral, xAI, Together AI |

A local model keeps prompts and completions on your machine. Other enabled tools,
such as web search or HTTP requests, can still contact external services.

The web console is ready after setup. Telegram is optional and uses user and
chat allowlists. WhatsApp is optional, text-only, and uses the unofficial
Baileys client with a phone-number allowlist. An empty Telegram or WhatsApp
allowlist denies all incoming messages.

## Limits and safety

- Desktop observation and control are Windows-only. Docker cannot reach the
  host desktop or editor, and can see only mounted paths.
- YBM is intended for supervised work. Consequential actions stop for approval;
  it is not fully unattended automation or a hardened production service.
- Terminal commands, file writes, browser and desktop control, dependency
  installation, and Git pushes are separately gated. Secrets are redacted from
  task output and logs, and every task has an audit trail.
- WhatsApp uses an unofficial client and carries account risk. Voice
  transcription is off by default and needs the `voice` extra.

Read the [threat model](docs/THREAT_MODEL.md) before granting access to important
files or accounts. See [Security](SECURITY.md) to report a vulnerability.

## Documentation

| Guide | Use it for |
|---|---|
| [Install](docs/INSTALL.md) | Install routes, runtime commands, local data, and WhatsApp setup |
| [Using YBM](docs/USING.md) | Console tour, approvals, and troubleshooting |
| [Capabilities](docs/CAPABILITIES.md) | Tools and their required access |
| [Architecture](docs/ARCHITECTURE.md) | Components, policy, and task flow |
| [Threat model](docs/THREAT_MODEL.md) | Trust boundaries and residual risks |
| [Roadmap](docs/ROADMAP.md) | Built features, known limits, and planned work |
| [Database inspection](docs/DATABASE_INSPECTION.md) | Inspecting, pruning, and tracing local state |
| [Contributing](CONTRIBUTING.md) | Development setup and verification |

MIT licensed.

Built by [Oney Erge](https://oneyerge.com).
