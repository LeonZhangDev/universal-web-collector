#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ProjectPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('sitefilter-native-host-test-' + [Guid]::NewGuid().ToString('N'))
$registryBase = "HKCU:\Software\SiteFilterNativeHostTests\$([Guid]::NewGuid().ToString('N'))"
$installer = Join-Path $PSScriptRoot 'install-native-host.ps1'
$uninstaller = Join-Path $PSScriptRoot 'uninstall-native-host.ps1'
$hostName = 'dev.zackzhang.sitefilter_collector'
$ownedNames = @('.sitefilter-native-host-owned.json', 'collector-startup.log', 'config.json', "$hostName.json", 'native-host.log', 'native-host.log.1', 'native-host.log.2', 'sitefilter-native-host.exe')

function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "ASSERTION FAILED: $Message" } }
function Write-TestText([string]$Path, [string]$Text) { [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false))) }
function Remove-TestTree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { [IO.Directory]::Delete($item.FullName, $false); return }
    if ($item.PSIsContainer) { foreach ($child in @(Get-ChildItem -LiteralPath $item.FullName -Force)) { Remove-TestTree $child.FullName }; [IO.Directory]::Delete($item.FullName, $false) }
    else { [IO.File]::Delete($item.FullName) }
}
function Get-RootFingerprint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '<absent>' }
    $parts = foreach ($item in @(Get-ChildItem -LiteralPath $Path -Force | Sort-Object Name)) {
        if ($item.PSIsContainer) { "D:$($item.Name)" } else { "F:$($item.Name):$((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash)" }
    }
    return ($parts -join '|')
}
function Get-DefaultFingerprint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '<absent>' }
    $key = Get-Item -LiteralPath $Path; $names = @($key.GetValueNames() | Sort-Object)
    $values = foreach ($name in $names) {
        $value = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($value -is [byte[]]) { $value = [Convert]::ToBase64String($value) } elseif ($value -is [array]) { $value = @($value) -join '\0' }
        "$name/$($key.GetValueKind($name))/$value"
    }
    $children = foreach ($child in @($key.GetSubKeyNames() | Sort-Object)) { "$child={$((Get-DefaultFingerprint (Join-Path $Path $child)))}" }
    return "V:$($values -join ',');S:$($children -join ',')"
}
function Assert-Fails([scriptblock]$Action, [string]$Message) { $failed = $false; try { & $Action | Out-Null } catch { $failed = $true }; Assert-True $failed $Message }
function Invoke-TestInstall([string]$InstallRoot, [string]$RegistryRoot, [string]$FakeExe, [string]$FailurePoint = '') {
    & $installer -ProjectPath $ProjectPath -SkipProvision -InstallRoot $InstallRoot -RegistryRoot $RegistryRoot -TestSafetyBase $root -TestHostExecutable $FakeExe -SkipSelfCheck -TestFailurePoint $FailurePoint | Out-Null
}
function Invoke-TestUninstall([string]$InstallRoot, [string]$RegistryRoot, [string]$ProcessState = 'Unowned', [string]$SignalLog = '') {
    & $uninstaller -InstallRoot $InstallRoot -RegistryRoot $RegistryRoot -TestSafetyBase $root -TestProcessState $ProcessState -TestSignalLog $SignalLog | Out-Null
}
function Set-TestOwnedDescriptor([string]$InstallRoot, [string]$Name) {
    $descriptorPath = Join-Path $root "$Name-runtime.json"
    Write-TestText $descriptorPath '{"protocol_version":1,"port":8000,"owned":true,"ready":true,"pid":123}'
    $configPath = Join-Path $InstallRoot 'config.json'
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $config.runtime_file = $descriptorPath
    Write-TestText $configPath ($config | ConvertTo-Json -Depth 8)
}

