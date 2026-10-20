param([string]$MSBuild = 'C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot 'runtime-d\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $MSBuild)) { throw "MSBuild not found: $MSBuild" }
if (-not (Test-Path -LiteralPath $python)) { throw 'Run tools/setup-runtime-d.ps1 first.' }
& $MSBuild (Join-Path $projectRoot 'native\SpatialPredictionStudio.sln') /t:Rebuild /p:Configuration=Release /p:Platform=x86 /verbosity:minimal /nologo
if ($LASTEXITCODE -ne 0) { throw 'C# build failed.' }
& (Join-Path $PSScriptRoot 'test-native.ps1') -MSBuild $MSBuild
if ($LASTEXITCODE -ne 0) { throw 'Native contract tests failed.' }
$exampleDir = Join-Path $projectRoot 'examples'
if (-not (Test-Path -LiteralPath (Join-Path $exampleDir 'samples.geojson'))) {
    & $python (Join-Path $projectRoot 'tools\make_examples.py') --output $exampleDir
    if ($LASTEXITCODE -ne 0) { throw 'Example data generation failed.' }
}
& $python (Join-Path $projectRoot 'tools\build_package.py')
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
& $python -m unittest discover -s (Join-Path $projectRoot 'tests') -p test_package.py -v
if ($LASTEXITCODE -ne 0) { throw 'Package acceptance test failed.' }
