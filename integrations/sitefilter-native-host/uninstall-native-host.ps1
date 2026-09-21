#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    # Test-only overrides. Never use them for a normal uninstall.
    [Parameter(DontShow)][string]$InstallRoot,
    [Parameter(DontShow)][string]$RegistryRoot = 'HKCU:\Software',
    [Parameter(DontShow)][string]$TestSafetyBase,
    [Parameter(DontShow)][ValidateSet('', 'Verified', 'Unowned', 'WrongCommand', 'PidChanged', 'Absent')][string]$TestProcessState = '',
    [Parameter(DontShow)][string]$TestSignalLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$HostName = 'dev.zackzhang.sitefilter_collector'
$OwnedFileNames = @('.sitefilter-native-host-owned.json', 'collector-startup.log', 'config.json', "$HostName.json", 'native-host.log', 'native-host.log.1', 'native-host.log.2', 'sitefilter-native-host.exe')

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
    $startInfo.FileName = $FilePath; $startInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false; $startInfo.CreateNoWindow = $true; $startInfo.RedirectStandardOutput = $true; $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process; $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "$FailureMessage (process did not start)" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync(); $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit(); $stdout = $stdoutTask.Result; $stderr = $stderrTask.Result; $exitCode = $process.ExitCode; $process.Dispose()
    $output = @($stdout.TrimEnd(), $stderr.TrimEnd()) | Where-Object { $_ }
    if ($exitCode -ne 0) { throw "$FailureMessage (exit $exitCode): $($output -join [Environment]::NewLine)" }
    return ($output -join [Environment]::NewLine).Trim()
}

