#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('ChromeForTesting', 'Chromium', 'Edge')][string]$Browser,
    [Parameter()][string]$BrowserPath,
    [Parameter()][string]$PythonPath,
    [Parameter()][string]$ChromeForTestingCacheRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $PythonPath) {
    $candidate = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.venv-native-host\Scripts\python.exe'
    if (Test-Path -LiteralPath $candidate) { $PythonPath = $candidate }
    else { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
}
if ($Browser -eq 'ChromeForTesting' -and -not $BrowserPath) {
    if (-not $ChromeForTestingCacheRoot) {
        if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }
        $ChromeForTestingCacheRoot = Join-Path $env:LOCALAPPDATA 'SiteFilter\BrowserTestCache\chrome-for-testing'
    }
    $ChromeForTestingCacheRoot = [System.IO.Path]::GetFullPath($ChromeForTestingCacheRoot)
    $metadata = Invoke-RestMethod -Uri 'https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json'
    $version = [string]$metadata.channels.Stable.version
    $download = $metadata.channels.Stable.downloads.chrome | Where-Object { $_.platform -eq 'win64' } | Select-Object -First 1
    if (-not $version -or -not $download.url) { throw 'Official Chrome for Testing metadata did not contain a stable win64 build.' }
    $versionRoot = Join-Path $ChromeForTestingCacheRoot $version
    $BrowserPath = Join-Path $versionRoot 'chrome-win64\chrome.exe'
    $archive = $null
    if (-not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) {
        if (Test-Path -LiteralPath $versionRoot) { throw "Incomplete Chrome for Testing cache exists; preserve or inspect it before retrying: $versionRoot" }
        $downloadRoot = Join-Path $ChromeForTestingCacheRoot 'downloads'
        New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
        $archive = Join-Path $downloadRoot "chrome-$version-win64.zip"
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { Invoke-WebRequest -UseBasicParsing -Uri $download.url -OutFile $archive }
        New-Item -ItemType Directory -Path $versionRoot | Out-Null
        Expand-Archive -LiteralPath $archive -DestinationPath $versionRoot
    }
    $archiveHash = if ($archive -and (Test-Path -LiteralPath $archive -PathType Leaf)) { (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash } else { 'cached-executable' }
    Write-Host "Chrome for Testing stable $version; executable=$BrowserPath; archive_sha256=$archiveHash"
}
if ($Browser -eq 'Chromium' -and -not $BrowserPath) {
    $BrowserPath = (& $PythonPath -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()" | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve Playwright Chromium. Run: uv run playwright install chromium' }
}
if (-not $BrowserPath -or -not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) { throw "Browser executable not found: $BrowserPath" }
Write-Host "$Browser executable version: $((Get-Item -LiteralPath $BrowserPath).VersionInfo.FileVersion)"

& $PythonPath (Join-Path $PSScriptRoot 'browser-native-host-probe.py') --self-test-validation
if ($LASTEXITCODE -ne 0) { throw 'Native messaging response validator self-test failed.' }
& $PythonPath (Join-Path $PSScriptRoot 'browser-native-host-probe.py') --browser $Browser --browser-path $BrowserPath --integration-root $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw "$Browser native messaging probe failed with exit code $LASTEXITCODE." }
