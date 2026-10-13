#!/bin/sh
# Free-tier fallback: API + Celery worker in ONE container (e.g. a single free Render web service, where background
# workers and preDeployCommand are not available). Migrations run at start instead of pre-deploy.
#
# Trade-offs (say them out loud): the worker shares the web instance's CPU/RAM, it stops when the free instance spins
# down after 15 min idle (queued tasks wait in the broker), and there is no independent scaling. Use render.yaml's
# separate worker service for anything beyond a demo.
set -e
python -m app.cli bootstrap
# --pool=solo: one task at a time in the worker process itself (no forked children) to keep memory low.
celery -A app.worker.celery_app worker -Q ingest,ai --pool=solo --loglevel=INFO &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
