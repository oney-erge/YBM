param(
  [ValidateSet("run", "doctor", "repair", "docker", "stop", "logs")]
  [string]$Action = "run",
  [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. .\scripts\install-utils.ps1
Initialize-Install -RepositoryRoot $PSScriptRoot -ProductName "YBM"
trap { Write-InstallFailure $_; Exit-InstallLock; exit 1 }
$url = "http://127.0.0.1:8765"

function Test-DockerRunning {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
  docker info *> $null
  if ($LASTEXITCODE -ne 0) { return $false }
  $container = docker compose ps --quiet ybm 2>$null
  return [bool]$container
}
function Wait-Ready {
  for ($i = 0; $i -lt 180; $i++) {
    try { Invoke-RestMethod -Uri "$url/health" -TimeoutSec 2 | Out-Null; return $true } catch { Start-Sleep -Seconds 1 }
  }
  return $false
}
function Initialize-Environment {
  if (Test-Path -LiteralPath .\.env) { return }
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
  $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
  $found = $false
  $lines = foreach ($line in Get-Content -LiteralPath .\.env.example) {
    if ($line -match '^AGENT_ADMIN_TOKEN=') { $found = $true; "AGENT_ADMIN_TOKEN=$token" } else { $line }
  }
  if (-not $found) { $lines += "AGENT_ADMIN_TOKEN=$token" }
  $path = Join-Path $PSScriptRoot ".env"
  [System.IO.File]::WriteAllLines($path, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
  Write-Host "Created .env with a unique local admin token."
}

if ($Action -eq "docker") {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker is not installed." }
  docker info *> $null
  if ($LASTEXITCODE -ne 0) { throw "Docker is installed but its engine is not running." }
  Enter-InstallLock
  Assert-InstallFreeSpace -Path $PSScriptRoot -RequiredGB 3
  Initialize-Environment
  docker compose up --detach --build
  if ($LASTEXITCODE -ne 0) { throw "Docker Compose build or startup failed." }
  if (-not (Wait-Ready)) { docker compose logs ybm; throw "YBM did not become ready at $url." }
  Complete-Install
  Write-Host "YBM is ready at $url/admin" -ForegroundColor Green
  if (-not $NoBrowser) { Start-Process "$url/admin" }
  exit 0
}
if ($Action -eq "stop") {
  if (Test-DockerRunning) { docker compose down; exit $LASTEXITCODE }
  & .\scripts\ybm.ps1 stop; exit $LASTEXITCODE
}
if ($Action -eq "logs") {
  if (Test-DockerRunning) { docker compose logs --follow; exit $LASTEXITCODE }
  & .\scripts\ybm.ps1 logs backend -Follow; exit $LASTEXITCODE
}
if ($Action -eq "doctor") { & .\scripts\ybm.ps1 doctor; exit $LASTEXITCODE }
if ($Action -eq "repair") {
  Remove-Item -LiteralPath .\backend\.venv\.ybm_sync_fingerprint -Force -ErrorAction SilentlyContinue
}
& .\scripts\ybm.ps1 run @($(if ($NoBrowser) { "-NoBrowser" }))
exit $LASTEXITCODE
