"""All configuration comes from environment variables (12-factor), so the same image runs locally,
in tests and on Render. Every tunable the README or eval mentions lives here."""

import re
from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: Literal["local", "test", "prod"] = "local"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # --- Connections -------------------------------------------------------------------------------
    DATABASE_URL: str = "postgresql+psycopg://vault:vault@localhost:5433/vault"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    BROKER_URL: str = "redis://localhost:6380/0"   # Celery broker (Valkey db0)
    CACHE_URL: str = "redis://localhost:6380/1"    # cache + rate limits (Valkey db1)
    REDIS_TIMEOUT_S: float = 0.2                   # cache/rate-limit calls fail open after 200 ms

    # --- Object storage (S3 API: RustFS locally, R2/S3 in the cloud) ---------------------------------
    S3_ENDPOINT_URL: str | None = "http://localhost:9200"
    S3_ACCESS_KEY_ID: str = "vaultlocal"
    S3_SECRET_ACCESS_KEY: str = "vaultlocalsecret"
    S3_BUCKET: str = "vault-uploads"
    S3_REGION: str = "us-east-1"                   # "auto" for Cloudflare R2
    S3_ADDRESSING_STYLE: Literal["path", "virtual", "auto"] = "path"

    # --- AI provider + models ------------------------------------------------------------------------
    LLM_PROVIDER: Literal["openai", "fake"] = "openai"
    OPENAI_API_KEY: str | None = None
    ANSWER_MODEL: str = "gpt-6.1-sol"              # grounded answers (streamed)
    ANSWER_EFFORT: str = "low"                     # Sol supports low..max; low keeps first-token latency ~1.8 s
    FAST_MODEL: str = "gpt-6-luna"                 # condense, follow-ups, fallback answers
    FAST_EFFORT: str = "none"
    INSIGHTS_MODEL: str = "gpt-6-luna"             # document insights + custom summaries
    INSIGHTS_EFFORT: str = "low"
    JUDGE_MODEL: str = "gpt-6.1-sol"               # offline eval judge only
    JUDGE_EFFORT: str = "high"
    EMBED_MODEL: str = "text-embedding-3-small"
    EMBED_DIMS: int = 1536                         # must match vector(1536) in migration 0001
    ANSWER_PROMPT_VERSION: Literal["v1", "v2"] = "v2"
    API_LLM_MAX_RETRIES: int = 1                   # API retries once; workers rely on Celery retries only
    API_LLM_TIMEOUT_S: float = 30.0
    WORKER_LLM_TIMEOUT_S: float = 60.0

    # --- RAG ---------------------------------------------------------------------------------------------
    CHUNK_TOKENS: int = 400
    CHUNK_OVERLAP: int = 60
    RETRIEVAL_K: int = 8                           # sources sent to the model
    RETRIEVAL_CANDIDATES: int = 40                 # per leg (vector, keyword) before fusion
    PER_DOC_CAP: int = 4                           # max sources per document in multi-doc scope
    LEXICAL_WEIGHT: float = 1.0                    # weight of the keyword leg in RRF
    RRF_K: int = 60
    GATE_MIN_SIMILARITY: float = 0.30              # below this (and no identifier hit) -> refuse without an LLM call
    TAU_HIGH: float = 0.50                         # min top similarity for a "high" confidence badge
    RETRIEVAL_MODE: Literal["hybrid", "vector", "keyword"] = "hybrid"
    HISTORY_TURNS: int = 3
    HISTORY_MAX_TOKENS: int = 1500
    TODAY_OVERRIDE: date | None = None             # pins "today" for reproducible evals

    # --- Per-step time budgets (seconds) -----------------------------------------------------------------
    CONDENSE_TIMEOUT_S: float = 4.0
    EMBED_QUERY_TIMEOUT_S: float = 3.0
    ANSWER_TTFT_TIMEOUT_S: float = 10.0
    ANSWER_IDLE_TIMEOUT_S: float = 20.0
    FOLLOWUPS_TIMEOUT_S: float = 4.0
    JSON_ANSWER_TIMEOUT_S: float = 45.0
    STREAMING_STALE_S: int = 90

    # --- Pipeline ------------------------------------------------------------------------------------
    EMBED_BATCH_SIZE: int = 128
    EMBED_BATCH_MAX_TOKENS: int = 250_000          # API limit is 300k tokens per request
    MAX_PAGES: int = 500
    INSIGHTS_MAX_INPUT_TOKENS: int = 100_000
    STALE_PROCESSING_MIN: int = 15
    MAX_TASK_RETRIES: int = 6

    # --- Limits --------------------------------------------------------------------------------------
    MAX_UPLOAD_MB: int = 25
    QUEUE_BACKPRESSURE_DEPTH: int = 500

    # --- Web -----------------------------------------------------------------------------------------
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # --- Seeded API users (local/demo). In prod set real secrets via env. ---------------------------------
    SEED_DEMO_API_KEY: str = "vault-demo-key"
    SEED_OTHER_API_KEY: str = "vault-other-key"
    SEED_EVAL_API_KEY: str = "vault-eval-key"

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Render/Heroku hand out postgres:// or postgresql://; SQLAlchemy needs the psycopg3 dialect name.
        return re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql+psycopg://", v)

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    def today(self) -> date:
        return self.TODAY_OVERRIDE or date.today()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
