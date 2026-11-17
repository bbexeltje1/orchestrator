# Stop the DRIFTLINE Orchestrator and the background services it started.
#
# Only two kinds of processes are ever stopped:
#   1. Python processes whose command line contains this project's
#      "orchestrator" module and this project's directory.
#   2. Background services recorded in run\*.json by start.ps1 / play.cmd
#      (stopped as a process tree, including their hidden cmd wrapper).
# Unrelated Python processes and services you started yourself are never
# touched.

#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Script:Root = $PSScriptRoot
if (-not $Script:Root) { $Script:Root = (Get-Location).Path }
$Script:RunDir = Join-Path $Script:Root 'run'

function Stop-ProcessTree($ProcessId) {
    $children = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ParentProcessId -eq $ProcessId }
    foreach ($child in $children) {
        Stop-ProcessTree $child.ProcessId
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

# --- managed background services (recorded by the launcher) ----------------

$records = @()
if (Test-Path $Script:RunDir) {
    foreach ($file in Get-ChildItem $Script:RunDir -Filter '*.json' -ErrorAction SilentlyContinue) {
        try { $records += , (Get-Content $file.FullName -Raw | ConvertFrom-Json) }
        catch { }
    }
}

$aliveServices = @()
foreach ($record in $records) {
    $pidProp = $record.PSObject.Properties['pid']
    $nameProp = $record.PSObject.Properties['name']
    if (-not $pidProp) { continue }
    if (Get-Process -Id $pidProp.Value -ErrorAction SilentlyContinue) {
        $name = if ($nameProp) { $nameProp.Value } else { 'service' }
        $aliveServices += , [pscustomobject]@{ Name = $name; Pid = $pidProp.Value }
    }
}

# --- orchestrator Python processes -----------------------------------------

$targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'orchestrator' -and
        $_.CommandLine -match ([regex]::Escape($Script:Root)) -and
        $_.Name -match '^python'
    }
$targets = @($targets)

if (-not $targets -and -not $aliveServices) {
    Write-Host 'No running DRIFTLINE Orchestrator processes or managed services were found.' -ForegroundColor Green
    if (Test-Path $Script:RunDir) {
        Get-ChildItem $Script:RunDir -Filter '*.json' -ErrorAction SilentlyContinue |
            Remove-Item -ErrorAction SilentlyContinue
    }
    exit 0
}

if ($targets) {
    Write-Host 'Found the following orchestrator process(es):' -ForegroundColor Yellow
    foreach ($process in $targets) {
        Write-Host ("  PID {0}: {1}" -f $process.ProcessId, $process.CommandLine) -ForegroundColor DarkGray
    }
}
if ($aliveServices) {
    Write-Host 'Found the following background service(s) started by this launcher:' -ForegroundColor Yellow
    foreach ($service in $aliveServices) {
        Write-Host ("  {0} (PID {1})" -f $service.Name, $service.Pid) -ForegroundColor DarkGray
    }
}

if (-not $Force) {
    $answer = Read-Host "Stop these processes? [y/N]"
    if ($answer -notmatch '^[Yy]') { Write-Host 'Aborted.'; exit 0 }
}

foreach ($service in $aliveServices) {
    try {
        Stop-ProcessTree $service.Pid
        Write-Host "Stopped $($service.Name) (PID $($service.Pid))" -ForegroundColor Green
    }
    catch {
        Write-Host "Failed to stop $($service.Name) (PID $($service.Pid)): $($_.Exception.Message)" -ForegroundColor Red
    }
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

if (Test-Path $Script:RunDir) {
    Get-ChildItem $Script:RunDir -Filter '*.json' -ErrorAction SilentlyContinue |
        Remove-Item -ErrorAction SilentlyContinue
}
