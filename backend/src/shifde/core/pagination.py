"""Keyset pagination helpers. IDs are UUIDv7 (time-ordered), so `id` is the cursor key."""

from __future__ import annotations

import base64
import binascii
from uuid import UUID

from pydantic import BaseModel

from shifde.core.errors import ValidationFailed

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


def encode_cursor(last_id: UUID) -> str:
    return base64.urlsafe_b64encode(last_id.bytes).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> UUID | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return UUID(bytes=base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed("invalid cursor") from exc


def paginate[R](rows: list[R], limit: int) -> tuple[list[R], bool]:
    """Split `limit + 1` fetched rows into (page rows, has_more)."""
    return rows[:limit], len(rows) > limit
