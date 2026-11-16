# Run the offline test suite plus local integration smoke tests.

#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipModelDownload
)

$Script:Root = $PSScriptRoot
if (-not $Script:Root) { $Script:Root = (Get-Location).Path }

& (Join-Path $Script:Root 'start.ps1') -Test -SkipModelDownload:$SkipModelDownload
exit $LASTEXITCODE
