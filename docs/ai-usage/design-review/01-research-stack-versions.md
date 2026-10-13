# Tech stack verification as of 2026-10-13

Every version below was read from PyPI, npm, Docker Hub or official docs on 2026-10-13. Anything I could not confirm is marked **UNVERIFIED**.

## 1. Stack table

| Item | Latest stable (date) | How to use it today | Gotchas | Source |
|---|---|---|---|---|
| **FastAPI** | 0.143.0 (2026-10-08). Needs Python >=3.10 | `Annotated[...]` params. Declare return types, because since 0.130 FastAPI serializes Pydantic return types in Rust (about 2x faster). Install plain `fastapi` + `uvicorn[standard]` + `python-multipart`. `fastapi[standard]` now also pulls OTel SDK/exporter, fastapi-cli and httpx | **0.132: JSON requests without a valid `Content-Type` are rejected by default.** A `curl -d '{...}'` demo with no `-H 'Content-Type: application/json'` fails; the switch is `strict_content_type=False`. 0.128 dropped `pydantic.v1`. 0.129 dropped Python 3.9. 0.131 deprecated `ORJSONResponse`/`UJSONResponse`. 0.137: `router.routes` is now a tree, not a plain list. 0.142 added native OpenTelemetry; 0.143 made OTEL_* auto-configure opt-in (`FASTAPI_OTEL_AUTO_CONFIGURE=true`) | [PyPI](https://pypi.org/project/fastapi/#history), [0.132](https://github.com/fastapi/fastapi/releases/tag/0.132.0), [0.128](https://github.com/fastapi/fastapi/releases/tag/0.128.0), [0.129](https://github.com/fastapi/fastapi/releases/tag/0.129.0), [0.130](https://github.com/fastapi/fastapi/releases/tag/0.130.0), [0.131](https://github.com/fastapi/fastapi/releases/tag/0.131.0), [0.137](https://github.com/fastapi/fastapi/releases/tag/0.137.0), [OTel](https://fastapi.tiangolo.com/advanced/opentelemetry/) |
| **SSE: native vs sse-starlette** | Native since FastAPI **0.135.0**. sse-starlette 3.5.0 (2026-09-28) is still maintained but no longer needed | `from fastapi.sse import EventSourceResponse, ServerSentEvent`. Use `@app.post("/chats/{id}/messages/stream", response_class=EventSourceResponse)` with an `async def` generator that does `yield ServerSentEvent(data=..., event="token", id=...)`. Use `raw_data=` for non-JSON text. Works on any HTTP method. A 15 s keep-alive ping, `Cache-Control: no-cache` and `X-Accel-Buffering: no` are automatic | Browser `EventSource` only does GET, so a POST stream needs a `fetch()` + `ReadableStream` reader on the frontend. `yield` dependencies default to `scope="request"` (exit after the response is sent, behaviour restored in 0.118), so a `Depends(get_db)` session stays open for the whole LLM stream. Open short sessions inside the generator: load context, close, stream, then persist | [SSE docs](https://fastapi.tiangolo.com/tutorial/server-sent-events/), [0.135](https://github.com/fastapi/fastapi/releases/tag/0.135.0), [deps with yield](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/), [sse-starlette](https://pypi.org/project/sse-starlette/#history) |
| **Pydantic v2** | 2.14.1 (2026-10-11) | `BaseModel`, `ConfigDict(from_attributes=True)` for ORM to schema, `Field(...)`, `model_validate` / `model_dump` | Released 2 days ago; pin `~=2.14`. These same models also serve as LLM output schemas (see OpenAI/Claude rows) | [PyPI JSON](https://pypi.org/pypi/pydantic/json) |
| **pydantic-settings** | 2.15.0 (2026-08-07). Needs Python >=3.10 | `class Settings(BaseSettings): model_config = SettingsConfigDict(env_file=".env", extra="ignore")`. Use `SecretStr` for API keys | Separate package from pydantic | [PyPI JSON](https://pypi.org/pypi/pydantic-settings/json) |
| **Uvicorn / Starlette** | uvicorn 0.54.0 (2026-09-25). Starlette 1.8.0 (2026-10-13) | `uvicorn app.main:app --reload --port 8000` | Starlette `TestClient` now prefers **httpx2** and gives a deprecation warning if it falls back to httpx | [uvicorn](https://pypi.org/project/uvicorn/#history), [starlette](https://pypi.org/project/starlette/#history), [testclient.py](https://raw.githubusercontent.com/encode/starlette/main/starlette/testclient.py) |
| **SQLAlchemy 2.x async + psycopg 3** | **2.1.4** (2026-10-07; 2.1.0 GA was 2026-09-24, needs Python >=3.11). **2.0.54** (2026-09-15) is the maintenance line | **One driver serves both sides.** API: `create_async_engine("postgresql+psycopg://u:p@localhost:5433/vault")`, which auto-selects the async variant (explicit form: `postgresql+psycopg_async://`). Celery worker and Alembic: `create_engine("postgresql+psycopg://...")`. Use `async_sessionmaker(engine, expire_on_commit=False)` and `selectinload()` | **Apple Silicon:** the 2.0.x wheel auto-installs greenlet only for `aarch64/x86_64/amd64/...`, not macOS `arm64`. 2.1 never auto-installs it. Always install `sqlalchemy[asyncio]` or you get MissingGreenlet / "greenlet required" in a local venv. Lazy loads in async also raise MissingGreenlet. 2.1 also changes: default PG driver is psycopg 3, and autoflush is unconditional. Recommend pinning 2.0.54 for a one-day build (2.1 is 19 days old) | [sqlalchemy.org](https://www.sqlalchemy.org/), [2.1 migration](https://docs.sqlalchemy.org/en/21/changelog/migration_21.html), [PG dialect](https://docs.sqlalchemy.org/en/21/dialects/postgresql.html), [asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html), [2.0.54 metadata](https://pypi.org/pypi/SQLAlchemy/2.0.54/json) |
| **psycopg** | 3.3.6 (2026-09-18). Needs Python >=3.10 | `psycopg[binary,pool]` | Binary wheels mean no libpq build. Install only psycopg 3; no asyncpg or psycopg2 needed | [PyPI](https://pypi.org/project/psycopg/#history) |
| **Alembic + pgvector** | Alembic 1.20.0 (2026-09-11). Needs SQLAlchemy >=2.0 and has 2.1 fixes | Run migrations with the **sync** psycopg URL (simplest; avoids the async template). First migration: `op.execute("CREATE EXTENSION IF NOT EXISTS vector")` | (1) Autogenerate never emits `CREATE EXTENSION`. (2) Autogenerate renders the pgvector type by module path. Add `import pgvector.sqlalchemy` to `script.py.mako`, otherwise migrations hit a NameError (reported by third parties; check on first autogenerate). (3) HNSW indexes with `postgresql_ops` render wrong and cause a perpetual diff ([alembic#1603](https://github.com/sqlalchemy/alembic/issues/1603)). Write the index with `op.execute(...)` and exclude it from autogenerate via `include_object`. (4) `CREATE INDEX CONCURRENTLY` cannot run inside Alembic's transaction; use plain `CREATE INDEX` for the demo | [changelog](https://alembic.sqlalchemy.org/en/latest/changelog.html), [pgvector-python#118](https://github.com/pgvector/pgvector-python/issues/118), [Tiger Data note](https://www.tigerdata.com/blog/pgai-vectorizer-meets-python-integrating-sqlalchemy-and-alembic) |
| **pgvector-python** | 0.5.1 (2026-10-09) | `from pgvector.sqlalchemy import VECTOR` (`Vector` is an alias; `HALFVEC` also exists). `embedding: Mapped[list[float]] = mapped_column(VECTOR(1536))`. Query: `select(Chunk).order_by(Chunk.embedding.cosine_distance(q)).limit(k)`. Index: `Index("ix_chunk_emb", Chunk.embedding, postgresql_using="hnsw", postgresql_with={"m":16,"ef_construction":64}, postgresql_ops={"embedding":"vector_cosine_ops"})` | `register_vector` / `register_vector_async` is only needed for `ARRAY(VECTOR)` columns, not normal ORM use. Similarity = `1 - cosine_distance` | [README](https://github.com/pgvector/pgvector-python), [PyPI](https://pypi.org/project/pgvector/) |
| **pgvector extension + Docker image** | Extension **0.8.7** (2026-10-01). Image **`pgvector/pgvector:0.8.7-pg18-trixie`** (also `pg18`, `pg17-trixie`; all updated 2026-10-01) | Raw DDL: `CREATE INDEX ix_chunk_emb ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);`. Per query: `BEGIN; SET LOCAL hnsw.ef_search = 100; SELECT ... ORDER BY embedding <=> :q LIMIT k;` | HNSW limit is **2,000 dims for `vector`** (4,000 for `halfvec`), so text-embedding-3-large (3072) cannot be indexed as `vector`. `ef_search` defaults to 40. Filtered ANN (`WHERE document_id = ANY(...)`) can return fewer than k rows; fix with `SET LOCAL hnsw.iterative_scan = relaxed_order`. The index is only used when ORDER BY uses `<=>` plus LIMIT. **PG18 image moved PGDATA.** Mount the volume at `/var/lib/postgresql`, not `/var/lib/postgresql/data` (the old path fails at start). Map host port 5433:5432 | [pgvector](https://github.com/pgvector/pgvector), [CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md), [tags](https://hub.docker.com/r/pgvector/pgvector/tags), [PG18 volume issue](https://github.com/docker-library/postgres/issues/1370) |
| **Celery + Redis broker** | **5.6.3** (2026-03-26). 5.7.0b1 (2026-10-06) is pre-release. 5.6 officially supports CPython 3.9–3.13, so **use Python 3.13, not 3.14** | `celery[redis]`. Config: `task_acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1` (default 4), `broker_transport_options={"visibility_timeout": 3600}`, `broker_connection_retry_on_startup=True`, `task_ignore_result=True`. Keep status in Postgres. Tasks are sync: use the sync engine and the sync OpenAI client in the worker. URL with password: `redis://:pw@host:6380/0` | **Released kombu 5.6.2 caps `redis<6.5`.** Pinning redis-py 8 conflicts; kombu main (5.7) allows `<9`. Redis visibility timeout defaults to 1 h; a task not acked in time is redelivered, possibly in a loop, so tasks must be **idempotent**. Redis `maxmemory-policy` must not evict broker keys. **macOS host:** prefork `fork()` can crash with `objc[...] +initialize may have been in progress in another thread when fork() was called`. Fixes: run the worker in Docker (Linux), or `--pool=threads`/`--pool=solo`, or start with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES NO_PROXY=*`. HUP restart is disabled on macOS. Load torch/sentence-transformers models after fork. Native `async def` tasks: **UNVERIFIED / not in 5.6** | [PyPI](https://pypi.org/project/celery/#history), [5.6 notes](https://docs.celeryq.dev/en/stable/history/whatsnew-5.6.html), [Redis broker](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html), [config](https://docs.celeryq.dev/en/stable/userguide/configuration.html), [pools](https://docs.celeryq.dev/en/stable/userguide/concurrency/index.html), [workers](https://docs.celeryq.dev/en/stable/userguide/workers.html), [kombu metadata](https://pypi.org/pypi/kombu/json), [kombu main](https://raw.githubusercontent.com/celery/kombu/main/requirements/extras/redis.txt), [macOS fork](https://hynek.me/til/rq-macos/) |
| **Flower** | 2.2.0 (2026-09-22; revived after 3 years). Requires `celery>=5.0.5`, tornado 6.5+ | `celery -A app.worker flower --port=5555` | Optional; nice for a live demo of tasks | [PyPI](https://pypi.org/project/flower/#history), [metadata](https://pypi.org/pypi/flower/json) |
| **arq / RQ / Dramatiq** | arq 0.28.0 (2026-04-16): **"maintenance only mode"**. RQ 2.12.0: has `SpawnWorker` using `posix_spawn` "to avoid unsafe forking on platforms such as macOS". Dramatiq 2.2.1 (2026-09-02): active | arq is asyncio-native but frozen. RQ is the simplest to explain. Dramatiq is clean but less known to panels | Celery remains the default the brief names. RQ SpawnWorker is the cleanest macOS-native option | [arq](https://github.com/python-arq/arq), [RQ](https://pypi.org/pypi/rq/json), [RQ workers](https://python-rq.org/docs/workers/), [Dramatiq](https://pypi.org/project/dramatiq/#history) |
| **redis-py** | 8.1.0 (2026-07-30), **but use 6.4.x with Celery 5.6** | `import redis.asyncio as aioredis` for API-side cache and rate limits | 8.0 switched to RESP3 on the wire and 5 s socket timeouts by default; Celery 5.6 can't use it anyway | [PyPI](https://pypi.org/project/redis/#history), [8.0 notes](https://github.com/redis/redis-py/releases) |
| **Redis server image** | `redis:8.10` (8.10.2, updated 2026-10-07) | Compose on host port 6380, no auth (local) | Whether a bare `redis:8` tag exists: **UNVERIFIED** | [tags](https://hub.docker.com/_/redis/tags) |
| **OpenAI Python SDK** | **3.29.0** (2026-10-11). Major v3 since 3.0.0 (2026-08-12 per a secondary source) | `AsyncOpenAI()` in the API, `OpenAI()` in the worker. **Responses API is recommended for new projects; Chat Completions is still supported.** Structured output: `client.responses.parse(model=..., input=..., text_format=MyModel).output_parsed`. Stream: `stream=True`, then on events with `event.type == "response.output_text.delta"` use `event.delta`. There is also a `client.responses.stream(...)` helper | **v3 replaced httpx with httpx2, uses the OS trust store and dropped certifi.** `python:3.13-slim` ships ca-certificates (fine); distroless/scratch images break, fixed with `SSL_CERT_FILE`. `respx`/`pytest-httpx` mocks no longer intercept, so mock at your own `LLMProvider` interface. Strict schemas need every field `required` and `additionalProperties:false`; express optional fields as `X \| None`. Usage tokens on `response.completed`: likely, check in a smoke test | [PyPI](https://pypi.org/project/openai/#history), [httpx2 notes](https://raw.githubusercontent.com/openai/openai-python/main/httpx2.md), [migrate to Responses](https://developers.openai.com/api/docs/guides/migrate-to-responses), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [streaming](https://developers.openai.com/api/docs/guides/streaming-responses), [helpers](https://raw.githubusercontent.com/openai/openai-python/main/helpers.md), [TLS write-up](https://ecorpit.com/openai-python-3-0-httpx2-certifi-tls-container-breakage-2026/) |
| **OpenAI chat models** | See price table in section 2. GPT-6 Astra (2026-09-03), Sol/Luna (2026-09-22), **GPT-6.1 Sol (2026-09-29)** | RAG answers: `gpt-6.1-sol`. Cheap or high-volume work (summaries, tags, follow-ups): `gpt-6-luna` with `reasoning={"effort": "none"}` or `"low"` | **Default reasoning effort is `medium` (slower first token).** Luna supports `none`; Astra does not; 6.1 Sol supports `low` and up. Prices **double above 272K input tokens**. Whether GPT-6 accepts `temperature` is **UNVERIFIED**; don't send it | [pricing](https://developers.openai.com/api/docs/pricing), [models](https://developers.openai.com/api/docs/models), [gpt-6.1-sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [reasoning](https://developers.openai.com/api/docs/guides/reasoning), [launch](https://www.vktr.com/ai-platforms/openai-launches-gpt6-sol-and-luna-cuts-api-prices-in-half/) |
| **OpenAI embeddings** | **No model newer than text-embedding-3.** `text-embedding-3-small`: 1536 dims, $0.02/1M. `-3-large`: 3072 dims, $0.13/1M. ada-002 is legacy ($0.10) | `embeddings.create(model="text-embedding-3-small", input=[...])`. `dimensions=` can shorten vectors (3-series only) | Limits: 8,192 tokens per input, ≤2,048 inputs per array, **≤300K tokens summed per request**, no empty strings. Batch by token count, not just item count | [guide](https://developers.openai.com/api/docs/guides/embeddings), [API ref](https://developers.openai.com/api/reference/resources/embeddings/methods/create) |
| **Anthropic SDK + Claude** | `anthropic` **1.13.0** (2026-10-09; 1.x also uses httpx2). Current models: `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, **`claude-haiku-5-5` (released 2026-10-07)**. All 1M context, 128K output | Stream: `async with client.messages.stream(...) as s: async for t in s.text_stream`, then `get_final_message()`. Structured output: `client.messages.parse(..., output_format=MyModel).parsed_output`; for raw schemas use `output_config={"format": ...}` (old dict `output_format` is deprecated). Native **citations** on `document` / `search_result` blocks (all active models) | **SDK 1.x removed `temperature/top_p/top_k`** (TypeError). Opus 4.7+ returns 400 on them; Haiku 5.5 docs say omit them. Opus 5.5 thinking can't be disabled; tune with `output_config.effort` (default `medium`). Haiku 5.5 default effort is `medium`; use `low` for chat latency. Assistant prefill returns 400. **Citations plus structured outputs returns 400.** The newer tokenizer gives about 30% more tokens than Haiku 4.5. Haiku 5.5 is priced 5x for prompts over 100K tokens | [models overview](https://platform.claude.com/docs/en/about-claude/models/overview), [pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview), [citations](https://platform.claude.com/docs/en/build-with-claude/citations), [PyPI](https://pypi.org/project/anthropic/#history) |
| **Claude embeddings (Voyage)** | **Confirmed: "Anthropic does not offer its own embedding model."** Recommended: Voyage AI (now MongoDB): `voyage-4-lite` $0.02, `voyage-4` $0.06, `voyage-4-large` $0.12 per 1M tokens, **200M free tokens**. 1024 dims default (256/512/2048), 32K context. `voyage-4-nano` is open-weight (Apache-2.0, 2048 dims default). Rerankers `rerank-3` $0.05, `rerank-3-lite` $0.02 | `voyageai>=0.3.7`: `vo.embed(texts, model="voyage-4", input_type="document"/"query")` | The key comes from **MongoDB Atlas → AI Model APIs**, a second vendor account and extra friction for a one-day build. Always pass `input_type` | [Claude embeddings doc](https://platform.claude.com/docs/en/build-with-claude/embeddings), [Voyage pricing](https://www.mongodb.com/docs/voyageai/management/billing/) |
| **Local sentence-transformers** | sentence-transformers 6.1.0 (2026-09-18; needs PyTorch) | `BAAI/bge-small-en-v1.5`: **384 dims**, 512 tokens, MIT; prefix queries with `"Represent this sentence for searching relevant passages: "`. `all-MiniLM-L6-v2`: **384 dims**, Apache-2.0 | **MiniLM truncates silently above 256 word pieces**, so chunks must be ≤ about 200 tokens or retrieval degrades. torch is a large download and is not fork-safe on macOS (see Celery row) | [PyPI](https://pypi.org/project/sentence-transformers/#history), [bge-small](https://huggingface.co/BAAI/bge-small-en-v1.5), [MiniLM](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) |
| **pypdf** (recommended primary) | 6.20.1 (2026-10-12). BSD-3 | `for i, p in enumerate(PdfReader(f).pages, 1): text = p.extract_text()` keeps page numbers for citations. `extraction_mode="layout"` is optional | No OCR. Scanned pages give empty or minimal text. Detect with `len(text.strip()) < ~25` on a page that has images, mark the page `needs_ocr`, and return a clear error | [PyPI](https://pypi.org/project/pypdf/#history), [extract docs](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) |
| **pdfplumber** | 0.11.10 (2026-06-15). MIT, built on pdfminer.six | `page.extract_text()`, `page.extract_tables()`, `page.chars` / `page.images` for scan detection | Slower; use only for tables | [PyPI](https://pypi.org/project/pdfplumber/#history) |
| **PyMuPDF** | 1.28.2 (2026-08-06) | Fastest and most capable | **AGPL-3.0 or commercial licence.** Avoid, or justify it explicitly | [PyPI](https://pypi.org/project/PyMuPDF/#history) |
| **pypdfium2** | 5.14.0 (2026-10-04). Apache-2.0 / BSD-3 | `page.get_textpage().get_text_bounded()`; `page.render(scale=2).to_pil()` for page thumbnails or future OCR | Permissive alternative to PyMuPDF | [PyPI](https://pypi.org/project/pypdfium2/#history) |
| **python-docx** | 1.2.0 (2025-06-16) | `Document(f).iter_inner_content()` yields Paragraph and Table in document order | DOCX has no page numbers; cite by heading or section and paragraph index instead | [PyPI](https://pypi.org/project/python-docx/#history), [source](https://python-docx.readthedocs.io/en/stable/_modules/docx/document.html) |
| **langchain-text-splitters** | 1.1.3 (2026-10-02) | `RecursiveCharacterTextSplitter.from_tiktoken_encoder(encoding_name="o200k_base", chunk_size=800, chunk_overlap=100)` | **Not dependency-free.** It requires `langchain-core>=1.4.7` (1.6.10), which pulls langsmith, httpx, tenacity, jsonpatch and pyyaml. Defensible; or write about 40 lines of your own splitter | [PyPI](https://pypi.org/project/langchain-text-splitters/#history), [deps](https://pypi.org/pypi/langchain-text-splitters/json), [core deps](https://pypi.org/pypi/langchain-core/json) |
| **tiktoken** | 0.14.0 (2026-08-17) | `tiktoken.get_encoding("o200k_base")` | `model.py` maps `gpt-5*` and o-series to `o200k_base` and embeddings-3 to `cl100k_base`. **It has no `gpt-6` entry, so `encoding_for_model("gpt-6.1-sol")` will raise.** The GPT-6 tokenizer is **UNVERIFIED**. Use o200k_base only to estimate chunk sizes; record billed tokens from API `usage` | [PyPI](https://pypi.org/project/tiktoken/#history), [model.py](https://github.com/openai/tiktoken/blob/main/tiktoken/model.py) |
| **structlog** | 26.1.0 (2026-06-06) | `merge_contextvars` → `add_log_level` → `TimeStamper(fmt="iso")` → `JSONRenderer` (Console renderer in dev). Bind `request_id` in middleware and `task_id`/`document_id` in Celery `task_prerun` | Configure in both processes (API and worker) | [PyPI](https://pypi.org/project/structlog/#history) |
| **prometheus-fastapi-instrumentator** | 8.1.0 (2026-07-26), active | `Instrumentator().instrument(app).expose(app)` | Optional. The brief asks for **JSON metrics APIs**, so build those from Postgres. FastAPI's native OTel exports via OTLP, not a Prometheus `/metrics` endpoint | [PyPI](https://pypi.org/project/prometheus-fastapi-instrumentator/#history), [FastAPI OTel](https://fastapi.tiangolo.com/advanced/opentelemetry/) |
| **slowapi** | 0.1.10 (2026-06-13, the first release since 2024-02) | `@limiter.limit("10/minute")` under the route decorator. The endpoint must accept `request: Request` | Sparse maintenance and decorator-order traps. A roughly 20-line Redis `INCR`+`EXPIRE` fixed-window dependency is easier to explain | [PyPI](https://pypi.org/project/slowapi/#history) |
| **python-multipart** | 0.0.32 (2026-06-04) | Just install it; `UploadFile` needs it | Import name changed to `python_multipart` in 0.0.13; the old `multipart` import warns. This only matters if you import it directly | [PyPI](https://pypi.org/project/python-multipart/#history), [rename issue](https://github.com/Kludex/python-multipart/issues/16) |
| **filetype / python-magic** | filetype 1.2.0 (2022, pure Python, no system deps). python-magic 0.4.27 (2022, **needs libmagic**: `brew install libmagic` / `apt libmagic1`) | Use filetype, plus your own checks: `%PDF-` magic bytes, and for DOCX `zipfile` contains `word/document.xml` | filetype's DOCX check only scans the first few zip headers within 6,000 bytes, so it can return generic `zip`. Keep the zipfile fallback | [filetype](https://pypi.org/project/filetype/#history), [docx matcher](https://github.com/h2non/filetype.py/blob/master/filetype/types/document.py), [python-magic](https://pypi.org/project/python-magic/#history) |
| **pytest** | pytest 9.1.1 (2026-06-19). pytest-asyncio 1.4.0 (2026-05-26) | `asyncio_mode = "auto"` | Use the Starlette TestClient (httpx2) or `httpx2.AsyncClient` with ASGITransport | [pytest](https://pypi.org/project/pytest/#history), [pytest-asyncio](https://pypi.org/project/pytest-asyncio/#history) |
| **Vite + React** | Vite **8.3.4** (Rolldown bundler since 8.0, 2026-03-12; Node `^20.19 \|\| >=22.12`, so Node 24 is fine). React **19.3.0**. @vitejs/plugin-react **6.1.2** (peer `vite ^8`) | `pnpm create vite@latest web -- --template react-ts`. Proxy `/api` to `http://localhost:8000` in `vite.config.ts` to avoid CORS | Port 5173 is free | [vite](https://registry.npmjs.org/vite/latest), [react](https://registry.npmjs.org/react/latest), [plugin-react](https://registry.npmjs.org/@vitejs/plugin-react/latest), [Vite 8](https://vite.dev/blog/announcing-vite8) |
| **Tailwind v4** | tailwindcss **4.3.3**. @tailwindcss/vite 4.3.3 (peer `vite ^5.2–^8`) | `pnpm add tailwindcss @tailwindcss/vite`, add `tailwindcss()` to Vite plugins, and put `@import "tailwindcss";` in CSS. **No `tailwind.config.js` needed** | v3 tutorials (postcss config, `@tailwind base`) are outdated | [install guide](https://tailwindcss.com/docs/installation/using-vite), [npm](https://registry.npmjs.org/tailwindcss/latest) |
| **Chart library** | Recharts **3.10.1** (peer React ^16.8–^19) | `<ResponsiveContainer><BarChart data=...>` for processing-time and document-status charts | Fine for a few charts | [npm](https://registry.npmjs.org/recharts/latest) |

## 2. LLM price sheet (USD per 1M tokens, standard tier)

**OpenAI** ([pricing](https://developers.openai.com/api/docs/pricing))

| Model | Input | Cached input | Output | Notes |
|---|---|---|---|---|
| `gpt-6-astra` (top flagship) | 10.00 | 1.00 | 50.00 | No `none` effort |
| **`gpt-6.1-sol`** (best value flagship) | 2.00 | 0.10 | 10.00 | 1.05M context, effort `low`–`max` (default `medium`) |
| `gpt-6-sol` | 2.00 | 0.20 | 10.00 | Replaced by 6.1 |
| **`gpt-6-luna`** (cost-efficient) | 0.10 | 0.01 | 0.50 | Effort `none`–`max` (default `medium`) |
| `gpt-5.4-mini` / `gpt-5-mini` / `gpt-5-nano` | 0.75 / 0.25 / 0.05 | 0.075 / 0.025 / 0.005 | 4.50 / 2.00 / 0.40 | Previous generation |
| `text-embedding-3-small` / `-3-large` | 0.02 / 0.13 | – | – | 1536 / 3072 dims |

GPT-6 family prices double above 272K input tokens.

**Anthropic** ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)). Batch API is 50% off.

| Model | Input | Cache read | Output |
|---|---|---|---|
| `claude-fable-5-1` | 10 | 0.25 | 50 |
| `claude-opus-5-5` | 4 | 0.20 | 20 |
| **`claude-sonnet-5-5`** | 2 | 0.10 | 10 |
| **`claude-haiku-5-5`** (prompts ≤100K tokens) | 0.10 | 0.01 | 0.50 |
| `claude-haiku-5-5` (prompts >100K tokens) | 0.50 | 0.05 | 2.50 |
| `claude-haiku-4-5` (legacy) | 1 | 0.10 | 5 |

**Cost at demo scale:** 1,000 documents × 20K tokens is 20M tokens. Embedding that with text-embedding-3-small costs **about $0.40**; with voyage-4-lite it is $0.40 or free inside the 200M free-token allowance.

## 3. Top 12 gotchas that would bite a one-day build

1. **Celery 5.6.3 cannot use redis-py 8.** Released kombu 5.6.2 requires `redis<6.5`, so pin `redis>=6.4,<6.5` ([kombu metadata](https://pypi.org/pypi/kombu/json)).
2. **MissingGreenlet on Apple Silicon.** SQLAlchemy 2.0 doesn't auto-install greenlet on macOS `arm64`, and 2.1 never does. Always install `sqlalchemy[asyncio]`. Also set `expire_on_commit=False` and use `selectinload()` ([metadata](https://pypi.org/pypi/SQLAlchemy/2.0.54/json), [asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)).
3. **Postgres 18 image moved PGDATA.** Mount the volume at `/var/lib/postgresql`; the old `/var/lib/postgresql/data` mount fails at container start. Host ports 5432 and 6379 are taken, so map 5433 and 6380 ([issue](https://github.com/docker-library/postgres/issues/1370)).
4. **Celery prefork on the macOS host can crash with ObjC fork-safety errors.** Run the worker in Docker; locally, use `--pool=threads` (or `solo`), or start with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES NO_PROXY=*` ([hynek](https://hynek.me/til/rq-macos/)).
5. **acks_late plus the Redis visibility timeout causes redelivery.** Make `process_document` idempotent: check status, delete and reinsert chunks inside one transaction. Set `worker_prefetch_multiplier=1` and a visibility timeout longer than the longest task ([Celery Redis doc](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)).
6. **HNSW limits.**
   - `vector` indexes cap at 2,000 dims, so use 3-small at 1536 dims, Voyage at 1024 or bge at 384.
   - The index is only used with `ORDER BY embedding <=> :q LIMIT k`.
   - Per-document filters can starve results; use `SET LOCAL hnsw.iterative_scan = relaxed_order` and raise `hnsw.ef_search` (default 40).
   - Source: [pgvector](https://github.com/pgvector/pgvector).
7. **Alembic does not understand pgvector.**
   - Write `CREATE EXTENSION vector` by hand.
   - Add `import pgvector.sqlalchemy` to `script.py.mako`.
   - Create the HNSW index with `op.execute`, not autogenerate (perpetual-diff bug).
   - Don't use `CONCURRENTLY` inside a migration.
   - Source: [alembic#1603](https://github.com/sqlalchemy/alembic/issues/1603).
8. **The OpenAI SDK is now v3 on httpx2.**
   - Old httpx snippets and `respx` mocks don't apply, so mock at your provider interface.
   - Use the Responses API.
   - Set `reasoning={"effort": "none"|"low"}` on chat paths, or the default `medium` slows the first token.
   - Sources: [httpx2 notes](https://raw.githubusercontent.com/openai/openai-python/main/httpx2.md), [reasoning](https://developers.openai.com/api/docs/guides/reasoning).
9. **Strict structured outputs need all fields required.** Use `X | None` for optional fields, keep schemas flat, and get the result from `responses.parse(...).output_parsed`. On Claude, **citations and structured outputs together return 400**, so stream the answer with citations and fetch follow-up questions in a separate structured call ([OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs), [Claude](https://platform.claude.com/docs/en/build-with-claude/citations)).
10. **Claude models in 2026 reject sampling parameters.** No `temperature/top_p/top_k` (TypeError in SDK 1.x, 400 on the API), no prefill, and thinking is always on for Opus 5.5. Control behaviour with `output_config.effort`. Choosing Claude also means a second embeddings vendor (Voyage via MongoDB Atlas) or local embeddings ([Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview), [embeddings](https://platform.claude.com/docs/en/build-with-claude/embeddings)).
11. **FastAPI 0.132+ rejects JSON without `Content-Type: application/json`**, which breaks quick curl demo scripts. Also, a `Depends(get_db)` session stays open for the whole SSE stream, so use short-lived sessions inside the generator ([0.132](https://github.com/fastapi/fastapi/releases/tag/0.132.0), [deps](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/)).
12. **Token-count mismatches.**
    - tiktoken has no GPT-6 mapping, so `encoding_for_model` raises; use `get_encoding("o200k_base")` for chunk sizes and take billed tokens from `usage`.
    - all-MiniLM-L6-v2 silently truncates at 256 word pieces.
    - Claude 4.7+ tokenizers count about 30% more tokens.
    - Sources: [model.py](https://github.com/openai/tiktoken/blob/main/tiktoken/model.py), [MiniLM](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2).

**Other notes:**
- Scanned PDFs give empty text with pypdf. Detect them per page and fail clearly instead of embedding empty chunks.
- Browser `EventSource` cannot POST.
- `fastapi[standard]` now pulls the OpenTelemetry SDK.
- Avoid PyMuPDF unless you can defend the AGPL licence.

## 4. Recommended pinned dependencies (pyproject-style)

```toml
[project]
name = "vault-ai"
version = "0.1.0"
requires-python = ">=3.13,<3.14"      # Celery 5.6 officially supports up to 3.13; SQLAlchemy 2.1 would need >=3.11
dependencies = [
  # API
  "fastapi==0.143.0",
  "uvicorn[standard]==0.54.0",
  "python-multipart==0.0.32",
  "pydantic==2.14.1",
  "pydantic-settings==2.15.0",
  # DB (one driver for async API + sync worker/Alembic)
  "sqlalchemy[asyncio]==2.0.54",       # 2.1.4 is GA but 19 days old; 2.0.x = stable, best-documented
  "psycopg[binary,pool]==3.3.6",
  "alembic==1.20.0",
  "pgvector==0.5.1",
  # Queue
  "celery[redis]==5.6.3",
  "kombu==5.6.2",
  "redis>=6.4,<6.5",                   # kombu 5.6.2 caps redis<6.5
  # AI (OpenAI path)
  "openai==3.29.0",
  "tiktoken==0.14.0",
  "langchain-text-splitters==1.1.3",   # pulls langchain-core 1.6.x
  # Extraction
  "pypdf==6.20.1",
  "pypdfium2==5.14.0",
  "python-docx==1.2.0",
  "filetype==1.2.0",
  # Observability
  "structlog==26.1.0",
]

[project.optional-dependencies]
claude = ["anthropic==1.13.0", "voyageai>=0.3.7"]          # if the Claude path is chosen
local-embeddings = ["sentence-transformers==6.1.0"]          # pulls torch (large)
tables = ["pdfplumber==0.11.10"]
ops = ["flower==2.2.0", "prometheus-fastapi-instrumentator==8.1.0"]

[dependency-groups]
dev = [
  "pytest==9.1.1",
  "pytest-asyncio==1.4.0",
  "httpx2",          # Starlette TestClient; version not checked, so unpinned
  "ruff",            # version not checked, so unpinned
]
```

**Infra pins:**
- `pgvector/pgvector:0.8.7-pg18-trixie`, volume mounted at `/var/lib/postgresql`, port `5433:5432`
- `redis:8.10`, port `6380:6379`
- `python:3.13-slim` base image (includes ca-certificates, which the OpenAI SDK v3 TLS change needs)
- Frontend: `vite@8.3.4`, `react@19.3.0`, `@vitejs/plugin-react@6.1.2`, `tailwindcss@4.3.3`, `@tailwindcss/vite@4.3.3`, `recharts@3.10.1`

## 5. UNVERIFIED items

- The GPT-6 family tokenizer encoding: tiktoken has no mapping, so `o200k_base` is an assumption.
- Whether GPT-6 models accept `temperature`.
- That the Responses stream's `response.completed` event carries `response.usage`. A secondary source says yes; confirm in a smoke test.
- The Alembic "renders the pgvector type without an import" behaviour, which comes from third-party guides.
- Whether a bare `redis:8` Docker tag exists.
- Native asyncio tasks in Celery 5.7 (still beta).
- The OpenAI SDK 3.0.0 release date (2026-08-12), which comes from a secondary article.