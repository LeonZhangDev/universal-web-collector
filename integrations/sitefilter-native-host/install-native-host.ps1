[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()][string]$ProjectPath,
    [Parameter()][switch]$SkipProvision,
    [Parameter()][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Distro,
    # Test-only overrides. Never use them for a normal installation.
    [Parameter(DontShow)][string]$InstallRoot,
    [Parameter(DontShow)][string]$RegistryRoot = 'HKCU:\Software',
    [Parameter(DontShow)][string]$TestHostExecutable,
    [Parameter(DontShow)][switch]$SkipSelfCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$HostName = 'dev.zackzhang.sitefilter_collector'
$ExtensionId = 'jaihdgjnnpmiabeoefmihmjhoodcjlhf'
$ExtensionOrigin = "chrome-extension://$ExtensionId/"
$PyInstallerVersion = '6.16.0'
$IntegrationRoot = $PSScriptRoot

function Invoke-External {
    param([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage)
    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit $LASTEXITCODE): $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine).Trim()
}

function Resolve-ProjectPath {
    param([string]$Candidate)
    if (-not $Candidate) {
        if (-not [Environment]::UserInteractive) {
            throw 'ProjectPath is required in a noninteractive session.'
        }
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Select the Universal Web Collector project folder'
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            throw 'Project folder selection was cancelled.'
        }
        $Candidate = $dialog.SelectedPath
    }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) {
        throw "ProjectPath is not a directory: $Candidate"
    }
    $resolved = (Resolve-Path -LiteralPath $Candidate).Path
    foreach ($marker in @('Makefile', 'pyproject.toml', 'integrations\sitefilter-native-host\host.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolved $marker) -PathType Leaf)) {
            throw "ProjectPath is missing required marker: $marker"
        }
    }
    return $resolved
}

function Get-DefaultDistro {
    $name = Invoke-External 'wsl.exe' @('-e', 'sh', '-lc', 'printf %s "$WSL_DISTRO_NAME"') 'Could not detect the default WSL distribution'
    if ($name -notmatch '^[A-Za-z0-9._-]{1,64}$') {
        throw 'The default WSL distribution name is empty or invalid. Pass -Distro explicitly.'
    }
    return $name
}

function Convert-WithWslPath {
    param([string]$Distribution, [string]$Path, [ValidateSet('Linux', 'Windows')][string]$Direction)
    $switches = if ($Direction -eq 'Linux') { @('-a', '-u') } else { @('-a', '-w') }
    $converted = Invoke-External 'wsl.exe' (@('-d', $Distribution, '--exec', 'wslpath') + $switches + @($Path)) "Could not convert path with WSL"
    if ($Direction -eq 'Linux' -and ($converted -notmatch '^/' -or $converted.Contains('\') -or $converted.Contains('..'))) {
        throw "WSL returned an invalid Linux path: $converted"
    }
    if ($Direction -eq 'Windows' -and -not [System.IO.Path]::IsPathRooted($converted)) {
        throw "WSL returned an invalid Windows path: $converted"
    }
    return $converted
}

function Set-PrivateAcl {
    param([string]$Path)
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Invoke-External 'icacls.exe' @($Path, '/inheritance:r', '/grant:r', "*$($sid):(OI)(CI)F", '/q') 'Could not restrict the install directory ACL' | Out-Null
}

function Set-PrivateFileAcl {
    param([string]$Path)
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Invoke-External 'icacls.exe' @($Path, '/inheritance:r', '/grant:r', "*$($sid):F", '/q') 'Could not restrict an installed file ACL' | Out-Null
}

function Write-NativeFrame {
    param([System.IO.Stream]$Stream, [hashtable]$Message)
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Message | ConvertTo-Json -Compress))
    $header = [BitConverter]::GetBytes([uint32]$bytes.Length)
    $Stream.Write($header, 0, 4)
    $Stream.Write($bytes, 0, $bytes.Length)
    $Stream.Flush()
}

