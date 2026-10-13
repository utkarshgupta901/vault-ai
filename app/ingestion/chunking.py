"""Chunking: split each page into ~400-token retrieval units, each with a breadcrumb header.

Rules (design §6 "Chunking"):
  - Split PER PAGE. A chunk never spans two pages, so its citation ("p. 4", "§ Termination") is exact.
  - A last piece under MIN_TAIL_TOKENS is merged into the previous chunk of the same page: a 20-token scrap retrieves
    badly and wastes one of the 8 source slots in a prompt.
  - header = "{title} > {heading_path} ({label})", with missing parts left out (never the text "None"). The header is
    embedded and indexed together with the content, so a chunk that only says "The fee is $60k" can still be found
    by its document and section name.
  - embed_hash = sha256(model|dims|header\\ncontent): the exact input to the exact model. Equal hash => the stored
    vector can be reused instead of paying for the same embedding twice (resume after a crash, reprocess).

Token maths for the whole ingestion package lives here too (tiktoken cl100k_base, the tokenizer of
text-embedding-3-*), so extraction and chunking always agree on what "1,500 tokens" means.
"""

import hashlib
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import settings

if TYPE_CHECKING:  # import only for type hints: extract.py imports this module at runtime
    from app.ingestion.extract import PageText

ENCODING = "cl100k_base"
MIN_TAIL_TOKENS = 50


@dataclass(frozen=True)
class ChunkDraft:
    """A chunk computed in memory, before it is written to the `chunks` table."""

    chunk_index: int          # 0-based position in the document (unique per document)
    page_start: int
    page_end: int             # always == page_start (chunks never cross pages); kept for the schema/citations
    heading_path: str | None
    header: str
    content: str
    token_count: int          # tokens of `content` (cl100k_base)
    embed_hash: str


# --- Tokens and splitting ------------------------------------------------------------------------------------------


@cache
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(ENCODING)


def count_tokens(text: str) -> int:
    # encode_ordinary: a document containing the literal text "<|endoftext|>" is just text, not a special token.
    return len(_encoding().encode_ordinary(text))


@cache
def _splitter(max_tokens: int, overlap: int) -> RecursiveCharacterTextSplitter:
    # Recursive = try to cut at paragraph breaks first, then lines, then words, then characters, so a piece only
    # breaks mid-sentence when a single sentence is longer than max_tokens. Lengths are measured in tokens.
    # disallowed_special=() for the same reason as count_tokens: user text must never make the tokenizer raise.
    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name=ENCODING, chunk_size=max_tokens, chunk_overlap=overlap, disallowed_special=()
    )


def _split_raw(text: str, max_tokens: int, overlap: int) -> list[str]:
    return [piece for piece in _splitter(max_tokens, overlap).split_text(text) if piece.strip()]


def _merge_short_tail(text: str, pieces: list[str]) -> list[str]:
    """Merge a last piece under MIN_TAIL_TOKENS into the piece before it.

    Gluing the two strings together would repeat the overlap (the tail starts with up to CHUNK_OVERLAP tokens
    copied from the end of the previous piece). Instead, take the original text from the start of the previous piece
    to the end. Finding the positions: pieces are exact substrings of the text (the splitter keeps separators), and
    the tail always ends the text, so its start is known exactly; the previous piece is the LAST occurrence of its
    text that starts no later than the tail. Searching backwards from that anchor (not forwards from the top) means
    the merged piece is never longer than the two pieces together, even for repetitive text ("Details follow. " x N).
    """
    if len(pieces) < 2 or count_tokens(pieces[-1]) >= MIN_TAIL_TOKENS:
        return pieces
    previous, tail = pieces[-2], pieces[-1]
    end = len(text.rstrip())
    tail_start = end - len(tail)
    previous_start = text.rfind(previous, 0, tail_start + len(previous))
    if previous_start < 0 or text[tail_start:end] != tail:  # defensive: not expected, but never lose text
        return [*pieces[:-2], previous + "\n" + tail]
    return [*pieces[:-2], text[previous_start:end]]


def split_by_tokens(text: str, max_tokens: int, overlap: int = 0) -> list[str]:
    """Split `text` into pieces of about max_tokens, preferring paragraph boundaries; blank pieces are dropped and a
    last piece under MIN_TAIL_TOKENS is merged into the one before it (so a piece may exceed max_tokens by < 50).
    Used for chunks (400/60) and for DOCX/MD section parts and TXT pseudo-pages (1,500/0)."""
    return _merge_short_tail(text, _split_raw(text, max_tokens, overlap))


# --- Header + hash ------------------------------------------------------------------------------------------------


def build_header(title: str | None, heading_path: str | None, label: str | None) -> str:
    """'Handbook > Leave > PTO (§ PTO)', 'Handbook (p. 4)' or '(p. 4)': missing parts are omitted, never 'None'."""
    crumbs = " > ".join(part.strip() for part in (title, heading_path) if part and part.strip())
    location = f"({label.strip()})" if label and label.strip() else ""
    return " ".join(part for part in (crumbs, location) if part)


def embed_text(header: str, content: str) -> str:
    """The exact string sent to the embedding model (and hashed). One function, so the two can never drift apart."""
    return f"{header}\n{content}"


def embed_hash(model: str, dims: int, header: str, content: str) -> str:
    return hashlib.sha256(f"{model}|{dims}|{embed_text(header, content)}".encode()).hexdigest()


# --- Chunking -----------------------------------------------------------------------------------------------------


def split_page(page_text: str) -> list[str]:
    return split_by_tokens(page_text, settings.CHUNK_TOKENS, settings.CHUNK_OVERLAP)


def chunk_pages(pages: list["PageText"], *, title: str, model: str, dims: int) -> list[ChunkDraft]:
    """Chunk every page independently and number the chunks across the whole document. Blank pages yield nothing."""
    drafts: list[ChunkDraft] = []
    for page in pages:
        if not page.text.strip():
            continue
        header = build_header(title, page.heading_path, page.label)
        for content in split_page(page.text):
            drafts.append(ChunkDraft(
                chunk_index=len(drafts),
                page_start=page.page_no,
                page_end=page.page_no,
                heading_path=page.heading_path,
                header=header,
                content=content,
                token_count=count_tokens(content),
                embed_hash=embed_hash(model, dims, header, content),
            ))
    return drafts
