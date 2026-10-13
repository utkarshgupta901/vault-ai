"""Text extraction: turn an uploaded file into "pages", the unit a citation points at.

  PDF   -> one page per physical page, labelled with the PDF's own page labels ("p. iv", "p. 12")
  DOCX  -> one page per heading section ("§ Termination"); sections over ~1,500 tokens are split into parts
  MD    -> like DOCX, using '#' headings (a Markdown file without headings is treated as plain text)
  TXT   -> ~1,500-token pseudo-pages ("part 1", "part 2", ...) cut on paragraph boundaries

Failures that no retry can fix raise PermanentError with a stable code (ENCRYPTED_PDF, CORRUPT_FILE,
NO_EXTRACTABLE_TEXT, TOO_MANY_PAGES); the worker fails the document with that code and does not retry.
A valid document never yields an empty page list: "nothing to index" is NO_EXTRACTABLE_TEXT.
"""

import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import docx
import structlog
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from app.config import settings
from app.core.errors import ErrorCode, PermanentError
from app.ingestion.chunking import split_by_tokens
from app.ingestion.sniff import DOCX, MD, PDF, TXT

log = structlog.get_logger(__name__)

SECTION_TOKENS = 1500       # max size of a DOCX/MD section page or a TXT pseudo-page
MIN_PAGE_CHARS = 20         # a PDF page with less text than this has no usable text layer (scan, figure, blank)
LABEL_HEADING_CHARS = 60

# pypdf is lenient, but a malformed file can still surface as a plain Python error from deep inside the parser.
# Listed explicitly (not `except Exception`) so Celery's SoftTimeLimitExceeded is never mistaken for a corrupt file.
_PDF_PARSE_ERRORS = (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError)
# python-docx: not a package / missing part / broken XML (lxml's XMLSyntaxError is a SyntaxError).
_DOCX_PARSE_ERRORS = (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError, SyntaxError)

_NO_TEXT = "The document contains no extractable text."


@dataclass(frozen=True)
class PageText:
    page_no: int                # 1-based: physical page (PDF), section/part number (DOCX, MD, TXT)
    label: str                  # what a citation shows: "p. 4", "§ Termination", "part 2"
    heading_path: str | None    # "Leave > PTO" for DOCX/MD sections, None otherwise
    text: str


@dataclass
class ExtractedDoc:
    pages: list[PageText]
    warnings: list[str] = field(default_factory=list)


def extract(path: Path, mime: str) -> ExtractedDoc:
    """Dispatch on the sniffed MIME type (decided from the bytes at upload, never from the filename)."""
    if mime == PDF:
        return _extract_pdf(path)
    if mime == DOCX:
        return _extract_docx(path)
    if mime == MD:
        return _extract_markdown(_read_text(path))
    if mime == TXT:
        return _extract_plain_text(_read_text(path))
    raise PermanentError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, f"Unsupported file type: {mime}.")


def _clean(text: str) -> str:
    """Postgres TEXT cannot store NUL characters, and lone surrogates (seen with broken PDF fonts) cannot be encoded
    as UTF-8. Both would make the INSERT fail, so remove/replace them here, once, for every format."""
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def _one_line(text: str) -> str:
    return " ".join(text.split())


# --- PDF ----------------------------------------------------------------------------------------------------------


def _extract_pdf(path: Path) -> ExtractedDoc:
    reader = _open_pdf(path)
    try:
        page_count = len(reader.pages)
    except _PDF_PARSE_ERRORS as exc:
        raise PermanentError(ErrorCode.CORRUPT_FILE, "The PDF's page tree could not be read.") from exc
    if page_count == 0:
        raise PermanentError(ErrorCode.CORRUPT_FILE, "The PDF has no pages.")
    if page_count > settings.MAX_PAGES:
        raise PermanentError(ErrorCode.TOO_MANY_PAGES,
                             f"The PDF has {page_count} pages; the limit is {settings.MAX_PAGES}.")

    labels = _pdf_labels(reader, page_count)
    pages: list[PageText] = []
    without_text = 0
    for index, page in enumerate(reader.pages):
        text = _clean(_pdf_page_text(page, index + 1))
        if len(text.strip()) < MIN_PAGE_CHARS:
            without_text += 1
        pages.append(PageText(page_no=index + 1, label=f"p. {labels[index]}", heading_path=None, text=text))

    if without_text == page_count:
        raise PermanentError(ErrorCode.NO_EXTRACTABLE_TEXT,
                             "No extractable text: looks like a scanned/image-only PDF (OCR is not supported).")
    # Some pages scanned: index the rest, and tell the user how many pages chat can't see.
    warnings = [f"pages_without_text:{without_text}"] if without_text else []
    return ExtractedDoc(pages=pages, warnings=warnings)


