[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    # Test-only overrides. Never use them for a normal uninstall.
    [Parameter(DontShow)][string]$InstallRoot,
    [Parameter(DontShow)][string]$RegistryRoot = 'HKCU:\Software'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$HostName = 'dev.zackzhang.sitefilter_collector'
$LegacyOwnedFiles = @('sitefilter-native-host.exe', 'config.json', "$HostName.json", '.sitefilter-native-host-owned.json', 'native-host.log', 'native-host.log.1', 'native-host.log.2', 'collector-startup.log', 'preexisting-registry-backup.json')

function ConvertFrom-RegistryData {
    param($Data, [Microsoft.Win32.RegistryValueKind]$Kind)
    switch ($Kind) {
        { $_ -in @([Microsoft.Win32.RegistryValueKind]::Binary, [Microsoft.Win32.RegistryValueKind]::None) } { return ,([Convert]::FromBase64String([string]$Data)) }
        ([Microsoft.Win32.RegistryValueKind]::MultiString) { return ,([string[]]@($Data)) }
        ([Microsoft.Win32.RegistryValueKind]::DWord) { return [int]$Data }
        ([Microsoft.Win32.RegistryValueKind]::QWord) { return [long]::Parse([string]$Data, [Globalization.CultureInfo]::InvariantCulture) }
        default { return [string]$Data }
    }
}

function Open-RegistryKeyWritable {
    param([string]$Path)
    if ($Path -notlike 'HKCU:\*') { throw "Registry path is not under HKCU: $Path" }
    $subPath = $Path.Substring('HKCU:\'.Length)
    return [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($subPath, $true)
}

function Restore-RegistryTree {
    param($Snapshot, [string]$OwnedManifestPath, [string]$AllowedRoot)
    $path = [string]$Snapshot.path
    $allowedPrefix = $AllowedRoot.TrimEnd('\') + '\'
    if ($path -ne $AllowedRoot -and -not $path.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Registry snapshot escapes its exact host key: $path" }

    if (-not $Snapshot.existed) {
        if (Test-Path -LiteralPath $path) {
            $key = Open-RegistryKeyWritable $path
            try {
                if ($key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) -eq $OwnedManifestPath) { $key.DeleteValue('', $false) }
                $isEmpty = $key.ValueCount -eq 0 -and $key.SubKeyCount -eq 0
            } finally { $key.Dispose() }
            if ($isEmpty) { Remove-Item -LiteralPath $path -Force }
        }
        return
    }

    if (-not (Test-Path -LiteralPath $path)) { New-Item -Path $path -Force | Out-Null }
    $key = Open-RegistryKeyWritable $path
    try {
        foreach ($value in @($Snapshot.values)) {
            $name = [string]$value.name
            if ($name -eq '') {
                $current = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                if ($null -ne $current -and $current -ne $OwnedManifestPath) { Write-Warning "Preserving a later default registry value at: $path"; continue }
            }
            $kind = [Microsoft.Win32.RegistryValueKind][Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$value.kind)
            $key.SetValue($name, (ConvertFrom-RegistryData $value.data $kind), $kind)
        }
        $originalNames = @($Snapshot.values | ForEach-Object { [string]$_.name })
        if ($originalNames -notcontains '') {
            $current = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($current -eq $OwnedManifestPath) { $key.DeleteValue('', $false) }
        }
    } finally { $key.Dispose() }
    foreach ($child in @($Snapshot.children)) { Restore-RegistryTree $child $OwnedManifestPath $AllowedRoot }
}

if (-not $InstallRoot) {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'SiteFilter\NativeHost'
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$RootPrefix = $InstallRoot.TrimEnd('\') + '\'
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$OwnerPath = Join-Path $InstallRoot '.sitefilter-native-host-owned.json'
$keys = @((Join-Path $RegistryRoot "Google\Chrome\NativeMessagingHosts\$HostName"), (Join-Path $RegistryRoot "Microsoft\Edge\NativeMessagingHosts\$HostName"))
foreach ($key in $keys) {
    if ($key -notlike 'HKCU:\Software\*' -or -not $key.EndsWith("\$HostName", [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe registry target: $key" }
}

if (-not (Test-Path -LiteralPath $InstallRoot)) { Write-Host 'SiteFilter native host is not installed.'; return }
if (-not (Test-Path -LiteralPath $OwnerPath -PathType Leaf)) { throw "Ownership marker missing; refusing to change InstallRoot: $InstallRoot" }
$state = Get-Content -LiteralPath $OwnerPath -Raw | ConvertFrom-Json
if ($state.host_name -ne $HostName -or [System.IO.Path]::GetFullPath([string]$state.install_root) -ne $InstallRoot) { throw "Ownership marker does not match InstallRoot; refusing removal: $InstallRoot" }

if ($state.version -eq 2) {
    foreach ($snapshot in @($state.registry_before)) {
        $snapshotPath = [string]$snapshot.path
        $allowedRoot = $keys | Where-Object { $_ -eq $snapshotPath } | Select-Object -First 1
        if (-not $allowedRoot) { throw "Ownership marker contains an unexpected registry root: $snapshotPath" }
        if ($PSCmdlet.ShouldProcess($snapshotPath, 'Restore pre-install registry state while preserving later additions')) { Restore-RegistryTree $snapshot $ManifestPath $allowedRoot }
    }
    $ownedFiles = @($state.owned_files)
} else {
    foreach ($keyPath in $keys) {
        if (Test-Path -LiteralPath $keyPath) {
            $key = Open-RegistryKeyWritable $keyPath
            try {
                if ($key.GetValue('') -eq $ManifestPath -and $PSCmdlet.ShouldProcess($keyPath, 'Remove legacy owned default registration')) {
                    $key.DeleteValue('', $false)
                    $isEmpty = $key.ValueCount -eq 0 -and $key.SubKeyCount -eq 0
                } else { $isEmpty = $false }
            } finally { $key.Dispose() }
            if ($isEmpty) {
                Remove-Item -LiteralPath $keyPath -Force
            }
        }
    }
    $ownedFiles = $LegacyOwnedFiles
}

foreach ($relativePath in @($ownedFiles | Where-Object { $_ -ne '.sitefilter-native-host-owned.json' }) + @('.sitefilter-native-host-owned.json')) {
    if ([string]::IsNullOrWhiteSpace([string]$relativePath) -or [System.IO.Path]::IsPathRooted([string]$relativePath)) { throw "Unsafe owned file entry: $relativePath" }
    $target = [System.IO.Path]::GetFullPath((Join-Path $InstallRoot ([string]$relativePath)))
    if (-not $target.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Owned file escapes InstallRoot: $relativePath" }
    if (Test-Path -LiteralPath $target -PathType Container) { throw "Owned file entry unexpectedly names a directory: $target" }
    if ((Test-Path -LiteralPath $target -PathType Leaf) -and $PSCmdlet.ShouldProcess($target, 'Remove recorded owned file')) { Remove-Item -LiteralPath $target -Force }
}

if ((Test-Path -LiteralPath $InstallRoot) -and @(Get-ChildItem -LiteralPath $InstallRoot -Force).Count -eq 0 -and $PSCmdlet.ShouldProcess($InstallRoot, 'Remove empty owned install directory')) { Remove-Item -LiteralPath $InstallRoot -Force }
elseif (Test-Path -LiteralPath $InstallRoot) { Write-Warning "Preserved unexpected files or directories under: $InstallRoot" }

Write-Host 'SiteFilter native host registrations and recorded integration files were removed. Collector data and unexpected files were not touched.'
