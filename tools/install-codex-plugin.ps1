#Requires -Version 7.0
<#
.SYNOPSIS
Refresh the existing local Stealth Commander plugin from this reviewed checkout.
.DESCRIPTION
Updates only stealth-commander@plugins-cli, preserves other marketplace entries,
and uses codex plugin add to manage the version cache. The source remains this
checkout, never a generated cache directory. Run with -WhatIf to inspect the plan.
This does not update or restart a separately configured PC runtime.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$PluginRoot = (Join-Path $PSScriptRoot '..'),
    [string]$MarketplaceFile = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.agents/plugins/marketplace.json'),
    [string]$CodexConfigRoot = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex' }),
    [string]$CodexCommand = 'codex'
)

$ErrorActionPreference = 'Stop'
$pluginId = 'stealth-commander@plugins-cli'
$expectedIcon = 'b708e8de067b7e01482dd109bd2b7f191150b84c1a13f03ebc1b6b5caaffd5c4'
$utf8 = [Text.UTF8Encoding]::new($false)

function Invoke-CodexJson {
    param([string[]]$Arguments)
    $global:LASTEXITCODE = 0
    $output = & $CodexCommand @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "codex $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return (($output -join [Environment]::NewLine) | ConvertFrom-Json -Depth 100)
}

function Get-Sha256 {
    param([byte[]]$Bytes)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
}

