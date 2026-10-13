"""Chunker rules: per page, breadcrumb header without 'None', small tails merged, stable embed_hash. Pure unit tests."""

from app.config import settings
from app.ingestion.chunking import (
    MAX_HEADER_TOKENS,
    MAX_LABEL_TOKENS,
    MIN_TAIL_TOKENS,
    _split_raw,
    build_header,
    chunk_pages,
    count_tokens,
    embed_hash,
    split_by_tokens,
    split_page,
)
from app.ingestion.extract import PageText
from app.ingestion.pipeline import _Pending, batches
from app.ingestion.titles import filename_title

MODEL, DIMS = "text-embedding-3-small", 1536


def _words(prefix: str, n: int) -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def _paragraph_of(tokens: int, prefix: str) -> str:
    """A paragraph of roughly `tokens` tokens (grown word by word, so the count is exact for this tokenizer)."""
    words: list[str] = []
    while count_tokens(" ".join(words)) < tokens:
        words.append(f"{prefix}{len(words)}")
    return " ".join(words)


def test_chunks_never_cross_pages() -> None:
    pages = [PageText(n, f"p. {n}", None, _words(f"page{n}w", 900)) for n in (1, 2, 3)]
    chunks = chunk_pages(pages, title="Report", model=MODEL, dims=DIMS)
    assert len(chunks) > 3
    for c in chunks:
        assert c.page_start == c.page_end
        assert f"page{c.page_start}w" in c.content
        assert all(f"page{other}w" not in c.content for other in (1, 2, 3) if other != c.page_start)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))   # numbered across the document
    assert all(c.token_count <= settings.CHUNK_TOKENS + MIN_TAIL_TOKENS for c in chunks)


def test_header_omits_missing_parts_and_never_says_none() -> None:
    assert build_header("Handbook", "Leave > PTO", "§ PTO") == "Handbook > Leave > PTO (§ PTO)"
    assert build_header("Handbook", None, "p. 4") == "Handbook (p. 4)"
    assert build_header(None, None, "p. 4") == "(p. 4)"
    assert build_header("  ", "", "part 1") == "(part 1)"
    pages = [PageText(1, "p. 1", None, "Some text on the page.")]
    for chunk in chunk_pages(pages, title="", model=MODEL, dims=DIMS):
        assert "None" not in chunk.header
        assert chunk.header == "(p. 1)"


def test_blank_pages_produce_no_chunks() -> None:
    pages = [PageText(1, "p. 1", None, "   "), PageText(2, "p. 2", None, "Real content here.")]
    chunks = chunk_pages(pages, title="T", model=MODEL, dims=DIMS)
    assert [c.page_start for c in chunks] == [2]


def test_short_tail_merges_into_previous_chunk_without_repeating_the_overlap() -> None:
    p1, p2, p3 = _paragraph_of(370, "a"), _paragraph_of(20, "b"), _paragraph_of(20, "c")
    page = f"{p1}\n\n{p2}\n\n{p3}"
    raw = _split_raw(page, settings.CHUNK_TOKENS, settings.CHUNK_OVERLAP)
    # Precondition: the splitter alone leaves a small tail that starts with the overlap (p2 copied again).
    assert len(raw) == 2 and count_tokens(raw[-1]) < MIN_TAIL_TOKENS and raw[-1].startswith(p2)
    merged = split_page(page)
    assert merged == [page]                     # one chunk, exactly the page text
    assert merged[0].count(p2) == 1             # the overlap is not duplicated


def test_tail_merge_when_there_is_no_overlap() -> None:
    p1, p2 = _paragraph_of(390, "a"), _paragraph_of(15, "b")
    page = f"{p1}\n\n{p2}"
    assert split_page(page) == [page]


def test_a_large_enough_tail_is_kept() -> None:
    page = f"{_paragraph_of(380, 'a')}\n\n{_paragraph_of(120, 'b')}"
    assert len(split_page(page)) == 2


def test_tail_merge_stays_bounded_on_repetitive_text() -> None:
    # Regression: a forward search found the previous piece at an earlier repeat and produced a 4,500-token "part".
    text = "Details follow. " * 3000
    for max_tokens, overlap in ((1500, 0), (settings.CHUNK_TOKENS, settings.CHUNK_OVERLAP)):
        pieces = split_by_tokens(text, max_tokens, overlap)
        assert len(pieces) > 1
        assert all(count_tokens(p) <= max_tokens + MIN_TAIL_TOKENS for p in pieces)
    no_overlap = split_by_tokens(text, 1500)
    assert abs(sum(count_tokens(p) for p in no_overlap) - count_tokens(text)) <= len(no_overlap)  # nothing lost


def test_single_small_page_keeps_its_only_chunk() -> None:
    assert split_page("Tiny page.") == ["Tiny page."]


def test_embed_hash_is_stable_and_covers_model_dims_header_and_content() -> None:
    base = embed_hash(MODEL, DIMS, "H (p. 1)", "content")
    assert base == embed_hash(MODEL, DIMS, "H (p. 1)", "content")
    assert len(base) == 64
    assert base != embed_hash("text-embedding-3-large", DIMS, "H (p. 1)", "content")
    assert base != embed_hash(MODEL, 512, "H (p. 1)", "content")
    assert base != embed_hash(MODEL, DIMS, "H (p. 2)", "content")
    assert base != embed_hash(MODEL, DIMS, "H (p. 1)", "content!")
    pages = [PageText(1, "p. 1", None, "Same text.")]
    first = chunk_pages(pages, title="T", model=MODEL, dims=DIMS)
    again = chunk_pages(pages, title="T", model=MODEL, dims=DIMS)
    assert [c.embed_hash for c in first] == [c.embed_hash for c in again]


def test_batches_respect_both_item_and_token_limits() -> None:
    items = [_Pending(chunk_id=i, text="x", tokens=100) for i in range(10)]
    assert [len(b) for b in batches(items, max_items=4, max_tokens=10_000)] == [4, 4, 2]
    assert [len(b) for b in batches(items, max_items=128, max_tokens=250)] == [2, 2, 2, 2, 2]
    assert [len(b) for b in batches(items, max_items=128, max_tokens=50)] == [1] * 10   # one oversize item still goes
    assert batches([], max_items=4, max_tokens=100) == []


def test_header_is_capped_and_keeps_its_location() -> None:
    # The header is repeated in every chunk's embedding input (8,192-token limit per input), so it has a hard cap.
    header = build_header("Handbook", _words("heading", 3000), "p. 4")
    assert count_tokens(header) <= MAX_HEADER_TOKENS + 2
    assert header.startswith("Handbook > heading0 ") and header.endswith("… (p. 4)")
    assert count_tokens(build_header(None, None, _words("label", 500))) <= MAX_LABEL_TOKENS + 3


def test_normal_headers_are_unchanged_by_the_cap() -> None:
    # Existing chunks keep their embed_hash: the cap only touches headers that were over the limit anyway.
    assert build_header("Handbook", "Leave > PTO", "§ PTO") == "Handbook > Leave > PTO (§ PTO)"


def test_filename_title_depends_only_on_the_filename() -> None:
    assert filename_title("Larkspur_Handbook-2026.pdf") == "Larkspur Handbook 2026"
    assert filename_title("NIST.AI.100-1.pdf") == "NIST.AI.100 1"
    assert filename_title("___.txt") == "___.txt"          # never empty
