$ErrorActionPreference = 'Stop'
$candidates = @()
if ($env:PYCHARM_HARNESS_PYTHON) { $candidates += $env:PYCHARM_HARNESS_PYTHON }
$command = Get-Command python.exe -ErrorAction SilentlyContinue
if ($command -and $command.Source -notmatch 'WindowsApps') { $candidates += $command.Source }
foreach ($root in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore')) {
    if (Test-Path -LiteralPath $root) {
        foreach ($version in Get-ChildItem -LiteralPath $root | Sort-Object PSChildName -Descending) {
            $key = Join-Path $version.PSPath 'InstallPath'
            if (Test-Path -LiteralPath $key) {
                $data = Get-ItemProperty -LiteralPath $key
                if ($data.ExecutablePath) { $candidates += $data.ExecutablePath }
                elseif ($data.'(default)') { $candidates += Join-Path $data.'(default)' 'python.exe' }
            }
        }
    }
}
$interpreter = $null
foreach ($candidate in $candidates | Select-Object -Unique) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        & $candidate -c "import platform,struct,sys;sys.exit(0 if sys.version_info >= (3,11) and sys.platform == 'win32' and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64') else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $interpreter = $candidate; break }
    }
}
if (-not $interpreter) { throw '64-bit Python 3.11+ for Windows AMD64 was not found. Set PYCHARM_HARNESS_PYTHON to a supported python.exe.' }
& $interpreter (Join-Path $PSScriptRoot 'harness.py') @args
exit $LASTEXITCODE
