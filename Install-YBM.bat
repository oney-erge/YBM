@echo off
setlocal EnableExtensions
title Install YBM

echo.
echo YBM installer
echo =============
echo.
echo [1/3] Getting the installer...

where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo.
  echo ERROR: Windows PowerShell is not available on this machine.
  echo Install PowerShell or use YBM-Setup.msi from the YBM release page.
  goto :failed
)

set "INSTALLER_FILE=%TEMP%\Install-YBM-%RANDOM%-%RANDOM%.ps1"
set "INSTALLER_URL=https://github.com/oney-erge/YBM/releases/latest/download/Install-YBM.ps1"
if defined YBM_INSTALLER_PS1_PATH (
  copy /y "%YBM_INSTALLER_PS1_PATH%" "%INSTALLER_FILE%" >nul
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '%INSTALLER_URL%' -OutFile '%INSTALLER_FILE%'"
)

if errorlevel 1 goto :download_failed
if not exist "%INSTALLER_FILE%" goto :download_failed

echo [2/3] Installing YBM and starting it...
echo       First setup usually takes 2-5 minutes. Keep this window open.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER_FILE%" -Verify
set "INSTALL_RESULT=%ERRORLEVEL%"
del /q "%INSTALLER_FILE%" >nul 2>nul
if not "%INSTALL_RESULT%"=="0" goto :failed

echo.
echo [3/3] YBM is installed and running.
echo       The setup page has opened in your browser.
echo.
if "%YBM_NO_PAUSE%"=="1" exit /b 0
echo Press any key to close this window.
pause >nul
exit /b 0

:download_failed
del /q "%INSTALLER_FILE%" >nul 2>nul
echo.
echo ERROR: Could not download the YBM installer.
echo Check your internet connection and try again, or use YBM-Setup.msi.

:failed
echo.
echo Installation did not finish. The error above explains the failed step.
if "%YBM_NO_PAUSE%"=="1" exit /b 1
echo Press any key to close this window.
pause >nul
exit /b 1
