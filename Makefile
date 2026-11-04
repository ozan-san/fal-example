.PHONY: up down load test lint

up:
	docker compose up -d --build --wait app grafana prometheus

down:
	docker compose down

load:
	docker compose --profile load run --rm --no-deps --build loadgen

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .
