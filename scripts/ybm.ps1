<#
.SYNOPSIS
  Single entry point for YBM: setup, doctor, start/stop/status/logs, test, e2e, db, config.
  Replaces the 15+ separate scripts that used to live here (see docs/HISTORY.md P1).

.EXAMPLE
  .\scripts\ybm.ps1 run
  .\scripts\ybm.ps1 setup
  .\scripts\ybm.ps1 doctor
  .\scripts\ybm.ps1 start -NoTelegram
  .\scripts\ybm.ps1 status
  .\scripts\ybm.ps1 logs worker -Follow
  .\scripts\ybm.ps1 test
  .\scripts\ybm.ps1 db inspect
  .\scripts\ybm.ps1 config set server.port 8765
#>
param(
  [Parameter(Position = 0)]
  [ValidateSet("run", "setup", "doctor", "start", "stop", "restart", "status", "logs", "test", "e2e", "e2e-login", "send", "trace", "scenario", "db", "config", "clean", "package-extension", "tray", "autostart", "backup", "check-updates", "ui-build", "ui-dev", "help")]
  [string]$Command = "help",

  [Parameter(Position = 1)]
  [string]$Sub = $null,

  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\lib\common.ps1"
. "$Script:YbmRoot\scripts\install-utils.ps1"
Initialize-Install -RepositoryRoot $Script:YbmRoot -ProductName "YBM"
trap { Write-InstallFailure $_; Exit-InstallLock; exit 1 }
Import-DotEnv

function Show-YbmHelp {
  @"
YBM - local agentic control stack

  ybm run                      the one command: install/update what's missing, start, open the console
                                (double-click YBM.bat at the repo root for the same thing, no terminal)
  ybm setup                    create venv, install deps, bootstrap config/.env
  ybm doctor                   preflight: env, config, connectivity, ports
  ybm start [flags]            start the stack (runs doctor first)
    -NoTelegram -NoWhatsApp -NoWorker -NoScheduler -NoLocalDeploy -SkipDoctor -Open
  ybm stop                     stop all YBM background processes
  ybm restart [flags]          stop then start
  ybm status                   show per-service status and health
  ybm logs <service> [-Follow] tail a service's log (backend, worker, ...)
  ybm test [pytest-args]       run backend/tests
  ybm e2e [args]                passthrough to scripts/run_all_e2e_tests.py
  ybm db inspect|clean|reset   inspect / prune / wipe the local database
  ybm config show              print effective config
  ybm config set <path> <val>  set a dotted config path (e.g. server.port 8765)
  ybm clean [flags]            wipe generated artifacts (-Caches -Workspaces -AdapterProposals -AllGenerated)
  ybm e2e-login                bootstrap the Telethon user session for live E2E checks
  ybm send "<message>"         send one ad-hoc message through the full pipeline and trace it
  ybm trace <task_id> [--json] full post-mortem for one task - reads the DB directly, no running backend needed
  ybm scenario record <name> [--profile <name>]
                                re-record a scenario fixture against a live LLM (real API calls, may cost money)
  ybm ui-build                 build the React admin console into backend/src/agent_control/static/admin
  ybm ui-dev                   run the admin console with hot reload against a running backend
  ybm package-extension        build the VS Code bridge extension .vsix
  ybm tray                     launch the system tray icon (Open Admin Console / Start / Stop / Status)
  ybm autostart enable|disable|status
                                run the tray icon automatically at login (per-user Startup folder shortcut)
  ybm backup [--out <dir>]     zip the database, config.yaml, .env, and secret vault (default: .agent_control/backups)
  ybm check-updates            compare the installed version against the latest GitHub release (read-only)
  ybm ui-build                 build the admin console into the backend's static dir (needs Node.js 22.22+)
  ybm ui-dev                   run the admin console with hot reload (needs Node.js 22.22+)
"@ | Write-Host
}

function Invoke-YbmUi {
  # README.md and the "no build found" placeholder page have always told
  # people to run `ybm ui-build`, and it was never in this script's
  # ValidateSet - so the single instruction shown to someone with no admin
  # console failed with a PowerShell parameter error, and the documented way
  # out of that state did not exist. The implementation itself already lived
  # in agent_control.cli; only this passthrough was missing, so delegate
  # rather than keeping a second copy of the npm logic here.
  param([string]$Mode)
  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  & (Get-YbmPython) -m agent_control.cli $Mode
  exit $LASTEXITCODE
}

function Get-YbmAutostartShortcutPath {
  Join-Path ([Environment]::GetFolderPath("Startup")) "YBM.lnk"
}

function Invoke-YbmAutostart {
  param([string]$Sub)
  $shortcutPath = Get-YbmAutostartShortcutPath
  switch ($Sub) {
    "enable" {
      $shell = New-Object -ComObject WScript.Shell
      $shortcut = $shell.CreateShortcut($shortcutPath)
      $shortcut.TargetPath = Get-YbmPythonW
      $shortcut.Arguments = "`"$Script:YbmRoot\scripts\tray_app.py`""
      $shortcut.WorkingDirectory = $Script:YbmRoot
      $shortcut.Description = "YBM tray icon"
      $shortcut.Save()
      Write-Host "Autostart enabled - $shortcutPath will launch the tray icon at login."
      Write-Host "Launching it now too, so you don't have to log out and back in..."
      Start-Process -FilePath (Get-YbmPythonW) -ArgumentList "`"$Script:YbmRoot\scripts\tray_app.py`"" -WorkingDirectory $Script:YbmRoot
    }
    "disable" {
      if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "Autostart disabled - removed $shortcutPath."
      } else {
        Write-Host "Autostart was not enabled (no shortcut at $shortcutPath)."
      }
    }
    "status" {
      if (Test-Path -LiteralPath $shortcutPath) {
        Write-Host "Autostart: enabled ($shortcutPath)"
      } else {
        Write-Host "Autostart: disabled"
      }
    }
    default {
      Write-Host "usage: ybm autostart enable | disable | status"
      exit 1
    }
  }
}