function Test-ReparsePoint {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return [bool]((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-SafeRoot {
    param([string]$Base, [string]$Target)
    $baseFull = [IO.Path]::GetFullPath($Base).TrimEnd('\'); $targetFull = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    if ($targetFull -ne $baseFull -and -not $targetFull.StartsWith($baseFull + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "InstallRoot escapes its safety base: $targetFull" }
    $cursor = $targetFull
    while ($cursor.Length -ge $baseFull.Length) { if (Test-ReparsePoint $cursor) { throw "Reparse points are not allowed in the install path: $cursor" }; if ($cursor -eq $baseFull) { break }; $cursor = Split-Path -Parent $cursor }
}

function Assert-NoReparseTree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (Test-ReparsePoint $Path) { throw "Reparse point is not allowed: $Path" }
    if (Test-Path -LiteralPath $Path -PathType Container) { foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force)) { Assert-NoReparseTree $child.FullName } }
}

function Open-RegistryKeyWritable {
    param([string]$Path)
    return [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Path.Substring('HKCU:\'.Length), $true)
}

function ConvertFrom-DefaultData {
    param($Snapshot, [Microsoft.Win32.RegistryValueKind]$Kind)
    if ($Kind -eq [Microsoft.Win32.RegistryValueKind]::Binary -or $Kind -eq [Microsoft.Win32.RegistryValueKind]::None) { return ,([Convert]::FromBase64String([string]$Snapshot.default_data)) }
    if ($Kind -eq [Microsoft.Win32.RegistryValueKind]::MultiString) { return ,([string[]]@($Snapshot.default_data)) }
    if ($Kind -eq [Microsoft.Win32.RegistryValueKind]::DWord) { return [int]$Snapshot.default_data }
    if ($Kind -eq [Microsoft.Win32.RegistryValueKind]::QWord) { return [long]::Parse([string]$Snapshot.default_data, [Globalization.CultureInfo]::InvariantCulture) }
    return [string]$Snapshot.default_data
}

function Invoke-VerifiedOwnedCollectorStop {
    param($Config, [switch]$VerifyOnly)
    $runtimePath = [string]$Config.runtime_file
    if (-not [IO.Path]::IsPathRooted($runtimePath) -or -not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) { return }
    $descriptor = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
    if ($descriptor.protocol_version -ne 1 -or $descriptor.owned -isnot [bool]) { throw 'Runtime descriptor protocol/types are invalid; refusing to signal any process.' }
    if (-not $descriptor.owned) { return }
    $pidIsInteger = $descriptor.pid -is [int] -or $descriptor.pid -is [long]
    $portIsInteger = $descriptor.port -is [int] -or $descriptor.port -is [long]
    if (-not $pidIsInteger -or [long]$descriptor.pid -le 0 -or -not $portIsInteger -or [long]$descriptor.port -le 0 -or $descriptor.ready -isnot [bool]) { throw 'Owned runtime descriptor fields are invalid; refusing to signal any process.' }
    if ($TestProcessState) {
        if ($TestProcessState -eq 'Unowned' -or $TestProcessState -eq 'Absent') { return }
        if ($TestProcessState -eq 'WrongCommand') { throw 'Test seam reports an owned process with the wrong command.' }
        if ($TestProcessState -eq 'PidChanged' -and -not $VerifyOnly) { throw 'Test seam reports that the owned PID changed before termination.' }
        if ($TestProcessState -eq 'Verified' -or $TestProcessState -eq 'PidChanged') {
            if (-not $VerifyOnly -and $TestSignalLog) { [IO.File]::AppendAllText($TestSignalLog, "TERM $($descriptor.pid)`n", (New-Object Text.UTF8Encoding($false))) }
            return
        }
    }
    $distro = [string]$Config.distro; $project = [string]$Config.project_path
    if ($distro -notmatch '^[A-Za-z0-9._-]{1,64}$' -or $project -notmatch '^/' -or $project.Contains('..') -or $project.Contains('\')) { throw 'Installed config has unsafe WSL values; refusing to signal any process.' }
    $mode = if ($VerifyOnly) { 'verify' } else { 'stop' }
    $processScript = @'
import os, pathlib, signal, sys, time
pid = int(sys.argv[1]); project = pathlib.Path(sys.argv[2]); mode = sys.argv[3]
proc = pathlib.Path('/proc') / str(pid)
if not proc.is_dir(): print('ABSENT'); raise SystemExit(0)
expected_python = project / '.venv/bin/python3'
def matches():
    try:
        cwd = pathlib.Path(os.readlink(proc / 'cwd'))
        exe = pathlib.Path(os.readlink(proc / 'exe')).resolve()
        argv = (proc / 'cmdline').read_bytes().split(b'\0')
        if argv and argv[-1] == b'': argv.pop()
        return cwd == project and exe == expected_python.resolve() and argv == [os.fsencode(str(expected_python)), b'backend/main.py']
    except (FileNotFoundError, PermissionError, ProcessLookupError): return False
if not matches(): print('MISMATCH'); raise SystemExit(21)
if mode == 'verify': print('VERIFIED'); raise SystemExit(0)
os.kill(pid, signal.SIGTERM)
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if not proc.exists() or not matches(): print('STOPPED'); raise SystemExit(0)
    time.sleep(0.2)
print('TIMEOUT'); raise SystemExit(22)
'@
    $result = Invoke-External 'wsl.exe' @('-d', $distro, '--exec', 'python3', '-c', $processScript, ([string]$descriptor.pid), $project, $mode) "Owned Collector exact verification/stop failed for PID $($descriptor.pid)"
    if ($result -eq 'ABSENT' -or $result -eq 'VERIFIED' -or $result -eq 'STOPPED') { return }
    throw 'Owned Collector verification was inconclusive.'
}

function Assert-OwnedFilesUnlocked {
    param([string]$Root)
    foreach ($name in $OwnedFileNames) {
        $path = Join-Path $Root $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $unlocked = $false
            for ($attempt = 0; $attempt -lt 25 -and -not $unlocked; $attempt++) {
                try {
                    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                    $stream.Dispose()
                    $unlocked = $true
                } catch {
                    if ($attempt -lt 24) { Start-Sleep -Milliseconds 200 }
                }
            }
            if (-not $unlocked) { throw "Owned file remains locked; no registry or file changes were made: $path" }
        }
    }
}

function Get-CurrentDefault {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return [ordered]@{ exists = $false; value_exists = $false; value = $null } }
    $key = Get-Item -LiteralPath $Path
    $has = $key.GetValueNames() -contains ''
    return [ordered]@{ exists = $true; value_exists = $has; value = if ($has) { $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) } else { $null } }
}

$explicitRoot = -not [string]::IsNullOrWhiteSpace($InstallRoot)
if (-not $InstallRoot) { if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }; $InstallRoot = Join-Path $env:LOCALAPPDATA 'SiteFilter\NativeHost' }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$SafetyBase = if ($TestSafetyBase) { [IO.Path]::GetFullPath($TestSafetyBase) } elseif ($explicitRoot) { Split-Path -Parent $InstallRoot } else { [IO.Path]::GetFullPath($env:LOCALAPPDATA) }
Assert-SafeRoot $SafetyBase $InstallRoot; Assert-NoReparseTree $InstallRoot
$ManifestPath = Join-Path $InstallRoot "$HostName.json"
$OwnerPath = Join-Path $InstallRoot '.sitefilter-native-host-owned.json'
$ChromeKey = Join-Path $RegistryRoot "Google\Chrome\NativeMessagingHosts\$HostName"
$EdgeKey = Join-Path $RegistryRoot "Microsoft\Edge\NativeMessagingHosts\$HostName"
foreach ($key in @($ChromeKey, $EdgeKey)) { if ($key -notlike 'HKCU:\Software\*' -or -not $key.EndsWith("\$HostName", [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe registry target: $key" } }

$markerExists = Test-Path -LiteralPath $OwnerPath -PathType Leaf
if (-not $markerExists) {
    $ownedRemnants = @($OwnedFileNames | Where-Object { Test-Path -LiteralPath (Join-Path $InstallRoot $_) -PathType Leaf })
    $ownedDefaults = @(@($ChromeKey, $EdgeKey) | Where-Object { (Get-CurrentDefault $_).value -eq $ManifestPath })
    if ($ownedRemnants.Count -eq 0 -and $ownedDefaults.Count -eq 0) { Write-Host 'SiteFilter native host is already uninstalled; unexpected files were preserved.'; return }
    throw 'Ownership marker is missing while owned artifacts remain; refusing uninstall.'
}

$state = Get-Content -LiteralPath $OwnerPath -Raw | ConvertFrom-Json
if ($state.version -ne 3 -or $state.host_name -ne $HostName -or [IO.Path]::GetFullPath([string]$state.install_root).TrimEnd('\') -ne $InstallRoot) { throw 'Ownership marker is invalid or unsupported; refusing uninstall.' }
if ((@($state.owned_files) -join '|') -ne ($OwnedFileNames -join '|')) { throw 'Ownership marker has an unexpected owned-file set; refusing uninstall.' }
$snapshots = @($state.registry_defaults)
if ($snapshots.Count -ne 2 -or (@($snapshots.browser | Sort-Object) -join '|') -ne 'chrome|edge') { throw 'Ownership marker has invalid registry-default state.' }
foreach ($snapshot in $snapshots) {
    if ($snapshot.key_existed -isnot [bool] -or $snapshot.default_existed -isnot [bool]) { throw 'Ownership marker has invalid registry-default types.' }
    if ($snapshot.default_existed) {
        try {
            $kind = [Microsoft.Win32.RegistryValueKind][Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$snapshot.default_kind)
            $null = ConvertFrom-DefaultData $snapshot $kind
        } catch { throw 'Ownership marker has invalid prior default data.' }
    }
}

# Validate process identity before confirmation, but never signal it until the
# single operation-wide ShouldProcess gate approves the whole uninstall.
$configPath = Join-Path $InstallRoot 'config.json'
$config = $null
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    Invoke-VerifiedOwnedCollectorStop $config -VerifyOnly
}
if (-not $PSCmdlet.ShouldProcess($InstallRoot, 'Stop the exactly verified owned Collector, restore owned registry defaults, and remove exact owned files')) { Write-Host 'Uninstall validation completed; no process, registry, or file changes were made.'; return }
if ($config) { Invoke-VerifiedOwnedCollectorStop $config }
Assert-OwnedFilesUnlocked $InstallRoot

foreach ($entry in @(@{ browser = 'chrome'; path = $ChromeKey }, @{ browser = 'edge'; path = $EdgeKey })) {
    $snapshot = @($snapshots | Where-Object { $_.browser -eq $entry.browser })[0]
    $current = Get-CurrentDefault $entry.path
    if (-not $current.value_exists -or $current.value -ne $ManifestPath) { continue }
    if ($snapshot.default_existed) {
        $kind = [Microsoft.Win32.RegistryValueKind][Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$snapshot.default_kind)
        $key = Open-RegistryKeyWritable $entry.path
        try { $key.SetValue('', (ConvertFrom-DefaultData $snapshot $kind), $kind) } finally { $key.Dispose() }
    } else {
        $key = Open-RegistryKeyWritable $entry.path
        try { $key.DeleteValue('', $false); $empty = $key.ValueCount -eq 0 -and $key.SubKeyCount -eq 0 } finally { $key.Dispose() }
        if (-not $snapshot.key_existed -and $empty) { Remove-Item -LiteralPath $entry.path -Force }
    }
}

$markerRemoved = $false
foreach ($name in @($OwnedFileNames | Where-Object { $_ -ne '.sitefilter-native-host-owned.json' })) {
    $target = [IO.Path]::GetFullPath((Join-Path $InstallRoot $name))
    if (-not $target.StartsWith($InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe owned path: $name" }
    if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
}
$remainingOwned = @($OwnedFileNames | Where-Object { $_ -ne '.sitefilter-native-host-owned.json' -and (Test-Path -LiteralPath (Join-Path $InstallRoot $_) -PathType Leaf) })
if ($remainingOwned.Count -eq 0 -and (Test-Path -LiteralPath $OwnerPath)) { Remove-Item -LiteralPath $OwnerPath -Force; $markerRemoved = $true }
if ((Test-Path -LiteralPath $InstallRoot) -and @(Get-ChildItem -LiteralPath $InstallRoot -Force).Count -eq 0) { Remove-Item -LiteralPath $InstallRoot -Force }
elseif (Test-Path -LiteralPath $InstallRoot) { Write-Warning "Preserved unexpected files under: $InstallRoot" }

Write-Host 'SiteFilter native host default registrations and exact owned files were removed; unrelated registry state and files were preserved.'
