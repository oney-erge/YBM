@echo off
title YBM
rem Windows PowerShell 5.1 started from a PowerShell 7 terminal inherits that
rem terminal's module path, and Get-FileHash and friends stop resolving. Clear
rem it so powershell.exe computes its own.
set "PSModulePath="
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 (
  echo.
  echo YBM did not start. Review the error above.
  pause
)
