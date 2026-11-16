# Probe every service and print the readiness report.

#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipModelDownload
)

$Script:Root = $PSScriptRoot
if (-not $Script:Root) { $Script:Root = (Get-Location).Path }

# Ensure the virtual environment exists; if not, fall back to a full start.
$venvPython = Join-Path $Script:Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host 'Virtual environment not found. Running the installer first...' -ForegroundColor Yellow
    & (Join-Path $Script:Root 'start.ps1') -SkipModelDownload:$SkipModelDownload -DryRun
    exit $LASTEXITCODE
}

$env:PYTHONPATH = $Script:Root
& $venvPython -m orchestrator.cli doctor
exit $LASTEXITCODE
