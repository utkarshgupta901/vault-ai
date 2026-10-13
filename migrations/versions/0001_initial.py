"""Initial schema (hand-written SQL so every constraint and index is explicit and reviewable).

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

-- Identity -------------------------------------------------------------------------------------------
CREATE TABLE users (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name                 text NOT NULL UNIQUE,
  api_key_hash         char(64) NOT NULL UNIQUE,              -- sha256(api key); the key itself is never stored
  daily_cost_limit_usd numeric(8,2) NOT NULL DEFAULT 2.00,    -- soft daily AI spend quota
  rate_limit_per_min   int NOT NULL DEFAULT 30,
  created_at           timestamptz NOT NULL DEFAULT now()
);

-- Documents ------------------------------------------------------------------------------------------
CREATE TABLE documents (
  id                  uuid PRIMARY KEY,                       -- generated in Python before the file is stored
  owner_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  filename            text NOT NULL,
  title               text,
  mime_type           text NOT NULL,
  size_bytes          bigint NOT NULL,
  sha256              char(64) NOT NULL,
  storage_key         text NOT NULL,                          -- uploads/{owner_id}/{document_id}.{ext}
  upload_count        int NOT NULL DEFAULT 1,
  status              text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','ready','failed')),
  stage               text CHECK (stage IN ('extracting','chunking','embedding')),
  progress_pct        smallint NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
  insights_status     text NOT NULL DEFAULT 'pending' CHECK (insights_status IN ('pending','processing','ready','failed')),
  insights_error_code text,
  current_run_id      uuid,                                   -- fences stale / duplicate task deliveries
  page_count          int,
  chunk_count         int,
  token_count         int,
  warnings            jsonb NOT NULL DEFAULT '[]',
  error_code          text,
  error_message       text,
  error_retryable     boolean,
  category            text,
  tags                text[] NOT NULL DEFAULT '{}',
  document_date       date,
  version             int NOT NULL DEFAULT 1,
  previous_version_id uuid REFERENCES documents(id) ON DELETE SET NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  ready_at            timestamptz,
  CONSTRAINT uq_documents_owner_sha256 UNIQUE (owner_id, sha256)   -- dedup: same bytes, same owner = same doc
);
-- Keyset pagination (newest first) with and without a status filter.
CREATE INDEX ix_documents_owner_created        ON documents (owner_id, created_at DESC, id DESC);
CREATE INDEX ix_documents_owner_status_created ON documents (owner_id, status, created_at DESC, id DESC);
CREATE INDEX ix_documents_tags                 ON documents USING gin (tags);

CREATE TABLE document_pages (
  document_id  uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_no      int NOT NULL,                                  -- physical page (1-based)
  label        text NOT NULL,                                 -- what humans see: "p. 4" or "§ 9 Termination"
  heading_path text,
  text         text NOT NULL,
  char_count   int NOT NULL,
  PRIMARY KEY (document_id, page_no)
);

-- Retrieval units ------------------------------------------------------------------------------------
CREATE TABLE chunks (
  id              bigserial PRIMARY KEY,
  document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  owner_id        uuid NOT NULL,                              -- second tenant filter, right on the searched table
  chunk_index     int NOT NULL,
  page_start      int NOT NULL,
  page_end        int NOT NULL,
  heading_path    text,
  header          text NOT NULL,                              -- "Title > Section (p. 4)", embedded with the content
  content         text NOT NULL,
  token_count     int NOT NULL,
  embed_hash      char(64) NOT NULL,                          -- sha256(model|dims|header+content): reuse vectors
  embedding_model text,
  embedding       vector(1536),                               -- NULL until embedded => resumable embedding stage
  tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(header, '') || ' ' || content)) STORED,
  CONSTRAINT uq_chunks_document_index UNIQUE (document_id, chunk_index)
);
-- Approximate nearest neighbour index for cosine distance (<=>). m/ef_construction = pgvector defaults.
CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
-- Inverted index for full-text (keyword) search.
CREATE INDEX ix_chunks_tsv            ON chunks USING gin (tsv);
CREATE INDEX ix_chunks_owner_hash     ON chunks (owner_id, embed_hash);

-- AI insights ------------------------------------------------------------------------------------------
CREATE TABLE document_insights (
  document_id         uuid PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  title               text,
  summary             text,
  key_points          jsonb NOT NULL DEFAULT '[]',
  document_type       text,
  category            text,
  tags                text[] NOT NULL DEFAULT '{}',
  entities            jsonb NOT NULL DEFAULT '[]',
  sentiment           text,
  sentiment_rationale text,
  risk_flags          jsonb NOT NULL DEFAULT '[]',
  suggested_questions jsonb NOT NULL DEFAULT '[]',
  language            text,
  document_date       date,
  injection_suspected boolean NOT NULL DEFAULT false,
  coverage            text NOT NULL DEFAULT 'full' CHECK (coverage IN ('full','sampled')),
  run_id              uuid,
  model               text,
  prompt_version      text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE document_summaries (
  id             bigserial PRIMARY KEY,
  document_id    uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  cache_key      char(64) NOT NULL,
  length         text NOT NULL,
  tone           text NOT NULL,
  focus          text,
  content        text NOT NULL,
  model          text NOT NULL,
  prompt_version text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_document_summaries_key UNIQUE (document_id, cache_key)
);

-- Chat -----------------------------------------------------------------------------------------------
CREATE TABLE chat_sessions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title           text,
  all_documents   boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_message_at timestamptz
);
CREATE INDEX ix_chat_sessions_owner_recent ON chat_sessions (owner_id, last_message_at DESC NULLS LAST, created_at DESC);

CREATE TABLE chat_session_documents (
  session_id  uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  PRIMARY KEY (session_id, document_id)
);

CREATE TABLE chat_messages (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seq                 bigint GENERATED ALWAYS AS IDENTITY,      -- total order (now() is identical within a txn)
  session_id          uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role                text NOT NULL CHECK (role IN ('user','assistant')),
  reply_to_id         uuid REFERENCES chat_messages(id) ON DELETE SET NULL,
  content             text NOT NULL DEFAULT '',
  status              text NOT NULL DEFAULT 'complete' CHECK (status IN ('streaming','complete','interrupted','failed')),
  client_message_id   text,                                     -- client idempotency key (user rows only)
  standalone_query    text,
  answer_type         text CHECK (answer_type IN ('answered','not_found','ungrounded')),
  confidence          text CHECK (confidence IN ('high','medium','low')),
  signals             jsonb,
  retrieval           jsonb,
  citations           jsonb NOT NULL DEFAULT '[]',
  followups           jsonb NOT NULL DEFAULT '[]',
  warnings            jsonb NOT NULL DEFAULT '[]',
  model               text,
  prompt_version      text,
  input_tokens        int,
  cached_input_tokens int,
  output_tokens       int,
  cost_usd            numeric(12,6),
  ttft_ms             int,
  latency_ms          int,
  timings             jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_chat_messages_client_id UNIQUE (session_id, client_message_id)
);
CREATE INDEX ix_chat_messages_session_seq ON chat_messages (session_id, seq);
-- At most one answer streaming per session at a time (a second concurrent ask gets 409 SESSION_BUSY).
CREATE UNIQUE INDEX ux_chat_messages_one_streaming ON chat_messages (session_id) WHERE status = 'streaming';

-- Telemetry ------------------------------------------------------------------------------------------
CREATE TABLE processing_runs (
  id             bigserial PRIMARY KEY,
  run_id         uuid NOT NULL,
  document_id    uuid REFERENCES documents(id) ON DELETE SET NULL,
  owner_id       uuid,
  task           text NOT NULL CHECK (task IN ('ingest','insights')),
  attempt        smallint NOT NULL,
  celery_task_id text,
  request_id     text,
  status         text NOT NULL CHECK (status IN ('running','succeeded','failed','retrying')),
  stage_ms       jsonb NOT NULL DEFAULT '{}',
  error_code     text,
  error_message  text,
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  duration_ms    int
);
CREATE INDEX ix_processing_runs_started  ON processing_runs (started_at);
CREATE INDEX ix_processing_runs_document ON processing_runs (document_id, started_at);
CREATE INDEX ix_processing_runs_run      ON processing_runs (run_id, task);

CREATE TABLE ai_usage (                                         -- no FKs: cost history survives deletes
  id                  bigserial PRIMARY KEY,
  owner_id            uuid,
  operation           text NOT NULL CHECK (operation IN
                        ('embed_chunks','embed_query','insights','condense','answer','followups',
                         'summary','compare','judge','eval_gen')),
  model               text NOT NULL,
  prompt_version      text,
  input_tokens        int NOT NULL DEFAULT 0,
  cached_input_tokens int NOT NULL DEFAULT 0,
  output_tokens       int NOT NULL DEFAULT 0,
  reasoning_tokens    int NOT NULL DEFAULT 0,
  cost_usd            numeric(12,6) NOT NULL DEFAULT 0,
  latency_ms          int,
  success             boolean NOT NULL DEFAULT true,
  error_code          text,
  cache_hit           boolean NOT NULL DEFAULT false,
  estimated           boolean NOT NULL DEFAULT false,         -- usage estimated (client disconnected mid-stream)
  document_id         uuid,
  message_id          uuid,
  request_id          text,
  task_id             text,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ai_usage_owner_created     ON ai_usage (owner_id, created_at);
CREATE INDEX ix_ai_usage_operation_created ON ai_usage (operation, created_at);

CREATE TABLE request_logs (
  id          bigserial PRIMARY KEY,
  request_id  text,
  owner_id    uuid,
  method      text NOT NULL,
  route       text NOT NULL,                                    -- path template, e.g. /api/v1/documents/{document_id}
  status_code smallint NOT NULL,
  ttfb_ms     int,                                              -- time to first byte (percentiles use this)
  total_ms    int,                                              -- full duration (long for streams)
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_request_logs_created       ON request_logs (created_at);
CREATE INDEX ix_request_logs_route_created ON request_logs (route, created_at);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS request_logs, ai_usage, processing_runs, chat_messages, chat_session_documents, chat_sessions,
  document_summaries, document_insights, chunks, document_pages, documents, users CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
