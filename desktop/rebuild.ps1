param(
    [string]$Distribution = '',
    [string]$LinuxProjectRoot = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectDirectory = Split-Path -Parent $PSScriptRoot
if ($ProjectDirectory -match '^\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)(\\.+)$') {
    if (-not $Distribution) { $Distribution = $Matches[1] }
    if (-not $LinuxProjectRoot) { $LinuxProjectRoot = $Matches[2].Replace('\', '/') }
}
if (-not $Distribution -or -not $LinuxProjectRoot.StartsWith('/')) {
    throw 'Specify -Distribution and -LinuxProjectRoot, or run this script from the WSL project.'
}

Write-Output 'Checking the WSL Python backend...'
& wsl.exe -d $Distribution --cd $LinuxProjectRoot -- .venv/bin/python -m compileall -q backend
if ($LASTEXITCODE -ne 0) { throw 'Backend syntax check failed.' }
Write-Output 'Backend syntax check passed.'

Write-Output 'Building and installing the Windows application without launching it...'
& (Join-Path $PSScriptRoot 'install.ps1') `
    -Distribution $Distribution `
    -LinuxProjectRoot $LinuxProjectRoot `
    -NoLaunch
