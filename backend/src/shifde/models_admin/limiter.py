"""DB-backed rate and concurrency limiter (shared by every API worker; time is the DB clock).

* Requests per minute: one counter row per (bucket, minute window). The increment is a single
  ``INSERT ... ON CONFLICT DO UPDATE ... WHERE requests < limit``, so concurrent callers can never
  exceed the limit and rejected attempts are not counted.
* Tokens per minute: checked before the call (current window + estimate), accounted after it.
* Concurrency: short-lived leases serialized per bucket by an advisory lock; leases expire, so a
  crashed worker cannot hold a slot forever. Acquire/release commit immediately so other workers see
  them; callers must therefore not hold unrelated uncommitted work when calling them.

Every statement names the client (`tenant_id`) as well as running under row-level security.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.errors import ModelRateLimited
from shifde.core.ids import uuid7

LEASE_SECONDS = 120


def take_request(session: Session, tenant_id: UUID, bucket: str, limit: int | None) -> None:
    """Count one request in the current minute or raise ModelRateLimited."""
    row = session.execute(
        text(
            "INSERT INTO shifde.rate_limit_windows AS w "
            "(tenant_id, bucket_key, window_start, requests) "
            "VALUES (:t, :b, date_trunc('minute', now()), 1) "
            "ON CONFLICT (tenant_id, bucket_key, window_start) DO UPDATE "
            "SET requests = w.requests + 1 WHERE w.requests < :limit RETURNING requests"
        ),
        {"t": tenant_id, "b": bucket, "limit": limit if limit is not None else 2**62},
    ).first()
    if row is None:
        raise ModelRateLimited("the model's requests-per-minute limit is exhausted; retry shortly")


def check_tokens(
    session: Session, tenant_id: UUID, bucket: str, limit: int | None, estimate: int
) -> None:
    if limit is None:
        return
    used = session.execute(
        text(
            "SELECT COALESCE(sum(tokens), 0) FROM shifde.rate_limit_windows WHERE tenant_id = :t "
            "AND bucket_key = :b AND window_start = date_trunc('minute', now())"
        ),
        {"t": tenant_id, "b": bucket},
    ).scalar_one()
    if int(used) + estimate > limit:
        raise ModelRateLimited("the model's tokens-per-minute limit is exhausted; retry shortly")


def add_tokens(session: Session, tenant_id: UUID, bucket: str, tokens: int) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.rate_limit_windows AS w "
            "(tenant_id, bucket_key, window_start, tokens) "
            "VALUES (:t, :b, date_trunc('minute', now()), :n) "
            "ON CONFLICT (tenant_id, bucket_key, window_start) DO UPDATE "
            "SET tokens = w.tokens + :n"
        ),
        {"t": tenant_id, "b": bucket, "n": tokens},
    )


def acquire_lease(
    session: Session,
    tenant_id: UUID,
    bucket: str,
    max_concurrency: int | None,
    ttl_seconds: float = LEASE_SECONDS,
) -> UUID | None:
    """Take a concurrency slot (committed immediately); None when the bucket is unlimited."""
    if max_concurrency is None:
        return None
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"lease:{tenant_id}:{bucket}"},
    )
    session.execute(
        text(
            "DELETE FROM shifde.concurrency_leases WHERE tenant_id = :t AND bucket_key = :b "
            "AND expires_at < now()"
        ),
        {"t": tenant_id, "b": bucket},
    )
    active = session.execute(
        text(
            "SELECT count(*) FROM shifde.concurrency_leases "
            "WHERE tenant_id = :t AND bucket_key = :b"
        ),
        {"t": tenant_id, "b": bucket},
    ).scalar_one()
    if int(active) >= max_concurrency:
        session.rollback()
        raise ModelRateLimited("the model's concurrency limit is reached; retry shortly")
    lease_id = uuid7()
    session.execute(
        text(
            "INSERT INTO shifde.concurrency_leases (id, tenant_id, bucket_key, expires_at) "
            "VALUES (:i, :t, :b, now() + make_interval(secs => :ttl))"
        ),
        {"i": lease_id, "t": tenant_id, "b": bucket, "ttl": ttl_seconds},
    )
    session.commit()
    return lease_id


def release_lease(session: Session, tenant_id: UUID, lease_id: UUID | None) -> None:
    """Drop a lease of `tenant_id` (keyed by client as well as by row-level security, B-1704)."""
    if lease_id is None:
        return
    if session.in_transaction() and not session.is_active:
        session.rollback()
    session.execute(
        text("DELETE FROM shifde.concurrency_leases WHERE id = :i AND tenant_id = :t"),
        {"i": lease_id, "t": tenant_id},
    )
    session.commit()
