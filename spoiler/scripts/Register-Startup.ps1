param(
    [string]$ExecutablePath = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Spoiler\App\Spoiler.exe')
)

$ErrorActionPreference = 'Stop'
$target = (Resolve-Path -LiteralPath $ExecutablePath).ProviderPath
if ([IO.Path]::GetFileName($target) -ne 'Spoiler.exe') {
    throw 'The startup target must be the installed Spoiler.exe.'
}
$startupDirectory = [Environment]::GetFolderPath('Startup')
if (-not $startupDirectory) { throw 'The current-user Startup folder is unavailable.' }
$shortcutPath = Join-Path $startupDirectory 'Spoiler.lnk'
$helperPath = Join-Path $PSScriptRoot '..\build\CreateNoTrackShortcut.exe'
if (-not (Test-Path -LiteralPath $helperPath)) {
    throw 'Compile tools\CreateNoTrackShortcut.cs into build\CreateNoTrackShortcut.exe first.'
}
$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.TargetPath -and
        -not [string]::Equals($shortcut.TargetPath, $target, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'An existing Spoiler startup shortcut points elsewhere; it was not replaced.'
    }
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) | Out-Null
    $shortcut = $null
    & $helperPath $shortcutPath $target ([IO.Path]::GetDirectoryName($target))
    if ($LASTEXITCODE -ne 0) { throw 'Startup shortcut creation failed.' }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    if (-not [string]::Equals($shortcut.TargetPath, $target, [StringComparison]::OrdinalIgnoreCase) -or
        $shortcut.Arguments -ne '' -or $shortcut.WindowStyle -ne 1) {
        throw 'Startup shortcut verification failed.'
    }
    [pscustomobject]@{
        Shortcut = $shortcutPath
        Target = $shortcut.TargetPath
        WorkingDirectory = $shortcut.WorkingDirectory
        Arguments = $shortcut.Arguments
        WindowStyle = $shortcut.WindowStyle
        TargetSHA256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    } | Format-List
} finally {
    if ($shortcut -and [Runtime.InteropServices.Marshal]::IsComObject($shortcut)) {
        [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) | Out-Null
    }
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null
}
