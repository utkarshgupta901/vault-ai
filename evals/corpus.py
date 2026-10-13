"""The demo corpus: which files exist, which are pre-seeded, and how to read their text back.

One source of truth for file names, used by the generator (scripts/make_demo_docs.py), the seed/doctor scripts and the
eval runner, so a rename can never leave one of them uploading a file the others don't know about."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_DOCS = ROOT / "demo_docs"
USER_DOCS = DEMO_DOCS / "user"          # the user's real document(s); gitignored, never generated

HANDBOOK = "Larkspur_Employee_Handbook_2026.pdf"
BOARD_UPDATE = "Q3_2026_Board_Update.pdf"
MSA = "Brightline_MSA.docx"
BRIGHTPATH = "Proposal_BrightPath.pdf"
CORELINK = "Proposal_CoreLink.pdf"
CALL_NOTES = "Vendor_Call_Notes_2026-09-14.txt"
RECEIPT = "Scanned_Receipt.pdf"
STRESS_MANUAL = "Stress_Test_Manual.pdf"

# Pre-seeded for the demo user (and uploaded for the eval user). The others are uploaded live during the demo
# (call notes, receipt) or only for the crash-recovery drill (stress manual), so they must NOT be pre-seeded:
# re-uploading identical bytes would just return `duplicate: true` and spoil the live beat.
SEED_FILES = (HANDBOOK, BOARD_UPDATE, MSA, BRIGHTPATH, CORELINK)
LIVE_UPLOADS = (CALL_NOTES, RECEIPT)
ALL_FILES = SEED_FILES + LIVE_UPLOADS + (STRESS_MANUAL,)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}


def user_files(docs_dir: Path = DEMO_DOCS) -> list[Path]:
    """Real documents the user dropped into <docs_dir>/user/ (sorted, so seeding order is stable)."""
    folder = docs_dir / "user"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)


def resolve(filename: str, docs_dir: Path = DEMO_DOCS) -> Path | None:
    """Find a corpus file by name: generated files first, then the user's folder."""
    for candidate in (docs_dir / filename, docs_dir / "user" / filename):
        if candidate.is_file():
            return candidate
    return None


def seed_paths(docs_dir: Path = DEMO_DOCS) -> list[Path]:
    return [docs_dir / name for name in SEED_FILES] + user_files(docs_dir)


def extract_pages(path: Path) -> list[str]:
    """Plain text per page (PDF), per document (DOCX) or per file (TXT/MD), read the way the ingestion pipeline does
    (pypdf / python-docx). Used by tests and the generator's self-check to prove planted facts are extractable."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return [page.extract_text() or "" for page in PdfReader(path).pages]
    if suffix == ".docx":
        import docx

        return ["\n".join(p.text for p in docx.Document(str(path)).paragraphs)]
    return [path.read_text(encoding="utf-8")]
