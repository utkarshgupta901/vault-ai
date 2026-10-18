"""Decide what a file really is from its bytes, never from the client's filename or Content-Type.

PDF  -> starts with %PDF-
DOCX -> a ZIP containing word/document.xml, checked against zip-bomb limits before anything unzips it
TXT/MD -> strict UTF-8 with no NUL bytes (extension only chooses between text/plain and text/markdown)
Anything else -> 415.
"""

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import IO

from app.core.errors import AppError, ErrorCode

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT = "text/plain"
MD = "text/markdown"
EXT = {PDF: "pdf", DOCX: "docx", TXT: "txt", MD: "md"}

# python-docx holds every part the package refers to at once, as bytes and each XML part again as a parsed tree, so
# the total is what a worker holds: under a 200 MB total, seven ~28 MB header parts in a 2.5 MB upload cost +400 MB.
# 64 MB still fits the largest real document: document.xml at extraction's text limit (under the per-entry cap below)
# plus a full upload of media, which is already compressed (images barely deflate) and so counts at about its size.
MAX_UNCOMPRESSED = 64 * 1024 * 1024
# Per entry, because the total alone lets one part be the bomb: under the old 200 MB total, a ~2 MB zip whose
# word/document.xml inflated to ~190 MB passed the total and the ratio, and python-docx would build an lxml tree of it
# in the worker. 30 MB still fits a document at extraction's text limit (~2.5M characters, see extract.py): Word writes
# roughly 2-8 bytes of XML per character of text. Bytes alone do not bound that tree (an element of a few bytes costs
# ~100 in it), so extract.py also counts elements before python-docx parses anything (MAX_XML_NODES).
MAX_ENTRY_UNCOMPRESSED = 30 * 1024 * 1024
MAX_RATIO = 100
MAX_ENTRIES = 5000
TEXT_CHUNK_BYTES = 64 * 1024   # how much of an upload the UTF-8 check holds in memory at a time


@dataclass(frozen=True)
class Sniffed:
    mime_type: str
    ext: str


def _check_docx(f: IO[bytes]) -> None:
    f.seek(0)
    try:
        with zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
            names = {i.filename for i in infos}
            if "word/document.xml" not in names:
                raise AppError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, 415, "ZIP file is not a Word (.docx) document.")
            total = sum(i.file_size for i in infos)
            largest = max(i.file_size for i in infos)
            compressed = sum(i.compress_size for i in infos) or 1
            encrypted = any(i.flag_bits & 0x1 for i in infos)
            if (len(infos) > MAX_ENTRIES or total > MAX_UNCOMPRESSED or largest > MAX_ENTRY_UNCOMPRESSED
                    or total / compressed > MAX_RATIO or encrypted):
                raise AppError(ErrorCode.UNSAFE_ARCHIVE, 422, "DOCX archive exceeds safety limits.",
                               {"entries": len(infos), "uncompressed_bytes": total, "largest_entry_bytes": largest})
    except zipfile.BadZipFile as exc:
        raise AppError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, 415, "Corrupt or unsupported ZIP/DOCX file.") from exc


def _is_text(f: IO[bytes]) -> bool:
    """Strict UTF-8 with no NUL bytes, checked TEXT_CHUNK_BYTES at a time so a 25 MB upload is never held in memory
    whole. The incremental decoder keeps a multibyte character that straddles two chunks until its last byte arrives;
    final=True at end of file rejects a file that stops in the middle of one."""
    f.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        while chunk := f.read(TEXT_CHUNK_BYTES):
            if b"\x00" in chunk:
                return False
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def sniff(f: IO[bytes], filename: str) -> Sniffed:
    f.seek(0)
    head = f.read(8)
    if head.startswith(b"%PDF-"):
        return Sniffed(PDF, "pdf")
    if head.startswith(b"PK\x03\x04"):
        _check_docx(f)
        return Sniffed(DOCX, "docx")
    if _is_text(f):
        suffix = PurePath(filename or "").suffix.lower()
        return Sniffed(MD, "md") if suffix in (".md", ".markdown") else Sniffed(TXT, "txt")
    raise AppError(
        ErrorCode.UNSUPPORTED_MEDIA_TYPE, 415, "Unsupported file type. Upload PDF, DOCX, TXT or Markdown.",
        {"supported": ["pdf", "docx", "txt", "md"]},
    )
