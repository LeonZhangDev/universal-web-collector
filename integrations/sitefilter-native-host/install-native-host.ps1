#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()][string]$ProjectPath,
    [Parameter()][switch]$SkipProvision,
    [Parameter()][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Distro,
    # Test-only overrides. Never use them for a normal installation.
    [Parameter(DontShow)][string]$InstallRoot,
    [Parameter(DontShow)][string]$RegistryRoot = 'HKCU:\Software',
    [Parameter(DontShow)][string]$TestHostExecutable,
    [Parameter(DontShow)][switch]$TestIgnoreInstalledUv,
    [Parameter(DontShow)][string]$TestUvArchivePath,
    [Parameter(DontShow)][string]$TestUvArchiveSha256,
    [Parameter(DontShow)][string]$TestUvInstallDirectory,
    [Parameter(DontShow)][switch]$TestRequireWindowsUv,
    [Parameter(DontShow)][ValidateSet('', 'after-root-swap', 'after-first-registry', 'self-check')][string]$TestFailurePoint = '',
    [Parameter(DontShow)][string]$TestSafetyBase,
    [Parameter(DontShow)][switch]$SkipSelfCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$HostName = 'dev.zackzhang.sitefilter_collector'
$ExtensionId = 'jaihdgjnnpmiabeoefmihmjhoodcjlhf'
$ExtensionOrigin = "chrome-extension://$ExtensionId/"
$PyInstallerVersion = '6.16.0'
$UvVersion = '0.12.15'
$UvArchiveSha256 = '477BD99A84E34891F2BD4C9152DDEB74E971ACCCCBC59C0F0301F11F08A32D46'
$UvArchiveUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$OwnedFileNames = @('.sitefilter-native-host-owned.json', 'collector-startup.log', 'config.json', "$HostName.json", 'native-host.log', 'native-host.log.1', 'native-host.log.2', 'sitefilter-native-host.exe')
$IntegrationRoot = $PSScriptRoot

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder; $null = $builder.Append('"'); $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') { $null = $builder.Append(('\' * ($slashes * 2 + 1))); $null = $builder.Append('"'); $slashes = 0; continue }
        if ($slashes) { $null = $builder.Append(('\' * $slashes)); $slashes = 0 }
        $null = $builder.Append($character)
    }
    if ($slashes) { $null = $builder.Append(('\' * ($slashes * 2))) }
    $null = $builder.Append('"'); return $builder.ToString()
}

function Invoke-External {
    param([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage)
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false; $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true; $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process; $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "$FailureMessage (process did not start)" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync(); $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit(); $stdout = $stdoutTask.Result; $stderr = $stderrTask.Result; $exitCode = $process.ExitCode; $process.Dispose()
    $output = @($stdout.TrimEnd(), $stderr.TrimEnd()) | Where-Object { $_ }
    if ($exitCode -ne 0) { throw "$FailureMessage (exit $exitCode): $($output -join [Environment]::NewLine)" }
    return ($output -join [Environment]::NewLine).Trim()
}

function Write-Utf8NoBomAtomic {
    param([string]$Path, [string]$Text)
    $parent = Split-Path -Parent $Path
    $temporary = Join-Path $parent ('.write-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Text, (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, $null) } else { [IO.File]::Move($temporary, $Path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}

function Test-ReparsePoint {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return [bool]((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-SafeRoot {
    param([string]$Base, [string]$Target)
    $baseFull = [IO.Path]::GetFullPath($Base).TrimEnd('\')
    $targetFull = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    if ($targetFull -ne $baseFull -and -not $targetFull.StartsWith($baseFull + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "InstallRoot escapes its safety base: $targetFull" }
    $cursor = $targetFull
    while ($cursor.Length -ge $baseFull.Length) {
        if (Test-ReparsePoint $cursor) { throw "Reparse points are not allowed in the install path: $cursor" }
        if ($cursor -eq $baseFull) { break }
        $cursor = Split-Path -Parent $cursor
    }
}

function Assert-NoReparseTree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (Test-ReparsePoint $Path) { throw "Reparse point is not allowed: $Path" }
    if (Test-Path -LiteralPath $Path -PathType Container) {
        foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force)) { Assert-NoReparseTree $child.FullName }
    }
}

function Remove-SafeTree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (Test-ReparsePoint $Path) { throw "Refusing to traverse a reparse point: $Path" }
    if (Test-Path -LiteralPath $Path -PathType Container) {
        foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force)) { Remove-SafeTree $child.FullName }
    }
    Remove-Item -LiteralPath $Path -Force
}

function Set-PrivateAclExact {
    param([string]$Path, [switch]$Directory)
    $account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    Invoke-External 'icacls.exe' @($Path, '/inheritance:r', '/q') "Could not disable ACL inheritance: $Path" | Out-Null
    $acl = Get-Acl -LiteralPath $Path
    foreach ($identity in @($acl.Access | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)) {
        if ($identity -ne $account) {
            & icacls.exe $Path '/remove:g' $identity '/q' 2>&1 | Out-Null
            & icacls.exe $Path '/remove:d' $identity '/q' 2>&1 | Out-Null
        }
    }
    $grant = if ($Directory) { "$account`:(OI)(CI)F" } else { "$account`:F" }
    Invoke-External 'icacls.exe' @($Path, '/grant:r', $grant, '/q') "Could not set private ACL: $Path" | Out-Null
    $result = Get-Acl -LiteralPath $Path
    $foreign = @($result.Access | Where-Object { $_.IdentityReference.Value -ne $account -or $_.IsInherited -or $_.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow })
    if (-not $result.AreAccessRulesProtected -or $foreign.Count -ne 0) { throw "ACL verification failed: $Path" }
}

function Resolve-ProjectPath {
    param([string]$Candidate)
    if (-not $Candidate) {
        if (-not [Environment]::UserInteractive) { throw 'ProjectPath is required in a noninteractive session.' }
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Select the Universal Web Collector project folder'
        if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { throw 'Project folder selection was cancelled.' }
        $Candidate = $dialog.SelectedPath
    }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) { throw "ProjectPath is not a directory: $Candidate" }
    $resolved = (Resolve-Path -LiteralPath $Candidate).Path
    foreach ($marker in @('Makefile', 'pyproject.toml', 'integrations\sitefilter-native-host\host.py')) { if (-not (Test-Path -LiteralPath (Join-Path $resolved $marker) -PathType Leaf)) { throw "ProjectPath is missing required marker: $marker" } }
    return $resolved
}

function Get-DefaultDistro {
    $name = Invoke-External 'wsl.exe' @('-e', 'sh', '-lc', 'printf %s "$WSL_DISTRO_NAME"') 'Could not detect the default WSL distribution'
    if ($name -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw 'The default WSL distribution is invalid. Pass -Distro explicitly.' }
    return $name
}

function Convert-WithWslPath {
    param([string]$Distribution, [string]$Path, [ValidateSet('Linux', 'Windows')][string]$Direction)
    $switches = if ($Direction -eq 'Linux') { @('-a', '-u') } else { @('-a', '-w') }
    $converted = Invoke-External 'wsl.exe' (@('-d', $Distribution, '--exec', 'wslpath') + $switches + @($Path)) 'Could not convert path with WSL'
    if ($Direction -eq 'Linux' -and ($converted -notmatch '^/' -or $converted.Contains('\') -or $converted.Contains('..'))) { throw "Invalid Linux path: $converted" }
    if ($Direction -eq 'Windows' -and -not [IO.Path]::IsPathRooted($converted)) { throw "Invalid Windows path: $converted" }
    return $converted
}

function Get-RegistryDefaultSnapshot {
    param([string]$Browser, [string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return [ordered]@{ browser = $Browser; key_existed = $false; default_existed = $false; default_kind = $null; default_data = $null } }
    $key = Get-Item -LiteralPath $Path
    if ($key.GetValueNames() -notcontains '') { return [ordered]@{ browser = $Browser; key_existed = $true; default_existed = $false; default_kind = $null; default_data = $null } }
    $kind = $key.GetValueKind(''); $value = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $data = if ($kind -eq [Microsoft.Win32.RegistryValueKind]::Binary -or $kind -eq [Microsoft.Win32.RegistryValueKind]::None) { [Convert]::ToBase64String([byte[]]$value) } elseif ($kind -eq [Microsoft.Win32.RegistryValueKind]::MultiString) { @([string[]]$value) } elseif ($kind -eq [Microsoft.Win32.RegistryValueKind]::QWord) { ([long]$value).ToString([Globalization.CultureInfo]::InvariantCulture) } else { $value }
    return [ordered]@{ browser = $Browser; key_existed = $true; default_existed = $true; default_kind = $kind.ToString(); default_data = $data }
}

function Get-OriginalRegistryState {
    param($ExistingState, [string]$ChromeKey, [string]$EdgeKey, [string]$ManifestPath)
    if ($ExistingState -and $ExistingState.version -eq 3) {
        $snapshots = @($ExistingState.registry_defaults)
        if ($snapshots.Count -ne 2 -or (@($snapshots.browser | Sort-Object) -join '|') -ne 'chrome|edge') { throw 'Existing version 3 ownership marker has invalid registry state.' }
        foreach ($snapshot in $snapshots) {
            if ($snapshot.key_existed -isnot [bool] -or $snapshot.default_existed -isnot [bool]) { throw 'Existing version 3 registry state has invalid types.' }
            if ($snapshot.default_existed) { try { $null = [Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$snapshot.default_kind) } catch { throw 'Existing version 3 registry state has an invalid value kind.' } }
        }
        return $snapshots
    }
    if ($ExistingState -and $ExistingState.version -eq 2) {
        $converted = @()
        foreach ($entry in @(@{ browser = 'chrome'; path = $ChromeKey }, @{ browser = 'edge'; path = $EdgeKey })) {
            $old = @($ExistingState.registry_before | Where-Object { [string]$_.path -eq $entry.path }) | Select-Object -First 1
            if ($old) {
                $default = @($old.values | Where-Object { [string]$_.name -eq '' }) | Select-Object -First 1
                $converted += [ordered]@{ browser = $entry.browser; key_existed = [bool]$old.existed; default_existed = [bool]($null -ne $default); default_kind = if ($default) { [string]$default.kind } else { $null }; default_data = if ($default) { $default.data } else { $null } }
            } else { $converted += Get-RegistryDefaultSnapshot $entry.browser $entry.path }
        }
        return $converted
    }
    if ($ExistingState -and $ExistingState.version -eq 1) {
        $converted = @()
        foreach ($entry in @(@{ browser = 'chrome'; path = $ChromeKey }, @{ browser = 'edge'; path = $EdgeKey })) {
            $current = Get-RegistryDefaultSnapshot $entry.browser $entry.path
            if ($current.default_existed -and [string]$current.default_data -eq $ManifestPath) {
                # Version 1 did not retain pre-install defaults. Remove only its known
                # default on eventual uninstall, but conservatively retain the key.
                $converted += [ordered]@{ browser = $entry.browser; key_existed = $true; default_existed = $false; default_kind = $null; default_data = $null }
            } else { $converted += $current }
        }
        return $converted
    }
    return @((Get-RegistryDefaultSnapshot 'chrome' $ChromeKey), (Get-RegistryDefaultSnapshot 'edge' $EdgeKey))
}

function Restore-RegistryDefaultExact {
    param($Snapshot, [string]$Path)
    if (-not $Snapshot.key_existed -and -not $Snapshot.default_existed) {
        if (Test-Path -LiteralPath $Path) {
            $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Path.Substring('HKCU:\'.Length), $true)
            try { $key.DeleteValue('', $false); $empty = $key.ValueCount -eq 0 -and $key.SubKeyCount -eq 0 } finally { $key.Dispose() }
            if ($empty) { Remove-Item -LiteralPath $Path -Force }
        }
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -Path $Path -Force | Out-Null }
    if (-not $Snapshot.default_existed) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Path.Substring('HKCU:\'.Length), $true)
        try { $key.DeleteValue('', $false) } finally { $key.Dispose() }
        return
    }
    $kind = [Microsoft.Win32.RegistryValueKind][Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$Snapshot.default_kind)
    $value = if ($kind -eq [Microsoft.Win32.RegistryValueKind]::Binary -or $kind -eq [Microsoft.Win32.RegistryValueKind]::None) { [Convert]::FromBase64String([string]$Snapshot.default_data) } elseif ($kind -eq [Microsoft.Win32.RegistryValueKind]::MultiString) { [string[]]@($Snapshot.default_data) } elseif ($kind -eq [Microsoft.Win32.RegistryValueKind]::DWord) { [int]$Snapshot.default_data } elseif ($kind -eq [Microsoft.Win32.RegistryValueKind]::QWord) { [long]::Parse([string]$Snapshot.default_data, [Globalization.CultureInfo]::InvariantCulture) } else { [string]$Snapshot.default_data }
    $subPath = $Path.Substring('HKCU:\'.Length)
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($subPath, $true)
    try { $key.SetValue('', $value, $kind) } finally { $key.Dispose() }
}

function Test-WindowsUvExecutable {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try { $version = & $Path '--version' 2>$null; return $LASTEXITCODE -eq 0 -and ($version -join '') -match '^uv [0-9]+\.' } catch { return $false }
}

function Get-WindowsUv {
    if (-not $TestIgnoreInstalledUv) {
        $command = Get-Command uv -ErrorAction SilentlyContinue
        if ($command -and (Test-WindowsUvExecutable $command.Source)) { return $command.Source }
        foreach ($candidate in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'))) { if (Test-WindowsUvExecutable $candidate) { $env:PATH = "$(Split-Path -Parent $candidate);$env:PATH"; return (Get-Item -LiteralPath $candidate).FullName } }
    }
    $archive = $TestUvArchivePath
    $expectedHash = if ($TestUvArchiveSha256) { $TestUvArchiveSha256.ToUpperInvariant() } else { $UvArchiveSha256 }
    $installDirectory = if ($TestUvInstallDirectory) { [IO.Path]::GetFullPath($TestUvInstallDirectory) } else { Join-Path $env:USERPROFILE '.local\bin' }
    if (-not $archive) { $archive = Join-Path $env:TEMP "uv-$UvVersion-windows.zip"; Invoke-WebRequest -UseBasicParsing -Uri $UvArchiveUrl -OutFile $archive }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expectedHash) { throw 'Pinned uv archive SHA-256 verification failed.' }
    $extract = Join-Path (Split-Path -Parent $installDirectory) ('.uv-extract-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $extract -Force | Out-Null
    try {
        Expand-Archive -LiteralPath $archive -DestinationPath $extract
        Assert-NoReparseTree $extract
        $uvSource = Get-ChildItem -LiteralPath $extract -Filter uv.exe -File -Recurse | Select-Object -First 1
        if (-not $uvSource) { throw 'Pinned uv archive did not contain uv.exe.' }
        New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
        Copy-Item -LiteralPath $uvSource.FullName -Destination (Join-Path $installDirectory 'uv.exe') -Force
    } finally { Assert-NoReparseTree $extract; Remove-SafeTree $extract }
    $resolved = Join-Path $installDirectory 'uv.exe'
    if (-not $TestUvArchivePath -and -not (Test-WindowsUvExecutable $resolved)) { throw 'The extracted pinned uv executable failed its version check.' }
    $env:PATH = "$installDirectory;$env:PATH"
    return $resolved
}

function Write-NativeFrame {
    param([IO.Stream]$Stream, [hashtable]$Message)
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Message | ConvertTo-Json -Compress)); $header = [BitConverter]::GetBytes([uint32]$bytes.Length)
    $Stream.Write($header, 0, 4); $Stream.Write($bytes, 0, $bytes.Length); $Stream.Flush()
}

function Read-NativeFrame {
    param([IO.Stream]$Stream)
    $header = New-Object byte[] 4
    if ($Stream.Read($header, 0, 4) -ne 4) { throw 'Packaged host returned no complete frame.' }
    $length = [BitConverter]::ToUInt32($header, 0); if ($length -lt 1 -or $length -gt 1MB) { throw 'Packaged host returned an invalid frame length.' }
    $body = New-Object byte[] $length; $offset = 0
    while ($offset -lt $length) { $read = $Stream.Read($body, $offset, $length - $offset); if ($read -eq 0) { throw 'Packaged host response ended early.' }; $offset += $read }
    return ([Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json)
}

function Invoke-HostSelfCheck {
    param([string]$Executable, [string]$LocalAppData)
    $process = New-Object Diagnostics.Process
    $process.StartInfo = New-Object Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = $Executable; $process.StartInfo.Arguments = $ExtensionOrigin; $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardInput = $true; $process.StartInfo.RedirectStandardOutput = $true; $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.EnvironmentVariables['LOCALAPPDATA'] = $LocalAppData
    if (-not $process.Start()) { throw 'Could not start packaged native host.' }
    Write-NativeFrame $process.StandardInput.BaseStream @{ v = 1; id = 'installer-self-check'; action = 'ping'; payload = @{} }
    $response = Read-NativeFrame $process.StandardOutput.BaseStream; $process.StandardInput.Close()
    if (-not $process.WaitForExit(70000)) { $process.Kill(); throw 'Native host self-check timed out.' }
    if ($response.v -ne 1 -or $response.id -ne 'installer-self-check' -or $response.ok -ne $true) { throw "Native host self-check failed: $($response | ConvertTo-Json -Compress)" }
    return $response
}

function Get-OwnedRuntimePid {
    param([string]$RuntimePath)
    if (-not (Test-Path -LiteralPath $RuntimePath -PathType Leaf)) { return [long]-1 }
    $descriptor = Get-Content -LiteralPath $RuntimePath -Raw | ConvertFrom-Json
    if ($descriptor.protocol_version -ne 1 -or $descriptor.owned -isnot [bool]) { throw 'Runtime descriptor protocol/types are invalid.' }
    if (-not $descriptor.owned) { return [long]-1 }
    $pidIsInteger = $descriptor.pid -is [int] -or $descriptor.pid -is [long]
    $portIsInteger = $descriptor.port -is [int] -or $descriptor.port -is [long]
    if (-not $pidIsInteger -or [long]$descriptor.pid -le 0 -or -not $portIsInteger -or [long]$descriptor.port -le 0 -or $descriptor.ready -isnot [bool]) { throw 'Owned runtime descriptor has invalid fields.' }
    return [long]$descriptor.pid
}

function Stop-VerifiedPreflightCollector {
    param([bool]$HadOwnedBefore, [string]$RuntimePath, [string]$Distribution, [string]$LinuxPath, [string]$PreflightLog)
    Write-Host "Staged preflight cleanup: owned_before=$HadOwnedBefore runtime=$RuntimePath"
    $afterPid = Get-OwnedRuntimePid $RuntimePath
    Write-Host "Staged preflight cleanup: owned_after_pid=$afterPid"
    if ($afterPid -lt 1) { return }
    $verifyScript = 'pid="$1"; project="$2"; [ -d "/proc/$pid" ] || exit 20; cwd=$(readlink -f "/proc/$pid/cwd") || exit 21; [ "$cwd" = "$project" ] || exit 22; cmd=$(tr "\0" " " < "/proc/$pid/cmdline") || exit 23; case "$cmd" in *"$project/.venv/bin/python3 backend/main.py"*) printf VERIFIED;; *) exit 24;; esac'
    $verified = Invoke-External 'wsl.exe' @('-d', $Distribution, '--exec', 'sh', '-c', $verifyScript, 'sitefilter-installer-preflight', ([string]$afterPid), $LinuxPath) 'New preflight Collector verification failed'
    if ($verified -ne 'VERIFIED') { throw 'New preflight Collector verification was inconclusive.' }
    Write-Host "Stopping verified preflight-observed Collector PID $afterPid before install mutation."
    Invoke-External 'wsl.exe' @('-d', $Distribution, '--exec', 'kill', '-TERM', ([string]$afterPid)) 'Could not stop the verified preflight Collector' | Out-Null
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        $previousPreference = $ErrorActionPreference
        try { $ErrorActionPreference = 'Continue'; & wsl.exe -d $Distribution --exec kill -0 ([string]$afterPid) 2>$null; $aliveExit = $LASTEXITCODE }
        finally { $ErrorActionPreference = $previousPreference }
        if ($aliveExit -ne 0) {
            for ($unlockAttempt = 0; $unlockAttempt -lt 25; $unlockAttempt++) {
                if (-not (Test-Path -LiteralPath $PreflightLog -PathType Leaf)) { return }
                try { $stream = [IO.File]::Open($PreflightLog, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None); $stream.Dispose(); return }
                catch { Start-Sleep -Milliseconds 200 }
            }
            throw 'Verified preflight Collector exited, but its exact staged log remained locked.'
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Verified preflight Collector PID $afterPid did not exit within 10 seconds."
}

$ResolvedProject = Resolve-ProjectPath $ProjectPath
if (-not $Distro) { $Distro = Get-DefaultDistro }
$LinuxProject = Convert-WithWslPath $Distro $ResolvedProject Linux
$RuntimeFile = Convert-WithWslPath $Distro "$LinuxProject/data/native-runtime.json" Windows
$installRootWasExplicit = -not [string]::IsNullOrWhiteSpace($InstallRoot)
if (-not $InstallRoot) { if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }; $InstallRoot = Join-Path $env:LOCALAPPDATA 'SiteFilter\NativeHost' }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$SafetyBase = if ($TestSafetyBase) { [IO.Path]::GetFullPath($TestSafetyBase) } elseif ($installRootWasExplicit) { Split-Path -Parent $InstallRoot } else { [IO.Path]::GetFullPath($env:LOCALAPPDATA) }
Assert-SafeRoot $SafetyBase $InstallRoot
Assert-NoReparseTree $InstallRoot
$parentRoot = Split-Path -Parent $InstallRoot
Assert-SafeRoot $SafetyBase $parentRoot
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$ChromeKey = Join-Path $RegistryRoot "Google\Chrome\NativeMessagingHosts\$HostName"
$EdgeKey = Join-Path $RegistryRoot "Microsoft\Edge\NativeMessagingHosts\$HostName"
foreach ($key in @($ChromeKey, $EdgeKey)) { if ($key -notlike 'HKCU:\Software\*' -or -not $key.EndsWith("\$HostName", [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe registry target: $key" } }
$plan = [ordered]@{ project_path = $ResolvedProject; linux_project_path = $LinuxProject; runtime_file = $RuntimeFile; distro = $Distro; install_root = $InstallRoot; manifest_path = $ManifestPath; allowed_origin = $ExtensionOrigin; chrome_registry_key = $ChromeKey; edge_registry_key = $EdgeKey }
if ($WhatIfPreference) { [PSCustomObject]$plan | ConvertTo-Json -Compress; return }
if (-not (Test-Path -LiteralPath $parentRoot)) { New-Item -ItemType Directory -Path $parentRoot -Force | Out-Null }

if (-not $SkipProvision) {
    & wsl.exe -d $Distro --exec sh -lc 'command -v uv >/dev/null 2>&1 || test -x "$HOME/.local/bin/uv"'
    if ($LASTEXITCODE -ne 0) { Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', 'curl -LsSf https://astral.sh/uv/install.sh | sh') 'Could not install uv in WSL' | Out-Null }
    $wslMake = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; cd "$1"; exec make "$2"'
    Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', $wslMake, 'sitefilter-installer', $LinuxProject, 'install') 'WSL make install failed' | Out-Null
    Invoke-External 'wsl.exe' @('-d', $Distro, '--exec', 'sh', '-lc', $wslMake, 'sitefilter-installer', $LinuxProject, 'build') 'WSL make build failed' | Out-Null
}

$stageRoot = Join-Path $parentRoot ('.NativeHost-stage-' + [Guid]::NewGuid().ToString('N'))
$rollbackRoot = Join-Path $parentRoot ('.NativeHost-rollback-' + [Guid]::NewGuid().ToString('N'))
$mutated = $false; $oldRootMoved = $false; $stageMoved = $false; $unexpectedNames = @()
$currentRegistry = @((Get-RegistryDefaultSnapshot 'chrome' $ChromeKey), (Get-RegistryDefaultSnapshot 'edge' $EdgeKey))
try {
    New-Item -ItemType Directory -Path $stageRoot | Out-Null; Set-PrivateAclExact $stageRoot -Directory
    $stageExe = Join-Path $stageRoot 'sitefilter-native-host.exe'
    if ($TestHostExecutable) { Copy-Item -LiteralPath $TestHostExecutable -Destination $stageExe }
    else {
        $uvPath = Get-WindowsUv
        $buildRoot = Join-Path $stageRoot '.build'; New-Item -ItemType Directory -Path $buildRoot | Out-Null
        Invoke-External $uvPath @('tool', 'run', '--from', "pyinstaller==$PyInstallerVersion", 'pyinstaller', '--noconfirm', '--clean', '--onefile', '--name', 'sitefilter-native-host', '--distpath', $stageRoot, '--workpath', (Join-Path $buildRoot 'work'), '--specpath', $buildRoot, (Join-Path $IntegrationRoot 'host.py')) 'PyInstaller packaging failed' | Out-Null
        Assert-NoReparseTree $buildRoot; Remove-SafeTree $buildRoot
    }
    if ($TestRequireWindowsUv -and $TestHostExecutable) { $null = Get-WindowsUv }

    $config = [ordered]@{ protocol_version = 1; allowed_origins = @($ExtensionOrigin); distro = $Distro; project_path = $LinuxProject; runtime_file = $RuntimeFile; startup_timeout_seconds = 60 }
    $manifest = (Get-Content -LiteralPath (Join-Path $IntegrationRoot 'host-manifest.template.json') -Raw).Replace('__HOST_EXECUTABLE_PATH__', ((Join-Path $InstallRoot 'sitefilter-native-host.exe').Replace('\', '\\'))).Replace('__SITEFILTER_EXTENSION_ORIGIN__', $ExtensionOrigin)
    Write-Utf8NoBomAtomic (Join-Path $stageRoot 'config.json') ($config | ConvertTo-Json -Depth 4)
    Write-Utf8NoBomAtomic (Join-Path $stageRoot "$HostName.json") $manifest

    $existingState = $null; $oldMarker = Join-Path $InstallRoot '.sitefilter-native-host-owned.json'
    if (Test-Path -LiteralPath $oldMarker -PathType Leaf) { try { $existingState = Get-Content -LiteralPath $oldMarker -Raw | ConvertFrom-Json } catch { throw 'Existing ownership marker is invalid; refusing replacement.' } }
    if ($existingState) {
        if ($existingState.version -notin @(1, 2, 3) -or $existingState.host_name -ne $HostName -or [IO.Path]::GetFullPath([string]$existingState.install_root).TrimEnd('\') -ne $InstallRoot) { throw 'Existing ownership marker does not identify this install root.' }
    } elseif (Test-Path -LiteralPath $InstallRoot) {
        $collisions = @($OwnedFileNames | Where-Object { $_ -ne '.sitefilter-native-host-owned.json' -and (Test-Path -LiteralPath (Join-Path $InstallRoot $_)) })
        if ($collisions.Count -ne 0) { throw "Unowned files collide with native-host files: $($collisions -join ', ')" }
    }
    $originalRegistry = Get-OriginalRegistryState $existingState $ChromeKey $EdgeKey $ManifestPath
    if ($originalRegistry.Count -ne 2 -or (@($originalRegistry.browser | Sort-Object) -join '|') -ne 'chrome|edge') { throw 'Migrated registry-default state is incomplete.' }
    foreach ($snapshot in $originalRegistry) {
        if ($snapshot.key_existed -isnot [bool] -or $snapshot.default_existed -isnot [bool]) { throw 'Migrated registry-default state has invalid types.' }
        if ($snapshot.default_existed) {
            try {
                $kind = [Microsoft.Win32.RegistryValueKind][Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$snapshot.default_kind)
                if ($kind -eq [Microsoft.Win32.RegistryValueKind]::Binary -or $kind -eq [Microsoft.Win32.RegistryValueKind]::None) { $null = [Convert]::FromBase64String([string]$snapshot.default_data) }
                elseif ($kind -eq [Microsoft.Win32.RegistryValueKind]::DWord) { $null = [int]$snapshot.default_data }
                elseif ($kind -eq [Microsoft.Win32.RegistryValueKind]::QWord) { $null = [long]::Parse([string]$snapshot.default_data, [Globalization.CultureInfo]::InvariantCulture) }
            } catch { throw 'Migrated registry-default state has invalid prior data.' }
        }
    }
    $state = [ordered]@{ version = 3; host_name = $HostName; install_root = $InstallRoot; owned_files = $OwnedFileNames; registry_defaults = $originalRegistry }
    Write-Utf8NoBomAtomic (Join-Path $stageRoot '.sitefilter-native-host-owned.json') ($state | ConvertTo-Json -Depth 10)
    foreach ($name in $OwnedFileNames) { $candidate = Join-Path $stageRoot $name; if (Test-Path -LiteralPath $candidate -PathType Leaf) { Set-PrivateAclExact $candidate } }
    Assert-NoReparseTree $stageRoot

    if (-not $SkipSelfCheck -and -not $TestHostExecutable) {
        $preflightLocal = Join-Path $stageRoot '.preflight-local'; $preflightConfig = Join-Path $preflightLocal 'SiteFilter\NativeHost'
        New-Item -ItemType Directory -Path $preflightConfig -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $stageRoot 'config.json') -Destination (Join-Path $preflightConfig 'config.json')
        $hadOwnedBefore = (Get-OwnedRuntimePid $RuntimeFile) -gt 0
        Write-Host "Staged preflight starting: owned_before=$hadOwnedBefore"
        try { $null = Invoke-HostSelfCheck $stageExe $preflightLocal }
        finally {
            Stop-VerifiedPreflightCollector -HadOwnedBefore $hadOwnedBefore -RuntimePath $RuntimeFile -Distribution $Distro -LinuxPath $LinuxProject -PreflightLog (Join-Path $preflightConfig 'collector-startup.log')
            Assert-NoReparseTree $preflightLocal; Remove-SafeTree $preflightLocal
        }
    }

    if (Test-Path -LiteralPath $InstallRoot) {
        Assert-NoReparseTree $InstallRoot
        foreach ($item in @(Get-ChildItem -LiteralPath $InstallRoot -Force)) { if ($OwnedFileNames -notcontains $item.Name) { $unexpectedNames += $item.Name } }
    }
    if (-not $PSCmdlet.ShouldProcess($InstallRoot, 'Atomically replace the staged native host and register Chrome and Edge')) { return }
    $mutated = $true
    if (Test-Path -LiteralPath $InstallRoot) { Move-Item -LiteralPath $InstallRoot -Destination $rollbackRoot; $oldRootMoved = $true }
    Move-Item -LiteralPath $stageRoot -Destination $InstallRoot; $stageMoved = $true
    if ($TestFailurePoint -eq 'after-root-swap') { throw 'Injected failure after root swap.' }
    foreach ($name in $unexpectedNames) { Move-Item -LiteralPath (Join-Path $rollbackRoot $name) -Destination (Join-Path $InstallRoot $name) }

    $index = 0
    foreach ($key in @($ChromeKey, $EdgeKey)) {
        if (-not (Test-Path -LiteralPath $key)) { New-Item -Path $key -Force | Out-Null }
        Set-Item -LiteralPath $key -Value $ManifestPath
        $index++
        if ($index -eq 1 -and $TestFailurePoint -eq 'after-first-registry') { throw 'Injected failure after first registry write.' }
    }
    if ($TestFailurePoint -eq 'self-check') { throw 'Injected failure at final self-check.' }
    if (-not $SkipSelfCheck -and -not $TestHostExecutable) { $response = Invoke-HostSelfCheck (Join-Path $InstallRoot 'sitefilter-native-host.exe') $env:LOCALAPPDATA; Write-Host "Native host self-check: $($response | ConvertTo-Json -Compress)" }
    Set-PrivateAclExact $InstallRoot -Directory
    foreach ($name in $OwnedFileNames) { $installedFile = Join-Path $InstallRoot $name; if (Test-Path -LiteralPath $installedFile -PathType Leaf) { Set-PrivateAclExact $installedFile } }

    if ($oldRootMoved) {
        foreach ($name in $OwnedFileNames) { $oldFile = Join-Path $rollbackRoot $name; if (Test-Path -LiteralPath $oldFile -PathType Leaf) { Remove-Item -LiteralPath $oldFile -Force } }
        if (@(Get-ChildItem -LiteralPath $rollbackRoot -Force).Count -eq 0) { Remove-Item -LiteralPath $rollbackRoot -Force } else { throw "Rollback directory retained unexpected entries: $rollbackRoot" }
    }
} catch {
    $failure = $_
    if ($mutated) {
        Restore-RegistryDefaultExact $currentRegistry[0] $ChromeKey; Restore-RegistryDefaultExact $currentRegistry[1] $EdgeKey
        if ($stageMoved -and (Test-Path -LiteralPath $InstallRoot)) {
            foreach ($name in $unexpectedNames) { $item = Join-Path $InstallRoot $name; if ((Test-Path -LiteralPath $item) -and $oldRootMoved) { Move-Item -LiteralPath $item -Destination (Join-Path $rollbackRoot $name) } }
            foreach ($name in $OwnedFileNames) { $file = Join-Path $InstallRoot $name; if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force } }
            if (@(Get-ChildItem -LiteralPath $InstallRoot -Force).Count -eq 0) { Remove-Item -LiteralPath $InstallRoot -Force }
        }
        if ($oldRootMoved -and (Test-Path -LiteralPath $rollbackRoot) -and -not (Test-Path -LiteralPath $InstallRoot)) { Move-Item -LiteralPath $rollbackRoot -Destination $InstallRoot }
    }
    throw $failure
} finally {
    if (Test-Path -LiteralPath $stageRoot) {
        try { Assert-NoReparseTree $stageRoot; Remove-SafeTree $stageRoot }
        catch { Write-Warning "Staging cleanup was deferred because an owned file is still in use: $($_.Exception.Message)" }
    }
}

[PSCustomObject]$plan | ConvertTo-Json -Compress