$sourceRoot = (Resolve-Path -LiteralPath $PluginRoot).ProviderPath.TrimEnd([IO.Path]::DirectorySeparatorChar)
$marketplacePath = (Resolve-Path -LiteralPath $MarketplaceFile).ProviderPath
$manifest = Get-Content -LiteralPath (Join-Path $sourceRoot 'plugin.json') -Raw | ConvertFrom-Json -Depth 100
$compat = Get-Content -LiteralPath (Join-Path $sourceRoot '.codex-plugin/plugin.json') -Raw | ConvertFrom-Json -Depth 100
if ($manifest.name -ne 'stealth-commander' -or $compat.name -ne $manifest.name -or $compat.version -ne $manifest.version) {
    throw 'The source plugin identity/version is not consistent.'
}
if ($manifest.author.name -ne 'Nexusmap.ai' -or $compat.author.name -ne 'Nexusmap.ai') {
    throw 'The source publisher must be Nexusmap.ai.'
}
$interface = $manifest.extensions.'com.openai'.interface
foreach ($presentation in @($interface, $compat.interface)) {
    if ($presentation.developerName -ne 'Nexusmap.ai' -or $presentation.logo -ne './assets/ninja-mask.png' -or $presentation.composerIcon -ne './assets/ninja-mask.png') {
        throw 'The source presentation must reference the supplied ninja image and Nexusmap.ai.'
    }
}
$iconPath = Join-Path $sourceRoot 'assets/ninja-mask.png'
if ((Get-FileHash -LiteralPath $iconPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedIcon) {
    throw 'The source icon differs from the approved ninja image.'
}
if ($sourceRoot -match '(?i)[\\/]\.codex[\\/]plugins[\\/]cache([\\/]|$)') {
    throw 'Use the reviewed source checkout, not a generated plugin cache.'
}

$marketplaces = Invoke-CodexJson -Arguments @('plugin', 'marketplace', 'list', '--json')
$matches = @($marketplaces.marketplaces | Where-Object name -EQ 'plugins-cli')
if ($matches.Count -ne 1) { throw 'Expected exactly one plugins-cli marketplace.' }
$marketplaceRoot = (Resolve-Path -LiteralPath $matches[0].root).ProviderPath
$expectedMarketplacePath = [IO.Path]::GetFullPath((Join-Path $marketplaceRoot '.agents/plugins/marketplace.json'))
if (-not $marketplacePath.Equals($expectedMarketplacePath, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The marketplace file does not belong to the resolved plugins-cli root.'
}
$relative = [IO.Path]::GetRelativePath($marketplaceRoot, $sourceRoot)
if ([IO.Path]::IsPathRooted($relative) -or $relative -eq '..' -or $relative.StartsWith('..' + [IO.Path]::DirectorySeparatorChar)) {
    throw 'The reviewed checkout must be inside the plugins-cli marketplace root.'
}
$sourcePath = './' + $relative.Replace('\', '/')
$originalBytes = [IO.File]::ReadAllBytes($marketplacePath)
$originalHash = Get-Sha256 -Bytes $originalBytes
$marketplace = $utf8.GetString($originalBytes) | ConvertFrom-Json -AsHashtable -Depth 100
if ($marketplace.name -ne 'plugins-cli') { throw 'Unexpected marketplace identity.' }
$entries = @($marketplace.plugins | Where-Object { $_.name -eq 'stealth-commander' })
if ($entries.Count -ne 1 -or $entries[0].source.source -ne 'local') {
    throw 'Expected exactly one existing local stealth-commander entry.'
}
$previousSource = $entries[0].source.path
$entries[0].source.path = $sourcePath
$newBytes = $utf8.GetBytes(($marketplace | ConvertTo-Json -Depth 100) + [Environment]::NewLine)
$newHash = Get-Sha256 -Bytes $newBytes

$plan = [ordered]@{
    plugin = $pluginId
    version = $manifest.version
    source_checkout = $sourceRoot
    previous_source = $previousSource
    new_source = $sourcePath
    marketplace = $marketplacePath
    approved_icon_sha256 = $expectedIcon
}
if (-not $PSCmdlet.ShouldProcess($pluginId, "Repoint its marketplace source to the reviewed checkout and install version $($manifest.version)")) {
    $plan['changed'] = $false
    $plan | ConvertTo-Json -Depth 10
    return
}

if ((Get-Sha256 -Bytes ([IO.File]::ReadAllBytes($marketplacePath))) -ne $originalHash) {
    throw 'Marketplace changed during preflight; rerun after inspecting it.'
}
$suffix = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N')
$backupPath = $marketplacePath + '.stealth-backup-' + $suffix
$tempPath = $marketplacePath + '.stealth-write-' + $suffix
[IO.File]::WriteAllBytes($backupPath, $originalBytes)
try {
    [IO.File]::WriteAllBytes($tempPath, $newBytes)
    [IO.File]::Move($tempPath, $marketplacePath, $true)
    $null = Invoke-CodexJson -Arguments @('plugin', 'add', $pluginId, '--json')
    $installed = Invoke-CodexJson -Arguments @('plugin', 'list', '--marketplace', 'plugins-cli', '--json')
    $found = @($installed.installed | Where-Object pluginId -EQ $pluginId)
    if ($found.Count -ne 1 -or -not $found[0].installed -or -not $found[0].enabled -or $found[0].version -ne $manifest.version) {
        throw 'Codex did not report the expected installed and enabled version.'
    }
    $cacheRoot = Join-Path $CodexConfigRoot ('plugins/cache/plugins-cli/stealth-commander/' + $manifest.version)
    foreach ($relativeFile in @('plugin.json', '.codex-plugin/plugin.json', 'mcp.json', '.mcp.json', 'assets/ninja-mask.png')) {
        $sourceFile = Join-Path $sourceRoot $relativeFile
        $cachedFile = Join-Path $cacheRoot $relativeFile
        if (-not (Test-Path -LiteralPath $cachedFile -PathType Leaf) -or
            (Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $cachedFile -Algorithm SHA256).Hash) {
            throw "Installed cache verification failed for $relativeFile."
        }
    }
    $plan['changed'] = $true
    $plan['backup'] = $backupPath
    $plan['verified_cache'] = $cacheRoot
    $plan['runtime_restart_verified'] = $false
    $plan | ConvertTo-Json -Depth 10
}
catch {
    $failure = $_
    $currentHash = Get-Sha256 -Bytes ([IO.File]::ReadAllBytes($marketplacePath))
    if ($currentHash -eq $originalHash) {
        Write-Warning 'The original marketplace is unchanged. Inspect plugin state before retrying.'
    }
    elseif ($currentHash -eq $newHash) {
        [IO.File]::WriteAllBytes($tempPath, $originalBytes)
        [IO.File]::Move($tempPath, $marketplacePath, $true)
        Write-Warning 'The marketplace was restored. Inspect plugin state before retrying; a partial install may have occurred.'
    }
    else {
        Write-Warning "The marketplace changed concurrently and was not overwritten. Original bytes are backed up at $backupPath."
    }
    throw $failure
}
finally {
    if (Test-Path -LiteralPath $tempPath -PathType Leaf) { Remove-Item -LiteralPath $tempPath }
}