# NOTE: none of the helper functions below use a parameter named "Args" -
# that collides with PowerShell's automatic $args variable and silently
# discards whatever is passed in (confirmed the hard way - see git history).
# Use $Argv instead.

# Dependencies actually needed for the app to run: voice (STT/TTS), tray
# (the tray icon), desktop (screenshot/control capability) - none of it is
# developer tooling. Kept as a function, not a constant, so -RuntimeOnly
# and the full developer set (below) can share the --no-desktop opt-out
# without duplicating that check.
#
# --inexact matters here specifically: this repo has exactly one venv,
# shared by the consumer run path and a developer's own `ybm setup`/`ybm
# test`/ruff. `uv sync` without it is EXACT - it uninstalls anything not
# covered by the given extras, not just installs what's missing. A
# runtime-only sync run against a venv that already has the dev extras
# (ruff, pytest, telethon) would silently strip them - confirmed the hard
# way running this exact change live, not a hypothetical.
function Get-YbmRuntimeExtraArgs {
  param([string[]]$Argv)
  $extraArgs = @("--extra", "voice", "--extra", "tray", "--inexact")
  if ($Argv -notcontains "--no-desktop") {
    $extraArgs += @("--extra", "desktop")
  }
  return $extraArgs
}

function Invoke-YbmUvSync {
  param([string]$Uv, [string[]]$Arguments)
  Invoke-InstallRetry "dependency synchronization" {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $output = & $Uv sync @Arguments 2>&1
      $code = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $previousPreference
    }
    $output | Out-Host
    if ($code -ne 0) {
      throw "uv sync failed with exit $($code): $($output -join [Environment]::NewLine)"
    }
  }
}

function Invoke-YbmSetup {
  param([string[]]$Argv)
  Enter-InstallLock
  Assert-InstallFreeSpace -Path $Script:YbmRoot -RequiredGB 3

  $runtimeOnly = ($Argv -contains "-RuntimeOnly") -or ($Argv -contains "--runtime-only")

  if (-not (Test-Path -LiteralPath $Script:YbmVenvPython)) {
    # Installs uv when it is missing rather than refusing to continue. That
    # refusal is what forced a separate first-run entry point to exist at all;
    # with this, YBM.bat handles the first launch and every launch after it.
    #
    # Called by absolute path below, never as bare `uv`: when uv was installed
    # a moment ago, its new PATH entry does not exist in THIS process, so the
    # bare name fails on exactly the fresh machine this branch is here for.
    $uv = Install-YbmUv
    Write-Host "Creating backend\.venv via uv sync (first run can take a minute)..."
    Push-Location (Join-Path $Script:YbmRoot "backend")
    try {
      if ($runtimeOnly) {
        # The consumer path (YBM.bat / `ybm run` / install.ps1, which now
        # just calls `ybm run` - docs/UI_UX_AUDIT.md Phase 10, second
        # review): a person double-clicking their way to a running console
        # has no use for pytest, ruff, or the Telethon E2E client. Every
        # dev-tooling extra stays behind the bare `ybm setup` a developer
        # types themselves, which keeps its original full set below.
        $extraArgs = Get-YbmRuntimeExtraArgs -Argv $Argv
      } else {
        # Keep this extras list identical to scripts/install.sh's
        # developer-path uv sync line - they drifted before (install.sh
        # only had "--extra dev", silently skipping pytest/telethon/voice/
        # desktop on a fresh Linux/macOS install). "dev" (ruff) is included
        # so a fresh `ybm setup` can actually run the
        # `uv run --frozen ruff check .` step AGENTS.md/CONTRIBUTING.md document.
        $extraArgs = @("--extra", "test", "--extra", "e2e", "--extra", "dev") + (Get-YbmRuntimeExtraArgs -Argv $Argv)
      }
      Invoke-YbmUvSync -Uv $uv -Arguments $extraArgs
    } finally {
      Pop-Location
    }
  } else {
    Write-Host "backend\.venv already exists - skipping venv creation (run 'uv sync' in backend/ to update deps)."
  }

  $telegramToken = $null
  for ($i = 0; $i -lt $Argv.Count; $i++) {
    if ($Argv[$i] -eq "--telegram-token" -and $i + 1 -lt $Argv.Count) {
      $telegramToken = $Argv[$i + 1]
    }
  }

  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  $pyArgs = @("-m", "agent_control.cli", "setup")
  if ($telegramToken) {
    $pyArgs += @("--telegram-token", $telegramToken)
  }
  & (Get-YbmPython) @pyArgs
  # Deliberately no `exit` here (there used to be one) - Invoke-YbmRun calls
  # this as a sub-step and needs to keep going afterward. $LASTEXITCODE from
  # the python call above is left set in the caller's scope either way; the
  # "setup" dispatch case below is what turns it into a process exit code
  # when this runs as the top-level command.
}

