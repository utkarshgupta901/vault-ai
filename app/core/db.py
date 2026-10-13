"""Database engines.

One driver (psycopg 3) serves both worlds:
- the API uses an async engine with a small connection pool;
- Celery workers use a *sync* engine with NullPool: Celery tasks are sync functions, and NullPool means no pooled
  connection can ever be inherited across fork() or leak a session-level advisory lock.
Engines are created lazily so importing this module never opens a connection (important for forked workers).
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import cache

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings


class Base(DeclarativeBase):
    pass


@cache
def get_async_engine() -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
    )


@cache
def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for normal (non-streaming) routes. Streaming routes open short sessions themselves,
    because a yield-dependency session would stay open for the whole stream."""
    async with get_async_sessionmaker()() as session:
        yield session


def async_session() -> AsyncSession:
    return get_async_sessionmaker()()


@cache
def get_sync_engine():
    return create_engine(settings.DATABASE_URL, poolclass=NullPool)


@contextmanager
def sync_session() -> Iterator[Session]:
    """Worker-side unit of work: commits on success, rolls back on error."""
    session = sessionmaker(get_sync_engine(), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engines_after_fork() -> None:
    """Called in each Celery child process: drop any engine object inherited from the parent."""
    get_sync_engine.cache_clear()
