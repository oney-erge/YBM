param(
  [Parameter(Mandatory = $true)][string]$Name,
  [Parameter(Mandatory = $true)][string]$ScriptPath,
  [int]$RestartDelaySeconds = 5,
  # Crash-loop breaker: if the child exits within $MinHealthySeconds more than
  # $CrashLoopMaxRestarts times inside a $CrashLoopWindowSeconds window, stop
  # restarting and mark the service "failed" instead of looping forever.
  # Without this a missing package produces a silent infinite restart loop
  # behind a green `ybm start` (see docs/archive/HISTORY.md P0).
  [int]$CrashLoopWindowSeconds = 60,
  [int]$CrashLoopMaxRestarts = 3,
  [int]$MinHealthySeconds = 20
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path "$PSScriptRoot\.."
$RunDir = Join-Path $Root ".agent_control\run"
$LogDir = Join-Path $Root ".agent_control\logs"
New-Item -ItemType Directory -Force -Path $RunDir, $LogDir | Out-Null

$StatusFile = Join-Path $RunDir "$Name.status.json"
$StopFile = Join-Path $RunDir "$Name.stop"
$RestartCount = 0
$RecentCrashTimes = New-Object System.Collections.Generic.List[datetime]

function Write-ServiceStatus {
  param(
    [string]$Status,
    [Nullable[int]]$ChildPid,
    [Nullable[int]]$LastExitCode,
    [string]$Message
  )
  $payload = [ordered]@{
    name = $Name
    status = $Status
    supervisor_pid = $PID
    child_pid = $ChildPid
    restart_count = $RestartCount
    last_exit_code = $LastExitCode
    message = $Message
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
  }
  $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatusFile -Encoding UTF8
}

function Get-LastLogLines {
  param([string]$Path, [int]$Count = 20)
  if (-not (Test-Path -LiteralPath $Path)) {
    return @()
  }
  return Get-Content -LiteralPath $Path -Tail $Count -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $StopFile) {
  Remove-Item -LiteralPath $StopFile -Force
}

Write-ServiceStatus -Status "starting" -ChildPid $null -LastExitCode $null -Message "supervisor starting"

while (-not (Test-Path -LiteralPath $StopFile)) {
  $RestartCount += 1
  $childOut = Join-Path $LogDir "$Name.child.out.log"
  $childErr = Join-Path $LogDir "$Name.child.err.log"
  $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
  Write-ServiceStatus -Status "starting" -ChildPid $null -LastExitCode $null -Message "starting child process"
  $startedAt = Get-Date
  $child = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList $arguments `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $childOut `
    -RedirectStandardError $childErr `
    -PassThru

  Write-ServiceStatus -Status "running" -ChildPid $child.Id -LastExitCode $null -Message "child process running"
  while (-not $child.HasExited -and -not (Test-Path -LiteralPath $StopFile)) {
    Start-Sleep -Seconds 5
    $child.Refresh()
    Write-ServiceStatus -Status "running" -ChildPid $child.Id -LastExitCode $null -Message "child process running"
  }

  if (-not $child.HasExited) {
    Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
    Write-ServiceStatus -Status "stopped" -ChildPid $child.Id -LastExitCode $null -Message "stop requested"
    break
  }

  $ranSeconds = ((Get-Date) - $startedAt).TotalSeconds
  if ($ranSeconds -lt $MinHealthySeconds) {
    $RecentCrashTimes.Add((Get-Date)) | Out-Null
    $cutoff = (Get-Date).AddSeconds(-$CrashLoopWindowSeconds)
    $kept = @($RecentCrashTimes | Where-Object { $_ -ge $cutoff })
    $RecentCrashTimes = New-Object System.Collections.Generic.List[datetime]
    foreach ($t in $kept) { $RecentCrashTimes.Add($t) | Out-Null }
  } else {
    $RecentCrashTimes.Clear()
  }

  if ($RecentCrashTimes.Count -gt $CrashLoopMaxRestarts) {
    $tail = Get-LastLogLines -Path $childErr -Count 20
    if (-not $tail) {
      $tail = Get-LastLogLines -Path $childOut -Count 20
    }
    $message = "crash-loop detected: $($RecentCrashTimes.Count) restarts within ${CrashLoopWindowSeconds}s - giving up"
    Write-ServiceStatus -Status "failed" -ChildPid $child.Id -LastExitCode $child.ExitCode -Message $message
    Write-Host ""
    Write-Host "[$Name] $message" -ForegroundColor Red
    foreach ($line in $tail) {
      Write-Host "[$Name]   $line" -ForegroundColor DarkRed
    }
    Write-Host "[$Name] full output: $childOut / $childErr" -ForegroundColor Red
    exit 1
  }

  Write-ServiceStatus -Status "exited" -ChildPid $child.Id -LastExitCode $child.ExitCode -Message "child exited after $([int]$ranSeconds)s; restarting"
  Start-Sleep -Seconds $RestartDelaySeconds
}

Write-ServiceStatus -Status "stopped" -ChildPid $null -LastExitCode $null -Message "supervisor stopped"
