[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Chrome', 'Edge')][string]$Browser,
    [Parameter(Mandatory)][string]$BrowserPath,
    [Parameter()][string]$PythonPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) { throw "Browser executable not found: $BrowserPath" }
if (-not $PythonPath) {
    $candidate = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.venv-native-host\Scripts\python.exe'
    if (Test-Path -LiteralPath $candidate) { $PythonPath = $candidate }
    else { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
}

& $PythonPath (Join-Path $PSScriptRoot 'browser-native-host-probe.py') --browser $Browser --browser-path $BrowserPath --integration-root $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw "$Browser native messaging probe failed with exit code $LASTEXITCODE." }
