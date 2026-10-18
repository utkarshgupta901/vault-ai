SHELL := /bin/bash
COMPOSE := docker compose
# Host port compose.yaml publishes the API on ("${API_PORT:-8000}:8000"). make reads it from the shell or the command
# line, not from .env, so export it (compose reads the shell too). Empty counts as unset, as with compose's ":-".
API_PORT := $(or $(API_PORT),8000)
# URL of the client targets (seed, smoke, doctor, eval*, freeze-starters, web-dev). `make ... API=` points them at
# another stack or a deploy; a plain `=` means a stray API variable in the shell cannot. API= never moves the port a
# stack publishes on, so up/demo/demo-luna refuse it rather than start on API_PORT and then wait 2 min on API.
API = http://localhost:$(API_PORT)
ifneq ($(filter up demo demo-luna,$(MAKECMDGOALS)),)
ifeq ($(origin API),command line)
$(error API= does not move the port the stack publishes on: use API_PORT=<port> with up/demo/demo-luna)
endif
endif
# Files for the presentation stack: compose.yaml alone, so the dev override (bind mounts + reload) can never apply.
# Add your own on top with e.g. DEMO_FILES="-f compose.yaml -f compose.local.yaml".
DEMO_FILES ?= -f compose.yaml
KEY ?= vault-demo-key
QUOTA_USER ?= demo
QUOTA_USD ?= 100

.DEFAULT_GOAL := help
.PHONY: help up demo demo-luna raise-quota down reset logs ps migrate seed test test-fast lint fmt ci smoke smoke-llm eval eval-demo eval-retrieval doctor pre-demo workers web-dev web-build psql redis-cli snapshot-demo restore-demo freeze-starters

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Dev stack (bind mounts + auto-reload: a file save restarts the API/workers, so not for presenting)
	$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory wait

demo: ## PRESENTATION MODE: built image only, no reload, no bind mounts (also switches back from demo-luna)
	$(COMPOSE) $(DEMO_FILES) up -d --build
	@$(MAKE) --no-print-directory wait

# Only the api container is recreated (--no-deps): workers, data and queues are untouched. Run it after `make demo`
# (it reuses the image that built); `make demo` again returns the API to the default answer model.
demo-luna: ## Demo stack with answers on gpt-6-luna (effort low): faster first token if Sol is slow today
	ANSWER_MODEL=gpt-6-luna ANSWER_EFFORT=low $(COMPOSE) $(DEMO_FILES) up -d --no-deps --force-recreate api
	@$(MAKE) --no-print-directory wait

raise-quota: ## Raise a user's daily AI spend limit (default: demo -> 100 USD; QUOTA_USER=, QUOTA_USD=)
	$(COMPOSE) exec -T postgres psql -U vault -d vault -v ON_ERROR_STOP=1 \
		-c "UPDATE users SET daily_cost_limit_usd = $(QUOTA_USD) WHERE name = '$(QUOTA_USER)' RETURNING name, daily_cost_limit_usd"

# Live first (the process is up), then ready until both worker pools answer: right after a start /health/ready says
# 200 "degraded" for ~10 s while the workers register, and an upload made then just sits in the queue.
# grep matches the API's compact JSON ({"workers":{"ok":true,...}); no jq or Python needed on the host.
# --max-time on every curl: a process that accepts the connection and never answers would otherwise block the loop
# forever (/health/ready itself caps its checks at ~2 s).
wait:
	@echo -n "waiting for API"; for i in $$(seq 1 60); do curl -sf --max-time 5 $(API)/health/live >/dev/null && break; \
		echo -n "."; sleep 2; done; curl -sf --max-time 5 $(API)/health/live >/dev/null || { echo " timeout"; exit 1; }; \
		echo " ok"
	@echo -n "waiting for workers"; for i in $$(seq 1 30); do curl -s --max-time 5 $(API)/health/ready \
		| grep -q '"workers":{"ok":true' && echo " ok" && exit 0; echo -n "."; sleep 2; done; \
		echo " timeout: see make logs S=worker-ingest / S=worker-ai"; exit 1

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
	uv run python scripts/seed_demo.py --api $(API) --key $(KEY)

