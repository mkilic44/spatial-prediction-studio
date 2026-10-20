$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$package = Join-Path $projectRoot 'dist\SpatialPredictionStudio.esriAddIn'
if (-not (Test-Path -LiteralPath $package)) { throw 'Run tools/build.ps1 first.' }
$targetDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'ArcGIS\AddIns\Desktop10.8\{DC2D1F95-349B-4F9A-A681-202610170101}'
New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
$target = Join-Path $targetDir 'SpatialPredictionStudio.esriAddIn'
if (Test-Path -LiteralPath $target) {
    $backupDir = Join-Path $projectRoot ('build\install-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    Copy-Item -LiteralPath $target -Destination (Join-Path $backupDir 'SpatialPredictionStudio.esriAddIn')
}
Copy-Item -LiteralPath $package -Destination $target -Force
$sourceHash = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash
$targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
if ($sourceHash -ne $targetHash) { throw 'The digest of the installed package does not match.' }
Write-Output "Installed: $target"
Write-Output "SHA256: $targetHash"
Write-Output 'Restart ArcMap to load the add-in.'
