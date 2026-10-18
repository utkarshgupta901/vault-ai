"""Text extraction: turn an uploaded file into "pages", the unit a citation points at.

  PDF   -> one page per physical page, labelled with the PDF's own page labels ("p. iv", "p. 12")
  DOCX  -> one page per heading section ("§ Termination"); sections over ~1,500 tokens are split into parts
  MD    -> like DOCX, using '#' headings (a Markdown file without headings is treated as plain text)
  TXT   -> ~1,500-token pseudo-pages ("part 1", "part 2", ...) cut on paragraph boundaries

Failures that no retry can fix raise PermanentError with a stable code (ENCRYPTED_PDF, CORRUPT_FILE,
NO_EXTRACTABLE_TEXT, TOO_MANY_PAGES for a PDF over MAX_PAGES, TOO_MUCH_TEXT for a TXT/MD/DOCX over MAX_PAGES pages'
worth of text, UNSAFE_ARCHIVE for a DOCX whose archive or XML is too big to open safely); the worker fails the document
with that code and does not retry.
A valid document never yields an empty page list: "nothing to index" is NO_EXTRACTABLE_TEXT.
"""

import re
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import docx
import structlog
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree
from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from app.config import settings
from app.core.errors import ErrorCode, PermanentError
from app.ingestion.chunking import count_tokens, split_by_tokens
from app.ingestion.sniff import DOCX, MAX_ENTRY_UNCOMPRESSED, MAX_UNCOMPRESSED, MD, PDF, TXT

log = structlog.get_logger(__name__)

SECTION_TOKENS = 1500       # max size of a DOCX/MD section page or a TXT pseudo-page
MAX_HEADING_TOKENS = 64     # a longer "heading" is a body paragraph that was styled as one (see _sections)
MIN_PAGE_CHARS = 20         # next to fuller pages, a PDF page with less text is a scan, figure or blank (warned about)
LABEL_HEADING_CHARS = 60
# Only PDFs have a page count to cap (settings.MAX_PAGES), so a TXT, MD or DOCX gets the same budget as text: MAX_PAGES
# pages of 5,000 characters AND 1,250 tokens, a dense small-print page (a typical page holds 2,000-3,000 characters).
# Tokens are what the limit protects: chunks, embedding calls and the worker's time limit all grow with them. English
# runs ~4 characters per token, so there the two limits agree (500 pages: 2.5M characters, ~625k tokens, ~2,000
# chunks). Chinese runs 1.3-2.4 tokens per character and emoji ~3, so 2.5M characters of those were up to 6-7M tokens
# and ~20k chunks, more than one worker run can embed inside its time limit. Characters are checked first because that
# is free and caps the work of counting tokens.
MAX_CHARS_PER_PAGE = 5_000
MAX_TOKENS_PER_PAGE = 1_250
# tiktoken builds the whole list of tokens before it can be counted (~36 bytes a token as Python ints): in one call,
# 2.5M emoji under the character limit held ~5M tokens, +500 MB, just to refuse the file. So tokens are counted this
# many characters at a time (measured 8-13 MB a slice in the densest scripts), stopping as soon as the total is over
# the limit. A cut can split a word into one extra token per slice, which does not matter to a limit.
TOKEN_COUNT_SLICE_CHARS = 64_000
# python-docx parses every XML part of a .docx into an lxml tree as soon as it opens the file, and that tree costs
# ~100-200 bytes per element or attribute however few bytes each took in the file (measured: 10 MB of '<w:p/>' became
# +200 MB, attribute-dense XML 30x its size). The upload's byte limits (sniff.py) cannot bound that, so the structure
# is counted before python-docx opens anything. A paragraph of Word prose with its properties and a few formatted runs
# is ~50-100 elements and attributes, ~1,000 a page, so 500k is ~500 pages of prose; documents built to sit just under
# it peaked at +65-100 MB and took under 4 s, which a 512 MB worker can afford. Table-dense pages hold several times
# more, so a long all-table document reaches the limit sooner.
MAX_XML_NODES = 500_000

# pypdf is lenient, but a malformed file can still surface as a plain Python error from deep inside the parser.
# Listed explicitly (not `except Exception`) so Celery's SoftTimeLimitExceeded is never mistaken for a corrupt file.
_PDF_PARSE_ERRORS = (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError)
# python-docx: not a package / missing part / broken XML (lxml's XMLSyntaxError is a SyntaxError). RecursionError: it
# resolves a continued vertical merge by recursing up the rows, so a long merge chain overflows the stack. That file
# fails the same way every time, so it is corrupt, not an unexpected (retryable) error.
_DOCX_PARSE_ERRORS = (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError, SyntaxError, RecursionError)
# zipfile, for an entry it cannot read: a compression method it does not support, a bad CRC, broken or truncated
# deflate data. (An encrypted entry was refused at upload, see sniff.py.)
_ZIP_READ_ERRORS = (NotImplementedError, zipfile.BadZipFile, zlib.error, EOFError)

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


