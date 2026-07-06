$ErrorActionPreference = "Stop"

function New-TextFromCodePoints {
    param([int[]]$CodePoints)
    return -join ($CodePoints | ForEach-Object { [char]$_ })
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LaunchButton = Join-Path $Root "start-dashboard.cmd"
$IconPath = Join-Path $Root "www\images\bitfinex-lending-icon.ico"
if (-not (Test-Path -LiteralPath $IconPath)) {
    $IconPath = Join-Path $Root "www\images\icon.ico"
}

if (-not (Test-Path -LiteralPath $LaunchButton)) {
    throw "Missing launch button: $LaunchButton"
}
if (-not (Test-Path -LiteralPath $IconPath)) {
    throw "Missing icon file: $IconPath"
}

$ChineseName = "Bitfinex " + (New-TextFromCodePoints @(0x81EA, 0x52A8, 0x653E, 0x8D37, 0x673A, 0x5668, 0x4EBA)) + ".lnk"
$Shell = New-Object -ComObject WScript.Shell
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutTargets = @(
    (Join-Path $Root $ChineseName),
    (Join-Path $Desktop $ChineseName)
)

foreach ($ShortcutPath in $ShortcutTargets) {
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $LaunchButton
    $Shortcut.WorkingDirectory = $Root
    $Shortcut.IconLocation = $IconPath
    $Shortcut.Description = "Start Bitfinex Lending Bot dashboard"
    $Shortcut.Save()
    Write-Host "Created: $ShortcutPath"
}

Write-Host ""
Write-Host "Done."
