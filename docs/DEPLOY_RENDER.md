# Deploying Vault AI to Render

The app is cloud-ready by construction: one Docker image (API + built dashboard, or Celery worker by command), all
configuration from environment variables, all state in managed services (Postgres, Key Value, S3-compatible storage).
Nothing is written to local disk except temp files during extraction.

## What gets created (`render.yaml` Blueprint)

| Render resource | Role | Notes |
|---|---|---|
| `vault-api` (web, Docker) | FastAPI + React dashboard on one origin | `preDeployCommand` runs `python -m app.cli bootstrap` (Alembic upgrade, bucket check, seed users); health check `/health/live` |
| `vault-worker` (worker, Docker) | Celery consuming `ingest` and `ai` | same image, different command; `--max-memory-per-child` recycles leaky children |
| `vault-kv` (Key Value, Valkey 8) | Celery broker + cache + rate-limit counters | `maxmemoryPolicy: noeviction`, internal only |
| `vault-db` (Postgres 17) | system of record incl. vectors | pgvector enabled by migration 0001 |
| Cloudflare R2 bucket (external) | original uploaded files | S3 API; Render's own object storage is not GA |

## Steps

1. **Object storage**: create an R2 bucket named `vault-uploads` and an API token with read/write on it. Note the
   endpoint `https://<account_id>.r2.cloudflarestorage.com`, access key id and secret.
2. **Push the repo** to GitHub (already done: private repo).
3. In Render: **New > Blueprint**, pick the repo. Render reads `render.yaml` and asks for every `sync: false` value:
   `OPENAI_API_KEY`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and the three `SEED_*_API_KEY`
   values (choose long random strings; only their sha256 is stored). Secrets are entered for both the API and the
   worker.
4. Deploy. The pre-deploy step migrates and seeds; when `vault-api` is live, open its URL: the dashboard loads from
   the same origin. Put the demo API key in the dashboard's Settings.
5. Check `GET /health/ready`: `status: ok` means the hard checks pass (DB reachable, pgvector installed, schema at
   Alembic head; otherwise 503 `unavailable`) and Redis, a consumer on both worker queues, the storage bucket and the
   AI provider (key set, recent calls not failing) are healthy; `degraded` (still 200) means a soft check failed: look
   for the `checks.<name>` with `ok: false` (most include an `error`).

## Cloud-specific details handled in code

- `DATABASE_URL` from Render is `postgres://...`; `app/config.py` rewrites it to `postgresql+psycopg://` (also used by
  Alembic), so no manual editing.
- Render injects `$PORT`; the image's `CMD` runs uvicorn through `sh -c` so `${PORT}` expands.
- boto3 is configured with `request_checksum_calculation="when_required"` (boto3 >= 1.36 sends CRC32 checksums R2
  rejects) and `S3_REGION=auto` for R2.
- The internal Key Value URL is plain `redis://` (no TLS inside the private network), so Celery needs no SSL options.
  `BROKER_URL` and `CACHE_URL` point at the same instance; cache keys (`qemb:`, `rl:`) never collide with Celery's.
- Small SQLAlchemy pools (`DB_POOL_SIZE=3`) because API + worker + pre-deploy share the plan's connection limit.

## Cost (Render list prices checked 2026-10-13)

| Option | Monthly | Trade-off |
|---|---|---|
| Blueprint as written (0.5c-512mb web + 0.5c-512mb worker + 0.1c-256mb Postgres + free Key Value) | ~$20 + R2 (free tier 10 GB) | real async workers; Key Value free tier has no persistence |
| Free fallback: one free web service running `scripts/start_all_in_one.sh` + free Postgres + free Key Value | $0 | spins down after 15 min idle (~1 min cold start), worker shares the web instance, free Postgres expires after 30 days |

To use the free fallback, change `vault-api` to `plan: free`, remove `preDeployCommand` and the `vault-worker` service,
set `dockerCommand: sh scripts/start_all_in_one.sh` (it runs the bootstrap at start), and change `vault-db` to
`plan: free` (expires after 30 days).

## Scaling path (what changes with load)

- More uploads: add `vault-worker` instances (or split into an `ingest` worker and an `ai` worker service).
- More chat traffic: add `vault-api` instances (stateless; sessions live in Postgres).
- Large corpora: bigger Postgres plan (HNSW index must fit in RAM), then `halfvec` vectors, then owner partitioning.
