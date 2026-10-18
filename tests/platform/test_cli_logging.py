"""app.cli reports through structlog (project rule: no print in app code), and `python -m app.cli bootstrap` still
shows the operator every step, at the default LOG_LEVEL, without ever printing an API key. The run is a fresh
interpreter calling cli.main() with the database, the migrations and the bucket stubbed: nothing here touches a stack."""

import ast
import os
import subprocess
import sys
from pathlib import Path

from app.config import settings

ROOT = Path(__file__).resolve().parents[2]

BOOTSTRAP = """
from contextlib import contextmanager
from app import cli

class NoUsersYet:
    def scalar(self, stmt): return None
    def add(self, user): pass

@contextmanager
def session():
    yield NoUsersYet()

attempts = []
def bucket_after_one_retry():
    attempts.append(1)
    if len(attempts) == 1:
        raise ConnectionError("object storage is still starting")

cli.migrate = lambda: None
cli.storage.ensure_bucket = bucket_after_one_retry
cli.time.sleep = lambda seconds: None
cli.sync_session = session
cli.main(["app.cli", "bootstrap"])
"""


def test_cli_has_no_print_calls() -> None:
    tree = ast.parse((ROOT / "app" / "cli.py").read_text())
    calls = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"]
    assert calls == []


def test_bootstrap_still_shows_every_step() -> None:
    env = os.environ | {"LOG_LEVEL": "INFO", "LOG_JSON": "false"}     # the CLI's defaults (the suite runs at WARNING)
    run = subprocess.run([sys.executable, "-c", BOOTSTRAP], cwd=ROOT, env=env, capture_output=True, text=True,
                         check=True, timeout=60)
    output = run.stdout + run.stderr
    for event in ("migrations_at_head", "waiting_for_object_storage", "bucket_ready", "users_seeded"):
        assert event in output, output
    assert "ConnectionError" in output and settings.S3_BUCKET in output
    assert "demo" in output and "eval" in output
    for key in (settings.SEED_DEMO_API_KEY, settings.SEED_OTHER_API_KEY, settings.SEED_EVAL_API_KEY):
        assert key not in output
