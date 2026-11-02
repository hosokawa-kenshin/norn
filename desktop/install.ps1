param(
    [string]$Distribution = '',
    [string]$LinuxProjectRoot = '',
    [switch]$SmokeTest,
    [switch]$NoLaunch
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

Write-Output 'Building the existing frontend inside WSL...'
& wsl.exe -d $Distribution --cd $LinuxProjectRoot -- bash desktop/build_frontend.sh
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }

$Manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'package.json') -Raw | ConvertFrom-Json
$Version = $Manifest.devDependencies.electron
$Architecture = if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq 'Arm64') { 'arm64' } else { 'x64' }
$InstallRoot = Join-Path $env:LOCALAPPDATA 'NORN'
$RuntimeDirectory = Join-Path $InstallRoot "runtime\electron-$Version-$Architecture"
$Executable = Join-Path $RuntimeDirectory 'electron.exe'
$ApplicationDirectory = Join-Path $InstallRoot 'app'
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null

if (-not (Test-Path -LiteralPath $Executable)) {
    $ArchiveName = "electron-v$Version-win32-$Architecture.zip"
    $Archive = Join-Path $InstallRoot $ArchiveName
    $Release = "https://github.com/electron/electron/releases/download/v$Version"
    Write-Output "Downloading official Electron $Version for Windows $Architecture..."
    Invoke-WebRequest -Uri "$Release/$ArchiveName" -OutFile $Archive
    $Checksums = (Invoke-WebRequest -Uri "$Release/SHASUMS256.txt").Content
    if ($Checksums -is [byte[]]) { $Checksums = [Text.Encoding]::UTF8.GetString($Checksums) }
    $Pattern = '(?m)^([a-fA-F0-9]{64})\s+\*?' + [regex]::Escape($ArchiveName) + '\s*$'
    $Match = [regex]::Match([string]$Checksums, $Pattern)
    if (-not $Match.Success) { throw 'The official runtime checksum was not found.' }
    $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
    if ($Actual -ne $Match.Groups[1].Value) { throw 'Electron checksum verification failed.' }
    New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null
    Expand-Archive -LiteralPath $Archive -DestinationPath $RuntimeDirectory -Force
    Remove-Item -LiteralPath $Archive
}

New-Item -ItemType Directory -Force -Path $ApplicationDirectory | Out-Null
foreach ($Name in @('main.cjs', 'preload.cjs', 'core.cjs', 'package.json')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $Name) -Destination (Join-Path $ApplicationDirectory $Name) -Force
}
$FrontendDirectory = Join-Path $ApplicationDirectory 'frontend'
if (Test-Path -LiteralPath $FrontendDirectory) { Remove-Item -LiteralPath $FrontendDirectory -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $ProjectDirectory 'frontend\dist') -Destination $FrontendDirectory -Recurse
$Configuration = @{ distribution = $Distribution; projectRoot = $LinuxProjectRoot; autoStart = $true }
$Json = $Configuration | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $ApplicationDirectory 'config.json'), $Json, [Text.UTF8Encoding]::new($false))

Add-Type -AssemblyName System.Drawing
$Assets = Join-Path $ApplicationDirectory 'assets'
New-Item -ItemType Directory -Force -Path $Assets | Out-Null
$Bitmap = [Drawing.Bitmap]::new(64, 64)
$Graphics = [Drawing.Graphics]::FromImage($Bitmap)
$Graphics.Clear([Drawing.Color]::FromArgb(32, 43, 36))
$Graphics.TextRenderingHint = [Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$Font = [Drawing.Font]::new('Segoe UI', 34, [Drawing.FontStyle]::Bold)
$Brush = [Drawing.SolidBrush]::new([Drawing.Color]::FromArgb(217, 240, 129))
$Graphics.DrawString('N', $Font, $Brush, [Drawing.PointF]::new(5, 2))
$Bitmap.Save((Join-Path $Assets 'norn.png'), [Drawing.Imaging.ImageFormat]::Png)
$Brush.Dispose()
$Font.Dispose()
$Graphics.Dispose()
$Bitmap.Dispose()

if (-not $SmokeTest) {
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'NORN.lnk'
    $Shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Executable
    $Shortcut.Arguments = '"' + $ApplicationDirectory + '"'
    $Shortcut.WorkingDirectory = $ApplicationDirectory
    $Shortcut.Description = 'NORN personal assistant'
    $Shortcut.Save()
    Write-Output "Start menu shortcut: $ShortcutPath"
}
Write-Output "Installed application: $ApplicationDirectory"
if ($NoLaunch) { return }
if ($SmokeTest) {
    try {
        Add-Type -AssemblyName System.Speech
        $Synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
        $JapaneseVoice = $Synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'ja-*' } | Select-Object -First 1
        if ($JapaneseVoice) {
            $Synth.SelectVoice($JapaneseVoice.VoiceInfo.Name)
            $Format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
            $Synth.SetOutputToWaveFile((Join-Path $ApplicationDirectory 'frontend\voice\desktop-smoke-norn.wav'), $Format)
            $Synth.Speak('ノルン。')
        }
        $Synth.Dispose()
    } catch { Write-Output 'Synthetic Japanese voice unavailable; skipping virtual-audio test.' }
    $SmokeDirectory = Join-Path $InstallRoot 'smoke-test'
    New-Item -ItemType Directory -Force -Path $SmokeDirectory | Out-Null
    $env:NORN_DESKTOP_USER_DATA = $SmokeDirectory
    $ResultPath = Join-Path $SmokeDirectory 'smoke-result.json'
    if (Test-Path -LiteralPath $ResultPath) { Remove-Item -LiteralPath $ResultPath }
    $Process = Start-Process -FilePath $Executable -ArgumentList @(('"' + $ApplicationDirectory + '"'), '--smoke-test') -WorkingDirectory $ApplicationDirectory -PassThru -Wait
    Remove-Item Env:\NORN_DESKTOP_USER_DATA
    if ($Process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $ResultPath)) {
        throw "Native smoke test failed. Inspect $SmokeDirectory\desktop.log"
    }
    Get-Content -LiteralPath $ResultPath
} else {
    Start-Process -FilePath $Executable -ArgumentList ('"' + $ApplicationDirectory + '"') -WorkingDirectory $ApplicationDirectory
    Write-Output 'NORN launched. Autostart is enabled by the application; the microphone remains off until enabled.'
}