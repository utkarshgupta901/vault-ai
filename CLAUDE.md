# Vault AI — rules for AI coding agents working in this repo

## What this is
FastAPI + Celery + Postgres/pgvector + Valkey + S3 (RustFS locally) document-chat (RAG) backend with a React dashboard.
Design: docs/BUILD_PLAN.md (authoritative) and docs/ai-usage/design-review/03-final-design-after-cross-exam.md (detail:
§3 DDL, §4 SSE contract, §5 pipeline rules, §6 prompts/SQL/citations). Where they conflict, BUILD_PLAN.md wins.

## Local stack (all project-owned; never touch other Postgres/Redis on this machine)
- Postgres 17 + pgvector: localhost:5433 (vault/vault/vault) · Valkey: localhost:6380 (db0 broker, db1 cache)
- S3 (RustFS): localhost:9200 (vaultlocal/vaultlocalsecret, bucket vault-uploads) · API: localhost:8000 · Vite: 5173
- `make up` (dev, reload) · `make demo` (built image) · `make test` · `make smoke` · `make reset` (wipes volumes)
- API keys (seeded): demo=`vault-demo-key`, other=`vault-other-key`, eval=`vault-eval-key`

## Hard rules
1. `openai` is imported ONLY in `app/ai/openai_provider.py`. Everything else uses `app.ai.get_provider()` (LLMProvider).
2. Every service/query function takes `owner_id` and filters by it. Another owner's resource => 404, never 403.
3. Never hold a DB session/transaction across an LLM await or while streaming. Use short `async_session()` blocks.
4. No blocking calls in async routes: boto3, Celery publish/inspect, sync Redis go through `anyio.to_thread.run_sync`.
5. All document/user text placed in a prompt goes through `app/ai/prompts/common.escape()` inside tagged blocks.
6. Status changes only via `app/services/state.py` helpers (fenced by `current_run_id`, always set `updated_at`).
7. Every model call writes an `ai_usage` row via `app/ai/usage.usage_row()` in the same transaction as its result.
8. Errors: raise `AppError(ErrorCode.X, status, message)`; pipeline: `PermanentError(code)` / `ProviderError`.
9. Status columns are TEXT + CHECK (see migration). New columns need a new Alembic migration (hand-written SQL).
10. Logging: structlog only (`structlog.get_logger(__name__)`), no print in app code.
11. Tests use FakeProvider (LLM_PROVIDER=fake) against the real compose Postgres (`vault_test` DB). No network to OpenAI.
12. Each parallel agent edits ONLY the files it owns (see BUILD_PLAN.md repo layout). Contract changes go through the lead.
13. Git safety: never run checkout/restore/reset/stash/clean on a dirty tree; use `git show` or a throwaway worktree.
14. Keep code explainable: small functions, docstrings that say WHY, no framework magic. Match surrounding style.