def _check_text_size(text: str) -> None:
    """TOO_MUCH_TEXT for a format without pages: the limit is MAX_PAGES pages' worth of text, in characters and in
    tokens (see MAX_TOKENS_PER_PAGE). Checked before splitting, because chunking an oversized text is itself
    expensive. Tokens are counted a slice at a time, and only until the total is over (see TOKEN_COUNT_SLICE_CHARS)."""
    _check_limit(len(text), MAX_CHARS_PER_PAGE, "characters")
    tokens = 0
    for start in range(0, len(text), TOKEN_COUNT_SLICE_CHARS):
        tokens += count_tokens(text[start : start + TOKEN_COUNT_SLICE_CHARS])
        _check_limit(tokens, MAX_TOKENS_PER_PAGE, "tokens")


def _check_limit(amount: int, per_page: int, unit: str) -> None:
    limit = settings.MAX_PAGES * per_page
    if amount > limit:
        # "more than the limit", not the amount: the token count stops early, so it is only a lower bound.
        raise PermanentError(ErrorCode.TOO_MUCH_TEXT, f"The document has more {unit} of text than the limit of "
                             f"{limit:,} (about {settings.MAX_PAGES} pages).")


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
    for index, page in enumerate(reader.pages):
        text = _clean(_pdf_page_text(page, index + 1))
        pages.append(PageText(page_no=index + 1, label=f"p. {labels[index]}", heading_path=None, text=text))

    lengths = [len(p.text.strip()) for p in pages]
    pages_with_text = sum(n > 0 for n in lengths)
    if pages_with_text == 0 or (max(lengths) < MIN_PAGE_CHARS and pages_with_text > 1):
        # No text on any page: there is no text layer, i.e. a scan. A few characters on each of several pages and
        # nothing longer anywhere: page or Bates numbers stamped on a scan. Short text on ONE page (a one-line
        # receipt "Invoice #1234 paid") is a real text layer and gets indexed. The trade-off: several pages of short
        # text and nothing longer (a two-page receipt) look exactly like stamps on a scan, and are refused.
        raise PermanentError(ErrorCode.NO_EXTRACTABLE_TEXT,
                             "No extractable text: looks like a scanned/image-only PDF (OCR is not supported).")
    # Some pages scanned: index the rest, and tell the user how many pages chat can't see. Next to pages of real text,
    # a near-empty page is a scanned page or a figure with a stray label; when the only text is one short page (the
    # receipt above), short IS the content, so only blank pages count.
    floor = MIN_PAGE_CHARS if max(lengths) >= MIN_PAGE_CHARS else 1
    without_text = sum(n < floor for n in lengths)
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
    previous Heading 2 (and anything deeper) but stays under the current Heading 1.

    A "heading" over MAX_HEADING_TOKENS is kept as body text instead. Real Word files contain whole paragraphs styled
    'Heading 1' or 'Title' (and Markdown files long '#' lines); as a heading, that paragraph would be copied into the
    page text of every part and into every chunk's header, pushing embedding inputs past the 8,192-token limit."""
    sections = [_Section(heading=None, heading_path=None)]
    stack: list[tuple[int, str]] = []
    for block in blocks:
        if block.level is None or count_tokens(block.text) > MAX_HEADING_TOKENS:
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
        _check_docx_xml(path)
        document = docx.Document(str(path))
        blocks = list(_docx_blocks(document))
    except _DOCX_PARSE_ERRORS as exc:
        raise PermanentError(ErrorCode.CORRUPT_FILE, "The Word document could not be read.") from exc
    _check_text_size("\n\n".join(block.text for block in blocks))
    return _pages_from_sections(_sections(blocks))


class _XmlNodeCounter:
    """An lxml parser target: counts the nodes a tree would hold, without building one. Elements and attributes, and
    also comments, processing instructions and namespace declarations: python-docx's parser keeps those in its tree
    too, each costing about what an element does (29 MB of '<?a?>' in a 591 KB upload cost +809 MB). Raising here
    stops the parse (lxml re-raises what its target raises), so an oversized part is never read to the end."""

    def __init__(self) -> None:
        self.nodes = 0

    def start(self, _tag: str, attrib: dict[str, str]) -> None:
        self._add(1 + len(attrib))

    def comment(self, _text: str) -> None:
        self._add(1)

    def pi(self, _target: str, _data: str | None = None) -> None:
        self._add(1)

    def start_ns(self, _prefix: str, _uri: str) -> None:
        self._add(1)

    def _add(self, nodes: int) -> None:
        self.nodes += nodes
        if self.nodes > MAX_XML_NODES:
            raise PermanentError(ErrorCode.UNSAFE_ARCHIVE, f"The Word document's XML has more than "
                                 f"{MAX_XML_NODES:,} nodes, more than can be opened safely.")

    def doctype(self, *_args: object) -> None:
        # Word never writes a DOCTYPE, and the entities one declares would be nodes this count cannot see.
        raise PermanentError(ErrorCode.UNSAFE_ARCHIVE,
                             "The Word document contains a DOCTYPE declaration, which Word never writes.")

    def close(self) -> None:
        pass


