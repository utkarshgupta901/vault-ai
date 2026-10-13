from typing import Any

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    """Every list endpoint returns {items, next_cursor}. next_cursor=null means there is no next page."""

    items: list[T]
    next_cursor: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
    request_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Missing or invalid API key"},
    404: {"model": ErrorEnvelope, "description": "Not found (or owned by someone else)"},
    422: {"model": ErrorEnvelope, "description": "Validation error"},
    429: {"model": ErrorEnvelope, "description": "Rate limited / quota exceeded"},
}
