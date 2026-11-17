# DRIFTLINE Orchestrator - one-command Windows installer and launcher.
#
#   .\play.cmd                      double-click "press play": install
#                                   everything, start every needed service in
#                                   the background, open the console. No
#                                   separate terminals are required.
#   .\start.ps1                     same as play.cmd, for a terminal
#   .\start.ps1 -Doctor             probe every service
#   .\start.ps1 -Test                run the offline + integration test suite
#   .\start.ps1 -SkipModelDownload  never download the THX-02 model
#   .\start.ps1 -DryRun              validate and report without launching
#
# Everything is user-local where possible. No administrator privileges are
# required, and nothing is executed from a download without a signature check.
# Background services started here are recorded under run\ and stopped again
# with .\stop.ps1; services that are already running are probed, never touched.

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

# Windows PowerShell 5.1 may default to TLS 1.0 for Invoke-WebRequest; make
# sure HTTPS calls (pypi, python.org, ollama.com) negotiate TLS 1.2. No-op
# where the framework already defaults to it.
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
}
catch { }

$Script:AppVersion = '1.1.1'
$Script:Root = $PSScriptRoot
if (-not $Script:Root) { $Script:Root = (Get-Location).Path }
Set-Location -Path $Script:Root

$Script:PythonSpec = '3.12'
$Script:OllamaUrl = 'https://ollama.com/download/OllamaSetup.exe'
$Script:ThxModel = 'HAL-X/thx-02:q4_k_m'
$Script:VenvDir = Join-Path $Script:Root '.venv'
$Script:RunDir = Join-Path $Script:Root 'run'
$Script:LogDir = Join-Path $Script:Root 'logs'
$Script:Report = [ordered]@{}
$Script:Errors = New-Object System.Collections.ArrayList
$Script:ServicesConfig = $null

# Service endpoints (probed with a real request, never a bare TCP connect).
$Script:OllamaApiUrl = 'http://127.0.0.1:11434/api/version'
$Script:QwenUrl = 'http://127.0.0.1:8090/v1/models'
$Script:OmniRouteUrl = 'http://127.0.0.1:20128/health'
$Script:ArenaUrl = 'http://127.0.0.1:17864/mcp'

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
# Background service management (no separate terminals)
#
# start.ps1 can run the services itself as hidden background processes. Only
# processes started here are recorded (run\<name>.json) and only those are
# ever stopped again by stop.ps1. Services that are already running are only
# probed. Nothing is modified, reconfigured or replaced.
# ---------------------------------------------------------------------------

function Test-HttpHealth {
    param(
        [string]$Url,
        [string]$Method = 'GET',
        [string]$Body,
        [string]$ContentType,
        [hashtable]$Headers
    )
    try {
        $params = @{ Uri = $Url; UseBasicParsing = $true; TimeoutSec = 5 }
        if ($Method -ne 'GET') { $params.Method = $Method }
        if ($Body) { $params.Body = $Body }
        if ($ContentType) { $params.ContentType = $ContentType }
        if ($Headers) { $params.Headers = $Headers }
        $response = Invoke-WebRequest @params
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    }
    catch {
        return $false
    }
}

