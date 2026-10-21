# Vault AI: AI-Powered Document Vault (case-study build plan)

> The original plan from 2026-10-13. Where it differs, the code, README and docs/DECISIONS.md are current.

## Context
**The case study:** build "ChatGPT for your documents".
- Upload PDF, DOCX, TXT or MD files.
- Process them asynchronously: extract → chunk → embed → AI insights.
- Chat with them using RAG, with page-level citations and multi-turn context.
- Expose monitoring/metrics APIs.

**Grading** is three equal parts: AI-First Development, Product Thinking, Technical Implementation.

**Constraints:**
- Built in **one day**, with Claude Code writing most of the code.
- Demoed live to a panel that will cross-question.
- The candidate knows FastAPI/async Python and Postgres/SQLAlchemy well, but is **new to Celery/Redis and RAG/embeddings**. Every component must be explainable, and teaching is part of the deliverable.

**Where the design came from:** a 9-agent design panel.
- Two researchers verified versions, models and prices as of 2026-10-13.
- Three architects each proposed a design: MVP-first, AI-first, and production-first.
- A judge scored and merged them.
- Two adversarial "interviewer" agents cross-examined the result, which produced 31 fixes.
- A separate agent verified the Render facts.
- Raw outputs: docs/ai-usage/design-review/.

