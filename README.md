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

YBM runs on your machine and turns a message into either a direct reply or a traceable task. Tasks
can use the tools you enable, including files, terminal commands, Chrome, VS Code, desktop control,
scheduled work, MCP servers, and coding agents.

## The question YBM answers

Plenty of agents can write you a paragraph. The hard question is whether you can let one **touch your
actual computer**. Every task runs the same five stages, and you can stop it at any of them:

```text
Plan  ->  Approve  ->  Execute  ->  Verify  ->  Receipt
 |         |            |            |           |
 |         |            |            |           +-- every file it touched, every
 |         |            |            |               command it ran, what left the
 |         |            |            |               machine, and what it is unsure of
 |         |            |            +-- the answer is checked against the evidence,
 |         |            |                not just asserted
 |         |            +-- policy is enforced per tool call, not once at the start
 |         +-- anything consequential stops and asks, showing its blast radius first
 +-- related changes are grouped and shown as one list, before any of them run
```

High-impact capabilities are off until you turn them on, filesystem access is limited to roots you
choose, and approvals are short-lived rather than standing grants. It is built for three jobs in
particular:

1. **Safely organise and change files on your actual computer.**
2. **Do browser and desktop work for you, asking before anything consequential.**
3. **Run a long multi-step task and prove afterwards exactly what happened.**

> YBM is alpha software. Start with a test directory, review the access settings, and keep the admin
> console bound to localhost unless you understand the authentication and network implications.

## Install

The installer gets everything YBM needs, starts it, and opens the setup page. The first start takes
2-5 minutes while Python and the runtime are downloaded. Later starts take seconds.

### From a source checkout

The root launchers provide the same lifecycle on every platform. They install
or repair the local runtime, start YBM, wait for readiness, and then open the
admin console.

```powershell
.\run.bat          # Windows
```

```bash
./run.command      # macOS
./run.sh           # Linux
```

Use `.\run.ps1` from PowerShell. Every launcher accepts `doctor`, `repair`,
`docker`, `logs`, and `stop`, and supports a no-browser mode. Re-running the
default command reuses a healthy environment.

Setup checks disk space, prevents concurrent dependency changes, retries
temporary downloads and package synchronization up to three times, and records
failures in `.setup/install.log`.

### Windows release: choose either installer

**Option 1 - guided installer**