try {
    New-Item -ItemType Directory -Path $root | Out-Null
    $fakeExe = Join-Path $root 'fake-host.exe'; Write-TestText $fakeExe 'fake host v1'
    $installerText = Get-Content -LiteralPath $installer -Raw; $uninstallerText = Get-Content -LiteralPath $uninstaller -Raw
    foreach ($scriptText in @($installerText, $uninstallerText)) {
        Assert-True ($scriptText -match 'os\.pidfd_open' -and $scriptText -match 'signal\.pidfd_send_signal') 'A process-stop path does not use pidfd for both binding and signaling.'
        Assert-True ($scriptText -notmatch 'os\.kill\s*\(' -and $scriptText -notmatch "--exec',\s*'kill'") 'A process-stop path retains a raw PID signal fallback.'
    }
    $installRoot = Join-Path $root 'main-install'; $registryRoot = "$registryBase\Main"
    $chromeKey = Join-Path $registryRoot "Google\Chrome\NativeMessagingHosts\$hostName"
    $edgeKey = Join-Path $registryRoot "Microsoft\Edge\NativeMessagingHosts\$hostName"

    $whatIfParent = Join-Path $root 'whatif-parent'; $whatIfInstall = Join-Path $whatIfParent 'NativeHost'
    $plan = (& $installer -ProjectPath $ProjectPath -SkipProvision -InstallRoot $whatIfInstall -RegistryRoot $registryRoot -TestSafetyBase $root -TestHostExecutable $fakeExe -SkipSelfCheck -WhatIf | Select-Object -Last 1) | ConvertFrom-Json
    Assert-True ($plan.linux_project_path.StartsWith('/')) 'WhatIf did not convert the project path.'
    Assert-True (-not (Test-Path -LiteralPath $whatIfParent)) 'WhatIf created an install parent.'
    Assert-True (-not (Test-Path -LiteralPath $registryRoot)) 'WhatIf wrote registry state.'

    # The one operation-wide confirmation gate must run before parent creation,
    # dependency provisioning, uv acquisition, staging, packaging, or registry writes.
    $confirmParent = Join-Path $root 'confirm-parent'; $confirmInstall = Join-Path $confirmParent 'NativeHost'; $confirmRegistry = "$registryBase\Confirm"
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $confirmOutput = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installer -ProjectPath $ProjectPath -SkipProvision -InstallRoot $confirmInstall -RegistryRoot $confirmRegistry -TestSafetyBase $root -TestHostExecutable $fakeExe -SkipSelfCheck -Confirm 2>&1
        $confirmExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    Assert-True ($confirmExit -ne 0) "Installer -Confirm unexpectedly completed without a noninteractive refusal: $($confirmOutput -join ' ')"
    Assert-True (-not (Test-Path -LiteralPath $confirmParent) -and -not (Test-Path -LiteralPath $confirmRegistry)) 'Denied installer confirmation caused filesystem or registry side effects.'

    # Self-check frame reads and process exit share one deadline. Each fixture
    # holds stdout open beyond the short test deadline and must be killed promptly.
    foreach ($mode in @('no-output', 'partial-header', 'partial-body', 'keep-open')) {
        $fixtureInstall = Join-Path $root "self-check-$mode"; $fixtureRegistry = "$registryBase\SelfCheck-$mode"
        $timer = [Diagnostics.Stopwatch]::StartNew()
        Assert-Fails { & $installer -ProjectPath $ProjectPath -SkipProvision -InstallRoot $fixtureInstall -RegistryRoot $fixtureRegistry -TestSafetyBase $root -TestHostExecutable $fakeExe -TestSelfCheckMode $mode -TestSelfCheckTimeoutMilliseconds 400 | Out-Null } "Self-check fixture '$mode' did not time out."
        $timer.Stop()
        Assert-True ($timer.Elapsed.TotalSeconds -lt 4) "Self-check fixture '$mode' exceeded its unified deadline."
        Assert-True (-not (Test-Path -LiteralPath $fixtureInstall) -and -not (Test-Path -LiteralPath $fixtureRegistry)) "Self-check fixture '$mode' mutated final state."
    }

    # PowerShell's comparison coercions must not accept JSON strings/numbers as
    # protocol integers/booleans, nor tolerate missing/extra/nested shape drift.
    $badResponses = @(
        '{"v":"1","id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok"}}}',
        '{"v":true,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok"}}}',
        '{"v":1,"id":1,"ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":1,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok"}},"extra":1}',
        '{"v":1,"ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":true,"port":8000,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":"1","port":8000,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":true,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":"8000","collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":0,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":65536,"collector":{"status":"ok"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":8000}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok"},"extra":1}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"starting"}}}',
        '{"v":1,"id":"installer-self-check","ok":true,"result":{"protocol_version":1,"port":8000,"collector":{"status":"ok","extra":1}}}'
    )
    $badIndex = 0
    foreach ($json in $badResponses) {
        $badIndex++; $fixtureInstall = Join-Path $root "bad-response-$badIndex"; $fixtureRegistry = "$registryBase\BadResponse-$badIndex"
        Assert-Fails { & $installer -ProjectPath $ProjectPath -SkipProvision -InstallRoot $fixtureInstall -RegistryRoot $fixtureRegistry -TestSafetyBase $root -TestHostExecutable $fakeExe -TestSelfCheckMode response -TestSelfCheckResponseJson $json -TestSelfCheckTimeoutMilliseconds 2000 | Out-Null } "Malformed self-check response $badIndex was accepted."
        Assert-True (-not (Test-Path -LiteralPath $fixtureInstall) -and -not (Test-Path -LiteralPath $fixtureRegistry)) "Malformed response $badIndex mutated final state."
    }
    $validFixtureRoot = Join-Path $root 'valid-response'; $validFixtureRegistry = "$registryBase\ValidResponse"
    & $installer -ProjectPath $ProjectPath -SkipProvision -InstallRoot $validFixtureRoot -RegistryRoot $validFixtureRegistry -TestSafetyBase $root -TestHostExecutable $fakeExe -TestSelfCheckMode response -TestSelfCheckTimeoutMilliseconds 2000 | Out-Null
    Invoke-TestUninstall $validFixtureRoot $validFixtureRegistry

    New-Item -Path (Join-Path $chromeKey 'existing-child') -Force | Out-Null
    Set-Item -LiteralPath $chromeKey -Value 42
    New-ItemProperty -LiteralPath $chromeKey -Name named -Value 'before' -PropertyType ExpandString | Out-Null
    New-ItemProperty -LiteralPath (Join-Path $chromeKey 'existing-child') -Name child -Value 7 -PropertyType DWord | Out-Null
    Invoke-TestInstall $installRoot $registryRoot $fakeExe

    $statePath = Join-Path $installRoot '.sitefilter-native-host-owned.json'; $state = Get-Content $statePath -Raw | ConvertFrom-Json
    Assert-True ($state.version -eq 3) 'Install state is not version 3.'
    Assert-True ((@($state.owned_files) -join '|') -eq ($ownedNames -join '|')) 'Owned file set is not exact.'
    Assert-True (-not ((Get-Content $statePath -Raw) -match 'children|\\\.\.\\')) 'Install state contains recursive or dot-segment registry data.'
    foreach ($path in @($installRoot) + @($ownedNames | ForEach-Object { Join-Path $installRoot $_ } | Where-Object { Test-Path -LiteralPath $_ })) {
        $acl = Get-Acl -LiteralPath $path; $account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Assert-True ($acl.AreAccessRulesProtected) "ACL inheritance remains enabled: $path"
        Assert-True (@($acl.Access | Where-Object { $_.IdentityReference.Value -ne $account -or $_.IsInherited }).Count -eq 0) "Foreign or inherited ACL remains: $path"
    }
    $configBytes = [IO.File]::ReadAllBytes((Join-Path $installRoot 'config.json'))
    Assert-True (-not ($configBytes.Length -ge 3 -and $configBytes[0] -eq 0xEF -and $configBytes[1] -eq 0xBB -and $configBytes[2] -eq 0xBF)) 'Config JSON has a UTF-8 BOM.'

    # A successful reinstall must converge and strip foreign ACLs from every owned path.
    & icacls.exe $installRoot '/grant' '*S-1-5-32-545:(OI)(CI)R' '/q' | Out-Null
    & icacls.exe (Join-Path $installRoot 'config.json') '/grant' '*S-1-5-32-545:R' '/q' | Out-Null
    Invoke-TestInstall $installRoot $registryRoot $fakeExe
    foreach ($path in @($installRoot) + @($ownedNames | ForEach-Object { Join-Path $installRoot $_ } | Where-Object { Test-Path -LiteralPath $_ })) {
        $acl = Get-Acl -LiteralPath $path; $account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Assert-True ($acl.AreAccessRulesProtected -and @($acl.Access | Where-Object { $_.IdentityReference.Value -ne $account -or $_.IsInherited }).Count -eq 0) "Reinstall did not converge ACLs: $path"
    }
    Assert-True ((Get-Item $chromeKey).GetValueKind('') -eq [Microsoft.Win32.RegistryValueKind]::String) 'Installed default is not a manifest string.'
    Assert-True ((Get-Item $chromeKey).GetValue('named') -eq 'before') 'Install changed a named value.'
    Assert-True ((Get-Item (Join-Path $chromeKey 'existing-child')).GetValue('child') -eq 7) 'Install changed a subkey.'

    $unexpected = Join-Path $installRoot 'unexpected-after-install.txt'; Write-TestText $unexpected 'preserve'
    foreach ($point in @('after-root-swap', 'after-first-registry', 'self-check')) {
        $rootBefore = Get-RootFingerprint $installRoot; $chromeBefore = Get-DefaultFingerprint $chromeKey; $edgeBefore = Get-DefaultFingerprint $edgeKey
        Assert-Fails { Invoke-TestInstall $installRoot $registryRoot $fakeExe $point } "Failure injection '$point' did not fail."
        Assert-True ((Get-RootFingerprint $installRoot) -eq $rootBefore) "Root rollback failed at $point."
        Assert-True ((Get-DefaultFingerprint $chromeKey) -eq $chromeBefore) "Chrome rollback failed at $point."
        Assert-True ((Get-DefaultFingerprint $edgeKey) -eq $edgeBefore) "Edge rollback failed at $point."
    }
    $rootBefore = Get-RootFingerprint $installRoot; $edgeBefore = Get-DefaultFingerprint $edgeKey
    Assert-Fails { Invoke-TestInstall $installRoot $registryRoot $fakeExe 'after-first-registry-concurrent' } 'Concurrent registry failure injection did not fail.'
    Assert-True ((Get-RootFingerprint $installRoot) -eq $rootBefore) 'Concurrent registry rollback changed the original root.'
    Assert-True ((Get-Item $chromeKey).GetValue('') -eq 'C:\concurrent\replacement.json') 'Rollback overwrote a concurrent later Chrome default.'
    Assert-True ((Get-DefaultFingerprint $edgeKey) -eq $edgeBefore) 'Rollback changed the not-yet-written Edge key.'
    Set-Item -LiteralPath $chromeKey -Value (Join-Path $installRoot "$hostName.json")

    Write-TestText (Join-Path $installRoot 'collector-startup.log') 'locked'
    $rootBefore = Get-RootFingerprint $installRoot; $chromeBefore = Get-DefaultFingerprint $chromeKey; $edgeBefore = Get-DefaultFingerprint $edgeKey
    $lock = [IO.File]::Open((Join-Path $installRoot 'collector-startup.log'), [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $lockedFailure = $false
    try {
        try { Invoke-TestUninstall $installRoot $registryRoot } catch { $lockedFailure = $true }
    } finally { $lock.Dispose() }
    Assert-True $lockedFailure 'Locked owned log did not fail preflight.'
    Assert-True ((Get-RootFingerprint $installRoot) -eq $rootBefore -and (Get-DefaultFingerprint $chromeKey) -eq $chromeBefore -and (Get-DefaultFingerprint $edgeKey) -eq $edgeBefore) 'Locked-log failure mutated state.'

    Set-TestOwnedDescriptor $installRoot 'main'
    $signalLog = Join-Path $root 'signals.log'
    $rootBefore = Get-RootFingerprint $installRoot; $chromeBefore = Get-DefaultFingerprint $chromeKey; $edgeBefore = Get-DefaultFingerprint $edgeKey
    & $uninstaller -InstallRoot $installRoot -RegistryRoot $registryRoot -TestSafetyBase $root -TestProcessState Verified -TestSignalLog $signalLog -WhatIf | Out-Null
    Assert-True (-not (Test-Path $signalLog)) 'Uninstall -WhatIf signaled an owned process.'
    Assert-True ((Get-RootFingerprint $installRoot) -eq $rootBefore -and (Get-DefaultFingerprint $chromeKey) -eq $chromeBefore -and (Get-DefaultFingerprint $edgeKey) -eq $edgeBefore) 'Uninstall -WhatIf mutated files or registry.'
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $confirmOutput = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $uninstaller -InstallRoot $installRoot -RegistryRoot $registryRoot -TestSafetyBase $root -TestProcessState Verified -TestSignalLog $signalLog -Confirm 2>&1
        $confirmExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    Assert-True ($confirmExit -ne 0) "Explicit -Confirm unexpectedly completed without a noninteractive refusal: $($confirmOutput -join ' ')"
    Assert-True (-not (Test-Path $signalLog)) 'Denied/noninteractive -Confirm signaled an owned process.'
    Assert-True ((Get-RootFingerprint $installRoot) -eq $rootBefore -and (Get-DefaultFingerprint $chromeKey) -eq $chromeBefore -and (Get-DefaultFingerprint $edgeKey) -eq $edgeBefore) 'Denied/noninteractive -Confirm mutated files or registry.'
    New-ItemProperty -LiteralPath $chromeKey -Name later -Value 'keep' -PropertyType String | Out-Null
    New-Item -Path (Join-Path $edgeKey 'later-child') -Force | Out-Null
    Set-Item -LiteralPath $chromeKey -Value 'C:\later\user-manifest.json'
    $edgeWritable = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($edgeKey.Substring('HKCU:\'.Length), $true)
    try { $edgeWritable.DeleteValue('', $false) } finally { $edgeWritable.Dispose() }
    Invoke-TestUninstall $installRoot $registryRoot 'Verified' $signalLog
    Assert-True ((Get-Content $signalLog -Raw) -match '^TERM [0-9]+') 'Verified owned process seam was not signaled.'
    Assert-True ((Get-Item $chromeKey).GetValue('') -eq 'C:\later\user-manifest.json') 'Later Chrome default was overwritten.'
    Assert-True ((Get-Item $chromeKey).GetValue('later') -eq 'keep') 'Later named value was removed.'
    Assert-True ((Get-Item $chromeKey).GetValue('named') -eq 'before') 'Pre-existing named value was changed.'
    Assert-True (Test-Path -LiteralPath (Join-Path $edgeKey 'later-child')) 'Later subkey was removed.'
    Assert-True (Test-Path -LiteralPath $unexpected) 'Unexpected file was removed.'
    Assert-True (-not (Test-Path -LiteralPath $statePath)) 'Completed marker was retained.'
    Invoke-TestUninstall $installRoot $registryRoot

    # Wrong argv and PID changes must never signal or mutate; verified retry succeeds.
    $mismatchRoot = Join-Path $root 'mismatch-install'; $mismatchRegistry = "$registryBase\Mismatch"
    Invoke-TestInstall $mismatchRoot $mismatchRegistry $fakeExe
    Set-TestOwnedDescriptor $mismatchRoot 'mismatch'
    $mismatchSignal = Join-Path $root 'mismatch-signals.log'; $before = Get-RootFingerprint $mismatchRoot
    Assert-Fails { Invoke-TestUninstall $mismatchRoot $mismatchRegistry 'WrongCommand' $mismatchSignal } 'Wrong-command owned process did not fail.'
    Assert-True (-not (Test-Path $mismatchSignal) -and (Get-RootFingerprint $mismatchRoot) -eq $before) 'Wrong command signaled or mutated.'
    Assert-Fails { Invoke-TestUninstall $mismatchRoot $mismatchRegistry 'PidChanged' $mismatchSignal } 'Changed owned PID did not fail.'
    Assert-True (-not (Test-Path $mismatchSignal) -and (Get-RootFingerprint $mismatchRoot) -eq $before) 'Changed PID signaled or mutated.'
    Assert-Fails { Invoke-TestUninstall $mismatchRoot $mismatchRegistry 'PidfdUnavailable' $mismatchSignal } 'Missing pidfd support did not safely refuse uninstall.'
    Assert-True (-not (Test-Path $mismatchSignal) -and (Get-RootFingerprint $mismatchRoot) -eq $before) 'Missing pidfd support fell back to a raw PID signal or mutated state.'
    Invoke-TestUninstall $mismatchRoot $mismatchRegistry 'Verified' $mismatchSignal

    # An unowned process seam is never signaled, while prior value types and
    # the complete named-value/subkey tree round-trip exactly.
    $roundRoot = Join-Path $root 'roundtrip-install'; $roundRegistry = "$registryBase\Roundtrip"
    $roundChrome = Join-Path $roundRegistry "Google\Chrome\NativeMessagingHosts\$hostName"
    $roundEdge = Join-Path $roundRegistry "Microsoft\Edge\NativeMessagingHosts\$hostName"
    New-Item -Path (Join-Path $roundChrome 'child') -Force | Out-Null
    Set-Item -LiteralPath $roundChrome -Value 42
    New-ItemProperty -LiteralPath $roundChrome -Name named -Value '%TEMP%\before' -PropertyType ExpandString | Out-Null
    New-ItemProperty -LiteralPath (Join-Path $roundChrome 'child') -Name binary -Value ([byte[]](1, 2, 3)) -PropertyType Binary | Out-Null
    $roundChromeBefore = Get-DefaultFingerprint $roundChrome; $roundEdgeBefore = Get-DefaultFingerprint $roundEdge
    Invoke-TestInstall $roundRoot $roundRegistry $fakeExe
    Set-TestOwnedDescriptor $roundRoot 'roundtrip'
    $unownedSignals = Join-Path $root 'unowned-signals.log'
    Invoke-TestUninstall $roundRoot $roundRegistry 'Unowned' $unownedSignals
    Assert-True (-not (Test-Path $unownedSignals)) 'Unowned process seam was signaled.'
    Assert-True ((Get-DefaultFingerprint $roundChrome) -eq $roundChromeBefore -and (Get-DefaultFingerprint $roundEdge) -eq $roundEdgeBefore) 'Registry tree or prior value type did not round-trip.'

    # Version 1 migration conservatively removes its known default later while
    # retaining an empty pre-existing key because original key ownership is unknowable.
    $v1Root = Join-Path $root 'v1-install'; $v1Registry = "$registryBase\V1"
    Invoke-TestInstall $v1Root $v1Registry $fakeExe
    Write-TestText (Join-Path $v1Root '.sitefilter-native-host-owned.json') (([ordered]@{ version = 1; host_name = $hostName; install_root = $v1Root } | ConvertTo-Json -Depth 4))
    Invoke-TestInstall $v1Root $v1Registry $fakeExe
    Assert-True ((Get-Content (Join-Path $v1Root '.sitefilter-native-host-owned.json') -Raw | ConvertFrom-Json).version -eq 3) 'Version 1 marker did not migrate.'
    Invoke-TestUninstall $v1Root $v1Registry
    $v1Chrome = Join-Path $v1Registry "Google\Chrome\NativeMessagingHosts\$hostName"
    Assert-True ((Test-Path $v1Chrome) -and ((Get-Item $v1Chrome).GetValueNames() -notcontains '')) 'Version 1 migration removed a conservatively retained key or left its default.'

    # Version 2 migration reads only exact hardcoded host roots and ignores a
    # malicious dot-segment snapshot rather than traversing or replaying it.
    $v2Root = Join-Path $root 'v2-install'; $v2Registry = "$registryBase\V2"
    $v2Chrome = Join-Path $v2Registry "Google\Chrome\NativeMessagingHosts\$hostName"; $v2Edge = Join-Path $v2Registry "Microsoft\Edge\NativeMessagingHosts\$hostName"
    Invoke-TestInstall $v2Root $v2Registry $fakeExe
    $escapeKey = Join-Path $v2Registry 'Escape'; New-Item -Path $escapeKey -Force | Out-Null; Set-Item -LiteralPath $escapeKey -Value 'sentinel'
    $v2State = [ordered]@{ version = 2; host_name = $hostName; install_root = $v2Root; owned_files = $ownedNames; registry_before = @(
        [ordered]@{ path = $v2Chrome; existed = $false; values = @(); children = @() },
        [ordered]@{ path = $v2Edge; existed = $false; values = @(); children = @() },
        [ordered]@{ path = "$v2Chrome\..\..\..\Escape"; existed = $true; values = @([ordered]@{ name = ''; kind = 'String'; data = 'attacker' }); children = @() }
    ) }
    Write-TestText (Join-Path $v2Root '.sitefilter-native-host-owned.json') ($v2State | ConvertTo-Json -Depth 10)
    Invoke-TestInstall $v2Root $v2Registry $fakeExe
    $migratedText = Get-Content (Join-Path $v2Root '.sitefilter-native-host-owned.json') -Raw
    Assert-True (-not ($migratedText -match 'registry_before|\\\.\.\\|"path"')) 'Version 2 migration retained recursive or dot-segment state.'
    Assert-True ((Get-Item $escapeKey).GetValue('') -eq 'sentinel') 'Malicious version 2 snapshot changed an unrelated key.'
    Invoke-TestUninstall $v2Root $v2Registry
    Assert-True ((Get-Item $escapeKey).GetValue('') -eq 'sentinel') 'Uninstall replayed a malicious version 2 snapshot.'

    # A target without our marker may retain arbitrary files, but a colliding
    # owned filename is refused without changing either the root or registry.
    $foreignRoot = Join-Path $root 'foreign-install'; $foreignRegistry = "$registryBase\Foreign"
    New-Item -ItemType Directory -Path $foreignRoot | Out-Null; Write-TestText (Join-Path $foreignRoot 'config.json') 'foreign'
    $foreignBefore = Get-RootFingerprint $foreignRoot
    Assert-Fails { Invoke-TestInstall $foreignRoot $foreignRegistry $fakeExe } 'Installer overwrote an unowned colliding file.'
    Assert-True ((Get-RootFingerprint $foreignRoot) -eq $foreignBefore -and -not (Test-Path $foreignRegistry)) 'Collision refusal mutated state.'

    # Pinned archive branch must verify and extract before the same run proceeds.
    $fixtureRoot = Join-Path $root 'uv-fixture'; New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
    Write-TestText (Join-Path $fixtureRoot 'uv.exe') 'fixture uv executable'
    $fixtureZip = Join-Path $root 'uv-fixture.zip'; Compress-Archive -Path (Join-Path $fixtureRoot 'uv.exe') -DestinationPath $fixtureZip
    $fixtureHash = (Get-FileHash $fixtureZip -Algorithm SHA256).Hash
    $uvRoot = Join-Path $root 'uv-install'; $uvRegistry = "$registryBase\Uv"; $uvBin = Join-Path $root 'fake-user-bin'
    & $installer -ProjectPath $ProjectPath -SkipProvision -InstallRoot $uvRoot -RegistryRoot $uvRegistry -TestSafetyBase $root -TestHostExecutable $fakeExe -TestRequireWindowsUv -TestIgnoreInstalledUv -TestUvArchivePath $fixtureZip -TestUvArchiveSha256 $fixtureHash -TestUvInstallDirectory $uvBin -SkipSelfCheck | Out-Null
    Assert-True (Test-Path (Join-Path $uvBin 'uv.exe')) 'Pinned verified uv fixture was not resolved in the same run.'
    Invoke-TestUninstall $uvRoot $uvRegistry

    # Exercise the production WSL uv resolution/version branch without running
    # make against a disposable install root.
    $wslUvRoot = Join-Path $root 'wsl-uv-install'; $wslUvRegistry = "$registryBase\WslUv"
    & $installer -ProjectPath $ProjectPath -InstallRoot $wslUvRoot -RegistryRoot $wslUvRegistry -TestSafetyBase $root -TestHostExecutable $fakeExe -TestSkipMake -SkipSelfCheck | Out-Null
    Assert-True (Test-Path (Join-Path $wslUvRoot '.sitefilter-native-host-owned.json')) 'Production WSL uv verification branch did not proceed.'
    Invoke-TestUninstall $wslUvRoot $wslUvRegistry

    # A junction root must fail before writes outside the intended target.
    $outside = Join-Path $root 'outside'; New-Item -ItemType Directory -Path $outside | Out-Null
    $outsideSentinel = Join-Path $outside 'sentinel.txt'; Write-TestText $outsideSentinel 'outside'
    $junction = Join-Path $root 'junction-install'; New-Item -ItemType Junction -Path $junction -Target $outside | Out-Null
    Assert-Fails { Invoke-TestInstall $junction "$registryBase\Junction" $fakeExe } 'Installer accepted a junction root.'
    Assert-Fails { Invoke-TestUninstall $junction "$registryBase\Junction" } 'Uninstaller accepted a junction root.'
    Assert-True ((Get-Content $outsideSentinel -Raw) -eq 'outside') 'Reparse test changed the outside target.'
    [IO.Directory]::Delete($junction, $false)

    Write-Host 'PASS: early confirmation, bounded strict self-checks, pidfd-only signaling, PS5-safe transaction, BOM-free writes, exact ACL/ownership, rollback seams, lock/process preflight, default-only registry ownership, idempotent uninstall, pinned uv supply, and reparse containment.'
} finally {
    if (Test-Path -LiteralPath $registryBase) { Remove-Item -LiteralPath $registryBase -Recurse -Force }
    if (Test-Path -LiteralPath $root) { Remove-TestTree $root }
}