function Invoke-ArenaBridgeProbe {
    $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"driftline-start","version":"1.1.1"}}}'
    return Test-HttpHealth -Url $Script:ArenaUrl -Method 'Post' -Body $body `
        -ContentType 'application/json' -Headers @{ Accept = 'application/json, text/event-stream' }
}

function Get-ServicesConfig {
    # Reads the services section of config/config.yaml via the venv Python
    # (PyYAML), so no YAML parsing is duplicated here in PowerShell. The
    # snippet is piped on stdin (python -) instead of passed via -c: Windows
    # PowerShell 5.1 mangles native command arguments that contain embedded
    # double quotes.
    $defaults = [ordered]@{
        auto_start              = $true
        health_wait_seconds     = 60
        ollama_start_command    = ''
        qwen_start_command      = ''
        omniroute_start_command = ''
        arena_start_command     = ''
        qwen_enabled            = $true
        thx_enabled             = $true
        omniroute_enabled       = $false
        arena_enabled           = $true
    }
    try {
        $code = @'
import json, os, pathlib, yaml

def as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)

root = os.environ.get("DRIFTLINE_CONFIG_ROOT", ".")
path = pathlib.Path(root) / "config" / "config.yaml"
data = {}
if path.exists():
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

services = dict(data.get("services") or {})
for key in ("auto_start", "health_wait_seconds", "ollama_start_command",
            "qwen_start_command", "omniroute_start_command", "arena_start_command"):
    env_key = "DRIFTLINE_SERVICES_" + key.upper()
    if env_key in os.environ:
        services[key] = os.environ[env_key]

out = {
    "auto_start": as_bool(services.get("auto_start"), True),
    "health_wait_seconds": int(services.get("health_wait_seconds") or 60),
    "ollama_start_command": str(services.get("ollama_start_command") or ""),
    "qwen_start_command": str(services.get("qwen_start_command") or ""),
    "omniroute_start_command": str(services.get("omniroute_start_command") or ""),
    "arena_start_command": str(services.get("arena_start_command") or ""),
}
for section, key in (("qwen", "qwen_enabled"), ("thx", "thx_enabled"),
                     ("omniroute", "omniroute_enabled"), ("arena", "arena_enabled")):
    value = (data.get(section) or {}).get("enabled")
    env_key = "DRIFTLINE_" + section.upper() + "_ENABLED"
    if env_key in os.environ:
        value = os.environ[env_key]
    if value is None:
        value = section != "omniroute"
    out[key] = as_bool(value, section != "omniroute")
print(json.dumps(out))
'@
        $env:DRIFTLINE_CONFIG_ROOT = $Script:Root
        try {
            $raw = $code | & $Script:VenvPython -
        }
        finally {
            Remove-Item Env:DRIFTLINE_CONFIG_ROOT -ErrorAction SilentlyContinue
        }
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $parsed = $raw | ConvertFrom-Json
            foreach ($key in @($defaults.Keys)) {
                $prop = $parsed.PSObject.Properties[[string]$key]
                if ($prop -and $null -ne $prop.Value) { $defaults[$key] = $prop.Value }
            }
        }
    }
    catch {
        Add-Error "Could not read the services section of config/config.yaml: $($_.Exception.Message)"
    }
    return $defaults
}

function Get-ServiceRecord($Name) {
    $path = Join-Path $Script:RunDir "$Name.json"
    if (-not (Test-Path $path)) { return $null }
    try { return (Get-Content $path -Raw | ConvertFrom-Json) }
    catch { return $null }
}

function Save-ServiceRecord($Name, $Process, $Command) {
    if (-not (Test-Path $Script:RunDir)) { New-Item -ItemType Directory -Path $Script:RunDir | Out-Null }
    $record = [ordered]@{
        name         = $Name
        pid          = $Process.Id
        process_name = $Process.ProcessName
        command      = $Command
        started_at   = (Get-Date).ToString('o')
    }
    $record | ConvertTo-Json | Set-Content -Path (Join-Path $Script:RunDir "$Name.json") -Encoding UTF8
}

function Clear-ServiceRecord($Name) {
    Remove-Item (Join-Path $Script:RunDir "$Name.json") -ErrorAction SilentlyContinue
}

function Test-RecordAlive($Record) {
    if (-not $Record) { return $false }
    $pidProp = $Record.PSObject.Properties['pid']
    if (-not $pidProp) { return $false }
    return [bool](Get-Process -Id $pidProp.Value -ErrorAction SilentlyContinue)
}

function Start-ManagedService {
    # Start one service in the background (hidden window, logged output) if it
    # is not already answering. Returns $true when the service is healthy.
    param(
        [string]$Name,
        [string]$DisplayName,
        [string]$Command,
        [scriptblock]$Probe,
        [string]$Hint = ''
    )
    if (-not $Command) {
        Write-Status $DisplayName 'SKIPPED' 'no start command configured'
        if ($Hint) { Write-Host "  $Hint" -ForegroundColor DarkGray }
        return $false
    }
    if (& $Probe) {
        Write-Status $DisplayName 'PASS' 'already running (left untouched)'
        return $true
    }

    $record = Get-ServiceRecord $Name
    if (Test-RecordAlive $record) {
        Write-Host "  $DisplayName was started by this launcher earlier (PID $($record.pid)); waiting for it to answer..." -ForegroundColor Cyan
    }
    else {
        if ($record) { Clear-ServiceRecord $Name }
        if (-not (Test-Path $Script:LogDir)) { New-Item -ItemType Directory -Path $Script:LogDir | Out-Null }
        $outLog = Join-Path $Script:LogDir "service-$Name.out.log"
        $errLog = Join-Path $Script:LogDir "service-$Name.err.log"
        Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
        Write-Host "  Starting $DisplayName in the background: $Command" -ForegroundColor Cyan
        try {
            $process = Start-Process -FilePath $env:ComSpec -ArgumentList "/c $Command" `
                -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $outLog -RedirectStandardError $errLog
        }
        catch {
            Add-Error "Failed to start ${DisplayName}: $($_.Exception.Message)"
            Write-Status $DisplayName 'BLOCKED' 'failed to start'
            return $false
        }
        Save-ServiceRecord $Name $process $Command
        $record = Get-ServiceRecord $Name
    }

    $wait = 60
    if ($Script:ServicesConfig -and $Script:ServicesConfig.health_wait_seconds) {
        $wait = [int]$Script:ServicesConfig.health_wait_seconds
    }
    $deadline = (Get-Date).AddSeconds($wait)
    while ((Get-Date) -lt $deadline) {
        if (& $Probe) {
            Write-Status $DisplayName 'PASS' "healthy in the background (PID $($record.pid))"
            return $true
        }
        if (-not (Test-RecordAlive $record)) {
            Write-Status $DisplayName 'BLOCKED' 'process exited during startup'
            Add-Error "$DisplayName exited during startup. See logs\service-$Name.err.log"
            Clear-ServiceRecord $Name
            return $false
        }
        Start-Sleep -Seconds 2
    }
    Write-Status $DisplayName 'BLOCKED' "did not answer within ${wait}s"
    Add-Error "$DisplayName did not become healthy. See logs\service-$Name.err.log"
    return $false
}

