[CmdletBinding()]
param([Parameter(Mandatory)][string]$ProjectPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Join-Path ([System.IO.Path]::GetTempPath()) ("sitefilter-native-host-test-" + [Guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $root 'install'
$uvInstallRoot = Join-Path $root 'uv-install'
$registryRoot = "HKCU:\Software\SiteFilterNativeHostTests\$([Guid]::NewGuid().ToString('N'))"
$uvRegistryRoot = "$registryRoot-Uv"
$fakeExe = Join-Path $root 'fake-host.exe'

function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "ASSERTION FAILED: $Message" } }
function Assert-RegistryValue($Key, [string]$Name, $Expected, [Microsoft.Win32.RegistryValueKind]$Kind) {
    $actualKind = $Key.GetValueKind($Name)
    Assert-True ($actualKind -eq $Kind) "Registry kind differs for '$Name': $actualKind"
    $actual = $Key.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    if ($Kind -eq [Microsoft.Win32.RegistryValueKind]::Binary) { Assert-True (([Convert]::ToBase64String($actual)) -eq ([Convert]::ToBase64String($Expected))) "Binary registry value differs for '$Name'." }
    elseif ($Kind -eq [Microsoft.Win32.RegistryValueKind]::MultiString) { Assert-True (($actual -join '|') -eq ($Expected -join '|')) "Multi-string registry value differs for '$Name'." }
    else { Assert-True ($actual -eq $Expected) "Registry value differs for '$Name': $actual" }
}

try {
    New-Item -ItemType Directory -Path $root | Out-Null
    Set-Content -LiteralPath $fakeExe -Value 'test executable' -Encoding ascii

    $whatIfJson = & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $ProjectPath -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck -WhatIf | Select-Object -Last 1
    $plan = $whatIfJson | ConvertFrom-Json
    Assert-True ($plan.linux_project_path.StartsWith('/')) 'Windows project path was not converted to an absolute Linux path.'
    Assert-True ($plan.runtime_file -match 'native-runtime\.json$') 'Runtime descriptor path is not project data/native-runtime.json.'
    Assert-True (-not (Test-Path -LiteralPath $installRoot)) 'WhatIf mutated the install root.'
    Assert-True (-not (Test-Path -LiteralPath $registryRoot)) 'WhatIf mutated the registry.'

    $invalidRejected = $false
    try { & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $root -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck -WhatIf 2>$null | Out-Null } catch { $invalidRejected = $true }
    Assert-True $invalidRejected 'A project without marker files was accepted.'

    $chromeKeyPath = Join-Path $registryRoot 'Google\Chrome\NativeMessagingHosts\dev.zackzhang.sitefilter_collector'
    $chromeChildPath = Join-Path $chromeKeyPath 'existing-child'
    New-Item -Path $chromeChildPath -Force | Out-Null
    Set-Item -LiteralPath $chromeKeyPath -Value 'C:\preserved\previous-manifest.json'
    New-ItemProperty -LiteralPath $chromeKeyPath -Name 'expand' -Value '%TEMP%\previous.json' -PropertyType ExpandString | Out-Null
    New-ItemProperty -LiteralPath $chromeKeyPath -Name 'dword' -Value 42 -PropertyType DWord | Out-Null
    New-ItemProperty -LiteralPath $chromeKeyPath -Name 'binary' -Value ([byte[]](1, 2, 254)) -PropertyType Binary | Out-Null
    New-ItemProperty -LiteralPath $chromeKeyPath -Name 'multi' -Value ([string[]]('one', 'two')) -PropertyType MultiString | Out-Null
    New-ItemProperty -LiteralPath $chromeChildPath -Name 'qword' -Value ([long]4294967297) -PropertyType QWord | Out-Null

    & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $ProjectPath -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck | Out-Null
    & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $ProjectPath -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck | Out-Null

    $manifest = Get-Content -LiteralPath (Join-Path $installRoot 'dev.zackzhang.sitefilter_collector.json') -Raw | ConvertFrom-Json
    $config = Get-Content -LiteralPath (Join-Path $installRoot 'config.json') -Raw | ConvertFrom-Json
    $state = Get-Content -LiteralPath (Join-Path $installRoot '.sitefilter-native-host-owned.json') -Raw | ConvertFrom-Json
    Assert-True ($state.version -eq 2 -and $state.owned_files.Count -ge 8) 'Versioned ownership state does not enumerate owned files.'
    Assert-True ($state.registry_before.Count -eq 2) 'Ownership state does not contain both registry snapshots.'
    Assert-True ($manifest.allowed_origins.Count -eq 1 -and $manifest.allowed_origins[0] -eq 'chrome-extension://jaihdgjnnpmiabeoefmihmjhoodcjlhf/') 'Manifest origin is not the fixed origin.'
    Assert-True ($config.allowed_origins.Count -eq 1 -and $config.allowed_origins[0] -eq $manifest.allowed_origins[0]) 'Config origin differs from manifest.'
    foreach ($browserPath in @('Google\Chrome', 'Microsoft\Edge')) {
        $key = Join-Path $registryRoot "$browserPath\NativeMessagingHosts\dev.zackzhang.sitefilter_collector"
        Assert-True ((Get-Item -LiteralPath $key).GetValue('') -eq (Join-Path $installRoot 'dev.zackzhang.sitefilter_collector.json')) "$browserPath registration is wrong."
    }

    $configAcl = Get-Acl -LiteralPath (Join-Path $installRoot 'config.json')
    $currentAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    Assert-True ($configAcl.AreAccessRulesProtected) 'Config file must have inherited ACLs disabled.'
    Assert-True (@($configAcl.Access | Where-Object { $_.IdentityReference.Value -eq $currentAccount -and $_.FileSystemRights.ToString().Contains('FullControl') -and -not $_.IsInherited }).Count -eq 1) 'Current user lacks explicit FullControl on config.'

    $publicDer = [Convert]::FromBase64String((Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.key') -Raw).Trim())
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($publicDer); $letters = 'abcdefghijklmnop'
    $idCharacters = foreach ($byte in $hash[0..15]) { $letters[$byte -shr 4]; $letters[$byte -band 15] }
    Assert-True ((-join $idCharacters) -eq 'jaihdgjnnpmiabeoefmihmjhoodcjlhf') 'Public key does not derive the fixed extension ID.'

    $laterFile = Join-Path $installRoot 'unexpected-after-install.txt'; Set-Content -LiteralPath $laterFile -Value 'preserve' -Encoding ascii
    New-ItemProperty -LiteralPath $chromeKeyPath -Name 'later-named' -Value 'keep' -PropertyType String | Out-Null
    $chromeLaterChild = Join-Path $chromeKeyPath 'later-child'; New-Item -Path $chromeLaterChild -Force | Out-Null; New-ItemProperty -LiteralPath $chromeLaterChild -Name 'value' -Value 7 -PropertyType DWord | Out-Null
    $edgeKeyPath = Join-Path $registryRoot 'Microsoft\Edge\NativeMessagingHosts\dev.zackzhang.sitefilter_collector'
    New-ItemProperty -LiteralPath $edgeKeyPath -Name 'later-named' -Value 'keep' -PropertyType String | Out-Null
    $edgeLaterChild = Join-Path $edgeKeyPath 'later-child'; New-Item -Path $edgeLaterChild -Force | Out-Null; New-ItemProperty -LiteralPath $edgeLaterChild -Name 'value' -Value 9 -PropertyType DWord | Out-Null
    $outsideSentinel = Join-Path $root 'collector-data-sentinel.txt'; Set-Content -LiteralPath $outsideSentinel -Value 'preserve' -Encoding ascii

    & (Join-Path $PSScriptRoot 'uninstall-native-host.ps1') -InstallRoot $installRoot -RegistryRoot $registryRoot | Out-Null
    Assert-True (Test-Path -LiteralPath $laterFile) 'Uninstall removed an unexpected install-root file.'
    Assert-True (Test-Path -LiteralPath $outsideSentinel) 'Uninstall removed data outside its install root.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $installRoot 'config.json'))) 'Uninstall left an owned config file.'
    $chromeKey = Get-Item $chromeKeyPath
    Assert-RegistryValue $chromeKey '' 'C:\preserved\previous-manifest.json' ([Microsoft.Win32.RegistryValueKind]::String)
    Assert-RegistryValue $chromeKey 'expand' '%TEMP%\previous.json' ([Microsoft.Win32.RegistryValueKind]::ExpandString)
    Assert-RegistryValue $chromeKey 'dword' 42 ([Microsoft.Win32.RegistryValueKind]::DWord)
    Assert-RegistryValue $chromeKey 'binary' ([byte[]](1, 2, 254)) ([Microsoft.Win32.RegistryValueKind]::Binary)
    Assert-RegistryValue $chromeKey 'multi' ([string[]]('one', 'two')) ([Microsoft.Win32.RegistryValueKind]::MultiString)
    Assert-RegistryValue (Get-Item $chromeChildPath) 'qword' ([long]4294967297) ([Microsoft.Win32.RegistryValueKind]::QWord)
    Assert-RegistryValue $chromeKey 'later-named' 'keep' ([Microsoft.Win32.RegistryValueKind]::String)
    Assert-True (Test-Path -LiteralPath $chromeLaterChild) 'Later Chrome subkey was removed.'
    $edgeKey = Get-Item $edgeKeyPath
    Assert-True ($null -eq $edgeKey.GetValue('', $null)) 'Owned Edge default value was not removed.'
    Assert-RegistryValue $edgeKey 'later-named' 'keep' ([Microsoft.Win32.RegistryValueKind]::String)
    Assert-True (Test-Path -LiteralPath $edgeLaterChild) 'Later Edge subkey was removed.'

    $directUv = (Get-Command uv -ErrorAction Stop).Source
    $savedPath = $env:PATH
    try {
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
        Assert-True ($null -eq (Get-Command uv -ErrorAction SilentlyContinue)) 'uv unexpectedly remains on the isolated PATH.'
        & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $ProjectPath -SkipProvision -InstallRoot $uvInstallRoot -RegistryRoot $uvRegistryRoot -TestWindowsUvPath $directUv -SkipSelfCheck | Out-Null
        Assert-True (Test-Path -LiteralPath (Join-Path $uvInstallRoot 'sitefilter-native-host.exe')) 'Direct per-user uv resolution did not package in the same process.'
    } finally { $env:PATH = $savedPath }
    & (Join-Path $PSScriptRoot 'uninstall-native-host.ps1') -InstallRoot $uvInstallRoot -RegistryRoot $uvRegistryRoot | Out-Null

    Write-Host 'PASS: WhatIf, validation, WSL paths, manifest identity, ACL, idempotence, typed recursive registry restore, later-state preservation, scoped uninstall, and same-process per-user uv resolution.'
} finally {
    foreach ($testRegistryRoot in @($registryRoot, $uvRegistryRoot)) { if (Test-Path -LiteralPath $testRegistryRoot) { Remove-Item -LiteralPath $testRegistryRoot -Recurse -Force } }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
