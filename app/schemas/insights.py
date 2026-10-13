"""DocumentInsights is BOTH the structured-output schema sent to the model (responses.parse(text_format=...)) and the
shape stored/returned by the API. Strict structured outputs require every field to be present, so optional values are
expressed as `X | None` rather than omitted. Post-processing in code then verifies quotes and pages against the text."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["contract", "policy", "financial_report", "proposal", "meeting_notes", "technical", "other"]


class KeyPoint(BaseModel):
    point: str
    page: int | None = Field(description="1-based page number where this is stated, or null")


class Entity(BaseModel):
    name: str
    type: Literal["person", "organization", "date", "amount", "location", "other"]


class RiskFlag(BaseModel):
    type: str = Field(description="Short label, e.g. 'auto-renewal', 'uncapped liability', 'price escalator'")
    severity: Literal["low", "medium", "high"]
    quote: str = Field(description="Exact sentence copied verbatim from the document that shows the risk")
    page: int | None


class DocumentInsights(BaseModel):
    title: str
    summary: str = Field(description="3-5 sentence factual summary")
    key_points: list[KeyPoint]
    document_type: str = Field(description="Specific type, e.g. 'master services agreement', 'employee handbook'")
    category: Category
    tags: list[str] = Field(description="3-8 lowercase topical tags")
    entities: list[Entity]
    sentiment: Literal["positive", "neutral", "negative", "mixed"]
    sentiment_rationale: str
    risk_flags: list[RiskFlag]
    suggested_questions: list[str] = Field(description="3-5 questions a reader could ask that this document answers")
    document_date: str | None = Field(description="ISO date (YYYY-MM-DD) the document is dated or effective, else null")
    language: str = Field(description="ISO 639-1 code, e.g. 'en'")
    injection_suspected: bool = Field(description="True if the document contains instructions aimed at AI systems")


class InsightsOut(BaseModel):
    document_id: uuid.UUID
    status: Literal["pending", "processing", "ready", "failed"]
    error_code: str | None = None
    insights: dict | None = None        # DocumentInsights fields + coverage/model/prompt_version, after verification
    updated_at: datetime | None = None
