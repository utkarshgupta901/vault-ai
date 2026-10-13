"""Extraction per format: labels, sections, pseudo-pages and permanent failure codes. Pure unit tests (no services)."""

from pathlib import Path

import pytest

from app.config import settings
from app.core.errors import ErrorCode, PermanentError
from app.ingestion.chunking import MAX_HEADER_TOKENS, MIN_TAIL_TOKENS, chunk_pages, count_tokens, embed_text
from app.ingestion.extract import MAX_HEADING_TOKENS, SECTION_TOKENS, extract
from app.ingestion.sniff import DOCX, MD, PDF, TXT
from tests.ingestion.helpers import (
    PAGE_TEXTS,
    encrypt_pdf,
    label_pdf_pages,
    make_docx,
    make_image_only_pdf,
    make_mixed_pdf,
    make_pdf,
)


def _file(tmp_path: Path, name: str, data: bytes | str) -> Path:
    path = tmp_path / name
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def _paragraphs(n: int, words: int = 60) -> str:
    return "\n\n".join(f"Paragraph {i}. " + " ".join(f"word{i}x{j}" for j in range(words)) for i in range(n))


# --- PDF ----------------------------------------------------------------------------------------------------------


def test_pdf_one_page_per_physical_page_with_default_labels(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.pdf", make_pdf()), PDF)
    assert [p.page_no for p in doc.pages] == [1, 2, 3]
    assert [p.label for p in doc.pages] == ["p. 1", "p. 2", "p. 3"]
    assert "LRK-4471" in doc.pages[1].text
    assert all(p.heading_path is None for p in doc.pages)
    assert doc.warnings == []


def test_pdf_uses_the_documents_own_page_labels(tmp_path: Path) -> None:
    data = label_pdf_pages(make_pdf([*PAGE_TEXTS, "Appendix text that is long enough to count."]))
    doc = extract(_file(tmp_path, "a.pdf", data), PDF)
    assert [p.label for p in doc.pages] == ["p. i", "p. ii", "p. 1", "p. 2"]
    assert [p.page_no for p in doc.pages] == [1, 2, 3, 4]   # page_no stays physical for the page viewer


def test_password_protected_pdf_is_encrypted_pdf(tmp_path: Path) -> None:
    data = encrypt_pdf(make_pdf(), user_password="s3cret")
    with pytest.raises(PermanentError) as err:
        extract(_file(tmp_path, "a.pdf", data), PDF)
    assert err.value.code == ErrorCode.ENCRYPTED_PDF


def test_pdf_encrypted_with_empty_user_password_is_readable(tmp_path: Path) -> None:
    data = encrypt_pdf(make_pdf(), user_password="")   # "permissions only" encryption
    doc = extract(_file(tmp_path, "a.pdf", data), PDF)
    assert "18 PTO days" in doc.pages[0].text


def test_image_only_pdf_is_no_extractable_text(tmp_path: Path) -> None:
    with pytest.raises(PermanentError) as err:
        extract(_file(tmp_path, "scan.pdf", make_image_only_pdf()), PDF)
    assert err.value.code == ErrorCode.NO_EXTRACTABLE_TEXT
    assert "OCR is not supported" in err.value.message


def test_partly_scanned_pdf_is_indexed_with_a_warning(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "mixed.pdf", make_mixed_pdf()), PDF)
    assert len(doc.pages) == 2                      # the scanned page is kept so page numbers stay true
    assert doc.warnings == ["pages_without_text:1"]


def test_too_many_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_PAGES", 2)
    with pytest.raises(PermanentError) as err:
        extract(_file(tmp_path, "a.pdf", make_pdf()), PDF)
    assert err.value.code == ErrorCode.TOO_MANY_PAGES


def test_corrupt_pdf(tmp_path: Path) -> None:
    with pytest.raises(PermanentError) as err:
        extract(_file(tmp_path, "a.pdf", b"%PDF-1.7\nthis is not really a pdf"), PDF)
    assert err.value.code == ErrorCode.CORRUPT_FILE


# --- DOCX ---------------------------------------------------------------------------------------------------------


