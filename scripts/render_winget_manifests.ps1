# Fill the winget manifest templates for a published release.
#
# The SHA256 is taken from the actual published installer, not from a build log:
# a manifest whose hash does not match what users download fails validation, and
# transcribing the number by hand is exactly how that happens.
#
#   .\scripts\render_winget_manifests.ps1 -Version 0.1.0
#
# Writes dist\winget\manifests\o\oney-erge\YBM\<version>\, which is the layout
# microsoft/winget-pkgs expects. See packaging/winget/README.md.

[CmdletBinding()]
param(
    # Released version, no "v" prefix. The tag v<Version> must already exist.
    [Parameter(Mandatory = $true)]
    [string]$Version,
    # Use a local installer instead of downloading the published one. For
    # rehearsing the rendering before a release exists.
    [string]$InstallerPath,
    [string]$OutputRoot = "dist\winget"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$TemplateDir = Join-Path $RepoRoot "packaging\winget"
if (-not [IO.Path]::IsPathRooted($OutputRoot)) { $OutputRoot = Join-Path $RepoRoot $OutputRoot }

$InstallerUrl = "https://github.com/oney-erge/YBM/releases/download/v$Version/YBM-Setup.msi"

if ($InstallerPath) {
    if (-not (Test-Path -LiteralPath $InstallerPath)) { throw "no installer at $InstallerPath" }
    $localInstaller = (Resolve-Path $InstallerPath).Path
    Write-Host "Hashing local installer: $localInstaller" -ForegroundColor Cyan
    Write-Host "  (the rendered manifest still points at $InstallerUrl)" -ForegroundColor DarkGray
} else {
    $localInstaller = Join-Path ([IO.Path]::GetTempPath()) "YBM-Setup-$Version.msi"
    Write-Host "Downloading $InstallerUrl" -ForegroundColor Cyan
    try {
        Invoke-WebRequest -Uri $InstallerUrl -OutFile $localInstaller -UseBasicParsing
    } catch {
        throw "could not download the installer for v$Version ($($_.Exception.Message)). Publish the release first, or pass -InstallerPath to rehearse."
    }
}

$sha = (Get-FileHash -LiteralPath $localInstaller -Algorithm SHA256).Hash.ToUpperInvariant()
# winget wants a plain calendar date for the release, not a timestamp.
$releaseDate = (Get-Date).ToString("yyyy-MM-dd")

$targetDir = Join-Path $OutputRoot "manifests\o\oney-erge\YBM\$Version"
if (Test-Path -LiteralPath $targetDir) { Remove-Item -LiteralPath $targetDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

foreach ($template in Get-ChildItem -LiteralPath $TemplateDir -Filter "oney-erge.YBM*.yaml") {
    $text = Get-Content -LiteralPath $template.FullName -Raw
    $text = $text.Replace("__VERSION__", $Version).
                  Replace("__SHA256__", $sha).
                  Replace("__RELEASE_DATE__", $releaseDate)
    # Copied through verbatim, comments included: the first line is the
    # `yaml-language-server` schema header, and `winget validate` warns when a
    # manifest is missing it.
    Set-Content -LiteralPath (Join-Path $targetDir $template.Name) -Value $text -Encoding utf8 -NoNewline
    Write-Host "  wrote $($template.Name)" -ForegroundColor DarkGray
}

if (-not $InstallerPath) { Remove-Item -LiteralPath $localInstaller -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "Manifests: $targetDir" -ForegroundColor Green
Write-Host "  SHA256 $sha" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  winget validate --manifest `"$targetDir`""
Write-Host "  then open a PR against https://github.com/microsoft/winget-pkgs"
