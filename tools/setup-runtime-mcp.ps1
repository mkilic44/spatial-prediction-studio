param(
    [Parameter(Mandatory=$true)][string]$BasePython,
    [string]$Wheelhouse,
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'runtime-mcp'
$pythonPath = Join-Path $runtimePath 'Scripts\python.exe'
$lockPath = Join-Path $projectRoot 'requirements-mcp-lock.txt'
if (-not (Test-Path -LiteralPath $BasePython -PathType Leaf)) { throw 'Base Python not found.' }
& $BasePython -c "import sys,struct; assert sys.version_info[:2] == (3,12); assert struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw '64-bit Python 3.12 is required.' }
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw 'MCP dependency lock file not found.' }
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    & $BasePython -m venv $runtimePath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the separate MCP environment.' }
}
$pipArgs = @('-m','pip','--isolated','install','--disable-pip-version-check',
             '--only-binary=:all:','--require-hashes','-r',$lockPath)
if ($Wheelhouse) {
    if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) { throw 'Wheel directory not found.' }
    $pipArgs += @('--find-links',(Resolve-Path -LiteralPath $Wheelhouse).Path)
}
if ($Offline) {
    if (-not $Wheelhouse) { throw 'Offline installation requires -Wheelhouse.' }
    $pipArgs += '--no-index'
}
& $pythonPath @pipArgs
if ($LASTEXITCODE -ne 0) { throw 'Locked MCP dependency installation failed.' }
& $pythonPath -m pip check
if ($LASTEXITCODE -ne 0) { throw 'MCP dependency consistency check failed.' }
& $pythonPath -c "import importlib.metadata as m; from mcp.server import MCPServer; import win32file,win32pipe,tomlkit; assert m.version('mcp')=='2.3.0'; print('MCP 2.3.0 ready in the separate environment.')"
if ($LASTEXITCODE -ne 0) { throw 'MCP import verification failed.' }