function Read-NativeFrame {
    param([System.IO.Stream]$Stream)
    $header = New-Object byte[] 4
    if ($Stream.Read($header, 0, 4) -ne 4) { throw 'Packaged host returned no complete frame.' }
    $length = [BitConverter]::ToUInt32($header, 0)
    if ($length -lt 1 -or $length -gt 1MB) { throw "Packaged host returned invalid frame length: $length" }
    $body = New-Object byte[] $length
    $offset = 0
    while ($offset -lt $length) {
        $read = $Stream.Read($body, $offset, $length - $offset)
        if ($read -eq 0) { throw 'Packaged host response ended early.' }
        $offset += $read
    }
    return ([Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json)
}

$ResolvedProject = Resolve-ProjectPath $ProjectPath
if (-not $Distro) { $Distro = Get-DefaultDistro }
$LinuxProject = Convert-WithWslPath $Distro $ResolvedProject Linux
$RuntimeFile = Convert-WithWslPath $Distro "$LinuxProject/data/native-runtime.json" Windows
if (-not $InstallRoot) {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'SiteFilter\NativeHost'
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$ExecutablePath = Join-Path $InstallRoot 'sitefilter-native-host.exe'
$OwnerPath = Join-Path $InstallRoot '.sitefilter-native-host-owned.json'
$ChromeKey = Join-Path $RegistryRoot "Google\Chrome\NativeMessagingHosts\$HostName"
$EdgeKey = Join-Path $RegistryRoot "Microsoft\Edge\NativeMessagingHosts\$HostName"

$plan = [ordered]@{
    project_path = $ResolvedProject
    linux_project_path = $LinuxProject
    runtime_file = $RuntimeFile
    distro = $Distro
    install_root = $InstallRoot
    manifest_path = $ManifestPath
    allowed_origin = $ExtensionOrigin
    chrome_registry_key = $ChromeKey
    edge_registry_key = $EdgeKey
}
if ($WhatIfPreference) {
    [PSCustomObject]$plan | ConvertTo-Json -Compress
    return
}

if ((Test-Path -LiteralPath $InstallRoot) -and -not (Test-Path -LiteralPath $OwnerPath)) {
    $existing = @(Get-ChildItem -LiteralPath $InstallRoot -Force -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        $backupRoot = "$InstallRoot.preinstall-backup-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"
        if (Test-Path -LiteralPath $backupRoot) { throw "Backup target already exists: $backupRoot" }
        if ($PSCmdlet.ShouldProcess($InstallRoot, "Preserve unowned preexisting contents at $backupRoot")) {
            Move-Item -LiteralPath $InstallRoot -Destination $backupRoot
            Write-Warning "Preserved unowned preexisting contents at: $backupRoot"
        }
    }
}

if (-not $SkipProvision) {
    & wsl.exe -d $Distro --exec sh -lc 'command -v uv >/dev/null 2>&1'
    if ($LASTEXITCODE -ne 0) {
        Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', 'curl -LsSf https://astral.sh/uv/install.sh | sh') 'Could not install uv in WSL from the official Astral installer' | Out-Null
    }
    $wslMake = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; cd "$1"; exec make "$2"'
    Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', $wslMake, 'sitefilter-installer', $LinuxProject, 'install') 'WSL make install failed' | Out-Null
    Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', $wslMake, 'sitefilter-installer', $LinuxProject, 'build') 'WSL make build failed' | Out-Null
}

if ($PSCmdlet.ShouldProcess($InstallRoot, 'Install the SiteFilter native messaging host')) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Set-PrivateAcl $InstallRoot

    if ($TestHostExecutable) {
        if (-not (Test-Path -LiteralPath $TestHostExecutable -PathType Leaf)) { throw 'TestHostExecutable does not exist.' }
        Copy-Item -LiteralPath $TestHostExecutable -Destination $ExecutablePath -Force
    } else {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) {
            $installer = Join-Path $env:TEMP 'sitefilter-uv-installer.ps1'
            try {
                Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
                if ($LASTEXITCODE -ne 0) { throw "uv installer exited with code $LASTEXITCODE" }
            } finally {
                Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
            }
            $uv = Get-Command uv -ErrorAction SilentlyContinue
            if (-not $uv) { throw 'uv installation completed but uv.exe was not found on PATH.' }
        }
        $buildRoot = Join-Path $InstallRoot 'build'
        New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
        Invoke-External $uv.Source @('tool', 'run', '--from', "pyinstaller==$PyInstallerVersion", 'pyinstaller', '--noconfirm', '--clean', '--onefile', '--name', 'sitefilter-native-host', '--distpath', $InstallRoot, '--workpath', (Join-Path $buildRoot 'work'), '--specpath', $buildRoot, (Join-Path $IntegrationRoot 'host.py')) 'PyInstaller packaging failed' | Out-Null
        Remove-Item -LiteralPath $buildRoot -Recurse -Force
    }

    $config = [ordered]@{
        protocol_version = 1
        allowed_origins = @($ExtensionOrigin)
        distro = $Distro
        project_path = $LinuxProject
        runtime_file = $RuntimeFile
        startup_timeout_seconds = 60
    }
    $manifestTemplate = Get-Content -LiteralPath (Join-Path $IntegrationRoot 'host-manifest.template.json') -Raw
    $manifest = $manifestTemplate.Replace('__HOST_EXECUTABLE_PATH__', ($ExecutablePath.Replace('\', '\\'))).Replace('__SITEFILTER_EXTENSION_ORIGIN__', $ExtensionOrigin)
    $config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $InstallRoot 'config.json') -Encoding utf8
    $manifest | Set-Content -LiteralPath $ManifestPath -Encoding utf8
    [ordered]@{ host_name = $HostName; install_root = $InstallRoot; version = 1 } | ConvertTo-Json | Set-Content -LiteralPath $OwnerPath -Encoding utf8
    foreach ($privateFile in @((Join-Path $InstallRoot 'config.json'), $ManifestPath, $OwnerPath, $ExecutablePath)) {
        Set-PrivateFileAcl $privateFile
    }

    $backups = @()
    foreach ($key in @($ChromeKey, $EdgeKey)) {
        if (Test-Path -LiteralPath $key) {
            $old = (Get-Item -LiteralPath $key).GetValue('')
            if ($old -and $old -ne $ManifestPath) { $backups += [ordered]@{ key = $key; value = $old } }
        }
    }
    if ($backups.Count -gt 0) {
        $backups | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $InstallRoot 'preexisting-registry-backup.json') -Encoding utf8
    }
    foreach ($key in @($ChromeKey, $EdgeKey)) {
        New-Item -Path $key -Force | Out-Null
        Set-Item -LiteralPath $key -Value $ManifestPath
    }
}

if (-not $SkipSelfCheck) {
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = $ExecutablePath
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardInput = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.ArgumentList.Add($ExtensionOrigin)
    if (-not $process.Start()) { throw 'Could not start packaged native host.' }
    Write-NativeFrame $process.StandardInput.BaseStream @{ v = 1; id = 'installer-self-check'; action = 'ping'; payload = @{} }
    $response = Read-NativeFrame $process.StandardOutput.BaseStream
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(70000)) { $process.Kill(); throw 'Native host self-check timed out.' }
    if ($response.id -ne 'installer-self-check' -or -not $response.ok) {
        throw "Native host self-check failed: $($response | ConvertTo-Json -Compress)"
    }
    Write-Host "Native host self-check: $($response | ConvertTo-Json -Compress)"
}

[PSCustomObject]$plan | ConvertTo-Json -Compress
