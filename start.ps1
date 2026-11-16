# DRIFTLINE Orchestrator - one-command Windows installer and launcher.
#
#   .\start.ps1                     install, test, launch safe idle mode
#   .\start.ps1 -Doctor             probe every service
#   .\start.ps1 -Test                run the offline + integration test suite
#   .\start.ps1 -SkipModelDownload  never download the THX-02 model
#   .\start.ps1 -DryRun              validate and report without launching
#
# Everything is user-local where possible. No administrator privileges are
# required, and nothing is executed from a download without a signature check.

#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Doctor,
    [switch]$Test,
    [switch]$SkipModelDownload,
    [switch]$DryRun,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Script:AppVersion = '1.0.1'
$Script:Root = $PSScriptRoot
if (-not $Script:Root) { $Script:Root = (Get-Location).Path }
Set-Location -Path $Script:Root

$Script:PythonSpec = '3.12'
$Script:OllamaUrl = 'https://ollama.com/download/OllamaSetup.exe'
$Script:ThxModel = 'HAL-X/thx-02:q4_k_m'
$Script:VenvDir = Join-Path $Script:Root '.venv'
$Script:Report = [ordered]@{}
$Script:Errors = New-Object System.Collections.ArrayList

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

function Write-Header($Title) {
    Write-Host ''
    Write-Host 'DRIFTLINE ORCHESTRATOR' -ForegroundColor Cyan
    Write-Host '======================' -ForegroundColor Cyan
    Write-Host $Title -ForegroundColor Cyan
}

function Write-Status($Label, $Status, $Detail) {
    $color = 'Gray'
    switch ($Status) {
        'PASS' { $color = 'Green' }
        'FAIL' { $color = 'Red' }
        'BLOCKED' { $color = 'Yellow' }
        'SKIPPED' { $color = 'DarkGray' }
        'NOT CONFIGURED' { $color = 'DarkGray' }
        default { $color = 'White' }
    }
    Write-Host ("{0,-26} " -f $Label) -NoNewline
    Write-Host ("{0,-16}" -f $Status) -ForegroundColor $color -NoNewline
    if ($Detail) { Write-Host " $Detail" -ForegroundColor DarkGray }
    $Script:Report[$Label] = "$Status`n$Detail"
}

function Add-Error($Message) {
    [void]$Script:Errors.Add($Message)
    Write-Host "  ! $Message" -ForegroundColor Yellow
}

function Assert-Confirm($Prompt) {
    if ($Force) { return $true }
    $answer = Read-Host "$Prompt [y/N]"
    return ($answer -match '^[Yy]')
}

# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

function Test-Windows {
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        throw "Windows PowerShell 5.1 or newer is required."
    }
    Write-Status 'Windows / PowerShell' 'PASS' "$([System.Environment]::OSVersion.Version) / PS $($PSVersionTable.PSVersion)"
}

function Test-Internet {
    try {
        $response = Invoke-WebRequest -Uri 'https://pypi.org/simple/' -UseBasicParsing -TimeoutSec 15
        Write-Status 'Internet connectivity' 'PASS' "pypi.org HTTP $($response.StatusCode)"
        return $true
    }
    catch {
        Write-Status 'Internet connectivity' 'FAIL' $_.Exception.Message
        Add-Error 'Internet access is required for dependency installation.'
        return $false
    }
}

function Get-PythonExecutable {
    foreach ($candidate in @('python', 'py')) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) {
            try {
                if ($candidate -eq 'py') {
                    $version = & py -3 --version 2>$null
                }
                else {
                    $version = & python --version 2>$null
                }
                if ($LASTEXITCODE -eq 0 -and $version) {
                    if ($candidate -eq 'py') { return @{ Command = 'py'; Args = @('-3'); Version = "$version" } }
                    return @{ Command = $candidate; Args = @(); Version = "$version" }
                }
            }
            catch { }
        }
    }
    return $null
}

