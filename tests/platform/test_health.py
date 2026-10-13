"""/health/ready stays bounded under concurrency and can no longer wedge the broker pool (incident 2026-10-13).

Before the fix, ~10 overlapping readiness calls each held a connection from Celery's broker pool and waited forever
for a second one; every later publish (upload enqueue) then hung too. These tests fire the same load at the
in-process app against the test broker (Valkey db15, no workers) and then publish a real task."""

import threading
import time
from collections.abc import Iterator
from functools import partial

import anyio
import pytest
import redis
from httpx import AsyncClient

from app.api import health
from app.api.health import SingleFlightCheck
from app.config import settings
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
    health.workers_check.reset()
    health.storage_check.reset()
    yield
    health.workers_check.reset()
    health.storage_check.reset()


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


def test_ai_check_reports_consecutive_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ai as ai

    monkeypatch.setattr(ai, "_consecutive_failures", 0)
    assert health._ai_check()["ok"] is True
    monkeypatch.setattr(ai, "_consecutive_failures", ai.PROVIDER_RESET_AFTER_FAILURES)
    check = health._ai_check()
    assert check["ok"] is False
    assert check["consecutive_failures"] == ai.PROVIDER_RESET_AFTER_FAILURES
    assert "failed" in check["error"]
