[CmdletBinding()]
param([Parameter(Mandatory)][string]$ProjectPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Join-Path ([System.IO.Path]::GetTempPath()) ("sitefilter-native-host-test-" + [Guid]::NewGuid().ToString('N'))
$installRoot = Join-Path $root 'install'
$registryRoot = "HKCU:\Software\SiteFilterNativeHostTests\$([Guid]::NewGuid().ToString('N'))"
$fakeExe = Join-Path $root 'fake-host.exe'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

try {
    New-Item -ItemType Directory -Path $root | Out-Null
    Set-Content -LiteralPath $fakeExe -Value 'test executable' -Encoding ascii

    $whatIfJson = & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $ProjectPath -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck -WhatIf | Select-Object -Last 1
    $plan = $whatIfJson | ConvertFrom-Json
    Assert-True ($plan.linux_project_path.StartsWith('/')) 'Windows project path was not converted to an absolute Linux path.'
    Assert-True ($plan.runtime_file -match 'native-runtime\.json$') 'Runtime descriptor path is not the project data/native-runtime.json file.'
    Assert-True (-not (Test-Path -LiteralPath $installRoot)) 'WhatIf mutated the install root.'
    Assert-True (-not (Test-Path -LiteralPath $registryRoot)) 'WhatIf mutated the registry.'

    $invalidRejected = $false
    try {
        & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $root -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck -WhatIf 2>$null | Out-Null
    } catch {
        $invalidRejected = $true
    }
    Assert-True $invalidRejected 'A project without marker files was accepted.'

    $preexistingChromeKey = Join-Path $registryRoot 'Google\Chrome\NativeMessagingHosts\dev.zackzhang.sitefilter_collector'
    New-Item -Path $preexistingChromeKey -Force | Out-Null
    Set-Item -LiteralPath $preexistingChromeKey -Value 'C:\preserved\previous-manifest.json'

    & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $ProjectPath -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck | Out-Null
    & (Join-Path $PSScriptRoot 'install-native-host.ps1') -ProjectPath $ProjectPath -SkipProvision -InstallRoot $installRoot -RegistryRoot $registryRoot -TestHostExecutable $fakeExe -SkipSelfCheck | Out-Null

    $manifest = Get-Content -LiteralPath (Join-Path $installRoot 'dev.zackzhang.sitefilter_collector.json') -Raw | ConvertFrom-Json
    $config = Get-Content -LiteralPath (Join-Path $installRoot 'config.json') -Raw | ConvertFrom-Json
    Assert-True ($manifest.allowed_origins.Count -eq 1) 'Manifest must have exactly one allowed origin.'
    Assert-True ($manifest.allowed_origins[0] -eq 'chrome-extension://jaihdgjnnpmiabeoefmihmjhoodcjlhf/') 'Manifest origin is not the fixed origin.'
    Assert-True ($config.allowed_origins.Count -eq 1 -and $config.allowed_origins[0] -eq $manifest.allowed_origins[0]) 'Config origin differs from manifest.'
    foreach ($browserPath in @('Google\Chrome', 'Microsoft\Edge')) {
        $key = Join-Path $registryRoot "$browserPath\NativeMessagingHosts\dev.zackzhang.sitefilter_collector"
        Assert-True ((Get-Item -LiteralPath $key).GetValue('') -eq (Join-Path $installRoot 'dev.zackzhang.sitefilter_collector.json')) "$browserPath registry entry is wrong."
    }
    $acl = Get-Acl -LiteralPath $installRoot
    Assert-True ($acl.AreAccessRulesProtected) 'Install directory must have inherited ACLs disabled.'
    $configAcl = Get-Acl -LiteralPath (Join-Path $installRoot 'config.json')
    Assert-True ($configAcl.AreAccessRulesProtected) 'Config file must have inherited ACLs disabled.'
    $currentAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    Assert-True (@($configAcl.Access | Where-Object { $_.IdentityReference.Value -eq $currentAccount -and $_.FileSystemRights.ToString().Contains('FullControl') -and -not $_.IsInherited }).Count -eq 1) 'Current user lacks explicit FullControl on config.'

    $publicDer = [Convert]::FromBase64String((Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.key') -Raw).Trim())
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($publicDer)
    $letters = 'abcdefghijklmnop'
    $idCharacters = foreach ($b in $hash[0..15]) { $letters[$b -shr 4]; $letters[$b -band 15] }
    $derived = -join $idCharacters
    Assert-True ($derived -eq 'jaihdgjnnpmiabeoefmihmjhoodcjlhf') 'Committed public key does not derive the fixed extension ID.'

    $sentinel = Join-Path $root 'collector-data-sentinel.txt'
    Set-Content -LiteralPath $sentinel -Value 'preserve' -Encoding ascii
    & (Join-Path $PSScriptRoot 'uninstall-native-host.ps1') -InstallRoot $installRoot -RegistryRoot $registryRoot | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $installRoot)) 'Uninstall did not remove owned integration files.'
    Assert-True (Test-Path -LiteralPath $sentinel) 'Uninstall removed data outside its owned install root.'
    Assert-True ((Get-Item -LiteralPath $preexistingChromeKey).GetValue('') -eq 'C:\preserved\previous-manifest.json') 'Uninstall did not restore the preserved Chrome registration.'
    $edgeKey = Join-Path $registryRoot 'Microsoft\Edge\NativeMessagingHosts\dev.zackzhang.sitefilter_collector'
    Assert-True (-not (Test-Path -LiteralPath $edgeKey)) 'Edge owned registry key was not removed.'
    Write-Host 'PASS: WhatIf, path conversion, manifest/config, Chrome+Edge registration, ACL, idempotence, key identity, and uninstall scope.'
} finally {
    if (Test-Path -LiteralPath $registryRoot) { Remove-Item -LiteralPath $registryRoot -Recurse -Force }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
