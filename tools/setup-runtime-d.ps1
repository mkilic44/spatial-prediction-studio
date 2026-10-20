param([Parameter(Mandatory=$true)][string]$BasePython, [switch]$RestoreLock)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'runtime-d'
$python = Join-Path $runtimePath 'Scripts\python.exe'
$cLock = Join-Path $projectRoot 'requirements-lock.txt'
$dLock = Join-Path $projectRoot 'requirements-d-lock.txt'
$build = Join-Path $projectRoot 'build'
$cHash = (Get-FileHash -LiteralPath $cLock -Algorithm SHA256).Hash
$env:TABPFN_DISABLE_TELEMETRY = '1'
$env:DO_NOT_TRACK = '1'
$env:HF_HUB_OFFLINE = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
function Run-Pip([string[]]$Arguments) {
    & $python -m pip --isolated --disable-pip-version-check @Arguments
    if ($LASTEXITCODE -ne 0) { throw ('D dependency operation failed: ' + ($Arguments -join ' ')) }
}
& $BasePython -c "import struct,sys; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw '64-bit Python 3.12 is required.' }
if (-not (Test-Path -LiteralPath $python)) {
    & $BasePython -m venv $runtimePath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create runtime-d.' }
}
& $python -c "import sys; assert sys.prefix != sys.base_prefix; assert 'include-system-site-packages = false' in open(sys.prefix + '/pyvenv.cfg').read().lower()"
if ($LASTEXITCODE -ne 0) { throw 'The D environment is not isolated.' }
Run-Pip @('install', '--only-binary=:all:', '-r', $cLock)
if ($RestoreLock) {
    if (-not (Test-Path -LiteralPath $dLock)) { throw 'D lock file not found.' }
    Run-Pip @('install', '--only-binary=:all:', '-c', $cLock, '-r', $dLock, '--extra-index-url', 'https://download.pytorch.org/whl/cpu', '--dry-run', '--report', (Join-Path $build 'd-model-restore-dry-run.json'))
    Run-Pip @('install', '--only-binary=:all:', '-c', $cLock, '-r', $dLock, '--extra-index-url', 'https://download.pytorch.org/whl/cpu')
} else {
    $torchArgs = @('install', '--only-binary=:all:', '-c', $cLock, 'torch==2.8.0+cpu', '--index-url', 'https://download.pytorch.org/whl/cpu', '--extra-index-url', 'https://pypi.org/simple')
    Run-Pip ($torchArgs + @('--dry-run', '--report', (Join-Path $build 'd-model-torch-dry-run.json')))
    Run-Pip $torchArgs
    $modelArgs = @('install', '--only-binary=:all:', '-c', $cLock, 'tabm==0.0.3', 'pytabkit==1.7.3', 'pytorch-lightning==2.5.5', 'torchmetrics==1.8.2', 'lightning-utilities==0.15.2', 'rtdl-num-embeddings==0.0.12', 'psutil==7.0.0', 'tabpfn==2.2.1')
    Run-Pip ($modelArgs + @('--dry-run', '--report', (Join-Path $build 'd-model-packages-dry-run.json')))
    $modelReport = Get-Content -LiteralPath (Join-Path $build 'd-model-packages-dry-run.json') -Raw | ConvertFrom-Json
    $tabpfnRecord = @($modelReport.install | Where-Object { $_.metadata.name -eq 'tabpfn' })
    if ($tabpfnRecord.Count -gt 0 -and $tabpfnRecord[0].download_info.archive_info.hashes.sha256 -ne '9aa3a0df8ecebcae5f426b1fec9bb2e2a690880bc8ad079191e8d959bf0f157b') {
        throw 'The TabPFN wheel digest does not match the approved package.'
    }
    Run-Pip $modelArgs
}
Run-Pip @('check')
& $python -c "import sys,importlib.metadata as m,torch,numpy,sklearn,tabm,pytabkit,tabpfn; from pathlib import Path; assert torch.__version__ == '2.8.0+cpu' and torch.version.cuda is None; pins=[line.strip().split('==') for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines() if '==' in line]; assert all(m.version(n)==v for n,v in pins); expected={'torch':'2.8.0+cpu','tabm':'0.0.3','pytabkit':'1.7.3','tabpfn':'2.2.1'}; assert all(m.version(n)==v for n,v in expected.items()); print(expected)" $cLock
if ($LASTEXITCODE -ne 0) { throw 'D package verification failed.' }
$frozen = & $python -m pip --isolated freeze --all
if ($LASTEXITCODE -ne 0) { throw 'Could not create the D lock file.' }
if ($RestoreLock) {
    if (Compare-Object (Get-Content -LiteralPath $dLock) $frozen) { throw 'The D environment does not contain the same package list as the lock file; the lock file was not changed.' }
} else {
    [System.IO.File]::WriteAllLines($dLock, $frozen, [System.Text.UTF8Encoding]::new($false))
}
if ((Get-FileHash -LiteralPath $cLock -Algorithm SHA256).Hash -ne $cHash) { throw 'The C lock file changed.' }
Write-Output 'The D environment and its separate lock file were verified.'
