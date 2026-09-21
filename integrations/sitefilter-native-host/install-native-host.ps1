[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()][string]$ProjectPath,
    [Parameter()][switch]$SkipProvision,
    [Parameter()][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Distro,
    # Test-only overrides. Never use them for a normal installation.
    [Parameter(DontShow)][string]$InstallRoot,
    [Parameter(DontShow)][string]$RegistryRoot = 'HKCU:\Software',
    [Parameter(DontShow)][string]$TestHostExecutable,
    [Parameter(DontShow)][string]$TestWindowsUvPath,
    [Parameter(DontShow)][switch]$SkipSelfCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$HostName = 'dev.zackzhang.sitefilter_collector'
$ExtensionId = 'jaihdgjnnpmiabeoefmihmjhoodcjlhf'
$ExtensionOrigin = "chrome-extension://$ExtensionId/"
$PyInstallerVersion = '6.16.0'
$IntegrationRoot = $PSScriptRoot
$OwnedFileNames = @(
    'sitefilter-native-host.exe',
    'config.json',
    "$HostName.json",
    '.sitefilter-native-host-owned.json',
    'native-host.log',
    'native-host.log.1',
    'native-host.log.2',
    'collector-startup.log'
)

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
        if (-not [Environment]::UserInteractive) { throw 'ProjectPath is required in a noninteractive session.' }
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Select the Universal Web Collector project folder'
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { throw 'Project folder selection was cancelled.' }
        $Candidate = $dialog.SelectedPath
    }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) { throw "ProjectPath is not a directory: $Candidate" }
    $resolved = (Resolve-Path -LiteralPath $Candidate).Path
    foreach ($marker in @('Makefile', 'pyproject.toml', 'integrations\sitefilter-native-host\host.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolved $marker) -PathType Leaf)) { throw "ProjectPath is missing required marker: $marker" }
    }
    return $resolved
}

function Get-DefaultDistro {
    $name = Invoke-External 'wsl.exe' @('-e', 'sh', '-lc', 'printf %s "$WSL_DISTRO_NAME"') 'Could not detect the default WSL distribution'
    if ($name -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw 'The default WSL distribution name is empty or invalid. Pass -Distro explicitly.' }
    return $name
}

function Convert-WithWslPath {
    param([string]$Distribution, [string]$Path, [ValidateSet('Linux', 'Windows')][string]$Direction)
    $switches = if ($Direction -eq 'Linux') { @('-a', '-u') } else { @('-a', '-w') }
    $converted = Invoke-External 'wsl.exe' (@('-d', $Distribution, '--exec', 'wslpath') + $switches + @($Path)) 'Could not convert path with WSL'
    if ($Direction -eq 'Linux' -and ($converted -notmatch '^/' -or $converted.Contains('\') -or $converted.Contains('..'))) { throw "WSL returned an invalid Linux path: $converted" }
    if ($Direction -eq 'Windows' -and -not [System.IO.Path]::IsPathRooted($converted)) { throw "WSL returned an invalid Windows path: $converted" }
    return $converted
}

function Set-PrivateAcl {
    param([string]$Path, [switch]$Directory)
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $grant = if ($Directory) { "*$($sid):(OI)(CI)F" } else { "*$($sid):F" }
    Invoke-External 'icacls.exe' @($Path, '/inheritance:r', '/grant:r', $grant, '/q') "Could not restrict ACL: $Path" | Out-Null
}

function ConvertTo-RegistryData {
    param($Value, [Microsoft.Win32.RegistryValueKind]$Kind)
    switch ($Kind) {
        { $_ -in @([Microsoft.Win32.RegistryValueKind]::Binary, [Microsoft.Win32.RegistryValueKind]::None) } { return [Convert]::ToBase64String([byte[]]$Value) }
        ([Microsoft.Win32.RegistryValueKind]::MultiString) { return @([string[]]$Value) }
        ([Microsoft.Win32.RegistryValueKind]::DWord) { return [int64]$Value }
        ([Microsoft.Win32.RegistryValueKind]::QWord) { return ([int64]$Value).ToString([Globalization.CultureInfo]::InvariantCulture) }
        default { return [string]$Value }
    }
}

function Get-RegistryTree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return [ordered]@{ path = $Path; existed = $false; values = @(); children = @() } }
    $key = Get-Item -LiteralPath $Path
    $values = foreach ($name in $key.GetValueNames()) {
        $kind = $key.GetValueKind($name)
        $value = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        [ordered]@{ name = $name; kind = $kind.ToString(); data = ConvertTo-RegistryData $value $kind }
    }
    $children = foreach ($childName in $key.GetSubKeyNames()) { Get-RegistryTree (Join-Path $Path $childName) }
    return [ordered]@{ path = $Path; existed = $true; values = @($values); children = @($children) }
}

function Get-WindowsUv {
    param([string]$TestCandidate)
    if ($TestCandidate) {
        if (-not (Test-Path -LiteralPath $TestCandidate -PathType Leaf)) { throw "TestWindowsUvPath does not exist: $TestCandidate" }
        return (Get-Item -LiteralPath $TestCandidate).FullName
    }
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $installer = Join-Path $env:TEMP 'sitefilter-uv-installer.ps1'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) { throw "uv installer exited with code $LASTEXITCODE" }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe')
    )
    $resolved = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $resolved) { throw "uv installation completed, but uv.exe was not found at: $($candidates -join ', ')" }
    $uvDirectory = Split-Path -Parent $resolved
    $env:PATH = "$uvDirectory;$env:PATH"
    return (Get-Item -LiteralPath $resolved).FullName
}

