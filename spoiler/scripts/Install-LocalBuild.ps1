param([switch]$RegisterStartup)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$source = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'build\Spoiler.exe')).ProviderPath
$installDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Spoiler\App'
$target = [IO.Path]::GetFullPath((Join-Path $installDirectory 'Spoiler.exe'))
$expectedHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
$running = @(Get-Process -Name Spoiler -ErrorAction SilentlyContinue | Where-Object {
    [string]::Equals($_.Path, $target, [StringComparison]::OrdinalIgnoreCase)
})
if ($running.Count -gt 0) { throw 'Exit the installed Spoiler from its tray menu before replacing it.' }

New-Item -ItemType Directory -Force -Path $installDirectory | Out-Null
$backup = $null
if (Test-Path -LiteralPath $target) {
    $previousHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ($previousHash -ne $expectedHash) {
        $backupDirectory = Join-Path $projectRoot 'build\backup'
        New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
        $backup = Join-Path $backupDirectory ('Spoiler-' + $previousHash.Substring(0, 16) + '.exe')
        if (Test-Path -LiteralPath $backup) {
            if ((Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash -ne $previousHash) {
                throw 'Existing rollback backup differs; it was preserved.'
            }
        } else { Copy-Item -LiteralPath $target -Destination $backup }
    }
}

$incoming = $target + '.incoming-' + [Guid]::NewGuid().ToString('N')
try {
    Copy-Item -LiteralPath $source -Destination $incoming
    if ((Get-FileHash -LiteralPath $incoming -Algorithm SHA256).Hash -ne $expectedHash) {
        throw 'Installation candidate checksum mismatch.'
    }
    if (Test-Path -LiteralPath $target) {
        [IO.File]::Replace($incoming, $target, [System.Management.Automation.Language.NullString]::Value)
    }
    else { [IO.File]::Move($incoming, $target) }
} finally {
    # Only this invocation's exact generated candidate is removed.
    if (Test-Path -LiteralPath $incoming) { Remove-Item -LiteralPath $incoming }
}
if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'Installed executable checksum mismatch.'
}
if ($RegisterStartup) { & (Join-Path $PSScriptRoot 'Register-Startup.ps1') -ExecutablePath $target }
[pscustomobject]@{
    Installed = $target
    Version = (Get-Item -LiteralPath $target).VersionInfo.FileVersion
    SHA256 = $expectedHash
    RollbackBackup = $backup
}
