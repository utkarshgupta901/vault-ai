"""Opaque keyset cursors. A cursor encodes the sort key of the last item returned (e.g. created_at + id), so the next
page is `WHERE (created_at, id) < (cursor)` — stable under concurrent inserts and O(log n) with the matching index,
unlike OFFSET which rescans and skips/duplicates rows when data changes.

A cursor comes back from the client, so it is untrusted input: anything that is not exactly what encode_cursor wrote
(bad base64, not a JSON object, a missing key, a wrong type, an unparsable value) is 400 INVALID_CURSOR, never a 500.
Use `decode_keyset` to decode AND convert the fields in one step, so no caller can forget part of that check."""

import base64
import json
from collections.abc import Callable
from typing import Any

from app.core.errors import AppError, ErrorCode


def _invalid() -> AppError:
    return AppError(ErrorCode.INVALID_CURSOR, 400, "Invalid pagination cursor.")


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
        raise _invalid() from exc


def _scalar(value: Any) -> str:
    """encode_cursor only writes strings (timestamps, ids) and integers (seq). Anything else (null, a number where a
    string belongs, a list) is a tampered cursor. Converters then always get a str: uuid.UUID(123) would raise
    AttributeError, not ValueError."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise TypeError(f"cursor field must be a string or an integer, not {type(value).__name__}")
    return str(value)


def decode_keyset(cursor: str | None, **fields: Callable[[str], Any]) -> tuple[Any, ...] | None:
    """Decode a cursor and convert its fields, in the order given; None when there is no cursor (first page):

        decode_keyset(cursor, created_at=datetime.fromisoformat, id=uuid.UUID)  ->  (datetime, UUID) | None

    Every listed key must be present (so `{}` is invalid, not "page 1") and convert cleanly; otherwise 400."""
    data = decode_cursor(cursor)
    if data is None:
        return None
    try:
        return tuple(convert(_scalar(data[key])) for key, convert in fields.items())
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid() from exc
