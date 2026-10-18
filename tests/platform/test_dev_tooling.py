"""Local tooling the demo and the test suite lean on: which host ports `make` and tests/conftest.py use, and that
`make wait` cannot hang. Every test runs a subprocess (`make -n`, or a fresh interpreter that imports tests.conftest
without running its fixtures), so nothing here starts, stops or talks to a stack."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
# Settings of the caller that would leak into the child: ports exported in this shell, or `make test API_PORT=...`
# (make hands its command-line variables down through MAKEFLAGS).
LEAKY = ("API", "API_PORT", "PG_PORT", "VALKEY_PORT", "S3_PORT", "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES")
needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def child_env(**extra: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in LEAKY} | extra


# --- tests/conftest.py ------------------------------------------------------------------------------------------
def harness_urls(**env: str) -> dict[str, str]:
    """The service URLs tests/conftest.py sets when a fresh interpreter imports it with these variables."""
    code = ("import json, os, tests.conftest; print(json.dumps({k: os.environ[k] for k in "
            "('DATABASE_URL', 'BROKER_URL', 'CACHE_URL', 'S3_ENDPOINT_URL')}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=child_env(**env), capture_output=True,
                         text=True, check=True, timeout=60)
    return json.loads(out.stdout.splitlines()[-1])


def hosts(urls: dict[str, str]) -> set[str]:
    return {re.search(r"(?:@|//)(localhost:\d+)", url).group(1) for url in urls.values()}


def test_harness_follows_the_ports_compose_publishes_on() -> None:
    # `export ...; make up; make test` with the ports moved: the suite must TRUNCATE / FLUSHDB the stack that make up
    # started, never whatever else holds the default ports on this machine.
    urls = harness_urls(PG_PORT="15433", VALKEY_PORT="16380", S3_PORT="19200")
    assert "@localhost:15433/" in urls["DATABASE_URL"]
    assert urls["BROKER_URL"].startswith("redis://localhost:16380/")
    assert urls["CACHE_URL"].startswith("redis://localhost:16380/")
    assert urls["S3_ENDPOINT_URL"] == "http://localhost:19200"


def test_harness_defaults_are_compose_defaults() -> None:
    # Empty counts as unset, as in compose's ${PG_PORT:-5433}.
    assert hosts(harness_urls(PG_PORT="")) == {"localhost:5433", "localhost:6380", "localhost:9200"}


# --- compose.yaml -----------------------------------------------------------------------------------------------
def published_ports() -> dict[str, list[str]]:
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    return {name: svc.get("ports", []) for name, svc in services.items()}


def test_stateful_services_are_published_on_loopback_only() -> None:
    # Postgres, Valkey (the Celery broker) and the object store run with the repo's public credentials: published on
    # all interfaces, anyone on the same Wi-Fi could read or write them. Host-side tools all use localhost.
    ports = published_ports()
    assert ports["postgres"] == ["127.0.0.1:${PG_PORT:-5433}:5432"]
    assert ports["valkey"] == ["127.0.0.1:${VALKEY_PORT:-6380}:6379"]
    assert ports["s3"] == ["127.0.0.1:${S3_PORT:-9200}:9000"]
    assert ports["api"] == ["${API_PORT:-8000}:8000"]               # the API (behind its API keys) is unchanged


# --- .github/workflows/ci.yml -----------------------------------------------------------------------------------
def test_ci_keeps_the_tokenizer_cache_outside_the_checkout() -> None:
    """An untracked .cache/ inside the checkout (it is not gitignored) makes the tree dirty for anything that checks it
    later, such as `git describe --dirty` or the eval runner's git_dirty(). A job-level env cannot read the runner
    context (runner.temp), so the path is a fixed one outside the workspace."""
    job = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["test"]
    cache_dir = job["env"]["TIKTOKEN_CACHE_DIR"]
    assert cache_dir.startswith("/") and "${{" not in cache_dir, cache_dir
    cache_step = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/cache"))
    assert cache_step["with"]["path"] == "${{ env.TIKTOKEN_CACHE_DIR }}"


# --- Makefile ---------------------------------------------------------------------------------------------------
def make_dry_run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """`make -n` on a copy of the Makefile in an empty folder. Recipes are printed, not run (a recursive $(MAKE) line
    runs, but with -n too); with no compose.yaml there and COMPOSE=false, nothing could reach a stack anyway."""
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    return subprocess.run(["make", "-n", "--no-print-directory", "-C", str(tmp_path), *args, "COMPOSE=false"],
                          env=child_env(**env), capture_output=True, text=True, timeout=30)


@needs_make
@pytest.mark.parametrize(("env", "args", "expected"), [
    ({}, [], "http://localhost:8000"),
    ({"API_PORT": "18000"}, [], "http://localhost:18000"),
    ({}, ["API_PORT=18000"], "http://localhost:18000"),
    ({"API_PORT": ""}, [], "http://localhost:8000"),                  # empty = unset, as compose's ${API_PORT:-8000}
    ({"API": "https://stray.example"}, [], "http://localhost:8000"),  # a stray shell variable is not a target...
    ({}, ["API=https://vault.example"], "https://vault.example"),     # ...the command line is
])
def test_client_targets_url(tmp_path: Path, env: dict[str, str], args: list[str], expected: str) -> None:
    result = make_dry_run(tmp_path, "smoke", *args, **env)
    assert result.returncode == 0, result.stderr
    assert re.search(r"--api (\S+)", result.stdout).group(1) == expected


@needs_make
@pytest.mark.parametrize("goal", ["up", "demo", "demo-luna"])
def test_stack_targets_refuse_api_url(tmp_path: Path, goal: str) -> None:
    # API= does not move the published port: compose would still publish 8000 while `make wait` polled the URL for
    # 2 minutes and failed. Refused before compose runs, naming the variable that does move it.
    result = make_dry_run(tmp_path, goal, "API=http://localhost:18000")
    assert result.returncode != 0 and "API_PORT" in result.stderr
    assert "up -d" not in result.stdout


@needs_make
def test_stack_targets_wait_on_the_moved_port(tmp_path: Path) -> None:
    result = make_dry_run(tmp_path, "demo", "API_PORT=18000")
    assert result.returncode == 0, result.stderr
    assert "http://localhost:18000/health/live" in result.stdout


@needs_make
def test_wait_bounds_every_request(tmp_path: Path) -> None:
    # A process that accepts the connection and never answers (a stuck API, another service on the port) would block
    # an unbounded curl forever, and the retry loop around it would never advance or time out.
    curls = re.findall(r"curl [^|;]*", make_dry_run(tmp_path, "wait").stdout)
    assert curls and all("--max-time" in c for c in curls), curls
