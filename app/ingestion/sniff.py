"""Decide what a file really is from its bytes, never from the client's filename or Content-Type.

PDF  -> starts with %PDF-
DOCX -> a ZIP containing word/document.xml, checked against zip-bomb limits before anything unzips it
TXT/MD -> strict UTF-8 with no NUL bytes (extension only chooses between text/plain and text/markdown)
Anything else -> 415.
"""

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

MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_RATIO = 100
MAX_ENTRIES = 5000


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
            compressed = sum(i.compress_size for i in infos) or 1
            encrypted = any(i.flag_bits & 0x1 for i in infos)
            if len(infos) > MAX_ENTRIES or total > MAX_UNCOMPRESSED or total / compressed > MAX_RATIO or encrypted:
                raise AppError(ErrorCode.UNSAFE_ARCHIVE, 422, "DOCX archive exceeds safety limits.",
                               {"entries": len(infos), "uncompressed_bytes": total})
    except zipfile.BadZipFile as exc:
        raise AppError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, 415, "Corrupt or unsupported ZIP/DOCX file.") from exc


def _is_text(f: IO[bytes]) -> bool:
    f.seek(0)
    data = f.read()
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8", errors="strict")
        return True
    except UnicodeDecodeError:
        return False


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
