.PHONY: dev test lint up down logs fmt

dev: .env
	docker compose up --build

up: .env
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

.env:
	cp .env.example .env

test:
	pytest -q

lint:
	ruff check .

fmt:
	ruff check --fix .
