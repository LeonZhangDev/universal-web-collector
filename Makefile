.PHONY: install start start-native start-dev backend frontend test build docker clean

install:
	uv sync --group dev
	uv run playwright install chromium
	cd frontend && npm install && npm install --package-lock-only

start:
	uv run python scripts/start.py

start-native:
	@set -eu; \
	uv_bin="$$(command -v uv 2>/dev/null || true)"; \
	if [ -z "$$uv_bin" ]; then uv_bin="$${HOME:-}/.local/bin/uv"; fi; \
	if [ ! -x "$$uv_bin" ]; then \
		echo 'start-native: uv not found on PATH or at $$HOME/.local/bin/uv' >&2; \
		exit 127; \
	fi; \
	UWC_START_UV="$$uv_bin"; export UWC_START_UV; \
	exec "$$uv_bin" run python scripts/start.py --native --no-open --idle-minutes 30

start-dev:
	uv run python scripts/start.py --dev

backend:
	uv run python backend/main.py

frontend:
	cd frontend && npm run dev

test:
	uv run pytest -q

build:
	cd frontend && npm run build

docker:
	docker compose up -d --build

clean:
	rm -rf __pycache__ .pytest_cache
