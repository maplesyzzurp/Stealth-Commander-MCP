[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDirectory
)

$ErrorActionPreference = 'Stop'
$reaVersion = '6.7.0'
$packageName = 'rea-agents'
$lockSource = Join-Path $PSScriptRoot 'rea-lock'
$lockDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $lockSource 'package-lock.json')).Hash

function Get-NormalizedFullPath([string]$Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.Length -gt $pathRoot.Length) {
        $fullPath = $fullPath.TrimEnd([char[]]@('\', '/'))
    }
    return $fullPath
}

$packageRoot = Get-NormalizedFullPath (Split-Path -Parent $PSScriptRoot)
$installPath = Get-NormalizedFullPath $InstallDirectory
$packagePrefix = $packageRoot + [System.IO.Path]::DirectorySeparatorChar
if ($installPath.Equals($packageRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $installPath.StartsWith($packagePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Choose an install directory outside this package.'
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $nodeCommand) { throw 'Node.js is required. Install a supported Node.js release, then rerun this script.' }
if ($null -eq $npmCommand) { throw 'npm.cmd is required. Install npm with Node.js, then rerun this script.' }

$nodeOutput = (& $nodeCommand.Source --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $nodeOutput -notmatch '^v(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)') {
    throw "Could not read the Node.js version: $nodeOutput"
}
$nodeMajor = [int]$Matches.major
$nodeMinor = [int]$Matches.minor
$supportedNode = ($nodeMajor -eq 22 -and $nodeMinor -ge 19) -or
    ($nodeMajor -eq 24 -and $nodeMinor -ge 11) -or
    ($nodeMajor -ge 26)
if (-not $supportedNode) {
    throw "REA $reaVersion requires Node.js 22.19+, 24.11+, or 26+. Found $nodeOutput."
}
$npmOutput = (& $npmCommand.Source --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not run npm: $npmOutput" }

$markerPath = Join-Path $installPath '.rea-setup.json'
$alreadyInstalled = $false
if (Test-Path -LiteralPath $installPath) {
    if (-not (Test-Path -LiteralPath $installPath -PathType Container)) {
        throw "Install path is not a directory: $installPath"
    }
    if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        if ($marker.package -ne $packageName -or
            $marker.version -ne $reaVersion -or
            $marker.dependency_lock_sha256 -ne $lockDigest -or
            $marker.install_directory -ne $installPath) {
            throw "The existing setup marker does not match this installation: $installPath"
        }
        $alreadyInstalled = $true
    } elseif (@(Get-ChildItem -LiteralPath $installPath -Force).Count -gt 0) {
        throw "Install directory is not empty and has no REA setup marker: $installPath"
    }
} else {
    New-Item -ItemType Directory -Path $installPath -Force | Out-Null
}

if (-not $alreadyInstalled) {
    $npmCache = Join-Path $installPath '.npm-cache'
    Copy-Item -LiteralPath (Join-Path $lockSource 'package.json') -Destination (Join-Path $installPath 'package.json')
    Copy-Item -LiteralPath (Join-Path $lockSource 'package-lock.json') -Destination (Join-Path $installPath 'package-lock.json')
    & $npmCommand.Source --cache $npmCache --prefix $installPath ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
    if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE." }
}

$reaPackage = Join-Path $installPath "node_modules\$packageName\package.json"
$cliEntry = Join-Path $installPath "node_modules\$packageName\scripts\rea.mjs"
if (-not (Test-Path -LiteralPath $reaPackage -PathType Leaf) -or
    -not (Test-Path -LiteralPath $cliEntry -PathType Leaf)) {
    throw 'The pinned REA package or CLI entry is missing from the local install.'
}
$installedPackage = Get-Content -LiteralPath $reaPackage -Raw | ConvertFrom-Json
if ($installedPackage.version -ne $reaVersion) {
    throw "Expected REA $reaVersion but found $($installedPackage.version)."
}

$marker = [ordered]@{
    package = $packageName
    version = $reaVersion
    install_directory = $installPath
    dependency_lock_sha256 = $lockDigest
}
$markerJson = $marker | ConvertTo-Json
[System.IO.File]::WriteAllText(
    $markerPath,
    $markerJson + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)

$helpOutput = & $nodeCommand.Source $cliEntry capture-browser-scenario --help 2>&1
$helpExitCode = $LASTEXITCODE
$helpText = $helpOutput | Out-String
if ($helpExitCode -ne 0 -or $helpText -notmatch 'Usage: rea capture-browser-scenario') {
    throw "REA CLI help check failed with exit code $helpExitCode."
}

Write-Output "Installed $packageName $reaVersion locally."
Write-Output "Node.js: $nodeOutput; npm: $npmOutput"
Write-Output "CLI entry: $cliEntry"
Write-Output 'CLI check: capture-browser-scenario --help passed.'
Write-Output "Example: & '$($nodeCommand.Source)' '$cliEntry' list-browser-targets 'http://127.0.0.1:9222' --format json"