test: ## Full test suite (Fake LLM provider, real Postgres/Valkey/S3 from the stack)
	uv run pytest -q

test-fast: ## Unit tests only (no services needed)
	uv run pytest -q -m "not integration"

lint: ## Ruff lint
	uv run ruff check app tests scripts evals

fmt: ## Ruff format + autofix
	uv run ruff format app tests scripts evals && uv run ruff check --fix app tests scripts evals

# The same three jobs as .github/workflows/ci.yml, one after the other; stops at the first failure. The tests need the
# stack's Postgres/Valkey/S3 running (make up), as `make test` does.
ci: ## What CI runs: lint, the full test suite, frontend lint + build + tests
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory test
	cd frontend && pnpm install --frozen-lockfile && pnpm lint && pnpm build && pnpm test

smoke: ## End-to-end smoke against the running stack
	uv run python scripts/smoke.py --api $(API) --key $(KEY)

smoke-llm: ## Verify every configured model/effort pair + embeddings + TTFT (real OpenAI key)
	uv run python scripts/smoke_llm.py

eval: ## RAG evaluation over the golden set (eval user)
	uv run python evals/run_eval.py --api $(API) --key vault-eval-key

eval-demo: ## Only demo-tagged cases, each repeated 3x (must pass 3/3)
	uv run python evals/run_eval.py --api $(API) --key vault-eval-key --tag demo --repeat 3

eval-retrieval: ## Retrieval-only metrics (hit@k, MRR per mode), no LLM answers
	uv run python evals/run_eval.py --api $(API) --key vault-eval-key --retrieval-only

freeze-starters: ## Freeze the demo docs' suggested questions into evals/golden_starters.yaml
	uv run python scripts/freeze_starters.py --api $(API) --key $(KEY)

# Dry run by default: prints one line per document/session it would delete or keep, then the exact apply command.
# APPLY=1 needs the delete counts and the plan id the dry run printed (EXPECT_DOCS=, EXPECT_SESSIONS=, EXPECT_PLAN=)
# and refuses when anything the plan deletes changed since. BACKUPS=1 (with APPLY=1) replaces failing PTO / stipend /
# proposals Backup sessions and asks the missing ones (one answer call each). NO_USER_DOCS=1 states that
# demo_docs/user/ holds no real document here.
pre-demo: ## Pre-snapshot cleanup, dry run (APPLY=1 EXPECT_DOCS=n EXPECT_SESSIONS=m EXPECT_PLAN=id executes it; BACKUPS=1)
	uv run python scripts/pre_demo.py --api $(API) --key $(KEY) $(if $(filter 1,$(APPLY)),--apply) \
		$(if $(EXPECT_DOCS),--expect-docs $(EXPECT_DOCS)) $(if $(EXPECT_SESSIONS),--expect-sessions $(EXPECT_SESSIONS)) \
		$(if $(EXPECT_PLAN),--expect-plan $(EXPECT_PLAN)) \
		$(if $(filter 1,$(BACKUPS)),--create-backups) $(if $(filter 1,$(NO_USER_DOCS)),--no-user-docs)

snapshot-demo: ## Save the demo DB + S3 objects to demo_snapshot/ (refuses unless pre-demo --check passes and every document's object is in S3)
	API=$(API) bash scripts/snapshot_demo.sh

restore-demo: ## Restore the demo DB + S3 objects from demo_snapshot/ (overwrites current data)
	bash scripts/restore_demo.sh

doctor: ## Pre-demo checks: health (FAIL on a rejected AI key/model), workers, API key, seeded docs + insights, spend, retrieval, one live embeddings call (fractions of a cent)
	uv run python scripts/doctor.py --api $(API) --key $(KEY)

workers: ## Which workers consume which queues
	$(COMPOSE) exec worker-ingest celery -A app.worker.celery_app inspect active_queues

web-dev: ## React dev server on :5173 (proxies /api to API=, default localhost:8000)
	cd frontend && VAULT_API=$(API) pnpm dev

web-build:
	cd frontend && pnpm build

psql: ## psql into the project database
	$(COMPOSE) exec postgres psql -U vault -d vault

redis-cli:
	$(COMPOSE) exec valkey valkey-cli
