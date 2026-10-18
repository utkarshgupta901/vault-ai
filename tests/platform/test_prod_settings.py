"""APP_ENV=prod refuses to run with this repo's public demo secrets: the seeded API keys (the README, .env.example
and the dashboard all print them) and the local RustFS credentials. local (compose) and test keep their defaults.

render.yaml gives the API service the S3 keys and the three seed keys, and the worker only the S3 keys (it never
seeds users): both must still start once the operator fills in real values."""

from typing import Any

import pytest
from pydantic import ValidationError

from app import cli
from app.config import Settings

S3_KEYS = ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
SEED_KEYS = ("SEED_DEMO_API_KEY", "SEED_OTHER_API_KEY", "SEED_EVAL_API_KEY")
REAL_S3 = {"S3_ACCESS_KEY_ID": "r2-access-key-id", "S3_SECRET_ACCESS_KEY": "r2-secret-not-in-the-repo"}
REAL_SEEDS = {"SEED_DEMO_API_KEY": "demo-key-not-in-the-repo", "SEED_OTHER_API_KEY": "other-key-not-in-the-repo",
              "SEED_EVAL_API_KEY": "eval-key-not-in-the-repo"}


@pytest.fixture(autouse=True)
def _only_what_each_test_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the shell nor a .env file may supply these (settings() also skips .env)."""
    for name in (*S3_KEYS, *SEED_KEYS):
        monkeypatch.delenv(name, raising=False)


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("env", ["local", "test"])
def test_local_and_test_keep_the_demo_defaults(env: str) -> None:
    s = settings(APP_ENV=env)
    assert (s.SEED_DEMO_API_KEY, s.S3_ACCESS_KEY_ID) == ("vault-demo-key", "vaultlocal")


def test_prod_api_with_real_values_starts() -> None:
    s = settings(APP_ENV="prod", **REAL_S3, **REAL_SEEDS)
    assert s.SEED_DEMO_API_KEY == REAL_SEEDS["SEED_DEMO_API_KEY"]


def test_prod_worker_without_seed_keys_starts() -> None:
    settings(APP_ENV="prod", **REAL_S3)          # render.yaml's vault-worker: S3 keys only


@pytest.mark.parametrize("name", [*S3_KEYS, *SEED_KEYS])
@pytest.mark.parametrize("kind", ["repo default", "empty"])
def test_prod_refuses_a_public_or_empty_secret(name: str, kind: str) -> None:
    value = Settings.model_fields[name].default if kind == "repo default" else ""
    with pytest.raises(ValidationError, match=name) as exc_info:
        settings(APP_ENV="prod", **(REAL_S3 | REAL_SEEDS | {name: value}))
    message = str(exc_info.value)
    assert not any(secret in message for secret in [*REAL_S3.values(), *REAL_SEEDS.values()])   # never echoed


def test_prod_refuses_unset_s3_keys() -> None:
    with pytest.raises(ValidationError, match="S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY"):
        settings(APP_ENV="prod", **REAL_SEEDS)


def test_seeding_in_prod_refuses_unset_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API service that was given no seed key at all looks like the worker and starts, but its deploy step
    (`python -m app.cli bootstrap`) must not seed the public defaults as working prod keys."""
    monkeypatch.setattr(cli, "settings", settings(APP_ENV="prod", **REAL_S3, SEED_EVAL_API_KEY="eval-real"))

    def no_db() -> None:
        raise AssertionError("refused only after opening the database")

    monkeypatch.setattr(cli, "sync_session", no_db)
    with pytest.raises(SystemExit, match="SEED_DEMO_API_KEY, SEED_OTHER_API_KEY"):
        cli.seed_users()
