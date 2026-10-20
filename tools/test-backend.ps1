param([string]$EvidenceRoot, [string]$StartAt)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot 'runtime-d\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'D Python environment not found.' }
if (-not $EvidenceRoot) { $EvidenceRoot = Join-Path $projectRoot ('build\backend-suite-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')) }
$evidence = [IO.Path]::GetFullPath($EvidenceRoot)
if (Test-Path -LiteralPath $evidence) { throw 'Choose a new folder for the evidence.' }
New-Item -ItemType Directory -Path $evidence | Out-Null
$cases = @(
    @{ Name='core'; Arguments=@('tests','--ignore=tests/test_package.py','--ignore=tests/test_models_d.py','--ignore=tests/test_tabpfn_d.py','--ignore=tests/test_model_integration_d.py','--ignore=tests/test_publication_models_d.py') },
    @{ Name='neural'; Arguments=@('tests/test_models_d.py','-k','not actual_models_and_baselines_share_identical_heldout_folds') },
    @{ Name='comparison'; Arguments=@('tests/test_models_d.py::test_actual_models_and_baselines_share_identical_heldout_folds') },
    @{ Name='tabpfn'; Arguments=@('tests/test_tabpfn_d.py') },
    @{ Name='nested-tabm'; Arguments=@('tests/test_model_integration_d.py','-k','tabm') },
    @{ Name='nested-realmlp'; Arguments=@('tests/test_model_integration_d.py','-k','realmlp') },
    @{ Name='nested-tabpfn'; Arguments=@('tests/test_model_integration_d.py','-k','tabpfn') },
    @{ Name='publication-tabm'; Arguments=@('tests/test_publication_models_d.py','-k','tabm') },
    @{ Name='publication-realmlp'; Arguments=@('tests/test_publication_models_d.py','-k','realmlp') },
    @{ Name='publication-tabpfn'; Arguments=@('tests/test_publication_models_d.py','-k','tabpfn') },
    @{ Name='package'; Arguments=@('tests/test_package.py') }
)
$results = @()
if ($StartAt) {
    $names = @($cases | ForEach-Object { $_.Name })
    $index = [Array]::IndexOf($names, $StartAt)
    if ($index -lt 0) { throw "Unknown test group: $StartAt" }
    $cases = $cases[$index..($cases.Length - 1)]
}
Push-Location $projectRoot
try {
    foreach ($case in $cases) {
        $xmlPath = Join-Path $evidence ($case.Name + '.xml')
        $caseArgs = @('-m','pytest') + $case.Arguments + @('-q','--tb=short','--disable-warnings','--basetemp',(Join-Path $evidence ($case.Name+'-temp')),'--junitxml',$xmlPath)
        & $python @caseArgs 2>&1 | Tee-Object -FilePath (Join-Path $evidence ($case.Name+'.log'))
        $exitStatus = $LASTEXITCODE
        if ($exitStatus -ne 0) { throw ($case.Name + ' tests failed; exit code ' + $exitStatus) }
        [xml]$testXml = Get-Content -LiteralPath $xmlPath -Raw
        $suites = $testXml.SelectNodes('//testsuite')
        $summary = @{ name=$case.Name; tests=0; failures=0; errors=0; skipped=0 }
        foreach ($suite in $suites) {
            foreach ($field in @('tests','failures','errors','skipped')) { $summary[$field] += [int]$suite.GetAttribute($field) }
        }
        if ($summary.tests -eq 0 -or $summary.failures -gt 0 -or $summary.errors -gt 0 -or $summary.skipped -gt 0) { throw ($case.Name + ' did not fully pass; review the XML evidence.') }
        $results += $summary
        ConvertTo-Json -InputObject $results -Depth 5 | Set-Content -LiteralPath (Join-Path $evidence 'summary.json') -Encoding UTF8
    }
} finally { Pop-Location }
Write-Output ('Verified test records: ' + $evidence)
