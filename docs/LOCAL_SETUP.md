# Local setup

## Before you start

The normal installers provide Python, YBM, and the built web console. Windows needs only an internet
connection. macOS and Linux also need Bash, `curl`, and `tar`, which are normally preinstalled.
Git, Node.js, and a system Python are not required.

The first start downloads the runtime and usually takes 2-5 minutes. Later starts take seconds.

## Windows option 1: MSI

1. **[Download YBM for Windows](https://github.com/oney-erge/YBM/releases/latest/download/YBM-Setup.msi).**
2. Open the file, install, and leave **Launch YBM now** selected.
3. In the browser page that opens, choose a model and start chatting.

Open **YBM** from the Start Menu next time. The per-user install lives in `%LOCALAPPDATA%\YBM`,
needs no administrator account, and can be removed from **Add or remove programs**. Start-at-login
is optional and off by default.

Windows shows an unknown-publisher warning because the MSI is not yet code signed. The MSI now has
a visible install/progress/completion flow and is installed, started, health-checked, stopped, and
uninstalled on a clean Windows CI runner before a release can publish. The release page also provides
checksums and signed build provenance. To verify a downloaded installer with GitHub CLI:

```powershell
gh attestation verify .\YBM-Setup.msi --repo oney-erge/YBM
```

## Windows option 2: double-click script

1. **[Download Install-YBM.bat](https://github.com/oney-erge/YBM/releases/latest/download/Install-YBM.bat).**
2. Double-click it and leave the progress window open for 2-5 minutes.
3. Configure a model in the browser page that opens.

The batch file downloads the plain-text `Install-YBM.ps1` from the same release. The PowerShell
installer downloads the complete release, verifies its SHA256 checksum, installs to
`%USERPROFILE%\ybm`, starts YBM, runs its health checks, and reports the failed step if anything goes
wrong. It needs no administrator account or script signature. Re-running it refreshes application
files while preserving settings and task data.

For a terminal instead of a double-click:

```powershell
irm https://raw.githubusercontent.com/oney-erge/YBM/main/scripts/install.ps1 | iex
```

Use `irm <url> | more` instead to inspect the script. From a checkout, the same bootstrap supports
`-DryRun`, `-Verify`, `-NoPrompt`, and `-InstallDir C:\path\to\ybm`. The corresponding environment
variables are `YBM_DRY_RUN`, `YBM_NO_PROMPT`, and `YBM_INSTALL_DIR`.

## macOS and Linux: install in three steps

1. Open Terminal.
2. Paste this command:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/oney-erge/YBM/main/scripts/install.sh | bash
   ```

3. In the browser page that opens, choose a model and start chatting.

Run `~/ybm/ybm.sh` next time. Re-running the installer refreshes the application while preserving
local settings and task data. Use `--no-desktop` with `ybm.sh` on a headless machine.

From a checkout, `install.sh` also accepts `--dry-run`, `--verify`, `--no-prompt`, and
`--install-dir DIR`. `YBM_INSTALL_DIR` and `YBM_DRY_RUN=1` provide environment equivalents.

## Docker for a headless server

Docker runs the published `ghcr.io/oney-erge/ybm:latest` headless image. It bundles the admin
console and WhatsApp dependencies, but it cannot attach to the host desktop, Chrome display, or
VS Code session.

```bash
cp .env.example .env
# Edit .env and set AGENT_ADMIN_TOKEN to a long random value.
docker compose pull
docker compose up -d
```

Open `http://127.0.0.1:8765/admin` and enter the admin token. The first-run wizard writes settings
to the host's ignored `config/config.yaml`. Store cloud API keys in the host `.env` so they survive
container recreation.

Compose publishes the console only on host loopback, keeps runtime state in the `ybm-state` volume,
and exposes `./workspace` as the allowed host workspace. To add the optional Ollama service:

```bash
docker compose --profile ollama up -d
docker compose exec ollama ollama pull qwen3:8b
```

To build the same image from a source checkout instead of pulling it:

```bash
docker build --tag ybm-control:local .
```

## What installation creates

1. The bootstrap downloads the latest release, including the prebuilt web console.
2. `uv` provides Python 3.12 and creates `backend/.venv`.
3. YBM creates ignored local config and secret files when they do not exist.
4. YBM initializes its local database, starts the services, and opens the console.

Existing config, tokens, task history, and other local state are retained. A source checkout is for
development: double-click `YBM.bat` on Windows or run `./ybm.sh` on macOS/Linux. Source UI changes
require Node.js 22.22 or newer and `ybm ui-build`; release installs do not.

## First-run configuration

The browser wizard is shown when no reachable model has been configured. It lets you:

1. Select Ollama, LM Studio, LocalDeploy, a cloud provider, or another OpenAI-compatible endpoint.
2. Verify the key or endpoint, select a model, and run one small completion before saving.
3. Use web chat immediately or optionally verify and connect a Telegram bot.

The wizard can be skipped. Without a working model, chat replies and task classification remain
unavailable until you configure one under Settings.

For a headless source install with no browser:

```bash
./backend/.venv/bin/ybm onboard
```

The Windows equivalent is:

```powershell
& .\backend\.venv\Scripts\ybm.exe onboard
```

## Runtime interfaces

The Windows wrapper and installed Python CLI share the main runtime operations, but they are not
identical.

| Operation | Windows wrapper | Installed CLI on macOS/Linux |
|---|---|---|
| Start and open | `YBM.bat` or `.\scripts\ybm.ps1 run` | `./ybm.sh` or `./backend/.venv/bin/ybm start --open` |
| Diagnose | `.\scripts\ybm.ps1 doctor` | `./backend/.venv/bin/ybm doctor` |
| Status | `.\scripts\ybm.ps1 status` | `./backend/.venv/bin/ybm status` |
| Follow worker log | `.\scripts\ybm.ps1 logs worker -Follow` | `./backend/.venv/bin/ybm logs worker --follow` |
| Stop | `.\scripts\ybm.ps1 stop` | `./backend/.venv/bin/ybm stop` |
| Trace a task | `.\scripts\ybm.ps1 trace <task_id>` | `./backend/.venv/bin/ybm trace-task <task_id>` |
| Change config | `.\scripts\ybm.ps1 config set <path> <value>` | `./backend/.venv/bin/ybm config-set <path> <value>` |
| Build UI | `.\scripts\ybm.ps1 ui-build` | `./backend/.venv/bin/ybm ui-build` |

Run `.\scripts\ybm.ps1 help` or `./backend/.venv/bin/ybm --help` for the authoritative command list.
The PowerShell wrapper also includes development tests, live E2E helpers, scenarios, cleanup,
extension packaging, tray, autostart, and restart workflows.

## Manual Windows setup

For development or recovery after the bootstrap installer has provided `uv`:

```powershell
.\scripts\ybm.ps1 setup
.\scripts\ybm.ps1 doctor
.\scripts\ybm.ps1 start -Open
```

The developer setup installs the test, E2E, lint, voice, tray, and desktop extras. The normal
`YBM.bat` path installs runtime extras only.

To save a Telegram token during setup:

```powershell
.\scripts\ybm.ps1 setup --telegram-token <token>
```

To point YBM at a local [LocalDeploy](https://github.com/oney-erge/LocalDeploy) checkout, add this to
`.env` before starting:

```text
YBM_LOCALDEPLOY_ROOT=C:\path\to\LocalDeploy
```

You can instead configure any catalog provider or custom OpenAI-compatible endpoint in the browser.

## Access and approvals

High-impact capabilities start disabled. Enable only the access needed for the current workflow
from the console's Access page, or change a specific value on Windows:

```powershell
.\scripts\ybm.ps1 config set <dotted.path> <value>
```

An enabled adapter does not override capability policy. Approval gates, risk ceilings, allowlists,
and allowed roots still apply. See [CAPABILITIES.md](CAPABILITIES.md) and
[THREAT_MODEL.md](THREAT_MODEL.md).

## Link WhatsApp

WhatsApp is disabled by default. It uses [Baileys](https://github.com/WhiskeySockets/Baileys), an
unofficial WhatsApp Web client. It does not need a Meta developer account or public webhook, but it
does carry a small account-flagging risk.

1. Set `channels.whatsapp.enabled: true` in `config/config.yaml` or use Settings.
2. Start or restart YBM.
3. Follow the bridge log: `.\scripts\ybm.ps1 logs whatsapp -Follow` on Windows, or
   `./backend/.venv/bin/ybm logs whatsapp --follow` on macOS/Linux.
4. Scan the QR code from WhatsApp under Settings, Linked Devices.
5. Add your number to `channels.whatsapp.allowed_numbers` as E.164 digits without `+`, such as
   `"15551234567"`, then restart.

The linked session persists under `.agent_control/whatsapp_auth/`. An empty allowed-number list
denies every message. Consider testing with a secondary number, and never commit a real number or
session data.

## Common Windows operations

```powershell
.\scripts\ybm.ps1 status
.\scripts\ybm.ps1 logs backend -Follow
.\scripts\ybm.ps1 logs worker -Follow
.\scripts\ybm.ps1 backup
.\scripts\ybm.ps1 check-updates
.\scripts\ybm.ps1 package-extension
.\scripts\ybm.ps1 stop
Invoke-RestMethod http://127.0.0.1:8765/health
```

Use service scripts under `scripts/services/` directly only when debugging one process in isolation.

## Local data

| Path | Contents |
|---|---|
| `.env` | Local tokens and provider keys |
| `config/config.yaml` | Local settings and access policy |
| `.agent_control/agent_control.db` | SQLite task, message, approval, and audit state |
| `.agent_control/workspaces/task_<id>` | Per-task generated files |
| `.agent_control/logs/<service>.jsonl` | Structured logs with secret redaction |
| `.agent_control/browser/screenshots` | Browser screenshots |
| `.agent_control/computer_use/screenshots` | Desktop screenshots |
| `.agent_control/coding_sessions` | Coding-agent session state |
| `.agent_control/adapters` | Generated adapter proposals, never auto-loaded |
| `.agent_control/whatsapp_auth` | WhatsApp linked-device credentials |

All of these paths are private and ignored by Git. Use
[DATABASE_INSPECTION.md](DATABASE_INSPECTION.md) to inspect or prune database state.
