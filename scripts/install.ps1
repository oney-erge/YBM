# One-command bootstrap for YBM on Windows:
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/oney-erge/YBM/main/scripts/install.ps1 | iex"
#
# With no terminal, download Install-YBM.bat from the latest release and
# double-click it. That wrapper downloads this script, verifies the release,
# starts YBM, and leaves readable feedback on screen if a step fails.
#
# Git, Python, Node.js, and uv do not need to be preinstalled. This downloads
# the latest release archive, which contains the prebuilt admin console, then
# lets YBM's launcher provide Python and the runtime environment.
#
# Everything after getting the code onto the machine is `scripts\ybm.ps1 run`:
# venv/dependency setup, config.yaml, admin/vault tokens, the update check,
# starting the stack, and opening the admin console. The LLM/Telegram choice
# happens in that browser (the first-run wizard), not in this terminal.
#
# Every launch after this is YBM.bat (double-click) or `ybm run`, both
# idempotent. Re-running this bootstrap refreshes release files without
# deleting local state.

[CmdletBinding()]
param(
    # Print what would happen and change nothing. The cheapest way to check an
    # installer change without a clean VM.
    [switch]$DryRun,
    # Reserved for future prompts. Accepted now so scripted callers and CI can
    # pass it unconditionally; nothing on this path currently blocks on input.
    [switch]$NoPrompt,
    # After installing, prove it works: backend health plus `ybm doctor`.
    [switch]$Verify,
    # Where to install. Also honoured via $env:YBM_INSTALL_DIR.
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"

$ReleaseZipUrl = "https://github.com/oney-erge/YBM/releases/latest/download/YBM-windows.zip"
$ChecksumsUrl = "https://github.com/oney-erge/YBM/releases/latest/download/SHA256SUMS.txt"

if (-not $InstallDir) {
    $InstallDir = if ($env:YBM_INSTALL_DIR) { $env:YBM_INSTALL_DIR } else { Join-Path $HOME "ybm" }
}
if ($env:YBM_NO_PROMPT -eq "1") { $NoPrompt = $true }
if ($env:YBM_DRY_RUN -eq "1")   { $DryRun = $true }

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Write-Good($msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Plan($msg) { Write-Host "[dry-run] $msg" -ForegroundColor Yellow }

function Fail($msg, $hint) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    if ($hint) { Write-Host "  $hint" -ForegroundColor Yellow }
    exit 1
}

# --- 1. Get the complete release -----------------------------------------
# Source archives intentionally omit the generated admin console. The public
# installer therefore downloads the release archive, not the main branch.
# That keeps Node.js a contributor dependency instead of an end-user one.
$inRepo = (Test-Path "backend\pyproject.toml") -and (Test-Path "AGENTS.md") -and (Test-Path "scripts\ybm.ps1")

if ($inRepo) {
    $RepoDir = (Get-Location).Path
    Write-Step "Already inside a YBM checkout"
    Write-Info $RepoDir
} elseif ($DryRun) {
    $RepoDir = $InstallDir
    Write-Plan "would download and extract $ReleaseZipUrl into $InstallDir"
} else {
    Write-Step "Downloading the latest YBM release"
    $tempZip = Join-Path ([IO.Path]::GetTempPath()) "ybm-$([guid]::NewGuid().ToString('N')).zip"
    $tempSums = Join-Path ([IO.Path]::GetTempPath()) "ybm-$([guid]::NewGuid().ToString('N'))-SHA256SUMS.txt"
    $tempDir = Join-Path ([IO.Path]::GetTempPath()) "ybm-$([guid]::NewGuid().ToString('N'))"
    try {
        Invoke-WebRequest -Uri $ReleaseZipUrl -OutFile $tempZip -UseBasicParsing
        Write-Step "Verifying the downloaded release"
        Invoke-WebRequest -Uri $ChecksumsUrl -OutFile $tempSums -UseBasicParsing
        $checksumLine = Get-Content -LiteralPath $tempSums | Where-Object {
            $_ -match '^[0-9A-Fa-f]{64}\s+(YBM-windows\.zip|YBM-[^\s]+-windows\.zip)$'
        } | Select-Object -First 1
        if (-not $checksumLine) {
            Fail "the release checksum file has no Windows archive entry" `
                 "Open https://github.com/oney-erge/YBM/releases/latest and report the broken release."
        }
        $expectedHash = ($checksumLine -split '\s+')[0].ToUpperInvariant()
        $actualHash = (Get-FileHash -LiteralPath $tempZip -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) {
            Fail "the downloaded release failed its SHA256 check" `
                 "Delete the download and try again. Expected $expectedHash but received $actualHash."
        }
        Write-Good "release checksum matches"
        Expand-Archive -LiteralPath $tempZip -DestinationPath $tempDir -Force
        $extracted = Get-ChildItem $tempDir -Directory | Select-Object -First 1
        if (-not $extracted) { Fail "the downloaded release was empty" "Re-run the installer." }
        New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir -Parent) | Out-Null
        if (Test-Path -LiteralPath $InstallDir) {
            Write-Info "refreshing the existing install and preserving local state"
            Get-ChildItem -LiteralPath $extracted.FullName -Force | ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
            }
        } else {
            Move-Item -LiteralPath $extracted.FullName -Destination $InstallDir
        }
    } catch {
        $status = if ($_.Exception.Response) { $_.Exception.Response.StatusCode.value__ } else { $null }
        if ($status -eq 404) {
            Fail "the latest release archive is not available (HTTP 404)" `
                 "Open https://github.com/oney-erge/YBM/releases/latest and check that YBM-windows.zip exists."
        }
        Fail "could not download the latest YBM release ($($_.Exception.Message))" `
             "Check your internet connection and re-run."
    } finally {
        Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
        Remove-Item $tempSums -Force -ErrorAction SilentlyContinue
        Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    $RepoDir = $InstallDir
    Write-Good "ready at $InstallDir"
}

# --- 2. Hand off to ybm.ps1 ----------------------------------------------
if ($DryRun) {
    Write-Plan "would run: $RepoDir\scripts\ybm.ps1 run"
    if ($Verify) { Write-Plan "would then run: ybm.ps1 doctor (--Verify)" }
    Write-Host ""
    Write-Host "Dry run complete - nothing was installed or changed." -ForegroundColor Yellow
    exit 0
}

Set-Location $RepoDir
Write-Step "Installing dependencies and starting YBM"
# `run` is already non-interactive - it is the double-click path - so -NoPrompt
# has nothing to suppress here and is not forwarded.
& "$RepoDir\scripts\ybm.ps1" run
if ($LASTEXITCODE -ne 0) {
    Fail "startup failed (exit $LASTEXITCODE)" `
         "Run '$RepoDir\scripts\ybm.ps1 doctor' to diagnose. Logs: $RepoDir\.agent_control\logs"
}

# --- 3. Optional post-install proof --------------------------------------
if ($Verify) {
    Write-Step "Verifying the install"
    & "$RepoDir\scripts\ybm.ps1" doctor
    if ($LASTEXITCODE -ne 0) {
        Fail "post-install verification failed" `
             "The stack installed but doctor reported problems - see the [FAIL] lines above."
    }
    Write-Good "verified"
}

Write-Host ""
Write-Host "Pick a model and (optionally) Telegram in the admin console that just opened." -ForegroundColor Cyan
Write-Host "Next time, just double-click YBM.bat in $RepoDir - no terminal needed." -ForegroundColor Cyan
