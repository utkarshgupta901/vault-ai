"""Cost ledger: every model call becomes one ai_usage row. Session-agnostic (works with sync and async sessions):
build the row with `usage_row(...)` and `session.add()` it in the same transaction as the work it paid for."""

import uuid

from app.ai.pricing import cost_usd
from app.ai.provider import Usage
from app.core.request_context import get_request_id
from app.models import AIUsage


def usage_row(
    *,
    operation: str,
    usage: Usage,
    owner_id: uuid.UUID | None,
    prompt_version: str | None = None,
    document_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    cache_hit: bool = False,
    success: bool = True,
    error_code: str | None = None,
    request_id: str | None = None,
    task_id: str | None = None,
) -> AIUsage:
    return AIUsage(
        owner_id=owner_id,
        operation=operation,
        model=usage.model,
        prompt_version=prompt_version,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cost_usd=cost_usd(usage) if not cache_hit else 0,
        latency_ms=usage.latency_ms,
        success=success,
        error_code=error_code,
        cache_hit=cache_hit,
        estimated=usage.estimated,
        document_id=document_id,
        message_id=message_id,
        request_id=request_id or get_request_id(),
        task_id=task_id,
    )
