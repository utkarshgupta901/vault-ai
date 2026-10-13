"""ORM models. The schema itself is defined in migrations/versions/0001_initial.py (hand-written SQL, so every
index and constraint is explicit); these classes mirror it for typed queries."""

from app.models.chat import ChatMessage, ChatSession, ChatSessionDocument
from app.models.chunk import Chunk
from app.models.document import Document, DocumentPage
from app.models.insight import DocumentInsight, DocumentSummary
from app.models.telemetry import AIUsage, ProcessingRun, RequestLog
from app.models.user import User

__all__ = [
    "AIUsage",
    "ChatMessage",
    "ChatSession",
    "ChatSessionDocument",
    "Chunk",
    "Document",
    "DocumentInsight",
    "DocumentPage",
    "DocumentSummary",
    "ProcessingRun",
    "RequestLog",
    "User",
]