function Install-PythonUserLocal {
    Write-Host 'Python not found. Installing a user-local runtime...' -ForegroundColor Yellow

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'Using winget (official Windows Package Manager): Python.Python.3.12' -ForegroundColor Cyan
        & winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
            return
        }
        Write-Host 'winget install failed; falling back to the official installer.' -ForegroundColor Yellow
    }

    $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
    $url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
    Write-Host "Downloading official installer: $url" -ForegroundColor Cyan
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing

    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Remove-Item $installer -ErrorAction SilentlyContinue
        throw 'Python installer signature could not be verified. Refusing to run it.'
    }

    Write-Host 'Running installer (user scope, no administrator required)...' -ForegroundColor Cyan
    & $installer /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=0
    if ($LASTEXITCODE -ne 0) { throw "Python installer exited with code $LASTEXITCODE" }
    Remove-Item $installer -ErrorAction SilentlyContinue
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
}

function Initialize-VirtualEnv {
    $python = Get-PythonExecutable
    if (-not $python) { Install-PythonUserLocal; $python = Get-PythonExecutable }
    if (-not $python) { throw 'No usable Python runtime could be found or installed.' }

    Write-Status 'Python runtime' 'PASS' "$($python.Version) ($($python.Command))"

    # Windows uses Scripts\python.exe; POSIX uses bin/python. Resolve whichever
    # exists so the same script provisions correctly on either platform.
    $venvCandidates = @(
        (Join-Path $Script:VenvDir 'Scripts\python.exe'),
        (Join-Path $Script:VenvDir 'bin/python'),
        (Join-Path $Script:VenvDir 'bin/python3')
    )
    $venvExists = $false
    foreach ($candidate in $venvCandidates) {
        if (Test-Path $candidate) { $venvExists = $true; break }
    }

    if (-not $venvExists) {
        Write-Host 'Creating isolated virtual environment...' -ForegroundColor Cyan
        if ($python.Command -eq 'py') { & py -3 -m venv $Script:VenvDir }
        else { & $python.Command -m venv $Script:VenvDir }
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
    }

    $Script:VenvPython = $null
    foreach ($candidate in $venvCandidates) {
        if (Test-Path $candidate) { $Script:VenvPython = $candidate; break }
    }
    if (-not $Script:VenvPython) {
        throw 'Virtual environment exists but no interpreter could be resolved.'
    }
    Write-Status 'Virtual environment' 'PASS' $Script:VenvPython
}

function Install-Dependencies {
    Write-Host 'Installing pinned dependencies...' -ForegroundColor Cyan
    & $Script:VenvPython -m pip install --upgrade pip --quiet
    & $Script:VenvPython -m pip install -r (Join-Path $Script:Root 'requirements.txt') --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install runtime dependencies.' }
    & $Script:VenvPython -c "import yaml, httpx; print('pyyaml', yaml.__version__, 'httpx', httpx.__version__)"
    Write-Status 'Dependencies' 'PASS' 'PyYAML + httpx installed'
}

function Test-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Status 'Git' 'BLOCKED' 'git not found on PATH'
        Add-Error 'Install Git from https://git-scm.com/downloads and re-run.'
        return $false
    }
    $version = & git --version
    Write-Status 'Git' 'PASS' $version
    return $true
}

# ---------------------------------------------------------------------------
# Ollama / THX-02
# ---------------------------------------------------------------------------

function Get-OllamaPath {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($path in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
            'C:\Program Files\Ollama\ollama.exe')) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function Test-OllamaSignature($Path) {
    if (-not $Path) { return $false }
    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ($signature.Status -ne 'Valid') {
        Write-Host "  Ollama signature status: $($signature.Status)" -ForegroundColor Yellow
        return $false
    }
    if ($signature.SignerCertificate.Subject -notmatch 'Ollama') {
        Write-Host "  Ollama signer: $($signature.SignerCertificate.Subject)" -ForegroundColor Yellow
        return $false
    }
    return $true
}

