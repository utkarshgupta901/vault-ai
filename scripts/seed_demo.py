"""Seed the demo user's vault with the pre-loaded corpus and wait until it is fully processed.

    uv run python scripts/seed_demo.py [--api http://localhost:8000] [--key vault-demo-key]

Uploads the five pre-seeded corpus files (handbook, board update, MSA, both proposals) plus any real document in
demo_docs/user/, then waits until each is ready AND its insights are done, and prints a table.

Deliberately NOT uploaded: the call notes and the scanned receipt (uploaded live in the demo: a pre-seeded copy would
turn the live upload into a boring `duplicate: true`) and the 120-page stress manual (crash-recovery drill only).

Idempotent: uploads are deduplicated by sha256 per owner, so re-running costs nothing and returns the same documents.
Exit code 1 if any document failed or did not finish in time."""

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from evals.api_client import ApiError, VaultClient  # noqa: E402
from evals.corpus import DEMO_DOCS, seed_paths  # noqa: E402

COLUMNS = ("file", "upload", "status", "pages", "chunks", "insights", "category", "date")


def row(name: str, doc: dict[str, Any]) -> tuple[str, ...]:
    status = doc.get("status") or "?"
    if doc.get("error"):
        status = f"{status} ({doc['error'].get('code')})"
    return (name, "duplicate" if doc.get("duplicate") else "new", status, str(doc.get("page_count") or "-"),
            str(doc.get("chunk_count") or "-"), doc.get("insights_status") or "-", doc.get("category") or "-",
            str(doc.get("document_date") or "-"))


def print_table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(COLUMNS)]
    print("  ".join(c.ljust(w) for c, w in zip(COLUMNS, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)))


def is_done(doc: dict[str, Any]) -> bool:
    return doc.get("status") == "ready" and doc.get("insights_status") == "ready"


def main(argv: list[str] | None = None, *, transport: Any = None) -> int:
    p = argparse.ArgumentParser(description="Seed the demo corpus for the demo user.")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--key", default="vault-demo-key")
    p.add_argument("--docs-dir", type=Path, default=DEMO_DOCS)
    p.add_argument("--timeout", type=float, default=900, help="seconds to wait for processing + insights")
    args = p.parse_args(argv)

    paths = seed_paths(args.docs_dir)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print(f"missing corpus files (run scripts/make_demo_docs.py first): {missing}", file=sys.stderr)
        return 1
    print(f"seeding {len(paths)} documents to {args.api} ...")

    def on_change(doc: dict[str, Any]) -> None:
        print(f"  {doc['filename']}: {doc['status']}{'/' + doc['stage'] if doc.get('stage') else ''}"
              f" insights={doc.get('insights_status')}")

    try:
        with VaultClient(args.api, args.key, transport=transport) as client:
            docs = client.ensure_uploaded(paths, need_insights=True, timeout_s=args.timeout, on_change=on_change)
    except (ApiError, httpx.HTTPError) as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    print()
    print_table([row(name, doc) for name, doc in docs.items()])
    incomplete = [name for name, doc in docs.items() if not is_done(doc)]
    if incomplete:
        print(f"\nnot fully processed: {incomplete}", file=sys.stderr)
        return 1
    print(f"\nall {len(docs)} documents ready with insights")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
