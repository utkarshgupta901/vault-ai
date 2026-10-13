"""Opaque keyset cursors. A cursor encodes the sort key of the last item returned (e.g. created_at + id), so the next
page is `WHERE (created_at, id) < (cursor)` — stable under concurrent inserts and O(log n) with the matching index,
unlike OFFSET which rescans and skips/duplicates rows when data changes."""

import base64
import json
from typing import Any

from app.core.errors import AppError, ErrorCode


def encode_cursor(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, default=str).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.INVALID_CURSOR, 400, "Invalid pagination cursor.") from exc
