.PHONY: install start start-native start-dev backend frontend test build docker clean \
	add-site selfcheck drift guard

install:
	uv sync --group dev
	uv run playwright install chromium
	cd frontend && npm install && npm install --package-lock-only

add-site:
	@test -n "$(SITE)" || { echo 'usage: make add-site SITE=<站点名>   (或 make add-site SITE=--all)'; exit 2; }
	uv run python scripts/add_site.py --verify $(SITE)

selfcheck:
	uv run python scripts/selfcheck.py

drift:
	uv run python scripts/drift_check.py

guard:
	uv run python scripts/gateguard.py

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
	uv run python scripts/gateguard.py

build:
	cd frontend && npm run build

docker:
	docker compose up -d --build

clean:
	rm -rf __pycache__ .pytest_cache
