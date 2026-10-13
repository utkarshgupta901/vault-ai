import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Computed, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.core.db import Base


class Chunk(Base):
    """A retrievable piece of a document (~400 tokens, never crossing a page boundary).

    Two indexes make hybrid search work:
      - `embedding` (pgvector, HNSW, cosine)  -> semantic similarity ("time off" ~ "PTO")
      - `tsv` (generated tsvector, GIN)       -> exact keywords/identifiers ("LRK-4471", "9.2")
    `owner_id` is denormalized here so retrieval filters by tenant directly on the table it searches.
    `embedding` is NULL until embedded, which makes the embed stage resumable after a crash.
    """

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    chunk_index: Mapped[int]
    page_start: Mapped[int]
    page_end: Mapped[int]
    heading_path: Mapped[str | None] = mapped_column(Text)
    header: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int]
    embed_hash: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.EMBED_DIMS))
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(header, '') || ' ' || content)", persisted=True),
    )