def test_docx_sections_follow_headings_with_breadcrumbs(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.docx", make_docx()), DOCX)
    labels = [p.label for p in doc.pages]
    # "Leave Policy" has no text of its own (a sub-heading follows), so it only appears in its children's path.
    assert labels == ["Intro", "§ Paid Time Off", "§ Parental Leave", "§ Security"]
    by_label = {p.label: p for p in doc.pages}
    assert by_label["Intro"].heading_path is None
    assert by_label["§ Paid Time Off"].heading_path == "Leave Policy > Paid Time Off"
    assert by_label["§ Parental Leave"].heading_path == "Leave Policy > Parental Leave"   # H2 replaces H2
    assert by_label["§ Security"].heading_path == "Security"                              # H1 resets the path
    assert [p.page_no for p in doc.pages] == [1, 2, 3, 4]


def test_docx_tables_are_read_in_document_order_as_pipe_rows(tmp_path: Path) -> None:
    pto = extract(_file(tmp_path, "a.docx", make_docx()), DOCX).pages[1].text
    assert "Tenure | Days | Carry-over" in pto
    assert "3+ years | 22 | 5" in pto
    assert pto.index("18 PTO days") < pto.index("Tenure | Days") < pto.index("Unused days expire")


def test_long_docx_section_is_split_into_parts(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.docx", make_docx(long_section_words=1500)), DOCX)
    security = [p for p in doc.pages if p.label.startswith("§ Security")]
    assert len(security) >= 2
    assert [p.label for p in security][:2] == ["§ Security (part 1)", "§ Security (part 2)"]
    assert all(p.heading_path == "Security" for p in security)
    assert all(p.text.startswith("Security\n\n") for p in security)       # every part carries its heading
    assert all(50 <= count_tokens(p.text) <= SECTION_TOKENS + MIN_TAIL_TOKENS for p in security)


def test_long_heading_is_truncated_in_the_label(tmp_path: Path) -> None:
    heading = "A very long heading " * 10
    doc = extract(_file(tmp_path, "a.md", f"# {heading}\n\nBody text."), MD)
    assert len(doc.pages[0].label) <= len("§ ") + 60
    assert doc.pages[0].label.endswith("…")


def test_corrupt_docx(tmp_path: Path) -> None:
    with pytest.raises(PermanentError) as err:
        extract(_file(tmp_path, "a.docx", b"PK\x03\x04 broken zip"), DOCX)
    assert err.value.code == ErrorCode.CORRUPT_FILE


# --- TXT / MD -------------------------------------------------------------------------------------------------------


def test_short_txt_is_one_pseudo_page(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.txt", "Employees receive 18 PTO days per year."), TXT)
    assert [(p.page_no, p.label) for p in doc.pages] == [(1, "part 1")]


def test_long_txt_becomes_pseudo_pages_cut_at_paragraphs(tmp_path: Path) -> None:
    text = _paragraphs(40)
    doc = extract(_file(tmp_path, "a.txt", text), TXT)
    assert len(doc.pages) >= 2
    assert [p.label for p in doc.pages] == [f"part {n}" for n in range(1, len(doc.pages) + 1)]
    assert all(count_tokens(p.text) <= SECTION_TOKENS + MIN_TAIL_TOKENS for p in doc.pages)
    assert all(p.text.startswith("Paragraph ") for p in doc.pages)   # every page starts at a paragraph boundary


def test_markdown_headings_make_sections_but_code_comments_do_not(tmp_path: Path) -> None:
    md = (
        "Preface line.\n\n"
        "# Setup\n\nInstall it.\n\n```bash\n# not a heading\npip install vault\n```\n\n"
        "## Config ##\n\nSet VAULT_KEY.\n"
    )
    doc = extract(_file(tmp_path, "a.md", md), MD)
    assert [p.label for p in doc.pages] == ["Intro", "§ Setup", "§ Config"]
    assert "# not a heading" in doc.pages[1].text
    assert doc.pages[2].heading_path == "Setup > Config"


def test_markdown_without_headings_is_treated_as_plain_text(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.md", "Just a note.\n\nNo headings here."), MD)
    assert [p.label for p in doc.pages] == ["part 1"]


def test_nul_characters_are_stripped(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.txt", b"Fee is \x00$60k.\x00"), TXT)
    assert doc.pages[0].text == "Fee is $60k."


def test_whitespace_only_text_is_no_extractable_text(tmp_path: Path) -> None:
    with pytest.raises(PermanentError) as err:
        extract(_file(tmp_path, "a.txt", "  \n\n \t "), TXT)
    assert err.value.code == ErrorCode.NO_EXTRACTABLE_TEXT


def test_special_token_text_does_not_crash_the_tokenizer(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.txt", "Prompt marker <|endoftext|> appears in this log."), TXT)
    assert "<|endoftext|>" in doc.pages[0].text


# --- Over-long "headings" ---------------------------------------------------------------------------------------------


def _docx_with_heading_styled_paragraph(words: int) -> bytes:
    """A real-world Word quirk: a whole paragraph of body text formatted with the 'Heading 1' style."""
    import io

    import docx

    d = docx.Document()
    d.add_heading("Terms", level=1)
    d.add_paragraph("Short intro.")
    d.add_heading(" ".join(f"Clause{i} applies to every supplier." for i in range(words)), level=1)
    d.add_paragraph("Closing paragraph.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_paragraph_styled_as_heading_is_body_text_so_embedding_inputs_stay_small(tmp_path: Path) -> None:
    doc = extract(_file(tmp_path, "a.docx", _docx_with_heading_styled_paragraph(1500)), DOCX)
    assert all(p.heading_path in (None, "Terms") for p in doc.pages)             # not a breadcrumb
    assert "Clause1499 applies" in "\n".join(p.text for p in doc.pages)           # ...but its text is kept
    drafts = chunk_pages(doc.pages, title="contract", model="m", dims=8)
    longest = max(count_tokens(embed_text(d.header, d.content)) for d in drafts)
    assert longest <= settings.CHUNK_TOKENS + MIN_TAIL_TOKENS + MAX_HEADER_TOKENS   # far below the 8,192 limit


def test_markdown_heading_line_over_the_limit_is_body_text(tmp_path: Path) -> None:
    long_line = " ".join(f"word{i}" for i in range(MAX_HEADING_TOKENS * 2))
    doc = extract(_file(tmp_path, "a.md", f"# Setup\n\nInstall it.\n\n## {long_line}\n\nMore text.\n"), MD)
    assert [p.heading_path for p in doc.pages] == ["Setup"]
    assert long_line in doc.pages[0].text