def _open_pdf(path: Path) -> PdfReader:
    try:
        reader = PdfReader(path)
    except _PDF_PARSE_ERRORS as exc:
        raise PermanentError(ErrorCode.CORRUPT_FILE, "The PDF could not be read (corrupt or not a PDF).") from exc
    if reader.is_encrypted:
        # Many PDFs are "encrypted" only to restrict printing/copying, with an empty user password: try that.
        try:
            unlocked = reader.decrypt("")
        except (*_PDF_PARSE_ERRORS, DependencyError, NotImplementedError):
            unlocked = 0  # e.g. AES without the optional crypto library, or an unknown security handler
        if not unlocked:
            raise PermanentError(ErrorCode.ENCRYPTED_PDF,
                                 "The PDF is password-protected. Remove the password and upload it again.")
    return reader


def _pdf_labels(reader: PdfReader, page_count: int) -> list[str]:
    """The labels printed on the pages ("i", "ii", "1", "A-3") when the PDF defines them, else "1", "2", ...
    A citation "p. 4" then matches what the reader sees in their PDF viewer, not just the physical index."""
    try:
        labels = list(reader.page_labels)
    except _PDF_PARSE_ERRORS:
        labels = []
    if len(labels) != page_count:
        labels = []
    return [(labels[i].strip() if labels and labels[i].strip() else str(i + 1)) for i in range(page_count)]


def _pdf_page_text(page, page_no: int) -> str:
    try:
        return page.extract_text() or ""
    except _PDF_PARSE_ERRORS as exc:
        # One broken page must not sink the document: count it as a page without text.
        log.warning("pdf_page_unreadable", page=page_no, error=str(exc))
        return ""


# --- Sections (shared by DOCX and Markdown) --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Block:
    level: int | None   # heading level (0 = Title, 1 = Heading 1, ...); None for body text or a table
    text: str


@dataclass
class _Section:
    heading: str | None         # None = content before the first heading ("Intro")
    heading_path: str | None    # "Leave > PTO"
    body: list[str] = field(default_factory=list)


def _sections(blocks: list[_Block]) -> list[_Section]:
    """Group blocks under their heading. A heading stack turns levels into a breadcrumb: a new Heading 2 replaces the
    previous Heading 2 (and anything deeper) but stays under the current Heading 1."""
    sections = [_Section(heading=None, heading_path=None)]
    stack: list[tuple[int, str]] = []
    for block in blocks:
        if block.level is None:
            sections[-1].body.append(block.text)
            continue
        while stack and stack[-1][0] >= block.level:
            stack.pop()
        stack.append((block.level, block.text))
        sections.append(_Section(heading=block.text, heading_path=" > ".join(text for _, text in stack)))
    return sections


def _section_label(heading: str | None) -> str:
    if heading is None:
        return "Intro"
    short = heading if len(heading) <= LABEL_HEADING_CHARS else heading[: LABEL_HEADING_CHARS - 1].rstrip() + "…"
    return f"§ {short}"


def _pages_from_sections(sections: list[_Section]) -> ExtractedDoc:
    # A heading directly followed by a sub-heading has no text of its own; it lives on in its children's heading_path.
    # Exception: a document that is nothing but headings still has text, so keep those.
    kept = [s for s in sections if s.body] or [s for s in sections if s.heading]
    pages: list[PageText] = []
    for section in kept:
        # Split the body, then start every part with the heading, so each part reads as "Heading ..." in the page
        # viewer and a long first paragraph can never leave the heading alone as a one-word part.
        parts = split_by_tokens("\n\n".join(section.body), SECTION_TOKENS) or [""]
        label = _section_label(section.heading)
        for n, part in enumerate(parts, start=1):
            pages.append(PageText(
                page_no=len(pages) + 1,
                label=f"{label} (part {n})" if len(parts) > 1 else label,
                heading_path=section.heading_path,
                text="\n\n".join(piece for piece in (section.heading, part) if piece),
            ))
    if not pages:
        raise PermanentError(ErrorCode.NO_EXTRACTABLE_TEXT, _NO_TEXT)
    return ExtractedDoc(pages=pages)


