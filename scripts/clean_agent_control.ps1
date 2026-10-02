param(
    [switch]$Caches,
    [switch]$Workspaces,
    [switch]$AdapterProposals,
    [switch]$AllGenerated
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path "$PSScriptRoot\.."
$AgentControl = Resolve-Path -LiteralPath (Join-Path $Root ".agent_control") -ErrorAction SilentlyContinue
if (-not $AgentControl) {
    Write-Host ".agent_control does not exist."
    exit 0
}

$targets = New-Object System.Collections.Generic.List[string]
if ($Caches -or $AllGenerated) {
    $targets.Add("browser\screenshots")
    $targets.Add("browser\chrome-profile\Default\Cache")
    $targets.Add("browser\chrome-profile\Default\Code Cache")
    $targets.Add("computer_use\screenshots")
    $targets.Add("code_interpreter")
    $targets.Add("mcp")
    $targets.Add("e2e_results")
    $targets.Add("e2e_fixtures")
    # e2e_runs / live_e2e_runs are dead names from a previous e2e runner
    # version - nothing in the current codebase writes or reads them
    # (docs/archive/HISTORY.md §1.4), but clean up any that reappear regardless.
    $targets.Add("e2e_runs")
    $targets.Add("live_e2e_runs")
}
if ($Workspaces -or $AllGenerated) {
    $targets.Add("workspaces")
    $targets.Add("coding_sessions")
}
if ($AdapterProposals -or $AllGenerated) {
    $targets.Add("adapters")
}

if ($targets.Count -eq 0) {
    Write-Host "Choose at least one switch: -Caches, -Workspaces, -AdapterProposals, or -AllGenerated."
    exit 0
}

$agentPath = [System.IO.Path]::GetFullPath($AgentControl.Path)
foreach ($relative in $targets) {
    $target = Join-Path $agentPath $relative
    $full = [System.IO.Path]::GetFullPath($target)
    if (-not $full.StartsWith($agentPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside .agent_control: $full"
    }
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
        Write-Host "Removed $full"
    }
}
