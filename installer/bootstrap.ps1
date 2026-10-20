<#
.SYNOPSIS
Installs, verifies or removes Spatial Prediction Studio for the current Windows user.

.DESCRIPTION
Downloads a pinned private CPython 3.12.3 from nuget.org, checks its SHA-512 digest, and runs
installer\sps_install.py with it. No administrator rights are needed. Install.cmd and
Uninstall.cmd in the repository root call this script.

.EXAMPLE
.\installer\bootstrap.ps1 -Action Install -WithMcp -McpClient claude_desktop

.EXAMPLE
.\installer\bootstrap.ps1 -Action Install -WithR
Also installs R 4.4.3 with CAST 1.0.4, which the kNNDM validation design needs.
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Verify')][string]$Action = 'Install',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\SpatialPredictionStudio'),
    [switch]$WithMcp,
    [ValidateSet('codex', 'chatgpt_desktop', 'antigravity', 'claude_desktop', 'claude_code', 'opencode', 'pycharm')]
    [string[]]$McpClient = @(),
    [switch]$WithTabPFN,
    [switch]$AcceptTabPFNLicense,
    [switch]$WithR,
    # Testing and continuous integration only:
    [string]$Staging,
    [switch]$EsriStubs,
    [string]$EsriReferenceDir,
    [switch]$SkipVerify
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Install-BootstrapPython([string]$Root, $Pin) {
    $python = Join-Path $Root 'bootstrap-python\python.exe'
    if (Test-Path -LiteralPath $python) { return $python }
    $cache = Join-Path $Root 'cache'
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $package = Join-Path $cache ('python.' + $Pin.version + '.nupkg')
    $valid = (Test-Path -LiteralPath $package) -and ((Get-FileHash -LiteralPath $package -Algorithm SHA512).Hash -eq $Pin.sha512)
    if (-not $valid) {
        Write-Host ('Downloading Python ' + $Pin.version + ' from nuget.org ...')
        $partial = $package + '.part'
        Invoke-WebRequest -UseBasicParsing -Uri $Pin.url -OutFile $partial
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA512).Hash -ne $Pin.sha512) {
            Remove-Item -LiteralPath $partial -Force
            throw 'The downloaded Python package does not match its pinned SHA-512 digest.'
        }
        Move-Item -LiteralPath $partial -Destination $package -Force
    }
    $extract = Join-Path $cache ('python-extract-' + [guid]::NewGuid().ToString('N'))
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($package, $extract)
    $target = Join-Path $Root 'bootstrap-python'
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Move-Item -LiteralPath (Join-Path $extract 'tools') -Destination $target
    Remove-Item -LiteralPath $extract -Recurse -Force
    if (-not (Test-Path -LiteralPath $python)) { throw 'The private Python could not be prepared.' }
    return $python
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Spatial Prediction Studio needs 64-bit Windows.' }
    $source = Split-Path -Parent $PSScriptRoot
    $pins = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'pins.json') -Raw | ConvertFrom-Json
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    $InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path
    $python = Join-Path $InstallRoot 'bootstrap-python\python.exe'
    if ($Action -eq 'Install') { $python = Install-BootstrapPython $InstallRoot $pins.python }
    elseif (-not (Test-Path -LiteralPath $python)) {
        Write-Host ('Spatial Prediction Studio is not installed under ' + $InstallRoot)
        exit 0
    }
    $arguments = @('-I', '-X', 'utf8', (Join-Path $PSScriptRoot 'sps_install.py'), $Action.ToLowerInvariant(),
                   '--source', $source, '--install-root', $InstallRoot)
    if ($WithMcp) { $arguments += '--with-mcp' }
    foreach ($client in $McpClient) { $arguments += @('--mcp-client', $client) }
    if ($WithTabPFN) { $arguments += '--with-tabpfn' }
    if ($AcceptTabPFNLicense) { $arguments += '--accept-tabpfn-license' }
    if ($WithR) { $arguments += '--with-r' }
    if ($Staging) { $arguments += @('--staging', $Staging) }
    if ($EsriStubs) { $arguments += '--esri-stubs' }
    if ($EsriReferenceDir) { $arguments += @('--esri-reference-dir', $EsriReferenceDir) }
    if ($SkipVerify) { $arguments += '--skip-verify' }
    & $python @arguments
    $code = $LASTEXITCODE
    if ($Action -eq 'Uninstall' -and $code -eq 0 -and -not (Test-Path -LiteralPath (Join-Path $InstallRoot 'current.json'))) {
        foreach ($name in @('bootstrap-python', 'cache')) {
            $path = Join-Path $InstallRoot $name
            # rd with the extended-length form also removes package files deeper than MAX_PATH.
            if (Test-Path -LiteralPath $path) { & cmd.exe /d /c rd /s /q ('\\?\' + $path) }
        }
    }
    exit $code
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
