param([Parameter(Mandatory=$true)][string]$BasePython)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'runtime'
if (-not (Test-Path -LiteralPath $BasePython)) { throw 'Python interpreter not found.' }
& $BasePython -c "import sys,struct; assert sys.version_info[:2] == (3,12); assert struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw '64-bit Python 3.12 is required.' }
if (-not (Test-Path -LiteralPath (Join-Path $runtimePath 'Scripts\python.exe'))) {
    & $BasePython -m venv $runtimePath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated environment.' }
}
$python = Join-Path $runtimePath 'Scripts\python.exe'
& $python -m pip --isolated install --no-cache-dir --only-binary=:all: -r (Join-Path $projectRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python -c "import struct,numpy,scipy,sklearn,rasterio,pyproj; assert struct.calcsize('P') == 8; print('64-bit environment ready')"
if ($LASTEXITCODE -ne 0) { throw 'Environment verification failed.' }