function Install-Ollama {
    Write-Host 'Ollama is not installed.' -ForegroundColor Yellow
    if (-not (Assert-Confirm 'Install Ollama from the official distribution? (~1.5 GB)') ) {
        Add-Error 'Ollama was not installed. THX-02 will remain BLOCKED (fallback mode).'
        return $false
    }
    $installer = Join-Path $env:TEMP 'OllamaSetup.exe'
    Write-Host "Downloading $Script:OllamaUrl" -ForegroundColor Cyan
    Invoke-WebRequest -Uri $Script:OllamaUrl -OutFile $installer -UseBasicParsing
    if (-not (Test-OllamaSignature $installer)) {
        Remove-Item $installer -ErrorAction SilentlyContinue
        throw 'Ollama installer signature could not be verified. Refusing to run it.'
    }
    Write-Host 'Running the official Ollama installer...' -ForegroundColor Cyan
    & $installer /SILENT
    if ($LASTEXITCODE -ne 0) { Add-Error "Ollama installer exited with code $LASTEXITCODE"; return $false }
    Remove-Item $installer -ErrorAction SilentlyContinue
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
    return $true
}

function Get-OllamaModels {
    $ollama = Get-OllamaPath
    if (-not $ollama) { return $null }
    try {
        $list = & $ollama list 2>$null
        return $list
    }
    catch { return $null }
}

function Install-ThxModel {
    if ($SkipModelDownload) {
        Write-Status 'THX-02 model' 'SKIPPED' '-SkipModelDownload was supplied'
        return
    }
    $models = Get-OllamaModels
    if ($models -match [regex]::Escape($Script:ThxModel) -or $models -match 'thx-02') {
        Write-Status 'THX-02 model' 'PASS' "$Script:ThxModel already installed"
        return
    }
    Write-Host 'THX-02 is not installed.' -ForegroundColor Yellow
    Write-Host "The download is approximately 3.7 GB." -ForegroundColor Yellow
    if (-not (Assert-Confirm "Download $Script:ThxModel now?") ) {
        Add-Error 'THX-02 was not downloaded. The orchestrator will run in fallback mode.'
        Write-Status 'THX-02 model' 'SKIPPED' 'download declined'
        return
    }
    $ollama = Get-OllamaPath
    Write-Host "Running: ollama pull $Script:ThxModel" -ForegroundColor Cyan
    & $ollama pull $Script:ThxModel
    if ($LASTEXITCODE -ne 0) { Add-Error 'ollama pull failed.' }
}

# ---------------------------------------------------------------------------
# Service probes (the Python orchestrator owns the real probes)
# ---------------------------------------------------------------------------

function Test-Qwen {
    $url = 'http://127.0.0.1:8090/v1/models'
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 8
        Write-Status 'Qwen local' 'PASS' "HTTP $($response.StatusCode) at $url"
        return $true
    }
    catch {
        Write-Status 'Qwen local' 'BLOCKED' "$url unreachable"
        Add-Error 'Start your existing llama.cpp Qwen server on 127.0.0.1:8090. ' +
                  'The existing installation is left untouched.'
        return $false
    }
}

function Test-OmniRoute {
    $url = 'http://127.0.0.1:20128/health'
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 8
        Write-Status 'OmniRoute' 'PASS' "HTTP $($response.StatusCode) at $url"
        return $true
    }
    catch {
        Write-Status 'OmniRoute' 'BLOCKED' "$url unreachable (optional; routing stays disabled)"
        return $false
    }
}

function Test-ArenaBridge {
    $url = 'http://127.0.0.1:17864/mcp'
    try {
        $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"driftline-start","version":"1.0.1"}}}'
        $response = Invoke-WebRequest -Uri $url -Method Post -Body $body -ContentType 'application/json' -Headers @{ Accept = 'application/json, text/event-stream' } -UseBasicParsing -TimeoutSec 8
        Write-Status 'Arena bridge' 'PASS' "HTTP $($response.StatusCode) at $url"
        return $true
    }
    catch {
        Write-Status 'Arena bridge' 'BLOCKED' "$url did not answer an MCP initialize request"
        Add-Error 'The adapter will report ARENA_NOT_CONNECTED and will not dispatch tasks.'
        return $false
    }
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

