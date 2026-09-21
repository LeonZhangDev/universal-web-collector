.PHONY: install start start-native start-dev backend frontend test build docker clean

install:
	uv sync --group dev
	uv run playwright install chromium
	cd frontend && npm install && npm install --package-lock-only

start:
	uv run python scripts/start.py

start-native:
	uv run python scripts/start.py --native --no-open --idle-minutes 30

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
