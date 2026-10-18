"""/health/ready stays bounded under concurrency and can no longer wedge the broker pool (incident 2026-10-13).

Before the fix, ~10 overlapping readiness calls each held a connection from Celery's broker pool and waited forever
for a second one; every later publish (upload enqueue) then hung too. These tests fire the same load at the
in-process app against the test broker (Valkey db15, no workers) and then publish a real task."""

import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from functools import partial
from typing import Any

import anyio
import pytest
import redis
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.ai import PROVIDER_RESET_AFTER_FAILURES, ProviderWatchdog, get_provider, set_provider
from app.ai.fake_provider import FakeProvider
from app.ai.provider import Msg, ProviderError, StreamEnd
from app.api import health
from app.api.health import AsyncSingleFlightCheck, SingleFlightCheck
from app.config import settings
from app.core.db import get_async_engine
from app.rag.generation import FallbackUsed, stream_answer
from app.worker.celery_app import celery_app
from app.worker.dispatch import _publish_in_thread


class CountingCheck:
    """A blocking check that records how many calls ran and how many ran at the same time."""

    def __init__(self, sleep_s: float = 0.0, value: object = True, error: Exception | None = None):
        self.sleep_s, self.value, self.error = sleep_s, value, error
        self.calls = self.running = self.max_running = 0
        self._lock = threading.Lock()

    def __call__(self) -> object:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            time.sleep(self.sleep_s)
            if self.error is not None:
                raise self.error
            return self.value
        finally:
            with self._lock:
                self.running -= 1


@pytest.fixture(autouse=True)
def fresh_health_caches() -> Iterator[None]:
    for check in (health.db_check, health.workers_check, health.storage_check):
        check.reset()
    yield
    for check in (health.db_check, health.workers_check, health.storage_check):
        check.reset()


# --- SingleFlightCheck (no services) ---------------------------------------------------------------------------------
async def test_concurrent_callers_share_one_check() -> None:
    fn = CountingCheck(sleep_s=0.3, value={"ingest": 1})
    check = SingleFlightCheck(fn, ttl_s=10)
    results: list[object] = []

    async def call() -> None:
        results.append(await check.get())

    async with anyio.create_task_group() as tg:
        for _ in range(20):
            tg.start_soon(call)
    assert results == [{"ingest": 1}] * 20
    assert (fn.calls, fn.max_running) == (1, 1)
    assert await check.get() == {"ingest": 1} and fn.calls == 1      # within the TTL: cached, no thread


async def test_abandoned_check_still_blocks_a_second_one() -> None:
    """A caller that times out abandons the thread; the next caller must wait for it, not start another inspect."""
    fn = CountingCheck(sleep_s=0.6, value="v")
    check = SingleFlightCheck(fn, ttl_s=10)
    for _ in range(3):
        with pytest.raises(TimeoutError), anyio.fail_after(0.1):
            await check.get()
    assert fn.calls == 1
    with anyio.fail_after(2):
        assert await check.get() == "v"      # waits for the original run and uses its result
    assert (fn.calls, fn.max_running) == (1, 1)


async def test_failures_are_cached_too() -> None:
    fn = CountingCheck(error=ConnectionError("broker down"))
    check = SingleFlightCheck(fn, ttl_s=10)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="broker down"):
            await check.get()
    assert fn.calls == 1                      # a dead dependency costs one check per TTL, not one per request


async def test_expired_result_is_refreshed() -> None:
    fn = CountingCheck(value="v")
    check = SingleFlightCheck(fn, ttl_s=0.05)
    await check.get()
    await anyio.sleep(0.1)
    await check.get()
    assert fn.calls == 2


# --- /health/ready ---------------------------------------------------------------------------------------------------
async def test_ready_answers_in_time_when_a_check_hangs(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "workers_check", SingleFlightCheck(CountingCheck(sleep_s=4), ttl_s=10))
    started = time.perf_counter()
    r = await client.get("/health/ready")
    elapsed = time.perf_counter() - started
    assert r.status_code == 200                      # workers are a soft dependency
    body = r.json()
    assert elapsed < health.CHECK_TIMEOUT_S + 1.0
    assert body["status"] == "degraded"
    assert body["checks"]["workers"]["ok"] is False
    assert body["checks"]["workers"]["error"] == f"timed out after {health.CHECK_TIMEOUT_S:g}s"
    assert body["checks"]["database"]["ok"] is True