function Write-NativeFrame {
    param([System.IO.Stream]$Stream, [hashtable]$Message)
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Message | ConvertTo-Json -Compress))
    $header = [BitConverter]::GetBytes([uint32]$bytes.Length)
    $Stream.Write($header, 0, 4); $Stream.Write($bytes, 0, $bytes.Length); $Stream.Flush()
}

function Read-NativeFrame {
    param([System.IO.Stream]$Stream)
    $header = New-Object byte[] 4
    if ($Stream.Read($header, 0, 4) -ne 4) { throw 'Packaged host returned no complete frame.' }
    $length = [BitConverter]::ToUInt32($header, 0)
    if ($length -lt 1 -or $length -gt 1MB) { throw "Packaged host returned invalid frame length: $length" }
    $body = New-Object byte[] $length; $offset = 0
    while ($offset -lt $length) { $read = $Stream.Read($body, $offset, $length - $offset); if ($read -eq 0) { throw 'Packaged host response ended early.' }; $offset += $read }
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
foreach ($key in @($ChromeKey, $EdgeKey)) {
    if ($key -notlike 'HKCU:\Software\*' -or -not $key.EndsWith("\$HostName", [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe registry target: $key" }
}

$plan = [ordered]@{ project_path = $ResolvedProject; linux_project_path = $LinuxProject; runtime_file = $RuntimeFile; distro = $Distro; install_root = $InstallRoot; manifest_path = $ManifestPath; allowed_origin = $ExtensionOrigin; chrome_registry_key = $ChromeKey; edge_registry_key = $EdgeKey }
if ($WhatIfPreference) { [PSCustomObject]$plan | ConvertTo-Json -Compress; return }

$existingState = $null
if (Test-Path -LiteralPath $OwnerPath -PathType Leaf) { $existingState = Get-Content -LiteralPath $OwnerPath -Raw | ConvertFrom-Json }
if ((Test-Path -LiteralPath $InstallRoot) -and -not $existingState) {
    $existing = @(Get-ChildItem -LiteralPath $InstallRoot -Force -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        $backupRoot = "$InstallRoot.preinstall-backup-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"
        if (Test-Path -LiteralPath $backupRoot) { throw "Backup target already exists: $backupRoot" }
        if ($PSCmdlet.ShouldProcess($InstallRoot, "Preserve unowned preexisting contents at $backupRoot")) { Move-Item -LiteralPath $InstallRoot -Destination $backupRoot; Write-Warning "Preserved unowned preexisting contents at: $backupRoot" }
    }
}

if (-not $SkipProvision) {
    & wsl.exe -d $Distro --exec sh -lc 'command -v uv >/dev/null 2>&1 || test -x "$HOME/.local/bin/uv"'
    if ($LASTEXITCODE -ne 0) { Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', 'curl -LsSf https://astral.sh/uv/install.sh | sh') 'Could not install uv in WSL from the official Astral installer' | Out-Null }
    $wslMake = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; cd "$1"; exec make "$2"'
    Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', $wslMake, 'sitefilter-installer', $LinuxProject, 'install') 'WSL make install failed' | Out-Null
    Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', $wslMake, 'sitefilter-installer', $LinuxProject, 'build') 'WSL make build failed' | Out-Null
}

if ($PSCmdlet.ShouldProcess($InstallRoot, 'Install the SiteFilter native messaging host')) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Set-PrivateAcl $InstallRoot -Directory

    if ($TestHostExecutable) {
        if (-not (Test-Path -LiteralPath $TestHostExecutable -PathType Leaf)) { throw 'TestHostExecutable does not exist.' }
        Copy-Item -LiteralPath $TestHostExecutable -Destination $ExecutablePath -Force
    } else {
        $uvPath = Get-WindowsUv $TestWindowsUvPath
        $buildRoot = Join-Path $InstallRoot 'build'
        New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
        Invoke-External $uvPath @('tool', 'run', '--from', "pyinstaller==$PyInstallerVersion", 'pyinstaller', '--noconfirm', '--clean', '--onefile', '--name', 'sitefilter-native-host', '--distpath', $InstallRoot, '--workpath', (Join-Path $buildRoot 'work'), '--specpath', $buildRoot, (Join-Path $IntegrationRoot 'host.py')) 'PyInstaller packaging failed' | Out-Null
        Remove-Item -LiteralPath $buildRoot -Recurse -Force
    }

    $config = [ordered]@{ protocol_version = 1; allowed_origins = @($ExtensionOrigin); distro = $Distro; project_path = $LinuxProject; runtime_file = $RuntimeFile; startup_timeout_seconds = 60 }
    $manifestTemplate = Get-Content -LiteralPath (Join-Path $IntegrationRoot 'host-manifest.template.json') -Raw
    $manifest = $manifestTemplate.Replace('__HOST_EXECUTABLE_PATH__', ($ExecutablePath.Replace('\', '\\'))).Replace('__SITEFILTER_EXTENSION_ORIGIN__', $ExtensionOrigin)
    $config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $InstallRoot 'config.json') -Encoding utf8
    $manifest | Set-Content -LiteralPath $ManifestPath -Encoding utf8

    if ($existingState -and $existingState.version -eq 2) {
        $registryBefore = @($existingState.registry_before)
    } else {
        $registryBefore = @((Get-RegistryTree $ChromeKey), (Get-RegistryTree $EdgeKey))
    }
    $state = [ordered]@{ version = 2; host_name = $HostName; install_root = $InstallRoot; owned_files = $OwnedFileNames; owned_directories = @(); registry_before = $registryBefore }
    $state | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $OwnerPath -Encoding utf8
    foreach ($privateFile in @((Join-Path $InstallRoot 'config.json'), $ManifestPath, $OwnerPath, $ExecutablePath)) { Set-PrivateAcl $privateFile }

    foreach ($key in @($ChromeKey, $EdgeKey)) {
        if (-not (Test-Path -LiteralPath $key)) { New-Item -Path $key -Force | Out-Null }
        Set-Item -LiteralPath $key -Value $ManifestPath
    }
}

if (-not $SkipSelfCheck) {
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = $ExecutablePath; $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardInput = $true; $process.StartInfo.RedirectStandardOutput = $true; $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.ArgumentList.Add($ExtensionOrigin)
    if (-not $process.Start()) { throw 'Could not start packaged native host.' }
    Write-NativeFrame $process.StandardInput.BaseStream @{ v = 1; id = 'installer-self-check'; action = 'ping'; payload = @{} }
    $response = Read-NativeFrame $process.StandardOutput.BaseStream
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(70000)) { $process.Kill(); throw 'Native host self-check timed out.' }
    if ($response.id -ne 'installer-self-check' -or -not $response.ok) { throw "Native host self-check failed: $($response | ConvertTo-Json -Compress)" }
    Write-Host "Native host self-check: $($response | ConvertTo-Json -Compress)"
}

[PSCustomObject]$plan | ConvertTo-Json -Compress
