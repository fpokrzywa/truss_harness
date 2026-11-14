"""Clock indirection so time-dependent logic is testable."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
