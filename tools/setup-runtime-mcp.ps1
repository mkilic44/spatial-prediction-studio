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
if (-not (Test-Path -LiteralPath $BasePython -PathType Leaf)) { throw 'Temel Python bulunamadi.' }
& $BasePython -c "import sys,struct; assert sys.version_info[:2] == (3,12); assert struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw '64 bit Python 3.12 gerekli.' }
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw 'MCP bagimlilik kilidi bulunamadi.' }
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    & $BasePython -m venv $runtimePath
    if ($LASTEXITCODE -ne 0) { throw 'Ayri MCP ortami olusturulamadi.' }
}
$pipArgs = @('-m','pip','--isolated','install','--disable-pip-version-check',
             '--only-binary=:all:','--require-hashes','-r',$lockPath)
if ($Wheelhouse) {
    if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) { throw 'Wheel dizini bulunamadi.' }
    $pipArgs += @('--find-links',(Resolve-Path -LiteralPath $Wheelhouse).Path)
}
if ($Offline) {
    if (-not $Wheelhouse) { throw 'Cevrimdisi kurulum icin -Wheelhouse gerekli.' }
    $pipArgs += '--no-index'
}
& $pythonPath @pipArgs
if ($LASTEXITCODE -ne 0) { throw 'Kilitli MCP bagimlilik kurulumu basarisiz.' }
& $pythonPath -m pip check
if ($LASTEXITCODE -ne 0) { throw 'MCP bagimlilik tutarliligi basarisiz.' }
& $pythonPath -c "import importlib.metadata as m; from mcp.server import MCPServer; import win32file,win32pipe,tomlkit; assert m.version('mcp')=='2.3.0'; print('MCP 2.3.0 ayri ortamda hazir.')"
if ($LASTEXITCODE -ne 0) { throw 'MCP ice aktarma dogrulamasi basarisiz.' }

