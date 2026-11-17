@echo off
setlocal
rem ---------------------------------------------------------------------------
rem DRIFTLINE Orchestrator - one-touch launcher.
rem
rem Double-click this file: it installs everything, starts every needed
rem service in the background and opens the orchestrator console. No separate
rem terminals are required. Extra arguments are forwarded to start.ps1, e.g.:
rem     play.cmd -Force               unattended first run (auto-accept the
rem                                   Ollama install and the ~3.7 GB model)
rem     play.cmd -Doctor              probe only, change nothing
rem     play.cmd -Test                run the test suites
rem     play.cmd -DryRun              install/validate/report, do not launch
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo Windows PowerShell was not found on PATH.
    echo This tool requires Windows 10/11 with PowerShell 5.1 or newer.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo play.cmd finished with exit code %EXITCODE%.
)

rem Keep the window open when double-clicked (or after any failure) so the
rem report stays readable. When run from an existing terminal, do not block.
set "PAUSE_AT_END="
echo %cmdcmdline% | find /i "%~0" >nul && set "PAUSE_AT_END=1"
if not "%EXITCODE%"=="0" set "PAUSE_AT_END=1"
if defined PAUSE_AT_END (
    echo.
    pause
)
exit /b %EXITCODE%
