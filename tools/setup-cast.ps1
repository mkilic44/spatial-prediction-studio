param([Parameter(Mandatory=$true)][string]$Conda)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$referenceRuntime = Join-Path $projectRoot 'reference-r'
if (-not (Test-Path -LiteralPath $Conda)) { throw 'Conda not found.' }
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath (Join-Path $referenceRuntime 'lib\R\bin\Rscript.exe'))) {
        & $Conda create --yes --prefix $referenceRuntime --file (Join-Path $projectRoot 'reference-r-conda-lock.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned R environment.' }
    }
    & $Conda run --no-capture-output --prefix $referenceRuntime Rscript --vanilla tools/install_cast_reference.R
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned CAST environment. Setup does not continue without a version match.' }
} finally { Pop-Location }