function Initialize-Config {
    $configDir = Join-Path $Script:Root 'config'
    if (-not (Test-Path $configDir)) { New-Item -ItemType Directory -Path $configDir | Out-Null }
    $target = Join-Path $configDir 'config.yaml'
    if (-not (Test-Path $target)) {
        Copy-Item (Join-Path $configDir 'config.example.yaml') $target
        Write-Status 'Configuration' 'PASS' 'created config/config.yaml from the template'
    }
    else {
        Write-Status 'Configuration' 'PASS' 'existing config/config.yaml preserved'
    }
    if (-not (Test-Path (Join-Path $Script:Root '.env'))) {
        Copy-Item (Join-Path $Script:Root '.env.example') (Join-Path $Script:Root '.env')
        Write-Host '  Created .env from .env.example (add secrets there; it is never packaged).' -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

function Install-TestDependencies {
    & $Script:VenvPython -m pip install pytest==9.2.0 --quiet
}

function Invoke-OfflineTests {
    Write-Host ''
    Write-Host 'Running offline tests (no external services)...' -ForegroundColor Cyan
    Install-TestDependencies
    $env:PYTHONPATH = $Script:Root
    & $Script:VenvPython -m pytest -q --disable-warnings
    $code = $LASTEXITCODE
    if ($code -eq 0) { Write-Status 'Offline tests' 'PASS' 'all tests passed' }
    else { Write-Status 'Offline tests' 'FAIL' "pytest exited with $code"; Add-Error 'Offline tests failed.' }
    return $code
}

function Invoke-IntegrationTests {
    Write-Host ''
    Write-Host 'Running integration smoke tests (local services only)...' -ForegroundColor Cyan
    $env:PYTHONPATH = $Script:Root
    & $Script:VenvPython -m orchestrator.cli doctor
    return $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

function Show-ReadinessReport {
    Write-Host ''
    $env:PYTHONPATH = $Script:Root
    & $Script:VenvPython -m orchestrator.cli doctor
}

function Start-IdleMode {
    Write-Host ''
    Write-Host 'Launching the orchestrator in safe idle mode...' -ForegroundColor Green
    $env:PYTHONPATH = $Script:Root
    & $Script:VenvPython -m orchestrator.cli idle
}

try {
    Write-Header "v$Script:AppVersion"

    if ($Doctor) {
        # Diagnostics must work on a fresh extraction: provision the runtime
        # and dependencies first, then run the report. No Ollama install, no
        # model download, no Arena dispatch and no idle mode happen here.
        Test-Windows
        if (-not (Test-Internet)) { exit 1 }
        Initialize-VirtualEnv
        Install-Dependencies
        Show-ReadinessReport
        exit 0
    }

    Test-Windows
    if (-not (Test-Internet)) { exit 1 }

    Initialize-VirtualEnv
    Install-Dependencies
    Test-Git

    $ollama = Get-OllamaPath
    if (-not $ollama) {
        Install-Ollama | Out-Null
        $ollama = Get-OllamaPath
    }
    if ($ollama) {
        if (Test-OllamaSignature $ollama) {
            Write-Status 'Ollama' 'PASS' $ollama
        }
        else {
            Write-Status 'Ollama' 'FAIL' 'signature verification failed'
            Add-Error 'The Ollama executable is not correctly signed. Not using it.'
        }
        Install-ThxModel
    }
    else {
        Write-Status 'Ollama' 'BLOCKED' 'not installed'
        Add-Error 'Ollama is missing; THX-02 is unavailable. The system runs in fallback mode.'
    }

    Test-Qwen
    Test-OmniRoute
    Test-ArenaBridge
    Initialize-Config

    $offline = Invoke-OfflineTests
    if ($offline -ne 0) {
        Write-Host ''
        Write-Host 'Offline tests failed; continuing to integration checks so you can see all errors.' -ForegroundColor Yellow
    }

    if ($Test) {
        Invoke-IntegrationTests | Out-Null
        Show-ReadinessReport
        exit $offline
    }

    Show-ReadinessReport

    if ($DryRun) {
        Write-Host ''
        Write-Host 'DryRun: not launching the orchestrator. Re-run without -DryRun to start.' -ForegroundColor Cyan
        exit 0
    }

    if ($Script:Errors.Count -gt 0) {
        Write-Host ''
        Write-Host 'Some services are not ready. The orchestrator will still start in safe idle mode.' -ForegroundColor Yellow
    }

    Start-IdleMode
    exit 0
}
catch {
    Write-Host ''
    Write-Host "FATAL: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    exit 1
}
