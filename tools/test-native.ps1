param([string]$MSBuild = 'C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& $MSBuild (Join-Path $projectRoot 'native\tests\NativeContracts.csproj') /t:Rebuild /verbosity:minimal /nologo
if ($LASTEXITCODE -ne 0) { throw 'Yerel sozlesme testleri derlenemedi.' }
$testRoot = Join-Path $projectRoot ('build\native-tests\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
& (Join-Path $projectRoot 'native\tests\bin\NativeContracts.exe') (Join-Path $projectRoot 'runtime\Scripts\python.exe') (Join-Path $projectRoot 'native\tests\worker_stub.py') $testRoot (Join-Path $projectRoot 'backend\sps\registry.json') | Tee-Object -FilePath (Join-Path $testRoot 'test.log')
if ($LASTEXITCODE -ne 0) { throw "Yerel sozlesme testleri basarisiz: $testRoot" }
& $MSBuild (Join-Path $projectRoot 'native\tests\PanelContracts.csproj') /t:Rebuild /verbosity:minimal /nologo
if ($LASTEXITCODE -ne 0) { throw 'Panel sozlesme testleri derlenemedi.' }
& (Join-Path $projectRoot 'native\tests\bin\PanelContracts.exe') (Join-Path $projectRoot 'native\SpatialPredictionStudio\bin\Release\SpatialPredictionStudio.dll') (Join-Path $testRoot 'accepted_methods.fixture.json') (Join-Path $testRoot 'panel') | Tee-Object -FilePath (Join-Path $testRoot 'panel.log')
if ($LASTEXITCODE -ne 0) { throw "Panel sozlesme testleri basarisiz: $testRoot" }
Write-Output "Yerel test kaniti: $testRoot"
& $MSBuild (Join-Path $projectRoot 'native\tests\OwnedProcessContracts.csproj') /t:Rebuild /verbosity:minimal /nologo
if ($LASTEXITCODE -ne 0) { throw 'Sahipli surec testleri derlenemedi.' }
& (Join-Path $projectRoot 'native\tests\bin\OwnedProcessContracts.exe') (Join-Path $projectRoot 'runtime-d\Scripts\python.exe') (Join-Path $projectRoot 'native\tests\owned_worker_stub.py') (Join-Path $testRoot 'owned-process') | Tee-Object -FilePath (Join-Path $testRoot 'owned-process.log')
if ($LASTEXITCODE -ne 0) { throw "Sahipli surec testleri basarisiz: $testRoot" }
