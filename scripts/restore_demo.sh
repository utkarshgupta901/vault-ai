#!/usr/bin/env bash
# Restore a demo snapshot made by scripts/snapshot_demo.sh. THIS REPLACES THE DATABASE CONTENTS.
#
#   scripts/restore_demo.sh [SNAPSHOT_DIR] [--yes]     # default: demo_snapshot/
#
# 1. pg_restore --clean --if-exists into the vault database, in a single transaction: either the whole snapshot is
#    restored or nothing changes (a half-restored demo database is worse than the old one).
# 2. Re-uploads every snapshot object to the bucket (the files that the restored rows point at).
# Asks for confirmation unless --yes (or YES=1) is given. Run `make doctor` afterwards.
# The compose project name comes from the checkout's folder name (compose.yaml has no `name:`), so run this from
# the main checkout, or set COMPOSE_PROJECT_NAME=vault-ai when running it from a git worktree.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAP="$ROOT/demo_snapshot"
ASSUME_YES="${YES:-0}"
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) SNAP="${arg%/}" ;;
  esac
done
DB="${RESTORE_DB:-vault}"

if [ ! -f "$SNAP/vault.dump" ] || [ ! -d "$SNAP/s3" ]; then
  echo "no snapshot found in $SNAP (expected vault.dump and s3/); run scripts/snapshot_demo.sh first" >&2
  exit 1
fi
[ -f "$SNAP/SNAPSHOT.txt" ] && cat "$SNAP/SNAPSHOT.txt"

if [ "$ASSUME_YES" != "1" ]; then
  read -r -p "Replace ALL data in database '$DB' with this snapshot? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "aborted"; exit 1 ;; esac
fi

cd "$ROOT"
echo "1/2 pg_restore into '$DB' ..."
docker compose exec -T postgres pg_restore -U vault -d "$DB" --clean --if-exists --no-owner --single-transaction \
  < "$SNAP/vault.dump"

echo "2/2 uploading S3 objects ..."
uv run python scripts/s3_sync.py upload "$SNAP/s3"
echo "restore complete; now run: make doctor"
