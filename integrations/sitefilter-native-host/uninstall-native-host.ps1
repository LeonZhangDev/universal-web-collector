[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    # Test-only overrides. Never use them for a normal uninstall.
    [Parameter(DontShow)][string]$InstallRoot,
    [Parameter(DontShow)][string]$RegistryRoot = 'HKCU:\Software'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$HostName = 'dev.zackzhang.sitefilter_collector'
if (-not $InstallRoot) {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'SiteFilter\NativeHost'
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$OwnerPath = Join-Path $InstallRoot '.sitefilter-native-host-owned.json'
$keys = @(
    (Join-Path $RegistryRoot "Google\Chrome\NativeMessagingHosts\$HostName"),
    (Join-Path $RegistryRoot "Microsoft\Edge\NativeMessagingHosts\$HostName")
)
$registryBackup = @()
$registryBackupPath = Join-Path $InstallRoot 'preexisting-registry-backup.json'
if (Test-Path -LiteralPath $registryBackupPath -PathType Leaf) {
    $registryBackup = @(Get-Content -LiteralPath $registryBackupPath -Raw | ConvertFrom-Json)
}

foreach ($key in $keys) {
    if (Test-Path -LiteralPath $key) {
        $current = (Get-Item -LiteralPath $key).GetValue('')
        if ($current -eq $ManifestPath -and $PSCmdlet.ShouldProcess($key, 'Remove owned native messaging registration')) {
            $saved = @($registryBackup | Where-Object { $_.key -eq $key })
            if ($saved.Count -eq 1) {
                Set-Item -LiteralPath $key -Value ([string]$saved[0].value)
            } else {
                Remove-Item -LiteralPath $key -Force
            }
        } elseif ($current -ne $ManifestPath) {
            Write-Warning "Preserving registration not owned by this installation: $key"
        }
    }
}

if (Test-Path -LiteralPath $InstallRoot) {
    if (-not (Test-Path -LiteralPath $OwnerPath -PathType Leaf)) {
        throw "Ownership marker missing; refusing to remove InstallRoot: $InstallRoot"
    }
    $owner = Get-Content -LiteralPath $OwnerPath -Raw | ConvertFrom-Json
    if ($owner.host_name -ne $HostName -or [System.IO.Path]::GetFullPath($owner.install_root) -ne $InstallRoot) {
        throw "Ownership marker does not match InstallRoot; refusing removal: $InstallRoot"
    }
    if ($PSCmdlet.ShouldProcess($InstallRoot, 'Remove owned native host files')) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
}

Write-Host 'SiteFilter native host registrations and owned integration files were removed. Collector data was not touched.'