def _check_docx_xml(path: Path) -> None:
    """UNSAFE_ARCHIVE for XML that python-docx could not parse safely (see MAX_XML_NODES), checked before it opens it.

    Every entry of the archive is counted, into one total, not just word/document.xml: python-docx also parses the
    styles, numbering, headers and footers when it opens the file and holds them all at once, and it finds the main
    part through the package's relationships, whatever its name. Each entry goes through the same call python-docx
    makes on a part (fromstring on its bytes, which sniff.py capped at 30 MB an entry), so encodings such as UTF-16 and
    UTF-32 are detected exactly as python-docx will; lxml's streaming parsers miss UTF-32. An entry that is not XML
    (an image) fails on its first bytes. An entry zipfile cannot read is skipped: python-docx reads parts with the same
    zipfile call, so it fails on that entry too if it is a part, and never opens it if it is not.

    Before any entry is read, the entry sizes (each, and in total) are checked against the upload's caps (sniff.py):
    the worker extracts stored objects without sniffing them again, and one stored under older, looser caps can still
    be reprocessed. The sizes in the zip headers bound the reads, because zipfile never returns more bytes than an
    entry declares."""
    counter = _XmlNodeCounter()
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        sizes = [info.file_size for info in infos]
        if sum(sizes) > MAX_UNCOMPRESSED or max(sizes, default=0) > MAX_ENTRY_UNCOMPRESSED:
            raise PermanentError(ErrorCode.UNSAFE_ARCHIVE, "The Word document's archive is larger than can be opened "
                                 "safely.")
        for info in infos:
            try:
                xml = archive.read(info)
            except _ZIP_READ_ERRORS:
                continue
            try:
                etree.fromstring(xml, etree.XMLParser(target=counter, resolve_entities=False))
            except etree.XMLSyntaxError:
                pass  # not XML (an image), or broken XML python-docx refuses too; its nodes so far are counted


def _docx_blocks(document) -> Iterator[_Block]:
    """Body paragraphs AND tables, in document order (a table between two paragraphs stays between them).
    Headers, footers, footnotes and text boxes are not read.

    The body's children are visited one at a time. document.iter_inner_content() would first build a Python list of
    every paragraph in the body: for 450k empty paragraphs that raised the peak from +66 MB to +96 MB."""
    style_names, default_style = _paragraph_style_names(document)
    paragraph_tag, table_tag = qn("w:p"), qn("w:tbl")
    for element in document.element.body.iterchildren(paragraph_tag, table_tag):
        if element.tag == table_tag:
            text = _table_text(Table(element, document))
            if text:
                yield _Block(level=None, text=text)
            continue
        paragraph = Paragraph(element, document)
        text = _clean(paragraph.text).strip()
        if not text:
            continue
        level = _heading_level(style_names.get(element.style, default_style))
        yield _Block(level=level, text=_one_line(text) if level is not None else text)


def _paragraph_style_names(document) -> tuple[dict[str, str], str]:
    """Style id -> name of every paragraph style, and the default paragraph style's name, read ONCE per document.

    paragraph.style resolves the same name per paragraph by scanning every style in styles.xml in Python (twice for a
    paragraph without a style of its own): with a styles.xml padded to 200k styles, well under MAX_XML_NODES, that was
    ~0.6 s a paragraph, so a short document could hold a worker until its time limit. As in python-docx, a paragraph
    whose style id is missing, unknown or not a paragraph style gets the default style."""
    names: dict[str, str] = {}
    for style in document.styles:
        if style.style_id and style.element.type == WD_STYLE_TYPE.PARAGRAPH:
            names.setdefault(style.style_id, style.name or "")
    default = document.styles.default(WD_STYLE_TYPE.PARAGRAPH)
    default_name = (default.name or "") if default is not None else ""
    return names, default_name


def _heading_level(name: str) -> int | None:
    """Word marks headings with paragraph styles: 'Title' -> 0, 'Heading 1' -> 1, 'Heading 2' -> 2, ..."""
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
    _check_text_size(text)
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
