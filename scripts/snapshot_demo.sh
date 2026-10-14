#!/usr/bin/env bash
# Freeze the demo state so demo day restores it instead of regenerating it (answers can drift; a snapshot can't).
#
#   scripts/snapshot_demo.sh [OUT_DIR]        # default: demo_snapshot/ (gitignored)
#
# Writes OUT_DIR/vault.dump (pg_dump custom format of the whole vault database: documents, chunks + vectors,
# insights, chat sessions, usage) and OUT_DIR/s3/ (the original uploaded file of every document in the database).
# pg_dump runs INSIDE the postgres container, so the client version always matches the server (17).
# The new snapshot is built in a temp folder and only swapped in when complete: a failed run never destroys the
# previous good snapshot. Run from anywhere; it cds to the repo root so `docker compose` finds compose.yaml, whose
# `name: vault-ai` targets the same compose project from the main checkout or from any git worktree.
#
# A snapshot is what demo day restores, so whatever is in it ends up on screen. It refuses (exit 1, the previous
# snapshot untouched) unless all of these hold; SNAPSHOT_ALLOW_UNCLEAN=1 prints the problem and snapshots anyway:
#   - `scripts/pre_demo.py --check` exits 0 against the API ($API, default http://localhost:8000): no vault holds non-corpus content (a document
#     `make pre-demo` would delete, a document only trusted because of a stray demo_docs/ file, or a chat that uses
#     or mentions one). Exit 2 (a vault could not be read, e.g. a rejected key) refuses too;
#   - the users table holds only the seeded users that pre_demo.py checks (demo, eval, other);
#   - every document's S3 object is downloaded: a document whose object is missing refuses (a restore could not
#     serve the file).
# Only the documents' storage keys are downloaded, so an object that no document points at (one a failed S3 delete
# left behind: the API logs it and still answers 204) is left out of the snapshot and named by s3_sync; it is not a
# reason to refuse.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/demo_snapshot}"
OUT="${OUT%/}"
DB="${SNAPSHOT_DB:-vault}"
SEEDED_USERS="'demo', 'eval', 'other'"

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp -d "$(dirname "$OUT")/.snapshot.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT"

unclean() {   # $1: what is wrong. Refuses, or warns and goes on when SNAPSHOT_ALLOW_UNCLEAN=1.
  if [ "${SNAPSHOT_ALLOW_UNCLEAN:-0}" = "1" ]; then
    echo "WARNING: $1; snapshotting anyway because SNAPSHOT_ALLOW_UNCLEAN=1" >&2
    return
  fi
  echo "REFUSING TO SNAPSHOT: $1." >&2
  echo "Review with 'make pre-demo', clean with the apply command it prints, then snapshot again" >&2
  echo "(or SNAPSHOT_ALLOW_UNCLEAN=1 make snapshot-demo to snapshot as is)." >&2
  exit 1
}

sql() {   # one read-only query, one value per line
  docker compose exec -T postgres psql -U vault -d "$DB" -X -A -t -v ON_ERROR_STOP=1 -c "$1"
}

echo "0/3 checking that the vaults hold only the demo corpus (scripts/pre_demo.py --check) ..."
check=0
uv run python scripts/pre_demo.py --check --api "${API:-http://localhost:8000}" || check=$?
case "$check" in
  0) ;;
  1) unclean "the vaults still hold non-corpus documents or chats (plan above)" ;;
  *) unclean "could not check the vaults (pre_demo.py exit $check: is the API up at ${API:-http://localhost:8000}, is every seeded key accepted?)" ;;
esac
others="$(sql "SELECT name FROM users WHERE name NOT IN ($SEEDED_USERS) ORDER BY name" | paste -sd, -)"
if [ -n "$others" ]; then
  unclean "the database has users that pre_demo.py does not check: $others"
fi

echo "1/3 pg_dump database '$DB' ..."
docker compose exec -T postgres pg_dump -U vault -d "$DB" --format=custom > "$TMP/vault.dump"

echo "2/3 downloading the S3 object of every document ..."
sql "SELECT storage_key FROM documents" | LC_ALL=C sort -u > "$TMP/keys.txt"
uv run python scripts/s3_sync.py download "$TMP/s3" --keys "$TMP/keys.txt"
(cd "$TMP/s3" && find . -type f ! -name .manifest.json | sed 's|^\./||' | LC_ALL=C sort) > "$TMP/got.txt"
missing="$(LC_ALL=C comm -23 "$TMP/keys.txt" "$TMP/got.txt" | paste -sd, -)"
rm -f "$TMP/keys.txt" "$TMP/got.txt"
if [ -n "$missing" ]; then
  unclean "documents whose S3 object is missing (a restore could not serve the file): $missing"
fi

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
