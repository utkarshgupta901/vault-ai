"""Test harness.

- Runs against the REAL compose services (Postgres :5433 in a separate `vault_test` database, Valkey db 14/15,
  a `vault-test` S3 bucket) so SQL, pgvector and constraints are exercised for real.
- The LLM is always FakeProvider (deterministic, offline, free). No test talks to OpenAI.
- Celery runs eagerly (tasks execute inline when published).
- Tests that need services are auto-marked `integration`; `make test-fast` runs only pure unit tests.
"""

import os

# Parallel agents each set TEST_DB_NAME / TEST_CACHE_DB so their test runs never truncate each other's data.
TEST_DB = os.environ.get("TEST_DB_NAME", "vault_test")
TEST_CACHE_DB = os.environ.get("TEST_CACHE_DB", "14")

os.environ.update({
    "APP_ENV": "test",
    "LLM_PROVIDER": "fake",
    "OPENAI_API_KEY": "",
    "DATABASE_URL": f"postgresql+psycopg://vault:vault@localhost:5433/{TEST_DB}",
    "BROKER_URL": "redis://localhost:6380/15",
    "CACHE_URL": f"redis://localhost:6380/{TEST_CACHE_DB}",
    "S3_ENDPOINT_URL": "http://localhost:9200",
    "S3_BUCKET": "vault-test",
    "LOG_LEVEL": "WARNING",
})

import uuid  # noqa: E402
from collections.abc import AsyncIterator, Iterator  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.ai import set_provider  # noqa: E402
from app.ai.fake_provider import FakeProvider  # noqa: E402
from app.config import settings  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402

DEMO = {"X-API-Key": settings.SEED_DEMO_API_KEY}
OTHER = {"X-API-Key": settings.SEED_OTHER_API_KEY}
INTEGRATION_FIXTURES = {"client", "db", "sync_db", "demo_user_id", "other_user_id", "_services"}
APP_TABLES = ("request_logs, ai_usage, processing_runs, chat_messages, chat_session_documents, chat_sessions, "
              "document_summaries, document_insights, chunks, document_pages, documents")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs the compose stack (Postgres, Valkey, S3)")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if INTEGRATION_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def _services() -> Iterator[None]:
    """Create + migrate the test database, ensure the bucket, seed users. Runs once per session."""
    admin = "postgresql://vault:vault@localhost:5433/vault"
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    from app.cli import ensure_bucket, migrate, seed_users

    migrate(settings.DATABASE_URL)
    ensure_bucket(retries=3)
    seed_users()
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    yield


@pytest.fixture
def fake() -> Iterator[FakeProvider]:
    provider = FakeProvider()
    set_provider(provider)
    yield provider
    set_provider(None)


@pytest.fixture
async def db(_services: None, fake: FakeProvider) -> AsyncIterator:
    """Clean tables before each integration test and hand out an async session."""
    from app.core.db import async_session, get_async_engine

    async with get_async_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {APP_TABLES} RESTART IDENTITY CASCADE"))
    try:
        from app.core.redis import get_async_cache

        await get_async_cache().flushdb()
    except Exception:  # noqa: BLE001
        pass
    async with async_session() as session:
        yield session


@pytest.fixture
async def client(db) -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30) as c:
        yield c


@pytest.fixture
async def demo_user_id(db) -> uuid.UUID:
    return await db.scalar(text("SELECT id FROM users WHERE name = 'demo'"))


@pytest.fixture
async def other_user_id(db) -> uuid.UUID:
    return await db.scalar(text("SELECT id FROM users WHERE name = 'other'"))


async def upload(client: AsyncClient, name: str, content: bytes, headers: dict | None = None,
                 mime: str = "application/octet-stream"):
    return await client.post("/api/v1/documents", headers=headers or DEMO, files={"file": (name, content, mime)})
