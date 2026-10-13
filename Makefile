SHELL := /bin/bash
COMPOSE := docker compose
API := http://localhost:8000
KEY ?= vault-demo-key

.DEFAULT_GOAL := help
.PHONY: help up demo down reset logs ps migrate seed test test-fast lint fmt smoke smoke-llm eval eval-demo doctor workers web-dev web-build psql redis-cli

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Dev stack (bind mounts + auto-reload)
	$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory wait

demo: ## Demo stack: built image only, no reload, no bind mounts
	$(COMPOSE) -f compose.yaml up -d --build
	@$(MAKE) --no-print-directory wait

wait:
	@echo -n "waiting for API"; for i in $$(seq 1 60); do curl -sf $(API)/health/live >/dev/null && echo " ok" && exit 0; echo -n "."; sleep 2; done; echo " timeout"; exit 1

down: ## Stop the stack (keeps data)
	$(COMPOSE) down

reset: ## Stop and DELETE all project data volumes (postgres, valkey, s3)
	$(COMPOSE) down -v --remove-orphans

logs: ## Tail logs (S=service to filter)
	$(COMPOSE) logs -f --tail=100 $(S)

ps:
	$(COMPOSE) ps

migrate: ## Alembic upgrade head (inside the stack)
	$(COMPOSE) run --rm migrate python -m app.cli migrate

seed: ## Generate the demo corpus and upload it as the demo user
	uv run python scripts/make_demo_docs.py
	uv run python scripts/seed_demo.py --key $(KEY)

test: ## Full test suite (Fake LLM provider, real Postgres/Valkey/S3 from the stack)
	uv run pytest -q

test-fast: ## Unit tests only (no services needed)
	uv run pytest -q -m "not integration"

lint: ## Ruff lint
	uv run ruff check app tests scripts evals

fmt: ## Ruff format + autofix
	uv run ruff format app tests scripts evals && uv run ruff check --fix app tests scripts evals

smoke: ## End-to-end smoke against the running stack
	uv run python scripts/smoke.py --api $(API) --key $(KEY)

smoke-llm: ## Verify every configured model/effort pair + embeddings + TTFT (real OpenAI key)
	uv run python scripts/smoke_llm.py

eval: ## RAG evaluation over the golden set (eval user)
	uv run python evals/run_eval.py --api $(API) --key vault-eval-key

eval-demo: ## Only demo-tagged cases, each repeated 3x (must pass 3/3)
	uv run python evals/run_eval.py --api $(API) --key vault-eval-key --tag demo --repeat 3

doctor: ## Pre-demo checks: ports, health, workers, spend
	uv run python scripts/doctor.py --api $(API) --key $(KEY)

workers: ## Which workers consume which queues
	$(COMPOSE) exec worker-ingest celery -A app.worker.celery_app inspect active_queues

web-dev: ## React dev server on :5173 (proxies /api to :8000)
	cd frontend && pnpm dev

web-build:
	cd frontend && pnpm build

psql: ## psql into the project database
	$(COMPOSE) exec postgres psql -U vault -d vault

redis-cli:
	$(COMPOSE) exec valkey valkey-cli
