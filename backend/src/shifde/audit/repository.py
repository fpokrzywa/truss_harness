"""SQL for audit events. Append-only: this module only ever INSERTs and SELECTs."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any  # Any: audit `details` is a redacted free-form JSON document
from uuid import UUID

from sqlalchemy import insert, or_, select, text
from sqlalchemy.orm import Session

from shifde.audit.models import AuditEvent


@dataclass(frozen=True)
class ActivityRow:
    occurred_at: datetime
    actor: str | None
    action: str
    target_type: str | None
    target_id: str | None
    target_label: str | None


def insert_event(session: Session, values: dict[str, Any]) -> None:
    # Core INSERT without RETURNING: installation-level rows (tenant_id NULL) are not
    # SELECT-visible to ordinary principals, so RETURNING would trip RLS.
    session.execute(insert(AuditEvent).values(**values))


def recent_activity(
    session: Session,
    *,
    tenant_id: UUID,
    tenant_wide: bool,
    visible_workspace_ids: Collection[UUID],
    workspace_id: UUID | None,
    project_id: UUID | None,
    limit: int,
) -> list[ActivityRow]:
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if workspace_id is not None:
        stmt = stmt.where(AuditEvent.workspace_id == workspace_id)
    if project_id is not None:
        stmt = stmt.where(AuditEvent.project_id == project_id)
    if not tenant_wide:
        stmt = stmt.where(AuditEvent.workspace_id.in_(list(visible_workspace_ids)))
    stmt = stmt.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit)
    rows = session.scalars(stmt).all()
    return [
        ActivityRow(
            occurred_at=r.occurred_at,
            actor=r.details.get("actor"),
            action=r.action,
            target_type=r.target_type,
            target_id=r.target_id,
            target_label=r.details.get("target"),
        )
        for r in rows
    ]


def lock_chain(session: Session, tenant_id: UUID) -> None:
    """Serialize chain appends for one tenant until the transaction ends."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"audit:{tenant_id}"}
    )


def chain_head(session: Session, tenant_id: UUID) -> tuple[int, bytes] | None:
    row = session.execute(
        select(AuditEvent.chain_seq, AuditEvent.hash)
        .where(AuditEvent.tenant_id == tenant_id, AuditEvent.chain_seq.is_not(None))
        .order_by(AuditEvent.chain_seq.desc())
        .limit(1)
    ).first()
    if row is None or row[0] is None or row[1] is None:
        return None
    return int(row[0]), bytes(row[1])


def iter_chain(session: Session, tenant_id: UUID) -> Iterator[AuditEvent]:
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_id, AuditEvent.chain_seq.is_not(None))
        .order_by(AuditEvent.chain_seq)
        .execution_options(yield_per=500)
    )
    yield from session.scalars(stmt)


def list_events(
    session: Session,
    *,
    tenant_id: UUID,
    prefixes: Sequence[str] | None,
    actor_id: UUID | None,
    actor_label: str | None,
    action: str | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    before: UUID | None,
    limit: int,
    workspace_ids: Collection[UUID] | None = None,
    project_ids: Collection[UUID] | None = None,
) -> list[AuditEvent]:
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if workspace_ids is not None or project_ids is not None:  # scoped visibility
        stmt = stmt.where(
            or_(
                AuditEvent.workspace_id.in_(list(workspace_ids or [])),
                AuditEvent.project_id.in_(list(project_ids or [])),
            )
        )
    if prefixes:
        stmt = stmt.where(or_(*[AuditEvent.action.startswith(p) for p in prefixes]))
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if actor_label:
        escaped = actor_label.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        stmt = stmt.where(AuditEvent.details["actor"].astext.ilike(f"%{escaped}%", escape="\\"))
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if occurred_from is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= occurred_from)
    if occurred_to is not None:
        stmt = stmt.where(AuditEvent.occurred_at <= occurred_to)
    if before is not None:
        stmt = stmt.where(AuditEvent.id < before)
    return list(session.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit)))