1. **[Download YBM-Setup.msi](https://github.com/oney-erge/YBM/releases/latest/download/YBM-Setup.msi).**
2. Open it, install, and leave **Launch YBM now** selected.
3. In the browser, choose a model and start chatting.

**Option 2 - no MSI**

1. **[Download Install-YBM.bat](https://github.com/oney-erge/YBM/releases/latest/download/Install-YBM.bat).**
2. Double-click it and leave the progress window open for 2-5 minutes.
3. In the browser, choose a model and start chatting.

The second option is a plain-text wrapper around
[`Install-YBM.ps1`](https://github.com/oney-erge/YBM/releases/latest/download/Install-YBM.ps1),
does not require administrator access or script signing, verifies the downloaded release, and shows
which step failed if setup cannot finish. Open **YBM** from the Start Menu after an MSI install, or
double-click `YBM.bat` in `%USERPROFILE%\ybm` after a script install.

### macOS and Linux

1. Open Terminal.
2. Paste this command:

```bash
curl -fsSL https://raw.githubusercontent.com/oney-erge/YBM/main/scripts/install.sh | bash
```

3. When YBM opens in your browser, choose a model and start chatting.

Run `~/ybm/ybm.sh` next time. The installer needs Bash, `curl`, and `tar`; it provides Python and
everything else. Docker, source-checkout, headless, and verification instructions are in
[Local setup](docs/LOCAL_SETUP.md).

### Docker (headless)

The release image is
[`ghcr.io/oney-erge/ybm:latest`](https://github.com/oney-erge/YBM/pkgs/container/ybm). It includes
the web console and is intended for Telegram, WhatsApp, API, and server workflows. See
[Local setup](docs/LOCAL_SETUP.md#docker-for-a-headless-server) for the required admin token,
volumes, and loopback-only port mapping.

## First run

The browser wizard has two steps:

1. Choose a local model or configure a cloud provider. YBM verifies access, lists models when the
   provider supports it, and makes one small completion before saving the choice.
2. Use the built-in web chat immediately, or optionally connect Telegram. WhatsApp is configured
   later under Settings.

Both wizard steps can be skipped. A skipped model means chat and task classification will not work
until a model is configured under Settings. See [Local setup](docs/LOCAL_SETUP.md) for manual and
headless configuration.

## Choose a model

| Local, no API key | Cloud API key |
|---|---|
| Ollama, LM Studio, LocalDeploy | Anthropic, OpenAI, OpenRouter, Google Gemini, Groq, DeepSeek, Mistral, xAI, Together AI |

The provider picker also accepts a custom OpenAI-compatible endpoint. Anthropic uses its native
SDK; the remaining cloud providers and local runtimes use their OpenAI-compatible APIs.

Using a local model keeps model prompts and completions on the configured local endpoint. Other
enabled tools, such as web search or HTTP requests, can still contact external services.

## Channels

- **Web chat:** available in the admin console after a model is configured.
- **Telegram:** optional bot integration with user and chat allowlists. Text, commands, approvals,
  voice transcription, and artifact delivery are supported.
- **WhatsApp:** optional, text-only integration through the unofficial Baileys WhatsApp Web client.
  It requires Node.js, QR linking, and an explicit phone-number allowlist.

An empty Telegram or WhatsApp allowlist denies every incoming message.

## What it can do

- Search, read, and organize files inside configured roots.
- Run bounded terminal commands, Python code, and supported coding agents.
- Use Chrome, screenshots, and Windows desktop control when enabled.
- Schedule recurring work and continue long-running tasks.
- Connect to MCP servers and expose its own MCP entry point.
- Work with VS Code through the optional bridge extension.
- Store attributed memory and search an indexed local knowledge base.
- Produce task traces with tool results, approvals, LLM receipts, timing, and cost data.

Capabilities and adapters are separate controls. Enabling an adapter does not bypass its capability
policy. See [Capabilities](docs/CAPABILITIES.md) for the implemented tool catalog.

## Safety model

High-impact capabilities start disabled. Filesystem access is restricted to configured roots.
Terminal, browser, desktop control, dependency installation, and Git pushes are separately gated.
Risky operations require short-lived approvals enforced by the runtime. Secrets are redacted from
logs and task output, and each task retains an audit trail.

Read the [Threat model](docs/THREAT_MODEL.md) before granting access to important files or accounts.
See [SECURITY.md](SECURITY.md) for vulnerability reporting.

## How it works

```text
Telegram  \
WhatsApp  +--> Concierge --> Operator <--> Policy <--> Tools
Web chat  /         |            |
                    +--> reply    +--> Auditor --> result and trace
```

The FastAPI backend owns orchestration, policy, persistence, and adapters. The React console talks
to it through `/admin/api/*`. Telegram and WhatsApp are channel adapters over the same task pipeline,
and the VS Code extension is an editor bridge rather than a second agent runtime. See
[Architecture](docs/ARCHITECTURE.md) and [Roles](docs/ROLES.md) for details.

## Everyday commands

On Windows, use the repository lifecycle wrapper:

```powershell
.\scripts\ybm.ps1 doctor
.\scripts\ybm.ps1 start -Open
.\scripts\ybm.ps1 status
.\scripts\ybm.ps1 logs worker -Follow
.\scripts\ybm.ps1 stop
```

On macOS and Linux, use the installed console script:

```bash
./backend/.venv/bin/ybm doctor
./backend/.venv/bin/ybm start --open
./backend/.venv/bin/ybm status
./backend/.venv/bin/ybm logs worker --follow
./backend/.venv/bin/ybm stop
```

Run `.\scripts\ybm.ps1 help` or `./backend/.venv/bin/ybm --help` for the command available on that
interface. Windows-only conveniences include the tray app, login autostart, test runner, scenario
tools, and extension packaging.

## Development

```powershell
.\scripts\ybm.ps1 setup
.\scripts\ybm.ps1 doctor
.\scripts\ybm.ps1 test
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for focused verification commands. Deterministic backend tests
do not need a network connection, GPU, Telegram account, or paid model call. Live E2E and scenario
recording have separate prerequisites and can make external calls.

## Documentation

| Guide | Contents |
|---|---|
| [Local setup](docs/LOCAL_SETUP.md) | Installers, manual setup, runtime commands, and local data |
| [Architecture](docs/ARCHITECTURE.md) | Current components and message flow |
| [Roles](docs/ROLES.md) | Concierge, Operator, and Auditor responsibilities |
| [Capabilities](docs/CAPABILITIES.md) | Implemented tools and their policy gates |
| [Threat model](docs/THREAT_MODEL.md) | Trust boundaries, protections, and residual risk |
| [Database inspection](docs/DATABASE_INSPECTION.md) | Inspect, prune, reset, and trace local state |
| [Known gaps](docs/GAPS.md) | Current limitations and unimplemented behavior |
| [History](docs/HISTORY.md) | Design rationale and completed phases |

## Current limitations

- The project is alpha and is tested most heavily on Windows.
- Desktop observation and control are Windows-only.
- WhatsApp is text-only and uses an unofficial client, which carries account risk.
- Voice transcription is disabled by default and needs the `voice` dependency extra.
- Docker cannot access host desktop or editor sessions, and only mounted paths are visible.

MIT licensed.
