# ---- 前端构建 ----
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---- 运行时 ----
FROM python:3.13-slim
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY backend/ backend/
COPY --from=frontend /build/dist frontend/dist/

RUN uv run playwright install --with-deps chromium

ENV UWC_DOWNLOAD_DIR=/data/downloads \
    UWC_DB_PATH=/data/collector.db \
    UWC_BROWSER_STATE_DIR=/data/browser_state
VOLUME ["/data"]
EXPOSE 8000

WORKDIR /app/backend
CMD ["uv", "run", "--project", "/app", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
