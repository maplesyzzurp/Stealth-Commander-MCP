#Requires -Version 7.0
# Exercises marketplace preservation and failure recovery with an isolated fake CLI.
param([string]$FixtureParent = ([IO.Path]::GetTempPath()))
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$installer = Join-Path $repoRoot 'tools/install-codex-plugin.ps1'
$fixtureRoot = Join-Path $FixtureParent ('stealth-plugin-install-test-' + [Guid]::NewGuid().ToString('N'))
$utf8 = [Text.UTF8Encoding]::new($false)
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
$source = Join-Path $fixtureRoot 'source'
[IO.Directory]::CreateDirectory($source) | Out-Null
foreach ($relative in @('plugin.json', '.codex-plugin/plugin.json', 'mcp.json', '.mcp.json', 'assets/ninja-mask.png')) {
    $dest = Join-Path $source $relative
    [IO.Directory]::CreateDirectory((Split-Path $dest -Parent)) | Out-Null
    Copy-Item -LiteralPath (Join-Path $repoRoot $relative) -Destination $dest
}
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
    foreach ($relative in @('plugin.json', '.codex-plugin/plugin.json', 'mcp.json', '.mcp.json', 'assets/ninja-mask.png')) {
        $dest = Join-Path $cache $relative
        [IO.Directory]::CreateDirectory((Split-Path $dest -Parent)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $source $relative) -Destination $dest
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
$results.Add('Success: preserved unrelated entry, rooted source, exact backup/cache')

[IO.File]::WriteAllBytes($marketplacePath, $original)
[IO.File]::WriteAllText((Join-Path $fixtureRoot 'mode.txt'), 'fail', $utf8)
$failed = $false
try { $null = & $installer @parameters -Confirm:$false } catch { $failed = $true }
Assert-True $failed 'Failed CLI installation was not propagated.'
Assert-True (Same-Bytes $original ([IO.File]::ReadAllBytes($marketplacePath))) 'Failed installation did not restore exact marketplace bytes.'
$results.Add('Failure: propagated CLI error and restored marketplace bytes')
@{ passed = $results.Count; results = $results; fixture = $fixtureRoot; global_configuration_changed = $false } | ConvertTo-Json -Depth 10
