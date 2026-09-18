#!/usr/bin/env bash
# 一键启动(Unix): ./start.sh [参数同 scripts/start.py]
set -e

if ! command -v uv >/dev/null 2>&1; then
    echo "[x] 未检测到 uv, 先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

exec uv run python scripts/start.py "$@"