## Locked decisions
| Area | Decision | Why (panel line) |
|---|---|---|
| LLM | OpenAI only, behind an `LLMProvider` Protocol, plus a `FakeProvider` for offline tests | One key for chat + embeddings; swapping providers means touching one file |
| Models (env-configurable) | `ANSWER_MODEL=gpt-6.1-sol` (effort `low`), `FAST_MODEL=gpt-6-luna` (effort `none`/`low`), `EMBED_MODEL=text-embedding-3-small` (1536 dimensions) | Current family per research (developers.openai.com, 2026-10-13). The M0 smoke test verifies they work with the configured key. If not, fall back to `gpt-5-mini` etc. by changing env vars only |
| RAG | Thin custom pipeline. `langchain-text-splitters` is used only for splitting | Retrieval, prompting and citations are the product, so I own them line by line |
| Queue | Celery 5.6 + Redis-compatible broker; queues `ingest` and `ai` | The industry standard the panel expects; acks/retries/routing are explainable |
| DB | Postgres 17 + pgvector (HNSW index) + full-text search (GIN). psycopg3: async in the API, sync with NullPool in the worker. Alembic migrations | One system of record. Transactional deletes, no dual writes. PG17 matches Render |
| Frontend | Vite + React + TS + Tailwind, **built into the API image and served by FastAPI** (one origin: no CORS, no build-time API URL) | Visual streaming chat with citations; still one deployable unit |
| State | **All new**: project-owned containers and named volumes. Postgres :5433, Valkey :6380, RustFS (S3) :9200, API :8000. Homebrew PG/Redis are never touched | User requirement |
| Storage | S3 API everywhere: RustFS locally (MinIO's free images were withdrawn), Cloudflare R2 (or S3) in the cloud | The API and worker never share a disk, locally or in the cloud, so the same code path works in both |
| Auth | `X-API-Key` per user (sha256-hashed), every query owner-scoped, 404 on another owner's resources. Seeded users `demo`, `other`, `eval` | Real multi-tenancy story, cheap to build |
| Cloud | Render Blueprint (`render.yaml`): web service (API+SPA), worker, Postgres 17, Key Value (`noeviction`), R2. About $20/month; a free fallback is documented | User choice; facts verified |
| Repo | `/Users/puru/personal-repos/vault-ai`, GitHub repo `utkarshgupta901/vault-ai` (private during the build, public for review), push at each milestone. Work packages merge locally from worktree branches, **no GitHub PRs** | User choice |
| Demo docs | Synthetic "Larkspur Analytics" corpus with planted facts, plus **1 real document from the user**, plus a golden Q&A set | Answers are known in advance, so the demo is predictable and measurable |
| Teaching | Checkpoint per module, plus a study page that grows, plus a deep session and the full study page at the end | User choice |

## Architecture
```
React SPA ─┐                         ┌─► Postgres17+pgvector (system of record: docs, pages, chunks+vectors+tsvector,
Swagger ───┼─► FastAPI (async) ──────┤                         insights, chats, processing_runs, ai_usage, request_logs)
curl ──────┘   │  SSE (chat, status) ├─► Valkey db0 = Celery broker (AOF, noeviction) · db1 = cache + rate limits
               │                     ├─► RustFS/S3 (original files: uploads/{owner}/{doc_id}.{ext})
               └─► OpenAI (condense, query embed, streamed answer, follow-ups, summaries)
Valkey ─► worker-ingest (extract→chunk→embed) ─► enqueue ─► worker-ai (insights)   [both: sync SQL, S3, OpenAI]
```

**Flow (a): upload**
1. **Checks before the body is read:** key and size.
2. **Validate:** magic-byte sniff, empty-file check, sha256 dedup per owner.
3. **Store:** put the file in S3, insert `documents(status=queued, current_run_id)`, **commit, then enqueue**, and return 202 + `Location`.
4. **Worker:** claims the run (fenced by `run_id`), then extract → chunk → embed. The document becomes `ready` and the insights task is enqueued.
5. **UI:** follows `GET /documents/{id}/events` (SSE).

**Flow (b): ask**
1. Resolve the session's scope.
2. Condense the question, only when there is history.
3. Embed the query (cached in Redis).
4. **Hybrid retrieval:** pgvector top-40 + OR-tsquery top-40 (identifiers ranked first) → RRF → per-document cap → top 8.
5. **Gate:** if similarity is low and no identifier matched, send a fixed refusal and spend nothing on the model.
6. Stream the `answer.v2` prompt with `[S#]` citations.
7. Run deterministic checks (citation IDs valid, numbers/codes grounded) and assign confidence.
8. Persist the message (no DB transaction is held during streaming).
9. Generate follow-ups.

**Flow (c): multi-turn.** The last 3 turns (ordered by `seq`) are condensed into a standalone query. Retrieval uses the standalone query. The UI shows "Interpreted as …".

## Data model (12 tables; full DDL in docs/ai-usage/design-review/03-final-design-after-cross-exam.md §3)
- **`users`**: key hash, daily cost limit, rate limit.
- **`documents`**: metadata, status/stage/progress, `current_run_id`, error code/retryable, category/tags, `document_date`, `UNIQUE(owner_id, sha256)`.
- **`document_pages`**: page text and label, used for citation click-through.
- **`chunks`**: `owner_id`, page range, header, content, `embed_hash`, `vector(1536)` with HNSW, generated `tsv` with GIN.
- **`document_insights`**: structured insights plus prompt version. **`document_summaries`**: custom summaries, cached by key.
- **`chat_sessions`**, **`chat_session_documents`** (scope), **`chat_messages`**:
  - Ordering, status and answer data: `seq` identity, status, `standalone_query`, answer_type, confidence.
  - JSON columns: `retrieval`, `citations` (snapshot), `followups`.
  - Cost and timing: tokens, cost, ttft/latency.
- **`processing_runs`**: one row per attempt, with stage timings and errors.
- **`ai_usage`**: every LLM call, with tokens and cost (no FKs, so it survives deletes).
- **`request_logs`**: method, route, status, ttfb_ms/total_ms.

How this maps to the case study's "Data Storage" bullets:

| Requirement | Tables |
|---|---|
| Metadata & status | `documents` |
| Summaries & insights | `document_insights`, `document_summaries` |
| Conversations & history | `chat_sessions` |
| Messages with context & citations | `chat_messages` |
| Processing metrics & history | `processing_runs` |
| System performance | `request_logs`, `ai_usage` |

## API (`/api/v1`, error envelope `{error:{code,message,details,request_id}}`, cursor pagination)
- **Documents:**
  - `POST /documents` (multipart), `GET /documents` (status/category/tag/q filters).
  - `GET|DELETE /documents/{id}`.
  - `GET /documents/{id}/events` (SSE), `.../pages/{n}`, `.../file`.
  - `POST /documents/{id}/reprocess`.
- **AI:** `GET /documents/{id}/insights`, `POST /documents/{id}/summaries` (`{length, tone, focus}`), `GET /search` (retrieval debugging, no LLM call).
- **Chat:**
  - `POST /chat/sessions` (`document_ids` or `all_documents`; returns `starter_questions`), `GET /chat/sessions[/{id}]`.
  - `GET /chat/sessions/{id}/messages`.
  - `POST /chat/sessions/{id}/messages` (JSON) and `POST .../messages/stream` (SSE events: `meta`, `token`, `citations`, `followups`, `done`, `error`).
- **Metrics:** `GET /metrics/documents`, `/metrics/processing`, `/metrics/ai-usage` (cost by operation/model/day), `/metrics/chat`, `/metrics/system`.
- **Health:** `GET /health/live`, `GET /health/ready`. DB, pgvector and Alembic head are hard checks (503). Redis, workers, object storage and the OpenAI key report `degraded` (still 200).

## Reliability core (what to build vs only explain)
**Build:**
- Celery settings: `acks_late`, `reject_on_worker_lost`, `prefetch_multiplier=1`, `visibility_timeout=900` (longer than the 600 s hard limit).
- **`run_id` fencing.** Every write checks `current_run_id`, so stale or duplicate tasks are no-ops.
- **Idempotent, resumable stages.** Extract/chunk are delete-then-insert; embedding only touches `WHERE embedding IS NULL`.
- **Retries:** transient errors (OpenAI 429/5xx/timeout) retry with `Retry-After` or jittered backoff, up to 6 times. Permanent errors (encrypted, corrupt or scanned PDF, too many pages) fail fast with a code.
- **Commit, then enqueue.** A failed publish becomes `QUEUE_UNAVAILABLE` (retryable). Documents in `queued`/`processing` with no update for 15 min get a `stale` flag and can be reprocessed.

**P1 hardening:** advisory lock and crash counter (stops a poison file from looping).

**Explain only** (exact framings are in panel Part 2): transactional outbox, stale-run sweeper, RLS, LISTEN/NOTIFY, Batch API, presigned uploads.

## Scope tiers (build top-down; never cut what the demo uses)
**P0, must work:**
- Foundation (Compose: pg17, Valkey, RustFS + bucket init, migrate one-shot, api, worker-ingest, worker-ai; settings; 12 ORM models + Alembic 0001; schemas; error registry; auth; LLMProvider OpenAI+Fake; pricing/usage ledger; S3 storage).
- Documents API (sniffing, dedup, S3, CRUD, reprocess, status SSE).
- Ingestion (pypdf with page labels, python-docx sections, TXT/MD; chunker 400/60 page-aware with breadcrumb header; batched embeddings with per-owner hash reuse; `processing_runs`).
- Insights (`responses.parse` → `DocumentInsights`; quotes verified against the text; pages checked).
- RAG chat (query terms, hybrid SQL, gate, condense, `answer.v1`/`v2`, both message endpoints, citation and grounding checks, confidence, persist-then-follow-ups, per-step timeouts plus a pre-first-token fallback to the fast model).
- Metrics: documents, processing, ai-usage. Health live/ready.
- React UI (Documents with drag-and-drop and live status chips; Document detail with insights, summary options and page viewer; Chat with scope picker, streaming, citation chips → page popover, "Interpreted as", follow-up chips, retrieval debug drawer; Metrics cards and charts).
- Corpus generator + seed + golden set (43 cases) + `make eval` (hit@k, MRR, refusal precision/recall, citation validity, demo cases 3/3).
- Tests (Fake provider against real Postgres; cross-tenant route walker; SSE contract).
- Docs + `render.yaml` + multi-stage Dockerfile.

**P1, in order (the cut line is after 6):**
1. Follow-up suggestions.
2. Summary customization.
3. Rate limiting (Redis INCR+EXPIRE in MULTI) + soft daily cost quota + queue backpressure.
4. `/metrics/chat` + `/search`.
5. Query-embedding cache.
6. `client_message_id` idempotency.
7. Request logs + `/metrics/system`.
8. LLM judge + hand audit.
9. Advisory lock + crash counter.
10. Batch upload.
11. Document version lineage (`previous_version_id`).
12. Compare endpoint.

**P2 (only if all green):** reranker, Flower, Anthropic adapter, LISTEN/NOTIFY, contextual LLM headers, actual Render deploy (needs the user's Render and R2 accounts; ask first).

## Repo layout (owners = work packages)
```
vault-ai/
  CLAUDE.md README.md AI_USAGE.md render.yaml Dockerfile compose.yaml compose.override.yaml Makefile pyproject.toml uv.lock .env.example alembic.ini
  docs/{DECISIONS,PROMPTS,EVAL,DEPLOY_RENDER,BUILD_PLAN}.md  docs/ai-usage/{log.md,design-review/}
  migrations/                 app/main.py app/config.py                                  [WP0]
  app/core/{db,redis,errors,logging,auth,pagination,textnorm}.py app/models/* app/schemas/* [WP0]
  app/ai/{provider,openai_provider,fake_provider,pricing,usage}.py app/ai/prompts/common.py [WP0]
  app/services/{state,storage}.py app/worker/{celery_app,dispatch}.py                      [WP0]
  app/api/documents.py app/services/documents.py app/ingestion/sniff.py app/core/upload_guard.py   [WP1]
  app/ingestion/{extract,chunking,pipeline}.py app/worker/{tasks,attempt}.py app/ai/{insights,summaries}.py [WP2]
  app/rag/{query_terms,retrieval,prompting,citations,condense,followups,confidence}.py app/services/chat.py app/api/{chat,search}.py [WP3]
  app/core/{middleware,ratelimit}.py app/services/metrics.py app/api/{metrics,health}.py   [WP4]
  scripts/{make_demo_docs,seed_demo,doctor,smoke_llm}.py scripts/{snapshot_demo,restore_demo}.sh evals/  [WP5]
  frontend/ (Vite React TS)                                                                [WP6]
  tests/{documents,ingestion,chat,observability,security}/                                 [per WP]
  prep/  (gitignored: interview Q&A bank, demo checklist; never submitted)
```

## Build day: phases, workflows, checkpoints (~11 h incl. teaching)
Each wave runs as a **Workflow**. Every agent works in its own git worktree on its own branch, owns only its files, and builds against contracts frozen in WP0. Each wave ends with an adversarial review stage, then the lead merges into `main`, runs tests, commits and pushes.

| Time | Work | Milestone | Teach |
|---|---|---|---|
| 0:00–0:30 | Start OrbStack. `git init`, private GitHub repo, CLAUDE.md (rules: openai imported only in the provider; every service takes `owner_id`; no DB session across an LLM await; no blocking calls in async routes; escape all prompt text; git-safety rules). Copy the panel docs in. The user gets the OpenAI key (hard cap of about $30) and drops their real document into `demo_docs/` | – | Tour of the plan |
| 0:30–2:00 | **WP0** foundation + walking skeleton on Fake (upload TXT → ready → chat streams `[S1]` → metrics non-zero). `smoke_llm.py` checks every (model, effort) pair and measures TTFT over 20 runs | **M0** | **CP1:** Docker/Compose, data model and indexes (HNSW vs GIN), why S3 (RustFS locally), the Celery mental model. **Publish study page v1** |
| 2:00–4:00 | **Wave 1** (parallel): WP1 documents API · WP2 ingestion · WP3 RAG on Fake · Lead: corpus generator + golden set (including 3 Q&As on the user's document) | **M1:** corpus ingested with real embeddings | **CP2:** async pipeline: commit-then-enqueue, acks_late, run_id fencing, retries; live `kill -9` of a worker mid-embed |
| 4:00–5:45 | **Wave 2** (parallel): WP6 React UI · WP2b insights + summaries · WP3 on real data + follow-ups · WP4 metrics/health · Lead: `run_eval.py` | **M2:** real chat end to end in the UI | **CP3, the RAG deep dive:** embeddings and cosine similarity, chunking, hybrid search + RRF arithmetic, the gate, prompt design, citation checks. Psql `EXPLAIN` |
| 5:45–6:45 | Eval baseline v1 vs v2. Calibrate the gate τ, `TAU_HIGH`, `LEXICAL_WEIGHT` and the per-document cap. Fix failures | Eval report | **CP4:** hit@k, MRR, refusal precision/recall, why 3/3 |
| 6:45–7:45 | P1 list down to the cut line. **Review workflow** (correctness + security lenses, adversarial verify), `/code-review`, `/security-review`, fault-injection check | **M3 freeze** | **CP5:** cost maths from `ai_usage`, caching, rate limiting, scaling to 1000s and 1M documents |
| 7:45–8:30 | WP7 docs: README (setup, mermaid, API, assumptions, scale table, cuts), AI_USAGE (the real log), PROMPTS (with the measured v1→v2 delta), EVAL, DECISIONS, DEPLOY_RENDER. Prod-image test: single image, Render-like env | Docs done | – |
| 8:30–9:15 | Clean clone → `make reset seed` → `make eval-demo` → rehearsal 1 → `make pre-demo` (review the plan, then apply it) → `make snapshot-demo` (only after the cleanup) → backup screen recording → tag `demo-v1` → push | **M4** | – |
| 9:15–11:00 | **Deep session** (1.5–2 h, the whole system end to end) + **full study page** (diagrams, concept cards, the 45-question flashcard bank, demo script) + mock panel + rehearsal 2 | Ready | **CP6** |

**Checkpoint format:** a 5–10 minute walkthrough of what was built and why, the cross-questions it invites, and 3 quiz questions. The candidate explains the topic back. The study-page artifact is republished at each checkpoint.

## Cloud readiness (Render)
- **Dockerfile:** multi-stage. A node stage builds the SPA; `python:3.13-slim` + uv puts the venv at `/opt/venv`. One image serves as API or worker depending on the command.
- **Configuration:** env-only. `DATABASE_URL` is normalized from `postgres://` to `postgresql+psycopg://` in both settings and alembic `env.py`. Also `BROKER_URL`, `CACHE_URL`, `S3_*`, `OPENAI_API_KEY`.
- **`render.yaml`** (from the verified research):
  - **Web:** Docker; the image CMD runs `sh -c "uvicorn … --port ${PORT:-8000}"`, `preDeployCommand: python -m app.cli bootstrap` (Alembic upgrade + bucket + seed users), health check `/health/live`.
  - **Worker:** same image, `celery … -Q ingest,ai -c 2 --max-memory-per-child`.
  - **Postgres:** `postgresMajorVersion: "17"`.
  - **Key Value:** `maxmemoryPolicy: noeviction`, `ipAllowList: []`.
  - **Secrets:** `sync: false`.
- **R2 settings:** boto3 with `region_name="auto"` and `request_checksum_calculation="when_required"`. RustFS uses path-style addressing.
- **Free fallback:** an all-in-one start script (uvicorn + celery solo in one container, migrations run at start). Documented in `docs/DEPLOY_RENDER.md`.

## Panel artifacts (where the detail lives)
- `docs/ai-usage/design-review/`: synthesis + scorecard and the final design (Part 1, Part 2). This is evidence of AI-driven design review, so it belongs in the repo.
- `prep/` (gitignored): the 45-question interview bank, learning path, demo-day checklist and 6-minute script (Parts 3–5), plus the RAG research notes.
- The implementation follows the panel's §3 DDL, §4 SSE contract, §5 Celery config and attempt rules, §6 prompts/SQL/chunking/citation rules and the WP0 contracts. Where the panel's text conflicts with this plan, this plan wins: React instead of static HTML, S3 (RustFS) instead of a local volume, pg17 instead of pg18, Valkey to match Render.

## Verification
1. **`make smoke`:** health ready; upload → ready; duplicate → 200; no key + large Content-Length → 401 with no temp file; stream event order `meta → token* → citations → followups → done`; messages returned in `seq` order; metrics non-zero.
2. **`make smoke-llm`:** every model/effort pair responds, embeddings have 1536 dimensions, `usage` is present, TTFT p50/p95 is printed.
3. **`make test`:** pytest (Fake provider, real Postgres). Unit: chunker, query terms, RRF, citation parser, extractors. API: upload edge cases, the cross-tenant route walker, the SSE contract.
4. **`make eval`:** report in `evals/reports/`. Demo cases pass 3/3. hit@8 / MRR per retrieval mode. Refusal precision/recall. Zero invalid citations. The README numbers are copied from this report.
5. **Fault checks:** `kill -9` a worker child mid-embed and confirm it resumes without duplicate chunks. Stop Redis and confirm readiness reports `degraded` and uploads return 503 cleanly. A scanned PDF gets `NO_EXTRACTABLE_TEXT`.
6. **Clean clone:** `git clone` → `cp .env.example .env` → `make up seed` works on the first try. The prod image runs alone against compose services, simulating Render.
7. **UI walkthrough:** upload with live chips, insights, streaming chat with page chips, follow-ups, metrics. Rehearse the 6-minute demo twice.

## Needed from the user
- **OpenAI API key:** get one now, ideally with a hard spend cap. Put it in `.env`; never paste it in chat.
- **One real document:** PDF or DOCX, a few pages, nothing sensitive (it is sent to OpenAI).
- **Permissions during the build:** start OrbStack; `gh repo create --private`; docker compose; installing packages via uv/pnpm.
- **Optional:** Render and Cloudflare R2 accounts, only if we do the real deploy (P2).