# --- DOCX ---------------------------------------------------------------------------------------------------------


def _extract_docx(path: Path) -> ExtractedDoc:
    try:
        document = docx.Document(str(path))
        blocks = list(_docx_blocks(document))
    except _DOCX_PARSE_ERRORS as exc:
        raise PermanentError(ErrorCode.CORRUPT_FILE, "The Word document could not be read.") from exc
    return _pages_from_sections(_sections(blocks))


def _docx_blocks(document) -> Iterator[_Block]:
    """Body paragraphs AND tables, in document order (a table between two paragraphs stays between them).
    Headers, footers, footnotes and text boxes are not read."""
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            text = _table_text(item)
            if text:
                yield _Block(level=None, text=text)
            continue
        text = _clean(item.text).strip()
        if not text:
            continue
        level = _heading_level(item)
        yield _Block(level=level, text=_one_line(text) if level is not None else text)


def _heading_level(paragraph: Paragraph) -> int | None:
    """Word marks headings with paragraph styles: 'Title' -> 0, 'Heading 1' -> 1, 'Heading 2' -> 2, ..."""
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    if name.startswith("Title"):
        return 0
    if name.startswith("Heading"):
        digits = name.removeprefix("Heading").strip()
        return int(digits) if digits.isdigit() else 1
    return None


def _table_text(table: Table) -> str:
    """One line per row, cells separated by ' | ', so a row like 'Q3 | $4.2M | 61%' stays together in one chunk."""
    lines = []
    for row in table.rows:
        cells: list[str] = []
        previous = None
        for cell in row.cells:
            if cell is previous:  # a horizontally merged cell is returned once per grid column it spans
                continue
            previous = cell
            cells.append(_one_line(_clean(cell.text)))
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


# --- Markdown and plain text ----------------------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")   # ATX headings: "## Leave policy ##"
_MD_FENCE = re.compile(r"^\s*(```|~~~)")                   # inside a code fence, "# comment" is not a heading


def _read_text(path: Path) -> str:
    # The upload sniffer already required strict UTF-8; "utf-8-sig" drops a BOM, "replace" is belt and braces.
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    return _clean(text.replace("\r\n", "\n").replace("\r", "\n"))


def _markdown_blocks(text: str) -> list[_Block]:
    blocks: list[_Block] = []
    body: list[str] = []
    in_fence = False

    def flush_body() -> None:
        joined = "\n".join(body).strip()
        if joined:
            blocks.append(_Block(level=None, text=joined))
        body.clear()

    for line in text.split("\n"):
        if _MD_FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _MD_HEADING.match(line)
        if match:
            flush_body()
            blocks.append(_Block(level=len(match.group(1)), text=_one_line(match.group(2))))
        else:
            body.append(line)
    flush_body()
    return blocks


def _extract_markdown(text: str) -> ExtractedDoc:
    blocks = _markdown_blocks(text)
    if not any(block.level is not None for block in blocks):
        return _extract_plain_text(text)
    return _pages_from_sections(_sections(blocks))


def _extract_plain_text(text: str) -> ExtractedDoc:
    """Plain text has no pages, so make ~1,500-token pseudo-pages: small enough to show in the page viewer, cut at
    paragraph breaks where possible."""
    parts = split_by_tokens(text, SECTION_TOKENS)
    if not parts:
        raise PermanentError(ErrorCode.NO_EXTRACTABLE_TEXT, _NO_TEXT)
    return ExtractedDoc(pages=[
        PageText(page_no=n, label=f"part {n}", heading_path=None, text=part) for n, part in enumerate(parts, start=1)
    ])
