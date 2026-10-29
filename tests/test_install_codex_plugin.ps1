#Requires -Version 7.0
# Exercises complete payload verification and marketplace recovery with an isolated fake CLI.
param([string]$FixtureParent = ([IO.Path]::GetTempPath()))
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$installer = Join-Path $repoRoot 'tools/install-codex-plugin.ps1'
$fixtureRoot = Join-Path $FixtureParent ('stealth-plugin-install-test-' + [Guid]::NewGuid().ToString('N'))
$utf8 = [Text.UTF8Encoding]::new($false)
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
$source = Join-Path $fixtureRoot 'source'
[IO.Directory]::CreateDirectory($source) | Out-Null
$package = Get-Content -LiteralPath (Join-Path $repoRoot 'package.json') -Raw | ConvertFrom-Json
$payloadFiles = @('package.json') + @($package.files)
foreach ($relative in $payloadFiles) {
    $dest = Join-Path $source $relative
    [IO.Directory]::CreateDirectory((Split-Path $dest -Parent)) | Out-Null
    Copy-Item -LiteralPath (Join-Path $repoRoot $relative) -Destination $dest
}
# The fixture exercises installer behavior, independently of the source release hash gate.
foreach ($relative in @('plugin.json', '.codex-plugin/plugin.json')) {
    $path = Join-Path $source $relative
    $manifest = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $manifest.author.name = 'nexusmap.ai'
    if ($relative -eq 'plugin.json') { $manifest.extensions.'com.openai'.interface.developerName = 'nexusmap.ai' }
    else { $manifest.interface.developerName = 'nexusmap.ai' }
    [IO.File]::WriteAllText($path, ($manifest | ConvertTo-Json -Depth 100), $utf8)
}
$skillRoot = Join-Path $source 'skills/stealth-ghost-delegation'
$skillManifestPath = Join-Path $skillRoot 'install-manifest.json'
$skillManifest = [ordered]@{}
foreach ($relative in $payloadFiles) {
    if ($relative.StartsWith('skills/stealth-ghost-delegation/') -and $relative -ne 'skills/stealth-ghost-delegation/install-manifest.json') {
        $skillManifest[$relative.Substring('skills/stealth-ghost-delegation/'.Length)] = (Get-FileHash -LiteralPath (Join-Path $source $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
[IO.File]::WriteAllText($skillManifestPath, ($skillManifest | ConvertTo-Json -Depth 100), $utf8)
$marketplacePath = Join-Path $fixtureRoot '.agents/plugins/marketplace.json'
[IO.Directory]::CreateDirectory((Split-Path $marketplacePath -Parent)) | Out-Null
$marketplace = @{
    name = 'plugins-cli'
    interface = @{ displayName = 'Preserved marketplace' }
    plugins = @(
        @{ name = 'unrelated-plugin'; source = @{ source = 'local'; path = './preserve-me' }; policy = @{ installation = 'AVAILABLE'; authentication = 'ON_INSTALL' }; custom = 'unchanged' },
        @{ name = 'stealth-commander'; source = @{ source = 'local'; path = './old-cache' }; policy = @{ installation = 'AVAILABLE'; authentication = 'ON_INSTALL' }; category = 'Developer Tools' }
    )
}
$original = $utf8.GetBytes(($marketplace | ConvertTo-Json -Depth 100) + [Environment]::NewLine)
$fakeCli = Join-Path $fixtureRoot 'fake-codex.ps1'
$mock = @'
$root = $PSScriptRoot
[IO.File]::AppendAllText((Join-Path $root 'calls.log'), ($args -join ' ') + [Environment]::NewLine)
$mode = Get-Content -LiteralPath (Join-Path $root 'mode.txt') -Raw
$global:LASTEXITCODE = 0
if (($args[0..2] -join ' ') -eq 'plugin marketplace list') {
    @{ marketplaces = @(@{ name = 'plugins-cli'; root = $root }) } | ConvertTo-Json -Depth 10 -Compress
}
elseif (($args[0..1] -join ' ') -eq 'plugin add') {
    if ($mode.Trim() -eq 'fail') {
        $global:LASTEXITCODE = 17
        '{"status":"failed"}'
        return
    }
    $source = Join-Path $root 'source'
    $version = (Get-Content -LiteralPath (Join-Path $source 'plugin.json') -Raw | ConvertFrom-Json).version
    $cache = Join-Path $root ('.codex/plugins/cache/plugins-cli/stealth-commander/' + $version)
    $package = Get-Content -LiteralPath (Join-Path $source 'package.json') -Raw | ConvertFrom-Json
    foreach ($relative in (@('package.json') + @($package.files))) {
        $dest = Join-Path $cache $relative
        [IO.Directory]::CreateDirectory((Split-Path $dest -Parent)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $source $relative) -Destination $dest
    }
    $runtime = Join-Path $cache 'skills/stealth-ghost-delegation/scripts/stealth_commander_mcp.py'
    switch ($mode.Trim()) {
        'missing-runtime' { Remove-Item -LiteralPath $runtime }
        'stale-runtime' { [IO.File]::WriteAllText($runtime, 'stale runtime fixture') }
        'missing-skill' { Remove-Item -LiteralPath (Join-Path $cache 'skills/stealth-ghost-delegation/SKILL.md') }
        'stale-manifest' { [IO.File]::WriteAllText((Join-Path $cache 'skills/stealth-ghost-delegation/install-manifest.json'), '{}') }
        'stale-launcher' { [IO.File]::WriteAllText((Join-Path $cache 'bin/stealth-commander.js'), 'stale launcher fixture') }
        'extra-runtime' { [IO.File]::WriteAllText((Join-Path $cache 'skills/stealth-ghost-delegation/scripts/stale-unmanifested.py'), 'undeclared runtime fixture') }
    }
    '{"status":"installed"}'
}
elseif (($args[0..1] -join ' ') -eq 'plugin list') {
    $version = (Get-Content -LiteralPath (Join-Path $root 'source/plugin.json') -Raw | ConvertFrom-Json).version
    @{ installed = @(@{ pluginId = 'stealth-commander@plugins-cli'; version = $version; installed = $true; enabled = $true }) } | ConvertTo-Json -Depth 10 -Compress
}
else { throw ('Unexpected fake CLI call: ' + ($args -join ' ')) }
'@
[IO.File]::WriteAllText($fakeCli, $mock, $utf8)
$parameters = @{ PluginRoot = $source; MarketplaceFile = $marketplacePath; CodexConfigRoot = (Join-Path $fixtureRoot '.codex'); CodexCommand = $fakeCli }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Same-Bytes([byte[]]$Left, [byte[]]$Right) { return [Convert]::ToBase64String($Left) -eq [Convert]::ToBase64String($Right) }
$results = [Collections.Generic.List[string]]::new()

[IO.File]::WriteAllBytes($marketplacePath, $original)
[IO.File]::WriteAllText((Join-Path $fixtureRoot 'mode.txt'), 'ok', $utf8)
$dry = (& $installer @parameters -WhatIf | Out-String) | ConvertFrom-Json
Assert-True (-not $dry.changed) 'WhatIf claimed a change.'
Assert-True (Same-Bytes $original ([IO.File]::ReadAllBytes($marketplacePath))) 'WhatIf modified the marketplace.'
Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixtureRoot '.codex'))) 'WhatIf installed a cache.'
Assert-True (-not ((Get-Content -LiteralPath (Join-Path $fixtureRoot 'calls.log') -Raw).Contains('plugin add'))) 'WhatIf invoked installation.'
$results.Add('WhatIf: no marketplace or install mutation')

$installed = (& $installer @parameters -Confirm:$false | Out-String) | ConvertFrom-Json
$after = Get-Content -LiteralPath $marketplacePath -Raw | ConvertFrom-Json -AsHashtable
Assert-True $installed.changed 'Successful refresh was not reported.'
Assert-True ($after.plugins[0].source.path -eq './preserve-me' -and $after.plugins[0].custom -eq 'unchanged') 'Unrelated entry changed.'
Assert-True ($after.plugins[1].source.path -eq './source') 'Source did not resolve relative to marketplace root.'
Assert-True (Same-Bytes $original ([IO.File]::ReadAllBytes($installed.backup))) 'Backup does not preserve original bytes.'
Assert-True (Test-Path -LiteralPath $installed.verified_cache) 'Expected verified cache absent.'
Assert-True ($installed.verified_payload_files -eq $payloadFiles.Count) 'Complete payload was not verified.'
Assert-True (Test-Path -LiteralPath (Join-Path $installed.verified_cache 'skills/stealth-ghost-delegation/scripts/stealth_commander_mcp.py')) 'Verified runtime is absent.'
$results.Add('Success: complete skill/runtime payload, preserved unrelated entry, rooted source, exact backup/cache')

foreach ($case in @(
    @{ mode = 'fail'; error = 'failed with exit code 17' },
    @{ mode = 'missing-runtime'; error = 'Installed cache verification failed for skills/stealth-ghost-delegation/scripts/stealth_commander_mcp.py' },
    @{ mode = 'stale-runtime'; error = 'Installed cache verification failed for skills/stealth-ghost-delegation/scripts/stealth_commander_mcp.py' },
    @{ mode = 'missing-skill'; error = 'Installed cache verification failed for skills/stealth-ghost-delegation/SKILL.md' },
    @{ mode = 'stale-manifest'; error = 'Installed cache verification failed for skills/stealth-ghost-delegation/install-manifest.json' },
    @{ mode = 'stale-launcher'; error = 'Installed cache verification failed for bin/stealth-commander.js' },
    @{ mode = 'extra-runtime'; error = 'Installed cache does not match the complete authored skill payload' }
)) {
    [IO.File]::WriteAllBytes($marketplacePath, $original)
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'mode.txt'), $case.mode, $utf8)
    $failure = $null
    try { $null = & $installer @parameters -Confirm:$false } catch { $failure = $_ }
    Assert-True ($null -ne $failure -and $failure.Exception.Message.Contains($case.error)) "Expected $($case.mode) failure was not propagated: $failure"
    Assert-True (Same-Bytes $original ([IO.File]::ReadAllBytes($marketplacePath))) "$($case.mode) did not restore exact marketplace bytes and unrelated plugins."
    $results.Add("$($case.mode): rejected and restored exact marketplace bytes including unrelated plugins")
}