function Get-YbmSyncFingerprintPath {
  Join-Path $Script:YbmRoot "backend\.venv\.ybm_sync_fingerprint"
}

function Get-YbmLockFingerprint {
  # Combines pyproject.toml and uv.lock: uv.lock alone would miss the rare
  # case of a hand-edited pyproject.toml that hasn't been re-locked yet -
  # `uv sync` (no --frozen here) resolves fresh in that case, and the
  # fingerprint needs to change too or a stale venv would look "up to date."
  $paths = @(
    (Join-Path $Script:YbmRoot "backend\pyproject.toml"),
    (Join-Path $Script:YbmRoot "backend\uv.lock")
  ) | Where-Object { Test-Path -LiteralPath $_ }
  if ($paths.Count -eq 0) { return $null }
  $hashes = $paths | ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash }
  return ($hashes -join ":")
}

function Invoke-YbmRun {
  param([string[]]$Argv = @())
  $openBrowser = -not ($Argv -contains "-NoBrowser" -or $Argv -contains "--no-browser")
  # The one command a non-developer should ever need (docs/UI_UX_AUDIT.md
  # Phase 10, second review): install whatever's missing, do nothing when
  # there's nothing to do, and start the console in a few seconds - not
  # "double-click, then wait while a dependency manager evaluates the
  # complete developer environment." Wrapped by the double-clickable
  # YBM.bat at the repo root, so "run this file" is the entire instruction.
  Write-Host "YBM" -ForegroundColor Cyan
  Write-Host "==========="
  Write-Host ""

  # A first run downloads uv, a Python runtime, and a few hundred MB of
  # dependencies. That is minutes of a console window doing something the
  # person watching cannot identify, and an unexplained pause reads as a hang,
  # not as progress. Say what is happening and roughly how long before it
  # starts, and number the stages so the window is legibly moving.
  $firstRun = -not (Test-Path -LiteralPath $Script:YbmVenvPython)
  if ($firstRun) {
    Write-Host "First run - setting things up." -ForegroundColor Yellow
    Write-Host "This downloads Python and YBM's dependencies, and usually takes 2-5 minutes."
    Write-Host "You only wait once; after this YBM starts in a few seconds."
    if ($openBrowser) {
      Write-Host "Leave this window open - it opens your browser when it is ready."
    }
    Write-Host ""
  }

  Write-Host "[1/4] Checking install..." -ForegroundColor Cyan
  # -RuntimeOnly: a person double-clicking their way to a running console
  # has no use for pytest, ruff, or the Telethon E2E client - those stay
  # behind the bare `ybm setup` a developer types themselves.
  Invoke-YbmSetup -Argv @("-RuntimeOnly")
  if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Setup failed (exit $LASTEXITCODE) - see the message above." -ForegroundColor Red
    throw "Setup failed with exit $LASTEXITCODE"
  }

  # Resolve-YbmUv, not Get-Command: setup may have just installed uv, whose
  # PATH entry this process cannot see. Looking it up by name here made the
  # dependency sync silently skip itself on a first run.
  $uvCmd = Resolve-YbmUv
  if ($uvCmd) {
    $fingerprintPath = Get-YbmSyncFingerprintPath
    $currentFingerprint = Get-YbmLockFingerprint
    $storedFingerprint = if (Test-Path -LiteralPath $fingerprintPath) { Get-Content -LiteralPath $fingerprintPath -Raw } else { $null }
    if ($currentFingerprint -and $storedFingerprint -eq $currentFingerprint) {
      Write-Host "[2/4] Dependencies up to date - skipping sync." -ForegroundColor DarkGray
    } else {
      Write-Host "[2/4] Installing dependencies (the long part on a first run)..." -ForegroundColor Cyan
      Push-Location (Join-Path $Script:YbmRoot "backend")
      try {
        # No output redirection here on purpose: uv writes routine progress
        # ("Resolved N packages...") to stderr, and *>/2>&1 redirection
        # under this script's $ErrorActionPreference = "Stop" turns that
        # into a fatal NativeCommandError even on success - confirmed live,
        # not a hypothetical (this exact line halted `ybm run` the first
        # time). Letting it print normally avoids the whole gotcha.
        Invoke-YbmUvSync -Uv $uvCmd -Arguments (Get-YbmRuntimeExtraArgs -Argv @())
      } finally {
        Pop-Location
      }
      if ($currentFingerprint) {
        Set-Content -LiteralPath $fingerprintPath -Value $currentFingerprint -NoNewline
      }
    }
  }

  Complete-Install
  Write-Host ""
  Write-Host "[3/4] Checking for updates..." -ForegroundColor Cyan
  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  & (Get-YbmPython) -m agent_control.cli check-updates
  # Deliberately informational only, never auto-applied: pulling and
  # restarting onto new, unreviewed code without being asked is an
  # external-write action this script doesn't take on its own - same
  # reasoning as `ybm check-updates` itself (docs/UI_UX_AUDIT.md Phase 6).

  Write-Host ""
  Write-Host "[4/4] Starting YBM$(if ($openBrowser) { ' and opening the console' })..." -ForegroundColor Cyan
  $startArgv = @()
  if ($openBrowser) { $startArgv += "-Open" }
  Invoke-YbmStart -Argv $startArgv
}