def _broker() -> redis.Redis:
    return redis.from_url(settings.BROKER_URL)


async def _publish_probe_task() -> int:
    """Publish a real message (send_task ignores eager mode) to a throwaway queue; return how many landed."""
    queue = "platform-test-probe"
    _broker().delete(queue)
    try:
        await _publish_in_thread(partial(celery_app.send_task, "vault.platform_probe", queue=queue))
        return int(_broker().llen(queue))
    finally:
        _broker().delete(queue, f"_kombu.binding.{queue}")


@pytest.mark.filterwarnings("ignore::celery.exceptions.AlwaysEagerIgnored")   # the probe publishes for real
async def test_20_concurrent_readiness_calls_then_publish_succeeds(client: AsyncClient,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The incident load, through the real inspect code path against the test broker."""
    counting = CountingCheck()
    real_inspect = health._worker_queues

    def counted_inspect() -> dict[str, int]:
        counting()
        return real_inspect()

    monkeypatch.setattr(health, "workers_check", SingleFlightCheck(counted_inspect, ttl_s=10))
    statuses: list[int] = []

    async def probe() -> None:
        statuses.append((await client.get("/health/ready")).status_code)

    started = time.perf_counter()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            for _ in range(20):
                tg.start_soon(probe)
    assert statuses == [200] * 20
    assert time.perf_counter() - started < 3 * health.CHECK_TIMEOUT_S
    assert counting.calls == 1                          # one inspect served all 20 requests
    with anyio.fail_after(6):
        assert await _publish_probe_task() == 1         # the broker pool is still usable


@pytest.mark.filterwarnings("ignore::celery.exceptions.AlwaysEagerIgnored")
async def test_uncached_inspects_cannot_deadlock_the_pool(_services: None) -> None:
    """Even bypassing the single-flight cache, 15 simultaneous inspects (the old code wedged at 10) all finish,
    because each one runs on its own connection instead of holding a pooled one while waiting for another."""
    outcomes: list[str] = []

    def inspect() -> None:
        try:
            health._worker_queues()
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001 - an error is fine here; a hang is the bug
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=inspect, daemon=True) for _ in range(15)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 15
    for t in threads:
        await anyio.to_thread.run_sync(t.join, max(0.0, deadline - time.monotonic()))
    assert not [t for t in threads if t.is_alive()], "inspect threads are stuck: the broker pool deadlocked"
    assert outcomes.count("ok") >= 10
    with anyio.fail_after(6):
        assert await _publish_probe_task() == 1


# --- the AI check ------------------------------------------------------------------------------------------------------
@pytest.fixture
def watched() -> Iterator[ProviderWatchdog]:
    set_provider(None)
    provider = get_provider()
    assert isinstance(provider, ProviderWatchdog)
    yield provider
    set_provider(None)


async def test_ai_check_fails_when_either_path_keeps_failing(watched: ProviderWatchdog) -> None:
    fake: FakeProvider = watched.inner           # type: ignore[assignment]
    assert health._ai_check()["ok"] is True
    for _ in range(PROVIDER_RESET_AFTER_FAILURES):
        fake.fail_next.append(ProviderError("transient", "OpenAI request timed out."))
        with pytest.raises(ProviderError):
            await watched.acomplete_text(model="m", effort="low", instructions="", input="q", max_output_tokens=10)
        await watched.aembed(["q"])                  # embeddings keep working: must not mask the generation path
    check = health._ai_check()
    assert check["ok"] is False and check["auth_failed"] is False
    assert check["failures"] == {"embeddings": 0, "generation": PROVIDER_RESET_AFTER_FAILURES}
    assert check["consecutive_failures"] == PROVIDER_RESET_AFTER_FAILURES
    assert check["error"] == f"last {PROVIDER_RESET_AFTER_FAILURES} generation calls failed"


@pytest.mark.parametrize(("status", "error"), [
    (401, f"AI provider rejected our credentials (HTTP 401, embeddings: {settings.EMBED_MODEL})"),
    (403, f"AI provider rejected our credentials (HTTP 403, embeddings: {settings.EMBED_MODEL})"),
    (404, f"AI model not found (HTTP 404, embeddings: {settings.EMBED_MODEL})"),
])
async def test_ai_check_reports_a_rejected_key_or_model_generically(watched: ProviderWatchdog, status: int,
                                                                    error: str) -> None:
    """Incident 2026-10-17: every call returned 401 while readiness said ai ok with 0 failures."""
    fake: FakeProvider = watched.inner           # type: ignore[assignment]
    fake.fail_next.append(ProviderError(
        "bad_request", f"OpenAI rejected the request ({status}): Incorrect API key provided: sk-proj-****wxyz",
        None, status))
    with pytest.raises(ProviderError):
        await watched.aembed(["q"])
    check = health._ai_check()
    assert check["ok"] is False and check["auth_failed"] is True
    assert check["error"] == error
    assert "sk-proj" not in json.dumps(check) and "Incorrect" not in json.dumps(check)   # no vendor text
    await watched.aembed(["q"])                      # the key works again: the next success clears it
    assert health._ai_check()["ok"] is True


class MissingModel(FakeProvider):
    """A key whose project has no access to one model: that model's calls return 404, the others answer."""

    def __init__(self, missing: str) -> None:
        super().__init__()
        self.missing = missing

    async def astream_text(self, **kwargs: Any) -> AsyncIterator[str | StreamEnd]:
        if kwargs["model"] == self.missing:
            raise ProviderError("bad_request", "OpenAI rejected the request (404): model_not_found", None, 404)
        async for item in super().astream_text(**kwargs):
            yield item


async def test_ai_check_stays_red_while_the_answer_model_is_missing_and_turns_answer_on_the_fallback() -> None:
    """Regression: the fallback model's first token cleared the answer model's 404, so readiness said ai ok while
    every chat turn paid a failed first attempt (and it flapped red only between the 404 and that token)."""
    watched = ProviderWatchdog(MissingModel(settings.ANSWER_MODEL))
    set_provider(watched)
    msgs = [Msg(role="user", content="q")]
    try:
        items = [item async for item in stream_answer(watched, instructions="", input=msgs)]
        assert isinstance(items[0], FallbackUsed) and isinstance(items[-1], StreamEnd)   # the turn was answered
        check = health._ai_check()
        assert check["ok"] is False and check["auth_failed"] is True
        assert check["error"] == f"AI model not found (HTTP 404, generation: {settings.ANSWER_MODEL})"

        watched.inner.missing = "none"           # type: ignore[attr-defined]  # access granted: the next turn answers
        items = [item async for item in stream_answer(watched, instructions="", input=msgs)]
        assert not any(isinstance(item, FallbackUsed) for item in items)
        assert health._ai_check()["ok"] is True
    finally:
        set_provider(None)


async def test_ready_reports_a_rejected_key(client: AsyncClient, fake: FakeProvider) -> None:
    watched = ProviderWatchdog(fake)
    fake.fail_next.append(ProviderError("bad_request", "OpenAI rejected the request (401): sk-proj-****wxyz", None, 401))
    with pytest.raises(ProviderError):
        await watched.aembed(["q"])
    r = await client.get("/health/ready")
    ai_check = r.json()["checks"]["ai"]
    assert r.status_code == 200 and r.json()["status"] == "degraded"      # soft dependency: the API still serves
    assert ai_check["ok"] is False and ai_check["auth_failed"] is True
    assert "sk-proj" not in r.text


# --- the DB probe: single flight, generic public errors -------------------------------------------------------------
async def test_concurrent_readiness_calls_share_one_db_probe(client: AsyncClient,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """/health/ready is public: a burst of calls must not become a burst of DB queries."""
    monkeypatch.setattr(health, "workers_check", SingleFlightCheck(CountingCheck(value={"ingest": 1, "ai": 1}), 10))
    monkeypatch.setattr(health, "storage_check", SingleFlightCheck(CountingCheck(), ttl_s=10))
    probes: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "alembic_version" in statement:
            probes.append(statement)

    engine = get_async_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    statuses: list[int] = []

    async def probe() -> None:
        statuses.append((await client.get("/health/ready")).status_code)

    try:
        async with anyio.create_task_group() as tg:
            for _ in range(20):
                tg.start_soon(probe)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert statuses == [200] * 20
    assert len(probes) == 1


class Recorder:
    """Stands in for a module's structlog logger and keeps what was logged."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))


SECRET = "connection to server at 10.20.30.40 failed: password authentication failed for user vault"


async def test_ready_does_not_echo_dependency_errors(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint is unauthenticated: a raw exception can carry hosts, users or bucket URLs. Public text is
    generic; the detail goes to the log."""
    def broken_session() -> Any:
        raise ConnectionError(SECRET)

    monkeypatch.setattr(health, "async_session", broken_session)
    monkeypatch.setattr(health, "storage_check",
                        SingleFlightCheck(CountingCheck(error=ConnectionError("http://10.20.30.40:9200/bucket")), 10))
    recorder = Recorder()
    monkeypatch.setattr(health, "log", recorder, raising=False)
    r = await client.get("/health/ready")
    assert r.status_code == 503
    checks = r.json()["checks"]
    assert checks["database"]["error"] == "unreachable"
    assert checks["storage"]["error"] == "unreachable"
    assert "10.20.30.40" not in r.text
    logged = {kw.get("check"): kw.get("error") for _, kw in recorder.events}
    assert SECRET in logged["database"] and "10.20.30.40" in logged["storage"]


SCHEMA_ERROR = ProgrammingError("SELECT version_num FROM alembic_version", {},
                                Exception('relation "alembic_version" does not exist'))


@pytest.mark.parametrize(("error", "public"), [
    (ConnectionError("connection refused"), "unreachable"),
    (OperationalError("SELECT 1", {}, Exception("connection to server failed")), "unreachable"),
    (redis.exceptions.ConnectionError("Error 111 connecting to localhost:6380."), "unreachable"),
    (SCHEMA_ERROR, "check failed (see the API log)"),
    (redis.exceptions.AuthenticationError("invalid username-password pair"), "check failed (see the API log)"),
    (ValueError("unexpected reply"), "check failed (see the API log)"),
], ids=["os", "db-connection", "redis-connection", "db-schema", "redis-auth", "other"])
async def test_only_a_dependency_that_could_not_be_reached_is_called_unreachable(error: Exception,
                                                                                public: str) -> None:
    """Regression: every failure said "unreachable", so a missing alembic_version table or a rejected password sent
    the operator debugging the network. Through the single-flight caches too, which re-raise a cached failure."""
    async def direct() -> None:
        raise error

    probes = {"direct": direct, "async-cached": AsyncSingleFlightCheck(CountingProbe(error=error), ttl_s=10).get,
              "thread-cached": SingleFlightCheck(CountingCheck(error=error), ttl_s=10).get}
    for name, probe in probes.items():
        ok, value, _ = await health._timed(name, probe)
        assert (ok, value) == (False, public), name


# --- AsyncSingleFlightCheck (no services) -----------------------------------------------------------------------------
class CountingProbe:
    """An async check that counts its runs."""

    def __init__(self, sleep_s: float = 0.0, error: Exception | None = None):
        self.sleep_s, self.error, self.calls = sleep_s, error, 0

    async def __call__(self) -> str:
        self.calls += 1
        await anyio.sleep(self.sleep_s)
        if self.error is not None:
            raise self.error
        return "v"


async def test_async_check_is_shared_and_cached() -> None:
    fn = CountingProbe(sleep_s=0.1)
    check = AsyncSingleFlightCheck(fn, ttl_s=10, timeout_s=5)
    results: list[object] = []

    async def call() -> None:
        results.append(await check.get())

    async with anyio.create_task_group() as tg:
        for _ in range(20):
            tg.start_soon(call)
    assert results == ["v"] * 20 and fn.calls == 1
    assert await check.get() == "v" and fn.calls == 1


async def test_async_check_failures_are_cached() -> None:
    fn = CountingProbe(error=ConnectionError("db down"))
    check = AsyncSingleFlightCheck(fn, ttl_s=10, timeout_s=5)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="db down"):
            await check.get()
    assert fn.calls == 1


async def test_callers_that_give_up_do_not_start_a_second_run() -> None:
    fn = CountingProbe(sleep_s=0.4)
    check = AsyncSingleFlightCheck(fn, ttl_s=10, timeout_s=5)
    for _ in range(3):
        with pytest.raises(TimeoutError), anyio.fail_after(0.05):
            await check.get()
    with anyio.fail_after(2):
        assert await check.get() == "v"          # joins the run still in flight
    assert fn.calls == 1


async def test_a_run_past_its_time_limit_is_not_cached() -> None:
    """A hung probe is abandoned at the limit and the next caller probes again, so a DB that recovers shows up at
    once instead of after a TCP timeout."""
    fn = CountingProbe(sleep_s=10)
    check = AsyncSingleFlightCheck(fn, ttl_s=10, timeout_s=0.05)
    for expected_calls in (1, 2):
        with pytest.raises(TimeoutError), anyio.fail_after(2):
            await check.get()
        assert fn.calls == expected_calls