# ---------------------------------------------------------------------------
# Service probes (the Python orchestrator owns the real probes)
# ---------------------------------------------------------------------------

function Test-Qwen {
    if ($Script:ServicesConfig -and -not $Script:ServicesConfig.qwen_enabled) {
        Write-Status 'Qwen local' 'NOT CONFIGURED' 'disabled in config (not contacted)'
        return $false
    }
    $url = $Script:QwenUrl
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 8
        Write-Status 'Qwen local' 'PASS' "HTTP $($response.StatusCode) at $url"
        return $true
    }
    catch {
        Write-Status 'Qwen local' 'BLOCKED' "$url unreachable"
        Add-Error 'Your llama.cpp Qwen server is not answering on 127.0.0.1:8090. ' +
                  'Set services.qwen_start_command in config\config.yaml and start.ps1 will run it for you. ' +
                  'Your existing installation is never modified.'
        return $false
    }
}

function Test-OmniRoute {
    if ($Script:ServicesConfig -and -not $Script:ServicesConfig.omniroute_enabled) {
        Write-Status 'OmniRoute' 'NOT CONFIGURED' 'disabled in config (not contacted)'
        return $false
    }
    $url = $Script:OmniRouteUrl
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
    if ($Script:ServicesConfig -and -not $Script:ServicesConfig.arena_enabled) {
        Write-Status 'Arena bridge' 'NOT CONFIGURED' 'disabled in config (not contacted)'
        return $false
    }
    $url = $Script:ArenaUrl
    if (Invoke-ArenaBridgeProbe) {
        Write-Status 'Arena bridge' 'PASS' "answered an MCP initialize request at $url"
        return $true
    }
    Write-Status 'Arena bridge' 'BLOCKED' "$url did not answer an MCP initialize request"
    Add-Error 'The adapter will report ARENA_NOT_CONNECTED and will not dispatch tasks.'
    return $false
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
        # Upgrade in place: add sections introduced by newer versions (for
        # example "services") without touching any existing key, value or
        # comment. This is what brings a v1.0.x config up to date.
        $output = & $Script:VenvPython -m orchestrator.cli --base-dir $Script:Root init --upgrade
        if ($LASTEXITCODE -eq 0) {
            $joined = "$output".Trim()
            if ($joined -match 'nothing changed') {
                Write-Status 'Configuration' 'PASS' 'existing config/config.yaml preserved (all sections present)'
            }
            else {
                Write-Status 'Configuration' 'PASS' $joined
            }
        }
        else {
            Write-Status 'Configuration' 'BLOCKED' "config upgrade failed: $output"
            Add-Error 'Could not upgrade config/config.yaml. Add the services section manually (see CONFIGURATION.md).'
        }
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

function Start-Console {
    Write-Host ''
    Write-Host 'Everything is up. Launching the orchestrator console.' -ForegroundColor Green
    Write-Host 'Type help for commands, quit to leave. This one window does everything.' -ForegroundColor Green
    $env:PYTHONPATH = $Script:Root
    & $Script:VenvPython -m orchestrator.cli console
    Write-Host ''
    Write-Host 'Console closed. Background services started by this launcher keep running.' -ForegroundColor Cyan
    Write-Host 'Stop everything with: .\stop.ps1' -ForegroundColor Cyan
}

try {
    Write-Header "v$Script:AppVersion"

    if ($Doctor) {
        # Diagnostics must work on a fresh extraction: provision the runtime
        # and dependencies first, then run the report. No Ollama install, no
        # model download, no service startup, no Arena dispatch and no console
        # launch happen here.
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
    Initialize-Config
    $Script:ServicesConfig = Get-ServicesConfig

    $ollama = Get-OllamaPath
    if (-not $ollama -and $Script:ServicesConfig.thx_enabled) {
        Install-Ollama | Out-Null
        $ollama = Get-OllamaPath
    }
    if ($ollama) {
        if (Test-OllamaSignature $ollama) {
            Write-Status 'Ollama' 'PASS' $ollama
            $ollamaServing = $false
            if (-not $Script:ServicesConfig.thx_enabled) {
                # THX is disabled: detect and report the installation only.
                # Never start it, never pull a model, never contact the API.
                Write-Status 'Ollama service' 'SKIPPED' 'thx disabled in config (not contacted)'
            }
            elseif ($Script:ServicesConfig.auto_start) {
                # Ollama is managed by this launcher: default command is
                # "<ollama> serve"; services.ollama_start_command overrides it.
                # An already-running Ollama is probed and left untouched.
                $serveCommand = [string]$Script:ServicesConfig.ollama_start_command
                if (-not $serveCommand) { $serveCommand = '"' + $ollama + '" serve' }
                $ollamaServing = Start-ManagedService -Name 'ollama' -DisplayName 'Ollama service' `
                    -Command $serveCommand `
                    -Probe { Test-HttpHealth -Url $Script:OllamaApiUrl }
                if ($ollamaServing) { Install-ThxModel }
                else { Add-Error 'Ollama is installed but not serving; THX-02 model handling is skipped (fallback mode).' }
            }
            else {
                $ollamaServing = Test-HttpHealth -Url $Script:OllamaApiUrl
                if ($ollamaServing) { Install-ThxModel }
                else { Add-Error 'Ollama is installed but not serving (services.auto_start is false); THX-02 model handling is skipped.' }
            }
        }
        else {
            Write-Status 'Ollama' 'FAIL' 'signature verification failed'
            Add-Error 'The Ollama executable is not correctly signed. Not using it.'
        }
    }
    else {
        if ($Script:ServicesConfig.thx_enabled) {
            Write-Status 'Ollama' 'BLOCKED' 'not installed'
            Add-Error 'Ollama is missing; THX-02 is unavailable. The system runs in fallback mode.'
        }
        else {
            Write-Status 'Ollama' 'NOT CONFIGURED' 'thx disabled in config; install not required'
        }
    }

    if ($Script:ServicesConfig.auto_start) {
        if ($Script:ServicesConfig.qwen_enabled) {
            # Help the user configure the Qwen start command: if llama-server
            # is on PATH we print the exact line to paste. We never guess a
            # model path and never start a guessed command.
            $qwenHint = 'Set services.qwen_start_command in config\config.yaml (e.g. llama-server -m C:\models\qwen.gguf --port 8090) and this launcher will run it for you.'
            $llama = Get-Command 'llama-server' -ErrorAction SilentlyContinue
            if (-not $llama) { $llama = Get-Command 'llama-server.exe' -ErrorAction SilentlyContinue }
            if ($llama) {
                $qwenHint = "llama-server detected at $($llama.Source). Set services.qwen_start_command in config\config.yaml to: `"$($llama.Source)`" -m <your-qwen-gguf> --port 8090"
            }
            Start-ManagedService -Name 'qwen' -DisplayName 'Qwen service' `
                -Command ([string]$Script:ServicesConfig.qwen_start_command) `
                -Probe { Test-HttpHealth -Url $Script:QwenUrl } `
                -Hint $qwenHint | Out-Null
        }
        else {
            Write-Status 'Qwen service' 'SKIPPED' 'disabled in config'
        }

        if ($Script:ServicesConfig.omniroute_enabled) {
            Start-ManagedService -Name 'omniroute' -DisplayName 'OmniRoute service' `
                -Command ([string]$Script:ServicesConfig.omniroute_start_command) `
                -Probe { Test-HttpHealth -Url $Script:OmniRouteUrl } `
                -Hint 'Optional. Set services.omniroute_start_command in config\config.yaml to auto-start OmniRoute.' | Out-Null
        }
        else {
            Write-Status 'OmniRoute service' 'SKIPPED' 'disabled in config (not contacted)'
        }

        if ($Script:ServicesConfig.arena_enabled) {
            Start-ManagedService -Name 'arena' -DisplayName 'Arena bridge service' `
                -Command ([string]$Script:ServicesConfig.arena_start_command) `
                -Probe { Invoke-ArenaBridgeProbe } `
                -Hint 'Set services.arena_start_command in config\config.yaml to auto-start your Arena bridge.' | Out-Null
        }
        else {
            Write-Status 'Arena bridge service' 'SKIPPED' 'disabled in config (not contacted)'
        }
    }
    else {
        Write-Host '  services.auto_start is false: services are probed but never started.' -ForegroundColor DarkGray
    }

    Test-Qwen
    Test-OmniRoute
    Test-ArenaBridge

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
        Write-Host 'Some services are not ready. The console still starts; blocked features degrade safely.' -ForegroundColor Yellow
    }

    Start-Console
    exit 0
}
catch {
    Write-Host ''
    Write-Host "FATAL: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    exit 1
}