function Invoke-YbmDoctor {
  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  & (Get-YbmPython) -m agent_control.cli doctor
  # $LASTEXITCODE survives the function return; callers read it directly.
  # Do NOT wrap this call (or the caller's call to this function) in
  # parens/Out-Null - that captures the child's stdout instead of letting
  # it stream to the console, which silently swallows every doctor line.
}

function Stop-YbmOrphansForName {
  param([string]$Name)
  $pidFile = Join-Path $Script:YbmRunDir "$Name.pid"
  if (Test-YbmPidAlive $pidFile) {
    return
  }
  $patterns = switch ($Name) {
    # "uvicorn agent_control.main:app" has not been how the backend starts for
    # a long time - services/run_backend.ps1 runs `-m agent_control.serve_backend`.
    # A stale pattern here is silently destructive: `ybm stop` leaves the real
    # backend alive, the next `ybm start` finds port 8765 answering and reports
    # "already running (external)" without writing a pid file, and from then on
    # `ybm status` says "stopped" while the thing serves happily and no longer
    # responds to `ybm stop` at all. Confirmed live, not hypothetical.
    "backend" { @("run_backend.ps1", "agent_control.serve_backend", "uvicorn agent_control.main:app") }
    "localdeploy" { @("run_localdeploy.ps1", "api_server.py") }
    "worker" { @("run_worker.ps1", "agent_control.cli run-worker") }
    "coding_session_watcher" { @("run_coding_session_watcher.ps1", "agent_control.cli run-coding-session-watcher") }
    "scheduler" { @("run_scheduler.ps1", "agent_control.cli run-scheduler") }
    "telegram_polling" { @("run_telegram_polling.ps1", "agent_control.cli poll-telegram") }
    "whatsapp" { @("run_whatsapp.ps1", "agent_control.cli poll-whatsapp") }
    default { @() }
  }
  if (-not $patterns) {
    return
  }
  $rootPath = $Script:YbmRoot
  $candidates = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $commandLine = $_.CommandLine
    if (-not $commandLine) { return $false }
    foreach ($pattern in $patterns) {
      $matchesPattern = $commandLine -like "*$pattern*"
      # Any `python -m agent_control.*` launch, not just the cli workers: a
      # module run's command line is just the interpreter and the module name,
      # so it never contains the repo path the fallback check below looks for.
      # Before this, `agent_control.serve_backend` could match its pattern and
      # still be skipped by that guard - the orphan survived either way.
      $isModuleLaunch = $pattern -like "agent_control.*"
      $isLocalDeploy = $Name -eq "localdeploy" -and $commandLine -like "*LocalDeploy*"
      if ($matchesPattern -and ($isModuleLaunch -or $isLocalDeploy -or $commandLine -like "*$rootPath*")) {
        return $true
      }
    }
    return $false
  }
  foreach ($candidate in $candidates) {
    $process = Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction SilentlyContinue
    if ($process) {
      Stop-YbmProcessTree -ProcessId $process.Id
      Write-Host "Stopped orphan $Name process (pid $($process.Id))"
    }
  }
}

