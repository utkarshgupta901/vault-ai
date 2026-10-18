"""Operational commands.

    python -m app.cli bootstrap   # alembic upgrade head + ensure bucket + seed API users (the `migrate` service)
    python -m app.cli migrate     # alembic upgrade head only
    python -m app.cli seed-users
"""

import sys
import time
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.config import settings
from app.core.auth import hash_api_key
from app.core.db import sync_session
from app.core.logging import configure_logging
from app.models import User
from app.services import storage

log = structlog.get_logger(__name__)   # main() configures it first: readable lines locally, JSON in prod
ROOT = Path(__file__).resolve().parents[1]

# name -> (settings attribute holding the key, daily cost limit USD, requests/min)
SEED_USERS = {
    "demo": ("SEED_DEMO_API_KEY", 20.00, 60),
    "other": ("SEED_OTHER_API_KEY", 2.00, 30),
    "eval": ("SEED_EVAL_API_KEY", 50.00, 600),
}


def migrate(database_url: str | None = None) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["skip_logging_config"] = True
    if database_url:
        cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")


def ensure_bucket(retries: int = 20) -> None:
    for attempt in range(retries):
        try:
            storage.ensure_bucket()
            return
        except Exception as exc:  # noqa: BLE001 - object store may still be starting
            if attempt == retries - 1:
                raise
            log.info("waiting_for_object_storage", error=exc.__class__.__name__, attempt=attempt + 1)
            time.sleep(1.5)


def seed_users() -> None:
    if settings.APP_ENV == "prod":
        # Settings only checks the seed keys a process was given (the worker gets none), so an API service missing
        # them still starts; seeding the public defaults here would turn them into working prod keys.
        public = settings.public_secrets(key_attr for key_attr, _, _ in SEED_USERS.values())
        if public:
            raise SystemExit(f"Refusing to seed users in prod with empty or repo-default keys: {', '.join(public)}")
    with sync_session() as s:
        for name, (key_attr, limit, rpm) in SEED_USERS.items():
            key_hash = hash_api_key(getattr(settings, key_attr))
            user = s.scalar(select(User).where(User.name == name))
            if user is None:
                s.add(User(name=name, api_key_hash=key_hash, daily_cost_limit_usd=limit, rate_limit_per_min=rpm))
            else:
                user.api_key_hash, user.daily_cost_limit_usd, user.rate_limit_per_min = key_hash, limit, rpm


def main(argv: list[str]) -> None:
    configure_logging()
    cmd = argv[1] if len(argv) > 1 else "bootstrap"
    if cmd in ("bootstrap", "migrate"):
        migrate()
        log.info("migrations_at_head")
    if cmd in ("bootstrap",):
        ensure_bucket()
        log.info("bucket_ready", bucket=settings.S3_BUCKET)
    if cmd in ("bootstrap", "seed-users"):
        seed_users()
        log.info("users_seeded", users=list(SEED_USERS))     # names only: never the keys


if __name__ == "__main__":
    main(sys.argv)
