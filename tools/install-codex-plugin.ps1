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

function Get-PayloadFile {
    param([string]$Root, [string]$RelativePath)
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath -match '[:\\]' -or @($RelativePath.Split('/') | Where-Object { $_ -in @('', '.', '..') }).Count) {
        throw "Invalid release payload path: $RelativePath."
    }
    $file = $Root
    foreach ($part in $RelativePath.Split('/')) {
        $file = Join-Path $file $part
        $item = Get-Item -LiteralPath $file -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Release payload paths must not traverse links: $RelativePath."
        }
    }
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing release payload file: $RelativePath." }
    return $file
}

function Get-AuthoredSkillFiles {
    param([string]$Directory, [string]$Prefix = '')
    foreach ($item in Get-ChildItem -LiteralPath $Directory -Force) {
        if ($item.Name -in @('.runtime', '__pycache__', 'node_modules', '.venv') -or $item.Name -match '\.py[co]$') { continue }
        $relative = $Prefix + $item.Name
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Authored skill paths must not traverse links: $relative."
        }
        if ($item.PSIsContainer) { Get-AuthoredSkillFiles -Directory $item.FullName -Prefix ($relative + '/') }
        else { $relative }
    }
}

function Assert-SameFiles {
    param([string[]]$Expected, [string[]]$Actual, [string]$Description)
    if (@(Compare-Object -ReferenceObject @($Expected | Sort-Object -CaseSensitive) -DifferenceObject @($Actual | Sort-Object -CaseSensitive) -CaseSensitive).Count) {
        throw "$Description does not match the complete authored skill payload."
    }
}

$sourceRoot = (Resolve-Path -LiteralPath $PluginRoot).ProviderPath.TrimEnd([IO.Path]::DirectorySeparatorChar)
$marketplacePath = (Resolve-Path -LiteralPath $MarketplaceFile).ProviderPath
$manifest = Get-Content -LiteralPath (Join-Path $sourceRoot 'plugin.json') -Raw | ConvertFrom-Json -Depth 100
$compat = Get-Content -LiteralPath (Join-Path $sourceRoot '.codex-plugin/plugin.json') -Raw | ConvertFrom-Json -Depth 100
if ($manifest.name -ne 'stealth-commander' -or $compat.name -ne $manifest.name -or $compat.version -ne $manifest.version) {
    throw 'The source plugin identity/version is not consistent.'
}
if ($manifest.author.name -cne 'nexusmap.ai' -or $compat.author.name -cne 'nexusmap.ai') {
    throw 'The source publisher must be nexusmap.ai.'
}
$interface = $manifest.extensions.'com.openai'.interface
foreach ($presentation in @($interface, $compat.interface)) {
    if ($presentation.developerName -cne 'nexusmap.ai' -or $presentation.logo -ne './assets/ninja-mask.png' -or $presentation.composerIcon -ne './assets/ninja-mask.png') {
        throw 'The source presentation must reference the supplied ninja image and nexusmap.ai.'
    }
}
$iconPath = Join-Path $sourceRoot 'assets/ninja-mask.png'
if ((Get-FileHash -LiteralPath $iconPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedIcon) {
    throw 'The source icon differs from the approved ninja image.'
}
if ($sourceRoot -match '(?i)[\\/]\.codex[\\/]plugins[\\/]cache([\\/]|$)') {
    throw 'Use the reviewed source checkout, not a generated plugin cache.'
}

$package = Get-Content -LiteralPath (Join-Path $sourceRoot 'package.json') -Raw | ConvertFrom-Json -Depth 100
if ($package.name -ne $manifest.name -or $package.version -ne $manifest.version -or -not $package.files.Count) {
    throw 'The source package identity/version or payload is not consistent.'
}
$skillPrefix = 'skills/stealth-ghost-delegation/'
$skillManifestPath = $skillPrefix + 'install-manifest.json'
$skillRoot = Join-Path $sourceRoot 'skills/stealth-ghost-delegation'
$skillManifest = Get-Content -LiteralPath (Get-PayloadFile -Root $sourceRoot -RelativePath $skillManifestPath) -Raw | ConvertFrom-Json -AsHashtable -Depth 100
if (-not ($skillManifest -is [Collections.IDictionary]) -or -not $skillManifest.Count) { throw 'The source install manifest is empty or invalid.' }
$authoredSkillFiles = @(Get-AuthoredSkillFiles -Directory $skillRoot | Where-Object { $_ -cne 'install-manifest.json' })
Assert-SameFiles -Expected @($skillManifest.Keys) -Actual $authoredSkillFiles -Description 'Source install manifest'
$payloadFiles = @('package.json') + @($package.files)
$declaredSkillFiles = @($payloadFiles | Where-Object { $_.StartsWith($skillPrefix, [StringComparison]::Ordinal) } | ForEach-Object { $_.Substring($skillPrefix.Length) })
Assert-SameFiles -Expected @($authoredSkillFiles + 'install-manifest.json') -Actual $declaredSkillFiles -Description 'Source package file list'
$payloadHashes = [ordered]@{}
foreach ($relativeFile in $payloadFiles) {
    $sourceFile = Get-PayloadFile -Root $sourceRoot -RelativePath $relativeFile
    $payloadHashes[$relativeFile] = (Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash.ToLowerInvariant()
}
foreach ($relativeFile in $skillManifest.Keys) {
    $digest = $skillManifest[$relativeFile]
    if ($digest -isnot [string] -or $digest -cnotmatch '^[0-9a-f]{64}$' -or $payloadHashes[$skillPrefix + $relativeFile] -cne $digest) {
        throw "Source install manifest verification failed for $relativeFile."
    }
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
    foreach ($relativeFile in $payloadHashes.Keys) {
        try { $cachedFile = Get-PayloadFile -Root $cacheRoot -RelativePath $relativeFile }
        catch { throw "Installed cache verification failed for ${relativeFile}: $($_.Exception.Message)" }
        if ($payloadHashes[$relativeFile] -cne (Get-FileHash -LiteralPath $cachedFile -Algorithm SHA256).Hash.ToLowerInvariant()) {
            throw "Installed cache verification failed for $relativeFile."
        }
    }
    $cachedSkillFiles = @(Get-AuthoredSkillFiles -Directory (Join-Path $cacheRoot 'skills/stealth-ghost-delegation'))
    Assert-SameFiles -Expected @($authoredSkillFiles + 'install-manifest.json') -Actual $cachedSkillFiles -Description 'Installed cache'
    $plan['changed'] = $true
    $plan['backup'] = $backupPath
    $plan['verified_cache'] = $cacheRoot
    $plan['verified_payload_files'] = $payloadHashes.Count
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
