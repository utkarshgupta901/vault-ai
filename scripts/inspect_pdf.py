"""Debug helper: show what the extractor sees, page by page (label + the first 120 characters of text).

Use it when a citation label looks wrong or a PDF fails with NO_EXTRACTABLE_TEXT. The file type is sniffed from the
bytes exactly as the upload does, so DOCX/TXT/MD files work too. Add --chunks to also print the chunk headers.

    uv run python scripts/inspect_pdf.py path/to/file.pdf [--chars 120] [--chunks]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.core.errors import AppError, PermanentError  # noqa: E402
from app.ingestion.chunking import chunk_pages, count_tokens  # noqa: E402
from app.ingestion.extract import extract  # noqa: E402
from app.ingestion.sniff import sniff  # noqa: E402


def _preview(text: str, chars: int) -> str:
    return " ".join(text.split())[:chars]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--chars", type=int, default=120, help="characters of text to show per page")
    parser.add_argument("--chunks", action="store_true", help="also list the chunks the pipeline would embed")
    args = parser.parse_args(argv)

    try:
        with args.path.open("rb") as f:
            mime = sniff(f, args.path.name).mime_type
        doc = extract(args.path, mime)
    except (AppError, PermanentError) as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1

    print(f"{args.path.name}  ({mime})\n")
    print(f"{'#':>4}  {'label':<24} {'chars':>6} {'tokens':>6}  text")
    for page in doc.pages:
        print(f"{page.page_no:>4}  {page.label[:24]:<24} {len(page.text):>6} {count_tokens(page.text):>6}  "
              f"{_preview(page.text, args.chars)}")
    print(f"\n{len(doc.pages)} pages; warnings: {', '.join(doc.warnings) or 'none'}")

    if args.chunks:
        title = args.path.stem.replace("_", " ").replace("-", " ")
        chunks = chunk_pages(doc.pages, title=title, model=settings.EMBED_MODEL, dims=settings.EMBED_DIMS)
        print(f"\n{len(chunks)} chunks (CHUNK_TOKENS={settings.CHUNK_TOKENS}, CHUNK_OVERLAP={settings.CHUNK_OVERLAP})")
        for c in chunks:
            print(f"{c.chunk_index:>4}  p{c.page_start:<4} {c.token_count:>4} tok  {c.header}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