function Start-YbmService {
  param(
    [string]$Name,
    [string]$ScriptPath,
    [string]$ReadyUrl = $null,
    [int]$ReadyTimeoutSeconds = 30,
    [bool]$Required = $true
  )

  $pidFile = Join-Path $Script:YbmRunDir "$Name.pid"
  if (Test-YbmPidAlive $pidFile) {
    $existingPid = Get-Content -LiteralPath $pidFile
    return [pscustomobject]@{ Status = "ready"; Required = $Required; Detail = "already running (pid $existingPid)" }
  }

  Stop-YbmOrphansForName -Name $Name
  if ($ReadyUrl -and (Test-YbmHttpOk -Url $ReadyUrl -TimeoutSec 3)) {
    return [pscustomobject]@{ Status = "ready"; Required = $Required; Detail = "already running (external), reachable at $ReadyUrl" }
  }

  $out = Join-Path $Script:YbmLogDir "$Name.out.log"
  $err = Join-Path $Script:YbmLogDir "$Name.err.log"
  Remove-Item -LiteralPath (Join-Path $Script:YbmRunDir "$Name.status.json") -ErrorAction SilentlyContinue

  $supervisor = Join-Path $Script:YbmRoot "scripts\run_supervised.ps1"
  $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$supervisor`" -Name `"$Name`" -ScriptPath `"$ScriptPath`""
  $process = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $Script:YbmRoot `
    -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
  Set-Content -LiteralPath $pidFile -Value $process.Id

  # Truthful readiness: poll for a real signal instead of assuming success
  # the instant Start-Process returns (see docs/HISTORY.md P0).
  $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    if ($ReadyUrl -and (Test-YbmHttpOk -Url $ReadyUrl -TimeoutSec 2)) {
      return [pscustomobject]@{ Status = "ready"; Required = $Required; Detail = "reachable at $ReadyUrl (pid $($process.Id))" }
    }
    $status = Read-YbmServiceStatus -Name $Name
    if ($status -and $status.status -eq "failed") {
      return [pscustomobject]@{ Status = "failed"; Required = $Required; Detail = "$($status.message) - see .agent_control\logs\$Name.child.err.log" }
    }
    if (-not $ReadyUrl -and $status -and $status.status -eq "running") {
      return [pscustomobject]@{ Status = "running"; Required = $Required; Detail = "process running (pid $($status.child_pid))" }
    }
  }

  $status = Read-YbmServiceStatus -Name $Name
  if ($status -and $status.status -eq "failed") {
    return [pscustomobject]@{ Status = "failed"; Required = $Required; Detail = "$($status.message)" }
  }
  if ($ReadyUrl) {
    return [pscustomobject]@{ Status = "warn"; Required = $Required; Detail = "started (pid $($process.Id)) but not reachable at $ReadyUrl within ${ReadyTimeoutSeconds}s - check logs" }
  }
  return [pscustomobject]@{ Status = "running"; Required = $Required; Detail = "started (pid $($process.Id))" }
}

function Invoke-YbmStart {
  param([string[]]$Argv)

  $noTelegram = $Argv -contains "-NoTelegram"
  $noWhatsApp = $Argv -contains "-NoWhatsApp"
  $noWorker = $Argv -contains "-NoWorker"
  $noScheduler = $Argv -contains "-NoScheduler"
  $noLocalDeploy = $Argv -contains "-NoLocalDeploy"
  $skipDoctor = $Argv -contains "-SkipDoctor"
  # Only the installers pass this - never a bare `ybm.ps1 start`, which
  # would otherwise pop a nuisance browser tab on every dev restart.
  $openBrowser = $Argv -contains "-Open"

  if (-not $skipDoctor) {
    Write-Host "Preflight (ybm doctor)..."
    Write-Host ""
    Invoke-YbmDoctor
    if ($LASTEXITCODE -ne 0) {
      Write-Host ""
      Write-Host "Preflight failed. Fix the [FAIL] items above, run '.\scripts\ybm.ps1 setup', or pass -SkipDoctor to start anyway." -ForegroundColor Red
      exit 1
    }
    Write-Host ""
  }

  New-Item -ItemType Directory -Force -Path $Script:YbmRunDir, $Script:YbmLogDir | Out-Null

  $results = [ordered]@{}

  if (-not $noLocalDeploy) {
    $results["localdeploy"] = Start-YbmService -Name "localdeploy" -ScriptPath (Join-Path $Script:YbmRoot "scripts\services\run_localdeploy.ps1") `
      -ReadyUrl "http://127.0.0.1:8000/health" -ReadyTimeoutSeconds 30 -Required $false
  }

  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  & (Get-YbmPython) -m agent_control.cli init-db | Out-Null

  $results["backend"] = Start-YbmService -Name "backend" -ScriptPath (Join-Path $Script:YbmRoot "scripts\services\run_backend.ps1") `
    -ReadyUrl "http://127.0.0.1:8765/health" -ReadyTimeoutSeconds 45 -Required $true

  if (-not $noTelegram) {
    $results["telegram_polling"] = Start-YbmService -Name "telegram_polling" -ScriptPath (Join-Path $Script:YbmRoot "scripts\services\run_telegram_polling.ps1") -Required $true
  }
  if (-not $noWhatsApp) {
    # Gated on the config flag itself, unlike telegram_polling (always
    # attempted) - most installs never touch WhatsApp, and poll-whatsapp
    # refuses to start at all when disabled (cli.py's poll_whatsapp), so
    # attempting it anyway would crash-loop 4 times (~20s wasted) on every
    # single `ybm start`/`ybm run`, for every user, and leave a permanently
    # alarming "failed" status line for a feature nobody asked to run.
    # `channel-enabled` fails closed (exit 1 = treat as disabled) on a
    # broken config, matching build_service_specs()'s Python-path behavior.
    & (Get-YbmPython) -m agent_control.cli channel-enabled whatsapp | Out-Null
    if ($LASTEXITCODE -eq 0) {
      # Required $false even when enabled: a bridge hiccup (crash, not yet
      # linked) must not block the rest of the stack the way a required
      # service would.
      $results["whatsapp"] = Start-YbmService -Name "whatsapp" -ScriptPath (Join-Path $Script:YbmRoot "scripts\services\run_whatsapp.ps1") -Required $false
    }
  }
  if (-not $noWorker) {
    $results["worker"] = Start-YbmService -Name "worker" -ScriptPath (Join-Path $Script:YbmRoot "scripts\services\run_worker.ps1") -Required $true
    $results["coding_session_watcher"] = Start-YbmService -Name "coding_session_watcher" -ScriptPath (Join-Path $Script:YbmRoot "scripts\services\run_coding_session_watcher.ps1") -Required $true
  }
  if (-not $noScheduler) {
    $results["scheduler"] = Start-YbmService -Name "scheduler" -ScriptPath (Join-Path $Script:YbmRoot "scripts\services\run_scheduler.ps1") -Required $true
  }

  Write-Host ""
  Write-Host "Startup summary:"
  $width = ($results.Keys | Measure-Object -Property Length -Maximum).Maximum
  $hardFailure = $false
  foreach ($name in $results.Keys) {
    $r = $results[$name]
    $symbol = switch ($r.Status) {
      "ready" { "[OK]  " }
      "running" { "[OK]  " }
      "warn" { "[WARN]" }
      default { "[FAIL]" }
    }
    if ($r.Status -eq "failed" -and $r.Required) {
      $hardFailure = $true
    }
    Write-Host "$symbol $($name.PadRight($width))  $($r.Detail)"
  }
  Write-Host ""
  if ($hardFailure) {
    Write-Host "One or more required services failed to start. See .agent_control\logs, or run '.\scripts\ybm.ps1 logs <service>'." -ForegroundColor Red
    exit 1
  }
  $adminUrl = "http://127.0.0.1:8765/admin"
  Write-Host "Admin UI:        $adminUrl"
  Write-Host "Logs:            $Script:YbmLogDir"
  if ($openBrowser) {
    # Carries AGENT_ADMIN_TOKEN (if set) as a one-time ?token= URL param so
    # the browser's very first request is already authenticated - a fresh
    # install always generates a real token (bootstrap.run_setup), and
    # nothing else in the UI collects one from the user. lib/api.ts strips
    # it from the URL/history on load. Already loaded into $env: by this
    # script's own top-level Import-DotEnv call.
    $token = $env:AGENT_ADMIN_TOKEN
    $target = if ($token) { "${adminUrl}?token=$token" } else { $adminUrl }
    Start-Process $target
  }
}

function Invoke-YbmStop {
  if (-not (Test-Path -LiteralPath $Script:YbmRunDir)) {
    Write-Host "No stack pid directory found."
  } else {
    foreach ($pidFile in Get-ChildItem -LiteralPath $Script:YbmRunDir -Filter "*.pid") {
      $name = [System.IO.Path]::GetFileNameWithoutExtension($pidFile.Name)
      $stopFile = Join-Path $Script:YbmRunDir "$name.stop"
      New-Item -ItemType File -Force -Path $stopFile | Out-Null
      $processId = Get-Content -LiteralPath $pidFile.FullName -ErrorAction SilentlyContinue
      if ($processId) {
        $process = Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue
        if ($process) {
          Stop-YbmProcessTree -ProcessId $process.Id
          Write-Host "Stopped $name (pid $processId)"
        }
      }
      Remove-Item -LiteralPath $pidFile.FullName -Force
      Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    }
  }
  foreach ($name in $Script:YbmServiceOrder) {
    Stop-YbmOrphansForName -Name $name
  }
}

function Invoke-YbmStatus {
  Write-Host "Service".PadRight(24) "Status".PadRight(10) "Detail"
  foreach ($name in $Script:YbmServiceOrder) {
    $status = Read-YbmServiceStatus -Name $name
    if (-not $status) {
      Write-Host "$($name.PadRight(24)) $("not running".PadRight(10))"
      continue
    }
    # A status.json can outlive its process if the supervisor was killed
    # before it could write a final "stopped" state - verify the pid is
    # actually alive rather than trusting the file (see docs/HISTORY.md P6).
    $displayStatus = $status.status
    if ($status.child_pid -and $displayStatus -eq "running" -and -not (Get-Process -Id ([int]$status.child_pid) -ErrorAction SilentlyContinue)) {
      $displayStatus = "stale"
    }
    Write-Host "$($name.PadRight(24)) $($displayStatus.PadRight(10)) $($status.message) (pid $($status.child_pid), restarts $($status.restart_count))"
  }
  Write-Host ""
  foreach ($check in @(
    @{ Name = "LocalDeploy"; Url = "http://127.0.0.1:8000/health" },
    @{ Name = "Backend"; Url = "http://127.0.0.1:8765/health" },
    @{ Name = "Admin UI"; Url = "http://127.0.0.1:8765/admin" }
  )) {
    $ok = Test-YbmHttpOk -Url $check.Url -TimeoutSec 3
    $mark = if ($ok) { "[OK]  " } else { "[DOWN]" }
    Write-Host "$mark $($check.Name.PadRight(14)) $($check.Url)"
  }
}

function Invoke-YbmLogs {
  param([string]$Service, [string[]]$Argv)
  if (-not $Service) {
    Write-Host "usage: ybm logs <service> [-Follow] [-Tail N]"
    Write-Host "services: $($Script:YbmServiceOrder -join ', ')"
    return
  }
  $follow = $Argv -contains "-Follow"
  $tail = 40
  for ($i = 0; $i -lt $Argv.Count; $i++) {
    if ($Argv[$i] -eq "-Tail" -and $i + 1 -lt $Argv.Count) {
      $tail = [int]$Argv[$i + 1]
    }
  }
  $childOut = Join-Path $Script:YbmLogDir "$Service.child.out.log"
  $childErr = Join-Path $Script:YbmLogDir "$Service.child.err.log"
  $out = if (Test-Path -LiteralPath $childOut) { $childOut } else { Join-Path $Script:YbmLogDir "$Service.out.log" }
  $err = if (Test-Path -LiteralPath $childErr) { $childErr } else { Join-Path $Script:YbmLogDir "$Service.err.log" }
  if (-not (Test-Path -LiteralPath $out) -and -not (Test-Path -LiteralPath $err)) {
    Write-Host "No logs found for '$Service' yet."
    return
  }
  Write-Host "=== $out ==="
  if (Test-Path -LiteralPath $out) {
    Get-Content -LiteralPath $out -Tail $tail -Wait:$follow
  }
  if (-not $follow -and (Test-Path -LiteralPath $err)) {
    $errContent = Get-Content -LiteralPath $err -Tail $tail -ErrorAction SilentlyContinue
    if ($errContent) {
      Write-Host ""
      Write-Host "=== $err ==="
      $errContent | Write-Host
    }
  }
}

function Invoke-YbmTest {
  param([string[]]$Argv)
  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  # The wrapper imports the repo's .env at startup for normal operator
  # commands. The unit suite creates isolated TestClient instances that do
  # not send the real local admin token, so carrying it into pytest turns
  # otherwise-valid admin API tests into blanket 401s. Run from backend as
  # the documented Python workflow does, so read_env_value() cannot fall
  # back to the repo-root .env after the process variable is removed.
  # Individual auth tests set their own token explicitly with monkeypatch.
  $savedAdminToken = $env:AGENT_ADMIN_TOKEN
  Remove-Item Env:AGENT_ADMIN_TOKEN -ErrorAction SilentlyContinue
  Push-Location (Join-Path $Script:YbmRoot "backend")
  try {
    & (Get-YbmPython) -m pytest "tests" @Argv
    $testExitCode = $LASTEXITCODE
  } finally {
    Pop-Location
    if ($null -ne $savedAdminToken) {
      $env:AGENT_ADMIN_TOKEN = $savedAdminToken
    }
  }
  exit $testExitCode
}

function Invoke-YbmE2e {
  param([string[]]$Argv)
  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  & (Get-YbmPython) "$Script:YbmRoot\scripts\run_all_e2e_tests.py" @Argv
  exit $LASTEXITCODE
}

function Invoke-YbmDb {
  param([string]$Sub, [string[]]$Argv)
  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  switch ($Sub) {
    "inspect" { & (Get-YbmPython) -m agent_control.cli db-inspect }
    "clean" {
      $days = 30
      for ($i = 0; $i -lt $Argv.Count; $i++) {
        if ($Argv[$i] -eq "--days" -and $i + 1 -lt $Argv.Count) { $days = $Argv[$i + 1] }
      }
      & (Get-YbmPython) -m agent_control.cli db-clean --days $days
    }
    "reset" {
      $pyArgs = @("-m", "agent_control.cli", "db-reset")
      if ($Argv -contains "--yes") { $pyArgs += "--yes" }
      & (Get-YbmPython) @pyArgs
    }
    default {
      Write-Host "usage: ybm db inspect | clean [--days N] | reset [--yes]"
      exit 1
    }
  }
  exit $LASTEXITCODE
}

function Invoke-YbmConfig {
  param([string]$Sub, [string[]]$Argv)
  $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
  switch ($Sub) {
    "show" { & (Get-YbmPython) -m agent_control.cli config-summary }
    "set" {
      if ($Argv.Count -lt 2) {
        Write-Host "usage: ybm config set <dotted.path> <value>"
        exit 1
      }
      & (Get-YbmPython) -m agent_control.cli config-set $Argv[0] $Argv[1]
    }
    default {
      Write-Host "usage: ybm config show | set <dotted.path> <value>"
      exit 1
    }
  }
  exit $LASTEXITCODE
}

switch ($Command) {
  "help" { Show-YbmHelp }
  "setup" { Invoke-YbmSetup -Argv (@($Sub) + $Rest | Where-Object { $_ }); Complete-Install; exit $LASTEXITCODE }
  "run" { Invoke-YbmRun -Argv (@($Sub) + $Rest | Where-Object { $_ }) }
  "doctor" { Invoke-YbmDoctor; exit $LASTEXITCODE }
  "start" { Invoke-YbmStart -Argv (@($Sub) + $Rest | Where-Object { $_ }) }
  "stop" { Invoke-YbmStop }
  "restart" {
    $restartArgv = @($Sub) + $Rest | Where-Object { $_ }
    Invoke-YbmStop
    Start-Sleep -Seconds 2
    Invoke-YbmStart -Argv $restartArgv
  }
  "status" { Invoke-YbmStatus }
  "ui-build" { Invoke-YbmUi -Mode "ui-build" }
  "ui-dev" { Invoke-YbmUi -Mode "ui-dev" }
  "logs" { Invoke-YbmLogs -Service $Sub -Argv $Rest }
  "test" { Invoke-YbmTest -Argv (@($Sub) + $Rest | Where-Object { $_ }) }
  "e2e" { Invoke-YbmE2e -Argv (@($Sub) + $Rest | Where-Object { $_ }) }
  "db" { Invoke-YbmDb -Sub $Sub -Argv $Rest }
  "config" { Invoke-YbmConfig -Sub $Sub -Argv $Rest }
  "clean" {
    # clean_agent_control.ps1's params are all [switch]. Splatting an ARRAY of
    # "-Caches"-shaped strings does NOT re-parse them as flags - PowerShell
    # passes each element positionally instead, and every param here is a
    # switch with no positional slot, so every flag was silently dropped
    # ("ybm clean -Caches" printed the "choose at least one switch" usage
    # message no matter what flag was passed). Splatting a HASHTABLE does
    # bind correctly to named/switch parameters, so build one from the raw
    # "-Name" tokens instead.
    $cleanSwitches = @{}
    foreach ($arg in (@($Sub) + $Rest | Where-Object { $_ })) {
      if ($arg -match '^-(\w+)$') {
        $cleanSwitches[$matches[1]] = $true
      }
    }
    & "$Script:YbmRoot\scripts\clean_agent_control.ps1" @cleanSwitches
    exit $LASTEXITCODE
  }
  "e2e-login" {
    # Was a separate scripts/login_telegram_e2e.ps1 whose only job was to
    # prompt for two values and shell out; it also called bare `python`
    # rather than the venv interpreter, so it used whatever Python happened
    # to be on PATH (usually not the one with telethon installed).
    $apiId = $env:TELEGRAM_API_ID
    $apiHash = $env:TELEGRAM_API_HASH
    $session = if ($Sub) { $Sub } else { ".agent_control/telegram_e2e_user" }
    if (-not $apiId) { $apiId = Read-Host "Telegram API ID" }
    if (-not $apiHash) { $apiHash = Read-Host "Telegram API Hash" }
    if (-not $apiId -or -not $apiHash) {
      Write-Host "TELEGRAM_API_ID and TELEGRAM_API_HASH are required (get them from https://my.telegram.org)."
      exit 1
    }
    Set-Location $Script:YbmRoot
    & (Get-YbmPython) "$Script:YbmRoot\e2e\telegram_login.py" `
      --api-id $apiId --api-hash $apiHash --session $session
    exit $LASTEXITCODE
  }
  "send" {
    if (-not $Sub) {
      Write-Host 'usage: ybm send "<message>"'
      exit 1
    }
    $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
    & (Get-YbmPython) "$Script:YbmRoot\scripts\test_e2e.py" $Sub
    exit $LASTEXITCODE
  }
  "package-extension" {
    & "$Script:YbmRoot\scripts\package_vscode_extension.ps1"
    exit $LASTEXITCODE
  }
  "tray" {
    & (Get-YbmPython) "$Script:YbmRoot\scripts\tray_app.py"
    exit $LASTEXITCODE
  }
  "autostart" {
    Invoke-YbmAutostart -Sub $Sub
  }
  "backup" {
    $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
    & (Get-YbmPython) -m agent_control.cli backup @Rest
    exit $LASTEXITCODE
  }
  "check-updates" {
    $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
    & (Get-YbmPython) -m agent_control.cli check-updates
    exit $LASTEXITCODE
  }
  "trace" {
    if (-not $Sub) {
      Write-Host 'usage: ybm trace <task_id> [--json]'
      exit 1
    }
    $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
    & (Get-YbmPython) -m agent_control.cli trace-task $Sub @Rest
    exit $LASTEXITCODE
  }
  "scenario" {
    if ($Sub -ne "record" -or $Rest.Count -lt 1) {
      Write-Host 'usage: ybm scenario record <name> [--profile <name>]'
      exit 1
    }
    & (Get-YbmPython) "$Script:YbmRoot\backend\tests\scenario\record.py" @Rest
    exit $LASTEXITCODE
  }
  # Frontend build/dev live in the Python CLI, but AGENTS.md and README both
  # document them as `ybm ui-build` / `ybm ui-dev` while pointing developers at
  # this script as the interface. Without these branches the documented command
  # failed here on the ValidateSet and only worked via the console script.
  "ui-build" {
    $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
    & (Get-YbmPython) -m agent_control.cli ui-build @(@($Sub) + $Rest | Where-Object { $_ })
    exit $LASTEXITCODE
  }
  "ui-dev" {
    $env:PYTHONPATH = "$Script:YbmRoot\backend\src"
    & (Get-YbmPython) -m agent_control.cli ui-dev @(@($Sub) + $Rest | Where-Object { $_ })
    exit $LASTEXITCODE
  }
  default { Show-YbmHelp }
}
