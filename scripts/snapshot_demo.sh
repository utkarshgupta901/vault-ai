#!/usr/bin/env bash
# Freeze the demo state so demo day restores it instead of regenerating it (answers can drift; a snapshot can't).
#
#   scripts/snapshot_demo.sh [OUT_DIR]        # default: demo_snapshot/ (gitignored)
#
# Writes OUT_DIR/vault.dump (pg_dump custom format of the whole vault database: documents, chunks + vectors,
# insights, chat sessions, usage) and OUT_DIR/s3/ (every object in the bucket: the original uploaded files).
# pg_dump runs INSIDE the postgres container, so the client version always matches the server (17).
# The new snapshot is built in a temp folder and only swapped in when complete: a failed run never destroys the
# previous good snapshot. Run from anywhere; it cds to the repo root so `docker compose` finds the project.
# The compose project name comes from the checkout's folder name (compose.yaml has no `name:`), so run this from
# the main checkout, or set COMPOSE_PROJECT_NAME=vault-ai when running it from a git worktree.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/demo_snapshot}"
OUT="${OUT%/}"
DB="${SNAPSHOT_DB:-vault}"

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp -d "$(dirname "$OUT")/.snapshot.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT"

echo "1/3 pg_dump database '$DB' ..."
docker compose exec -T postgres pg_dump -U vault -d "$DB" --format=custom > "$TMP/vault.dump"

echo "2/3 downloading S3 objects ..."
uv run python scripts/s3_sync.py download "$TMP/s3"

echo "3/3 writing SNAPSHOT.txt ..."
{
  echo "created_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "git: $(git describe --always --dirty 2>/dev/null || echo unknown)"
  echo "database: $DB ($(wc -c < "$TMP/vault.dump" | tr -d ' ') bytes)"
  echo "objects: $(find "$TMP/s3" -type f ! -name .manifest.json | wc -l | tr -d ' ')"
} > "$TMP/SNAPSHOT.txt"

rm -rf "$OUT.previous"
if [ -d "$OUT" ]; then mv "$OUT" "$OUT.previous"; fi
mv "$TMP" "$OUT"
rm -rf "$OUT.previous"
trap - EXIT
echo "snapshot ready in $OUT"
cat "$OUT/SNAPSHOT.txt"
