# 一键启动(Windows): .\start.ps1 [参数同 scripts/start.py]
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[x] 未检测到 uv, 先安装任选其一:" -ForegroundColor Red
    Write-Host "    winget install --id=astral-sh.uv -e"
    Write-Host "    powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
    exit 1
}

uv run python scripts/start.py @args