$runtime = Join-Path $source 'skills/stealth-ghost-delegation/scripts/stealth_commander_mcp.py'
$runtimeBytes = [IO.File]::ReadAllBytes($runtime)
$callsBefore = [IO.File]::ReadAllText((Join-Path $fixtureRoot 'calls.log'))
[IO.File]::WriteAllText($runtime, 'tampered source fixture', $utf8)
$failure = $null
try { $null = & $installer @parameters -Confirm:$false } catch { $failure = $_ }
finally { [IO.File]::WriteAllBytes($runtime, $runtimeBytes) }
Assert-True ($null -ne $failure -and $failure.Exception.Message.Contains('Source install manifest verification failed')) 'Tampered source passed preflight.'
Assert-True ($callsBefore -ceq [IO.File]::ReadAllText((Join-Path $fixtureRoot 'calls.log'))) 'Source verification failure invoked Codex.'
Assert-True (Same-Bytes $original ([IO.File]::ReadAllBytes($marketplacePath))) 'Source verification failure modified the marketplace.'
$results.Add('Source tampering: rejected before Codex or marketplace mutation')

$extraSource = Join-Path $skillRoot 'scripts/undeclared-source.py'
[IO.File]::WriteAllText($extraSource, 'undeclared source fixture', $utf8)
$failure = $null
try { $null = & $installer @parameters -Confirm:$false } catch { $failure = $_ }
finally { Remove-Item -LiteralPath $extraSource }
Assert-True ($null -ne $failure -and $failure.Exception.Message.Contains('Source install manifest does not match')) 'Undeclared authored source passed preflight.'
Assert-True ($callsBefore -ceq [IO.File]::ReadAllText((Join-Path $fixtureRoot 'calls.log'))) 'Incomplete source manifest invoked Codex.'
$results.Add('Source inventory: undeclared authored script rejected before mutation')
@{ passed = $results.Count; results = $results; fixture = $fixtureRoot; global_configuration_changed = $false } | ConvertTo-Json -Depth 10
