param(
  [ValidateSet("run", "doctor", "repair", "docker", "stop", "logs")]
  [string]$Action = "run",
  [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 started from a PowerShell 7 terminal inherits that
# terminal's PSModulePath, whose PowerShell 7 entries shadow the built-in
# modules and make cmdlets such as Get-FileHash disappear mid-install. Drop
# those entries so 5.1 resolves its own. A no-op everywhere else.
if ($PSVersionTable.PSEdition -eq "Desktop" -and $env:PSModulePath) {
  # -like, not -match: backslashes are literal in a wildcard, so there is nothing to escape.
  $env:PSModulePath = (($env:PSModulePath -split ";") | Where-Object {
    $_ -and $_ -notlike "*\PowerShell\Modules" -and $_ -notlike "*\PowerShell\7\Modules"
  }) -join ";"
}

Set-Location $PSScriptRoot
. .\scripts\install-utils.ps1
Initialize-Install -RepositoryRoot $PSScriptRoot -ProductName "YBM"
trap { Write-InstallFailure $_; Exit-InstallLock; exit 1 }
$url = "http://127.0.0.1:8765"

# Windows PowerShell 5.1 turns any stderr line from a native command into a
# terminating error under $ErrorActionPreference = "Stop", even when it is
# redirected. Docker Desktop installed but not running writes exactly that, so
# these probes ran with the preference relaxed; before, `run.bat stop` crashed
# with a raw Docker API message on any machine where Docker was merely stopped.
function Test-DockerEngine {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  } finally {
    $ErrorActionPreference = $previous
  }
}
function Test-DockerRunning {
  if (-not (Test-DockerEngine)) { return $false }
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $container = docker compose ps --quiet ybm 2>$null
    return [bool]$container
  } catch {
    return $false
  } finally {
    $ErrorActionPreference = $previous
  }
}
function Invoke-DockerCompose {
  param([string[]]$ComposeArgs)
  # Compose writes build and pull progress to stderr, which 5.1 would treat as a
  # failure of a command that succeeded; $LASTEXITCODE is the real result.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & docker compose @ComposeArgs } finally { $ErrorActionPreference = $previous }
}
function Wait-Ready {
  for ($i = 0; $i -lt 180; $i++) {
    try { Invoke-RestMethod -Uri "$url/health" -TimeoutSec 2 | Out-Null; return $true } catch { Start-Sleep -Seconds 1 }
  }
  return $false
}

if ($Action -eq "docker") {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker is not installed." }
  if (-not (Test-DockerEngine)) { throw "Docker is installed but its engine is not running. Start Docker Desktop and try again." }
  Enter-InstallLock
  Assert-InstallFreeSpace -Path $PSScriptRoot -RequiredGB 3
  # No .env step: the container generates its own admin token and keeps it in its
  # state volume (scripts/docker-entrypoint.sh). Ask it for a signed-in link
  # rather than leaving the browser to prompt for a token.
  Invoke-DockerCompose @("up", "--detach", "--build")
  if ($LASTEXITCODE -ne 0) { throw "Docker Compose build or startup failed." }
  if (-not (Wait-Ready)) { Invoke-DockerCompose @("logs", "ybm"); throw "YBM did not become ready at $url." }
  Complete-Install
  Write-Host "YBM is ready at $url/admin" -ForegroundColor Green
  if (-not $NoBrowser) {
    $link = $null
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $link = (docker compose exec -T ybm ybm admin-url 2>$null | Select-Object -First 1) } catch { $link = $null } finally { $ErrorActionPreference = $previous }
    if ($link) { $link = "$link".Trim() }
    Start-Process $(if ($link) { $link } else { "$url/admin" })
  }
  exit 0
}
if ($Action -eq "stop") {
  if (Test-DockerRunning) { Invoke-DockerCompose @("down"); exit $LASTEXITCODE }
  & .\scripts\ybm.ps1 stop; exit $LASTEXITCODE
}
if ($Action -eq "logs") {
  if (Test-DockerRunning) { Invoke-DockerCompose @("logs", "--follow"); exit $LASTEXITCODE }
  & .\scripts\ybm.ps1 logs backend -Follow; exit $LASTEXITCODE
}
if ($Action -eq "doctor") { & .\scripts\ybm.ps1 doctor; exit $LASTEXITCODE }
if ($Action -eq "repair") {
  Remove-Item -LiteralPath .\backend\.venv\.ybm_sync_fingerprint -Force -ErrorAction SilentlyContinue
}
# Two literal calls, not @($(if ...)) as one argument: that expression hands
# ybm.ps1 an array (empty when -NoBrowser is off) for its [string]$Sub
# parameter, which PowerShell 5.1 and 7 both reject with "Cannot convert value
# to type System.String", so every default launch failed before doing anything.
if ($NoBrowser) { & .\scripts\ybm.ps1 run -NoBrowser } else { & .\scripts\ybm.ps1 run }
exit $LASTEXITCODE
