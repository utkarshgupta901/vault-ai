import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    """An API consumer. Auth = X-API-Key header; only the sha256 of the key is stored."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(String, unique=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    daily_cost_limit_usd: Mapped[Decimal] = mapped_column(Numeric(8, 2), server_default=text("2.00"))
    rate_limit_per_min: Mapped[int] = mapped_column(server_default=text("30"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
