$ErrorActionPreference = 'Stop'
$taskRoot = 'C:\Users\MIRAC\.codex\visualizations\2026\10\17\01a149b2-23b0-7712-8400-d26cba312d6a\SpatialPredictionStudio'
$installRoot = 'C:\Users\MIRAC\AppData\Local\Programs\SpatialPredictionStudio\versions\0.5.0-70573b90e438'
$target = 'C:\Users\MIRAC\.ai\mcp\mcp.json'
$template = Join-Path $installRoot 'client-configurations\pycharm.json'
$legacy = 'C:\Users\MIRAC\AppData\Roaming\JetBrains\PyCharm2026.2\options\llm.mcpServers.xml'
$emptySha = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
$templateSha = 'a68504ac230974ada95c149a0cb01a6fa0cee3d5d07dad1af19d7ed0b9dadcd3'
$legacySha = 'f45d9443bca73a7f82744b151056a69f4406ad62db0ba437bbbca863fba17e99'
$record = [ordered]@{ schema_version=1; created_utc=[DateTime]::UtcNow.ToString('o'); target=$target; source=$template; raw_config_persisted=$false; other_config_writes=$false; status='started'; global_config_writes=$false }
$staged = $null

function Assert-Preconditions {
    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'pycharm64.exe' OR Name = 'pycharm.exe'")
    if ($running.Count -ne 0) { throw 'PYCHARM_STILL_RUNNING' }
    if ([IO.Path]::GetFullPath($target) -cne $target) { throw 'TARGET_PATH_MISMATCH' }
    foreach ($path in @('C:\Users\MIRAC\.ai', 'C:\Users\MIRAC\.ai\mcp', $target)) {
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'TARGET_REPARSE_POINT' }
    }
    $file = Get-Item -LiteralPath $target -Force
    if ($file.PSIsContainer -or $file.Length -ne 0) { throw 'TARGET_NO_LONGER_EMPTY' }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $emptySha) { throw 'TARGET_HASH_CHANGED' }
    if ((Get-FileHash -LiteralPath $template -Algorithm SHA256).Hash.ToLowerInvariant() -ne $templateSha) { throw 'TEMPLATE_HASH_CHANGED' }
    if ((Get-FileHash -LiteralPath $legacy -Algorithm SHA256).Hash.ToLowerInvariant() -ne $legacySha) { throw 'LEGACY_METADATA_CHANGED' }
}

try {
    Assert-Preconditions
    $sourceBytes = [IO.File]::ReadAllBytes($template)
    $source = [Text.Encoding]::UTF8.GetString($sourceBytes) | ConvertFrom-Json
    $serverNames = @($source.mcpServers.PSObject.Properties.Name)
    if ($serverNames.Count -ne 1 -or $serverNames[0] -cne 'spatial_prediction_studio') { throw 'TEMPLATE_HAS_UNEXPECTED_SERVER' }
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
    $backup = Join-Path ([IO.Path]::GetDirectoryName($target)) ('mcp.json.sps-empty-backup-' + $stamp)
    $staged = Join-Path ([IO.Path]::GetDirectoryName($target)) ('.sps-mcp-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    if (Test-Path -LiteralPath $backup) { throw 'BACKUP_EXISTS' }
    $stream = [IO.File]::Open($staged, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($sourceBytes, 0, $sourceBytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    Assert-Preconditions
    [IO.File]::Replace($staged, $target, $backup, $false)
    $staged = $null
    $record.global_config_writes = $true
    $record.backup = $backup
    $record.backup_sha256 = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant()
    $record.target_sha256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    $record.legacy_metadata_unchanged = (Get-FileHash -LiteralPath $legacy -Algorithm SHA256).Hash.ToLowerInvariant() -eq $legacySha
    $record.exact_template_match = $record.target_sha256 -eq $templateSha
    $record.empty_original_preserved = $record.backup_sha256 -eq $emptySha
    $null = Get-Content -LiteralPath $target -Raw | ConvertFrom-Json
    if (-not ($record.exact_template_match -and $record.empty_original_preserved -and $record.legacy_metadata_unchanged)) { throw 'READBACK_FAILED' }
    $record.status = 'passed'
} catch {
    $record.status = 'failed'
    $record.error_type = $_.Exception.GetType().FullName
    $known = @('PYCHARM_STILL_RUNNING','TARGET_PATH_MISMATCH','TARGET_REPARSE_POINT','TARGET_NO_LONGER_EMPTY','TARGET_HASH_CHANGED','TEMPLATE_HASH_CHANGED','LEGACY_METADATA_CHANGED','TEMPLATE_HAS_UNEXPECTED_SERVER','BACKUP_EXISTS','READBACK_FAILED')
    $record.error_code = if ($known -contains $_.Exception.Message) { $_.Exception.Message } else { 'REPAIR_IO_ERROR' }
} finally {
    if ($staged -and [IO.Path]::GetDirectoryName($staged) -ceq 'C:\Users\MIRAC\.ai\mcp' -and (Test-Path -LiteralPath $staged -PathType Leaf)) {
        Remove-Item -LiteralPath $staged -Force
    }
    $record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskRoot 'build\mcp-pycharm-empty-repair.json') -Encoding UTF8
}
$record | ConvertTo-Json -Depth 5
if ($record.status -ne 'passed') { exit 1 }
