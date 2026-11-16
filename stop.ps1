# Stop the DRIFTLINE Orchestrator.
#
# Only processes whose command line contains this project's "orchestrator"
# module are stopped. Unrelated Python processes are never touched.

#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Script:Root = $PSScriptRoot
if (-not $Script:Root) { $Script:Root = (Get-Location).Path }

$targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'orchestrator' -and
        $_.CommandLine -match ([regex]::Escape($Script:Root)) -and
        $_.Name -match '^python'
    }

if (-not $targets) {
    Write-Host 'No running DRIFTLINE Orchestrator processes were found.' -ForegroundColor Green
    exit 0
}

Write-Host 'Found the following orchestrator process(es):' -ForegroundColor Yellow
foreach ($process in $targets) {
    Write-Host ("  PID {0}: {1}" -f $process.ProcessId, $process.CommandLine) -ForegroundColor DarkGray
}

if (-not $Force) {
    $answer = Read-Host "Stop these processes? [y/N]"
    if ($answer -notmatch '^[Yy]') { Write-Host 'Aborted.'; exit 0 }
}

foreach ($process in $targets) {
    try {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        Write-Host "Stopped PID $($process.ProcessId)" -ForegroundColor Green
    }
    catch {
        Write-Host "Failed to stop PID $($process.ProcessId): $($_.Exception.Message)" -ForegroundColor Red
    }
}
