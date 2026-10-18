"""Upload sniffing: text detection must stream the file, and DOCX archives must pass zip-bomb limits before anything
unzips them. Pure unit tests (no services)."""

import io
import os
import zipfile

import pytest

from app.config import settings
from app.core.errors import AppError, ErrorCode
from app.ingestion import sniff as sniff_module
from app.ingestion.sniff import DOCX, MAX_RATIO, MAX_UNCOMPRESSED, TXT, sniff
from tests.ingestion.helpers import make_docx

MB = 1024 * 1024


def _rejected(data: bytes, name: str = "a.txt") -> AppError:
    with pytest.raises(AppError) as err:
        sniff(io.BytesIO(data), name)
    return err.value


# --- TXT / MD: strict UTF-8, read in chunks -------------------------------------------------------------------------


class _BoundedReads(io.BytesIO):
    """An upload that fails the test if anything reads all of it in one call (uploads can be 25 MB)."""

    def read(self, size: int | None = -1) -> bytes:
        assert size is not None and 0 <= size <= MB, f"unbounded read({size}) of the whole upload"
        return super().read(size)


def test_text_check_reads_the_upload_in_bounded_chunks() -> None:
    data = ("Employees receive 18 PTO days per year. " * 50_000).encode()   # ~2 MB
    assert sniff(_BoundedReads(data), "a.txt").mime_type == TXT


@pytest.mark.parametrize("char", ["é", "€", "😀"])   # 2-, 3- and 4-byte UTF-8 sequences
def test_multibyte_character_split_across_chunks_is_still_text(char: str) -> None:
    encoded = char.encode()
    for bytes_in_first_chunk in range(1, len(encoded)):
        data = b"a" * (sniff_module.TEXT_CHUNK_BYTES - bytes_in_first_chunk) + encoded + b" tail"
        assert sniff(io.BytesIO(data), "a.txt").mime_type == TXT


def test_multibyte_character_cut_off_at_end_of_file_is_rejected() -> None:
    data = b"a" * (sniff_module.TEXT_CHUNK_BYTES - 1) + "é".encode()[:1]   # file ends exactly on a chunk boundary
    assert _rejected(data).code == ErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert _rejected("café".encode()[:-1]).code == ErrorCode.UNSUPPORTED_MEDIA_TYPE   # within the first chunk


@pytest.mark.parametrize("bad", [b"\x00", b"\xff", b"\xc3\x28"])   # NUL, never-valid byte, broken 2-byte sequence
def test_nul_or_invalid_utf8_in_a_later_chunk_is_rejected(bad: bytes) -> None:
    data = b"a" * (sniff_module.TEXT_CHUNK_BYTES + 10) + bad + b"more text"
    assert _rejected(data).code == ErrorCode.UNSUPPORTED_MEDIA_TYPE


# --- DOCX: zip-bomb limits ------------------------------------------------------------------------------------------


def _zip(document_xml: bytes, filler: bytes = b"") -> bytes:
    """A minimal 'DOCX' as sniff sees it. `filler` is stored uncompressed, so it lowers the overall ratio."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document_xml)
        if filler:
            zf.writestr("word/media/image1.png", filler, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


def test_real_docx_passes() -> None:
    assert sniff(io.BytesIO(make_docx()), "a.docx").mime_type == DOCX


def test_one_huge_entry_is_unsafe_even_when_total_and_ratio_are_within_limits() -> None:
    """The bomb in the review: a small zip whose word/document.xml alone inflates to far more XML than python-docx
    can turn into an lxml tree without exhausting the worker. Total size and overall ratio both look fine."""
    data = _zip(b" " * (40 * MB), filler=os.urandom(MB))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        total = sum(i.file_size for i in zf.infolist())
        assert total < MAX_UNCOMPRESSED and total / sum(i.compress_size for i in zf.infolist()) < MAX_RATIO
    err = _rejected(data, "bomb.docx")
    assert (err.code, err.status) == (ErrorCode.UNSAFE_ARCHIVE, 422)


def test_entry_just_under_the_per_entry_cap_passes() -> None:
    data = _zip(b" " * (sniff_module.MAX_ENTRY_UNCOMPRESSED - MB), filler=os.urandom(MB))
    assert sniff(io.BytesIO(data), "big.docx").mime_type == DOCX


def test_entries_each_under_the_per_entry_cap_are_unsafe_together() -> None:
    """python-docx holds every part the package refers to at once, as bytes and as a parsed tree, so the per-entry
    cap alone just splits the bomb across parts: seven ~28 MB header parts in a 2.5 MB upload cost the worker +400 MB.
    The total is what bounds the worker's memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"<w:document/>")
        for n in range(1, 4):
            zf.writestr(f"word/header{n}.xml", b" " * (25 * MB))
        zf.writestr("word/media/image1.png", os.urandom(MB), compress_type=zipfile.ZIP_STORED)
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        assert max(i.file_size for i in infos) < sniff_module.MAX_ENTRY_UNCOMPRESSED
        assert sum(i.file_size for i in infos) / sum(i.compress_size for i in infos) < MAX_RATIO
    err = _rejected(data, "bomb.docx")
    assert (err.code, err.status) == (ErrorCode.UNSAFE_ARCHIVE, 422)


def test_document_at_the_text_limit_next_to_a_full_upload_of_media_passes() -> None:
    """The total still fits the largest real document: document.xml for extraction's text limit (under the per-entry
    cap) plus as much already-compressed media (images barely deflate) as an upload can carry."""
    media = os.urandom(settings.MAX_UPLOAD_MB * MB)
    data = _zip(b" " * (sniff_module.MAX_ENTRY_UNCOMPRESSED - MB), filler=media)
    assert sniff(io.BytesIO(data), "big.docx").mime_type == DOCX
