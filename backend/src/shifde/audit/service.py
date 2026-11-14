"""Audit use cases: record events (per-tenant hash chain), verify the chain, read events.

Callers pass only non-secret metadata in ``details``; it is additionally scrubbed (field-name
denylist, registered secret values, token patterns) before storage. Events are written in the
caller's transaction (so an audited change and its audit row commit or roll back together) unless
the caller deliberately commits first.

Chain (FR-AUD-002, T12): each tenant event stores ``chain_seq`` (1, 2, ...) and
``hash = sha256(prev_hash || canonical_json(event))`` where ``prev_hash`` is the previous event's
hash (empty for the first). Appends are serialized per tenant by a transaction-level advisory
lock, and a unique index on (tenant_id, chain_seq) makes a fork impossible. Installation-level
events (tenant_id NULL) carry only the per-row content hash. Verification detects modification,
deletion in the middle and reordering; truncating the tail needs an external anchor (not in P2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any  # Any: audit `details` is a redacted free-form JSON document
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.audit import repository
from shifde.audit.models import AuditEvent
from shifde.core.clock import utcnow
from shifde.core.context import RequestContext
from shifde.core.db import forget_platform_admin, get_context, set_platform_admin
from shifde.core.errors import ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.redaction import scrub

# `category` filter on the read API: action-prefix groups.
CATEGORY_PREFIXES: dict[str, tuple[str, ...]] = {
    "security": ("identity.", "authz.", "secret.", "audit."),
    "tenancy": ("tenancy.",),
    "integrations": ("vendor.", "connection.", "model.", "egress.", "adapter."),
    # Phase 8 explorer categories (an absent category means all)
    "admin": (
        "tenancy.", "vendor.", "connection.", "model.", "egress.", "adapter.", "agent.",
        "prompt.", "tool.", "skill.", "orchestration.", "pipeline.", "publish.", "artifact.",
    ),
    "execution": ("run.", "approval."),
    "governance": ("governance.",),
    "data": ("ontology.", "data.", "governance.usage.exported", "audit.exported"),
}  # fmt: skip


# Installation-level actions: events with no client (tenant_id NULL). The table's INSERT policy
# lets a NULL-tenant row in only under the platform-administrator context (B-701), and `record`
# sets that context for the one insert, and only for these actions. Add an action here only if it
# can happen with no client: identity events before a client is known, and installation-wide
# configuration written by a platform administrator or the install tooling.
INSTALLATION_ACTIONS: frozenset[str] = frozenset(
    {
        "identity.login.failed",
        "identity.setup_token.rejected",
        "egress.default.updated",
        "installation.profile.applied",
        # an installation-level secret (tenant_id NULL, e.g. an installation IdP client secret)
        # resolved by `secrets.service.SecretProvider.resolve_system` (B-805); its row is visible
        # only under the platform-administrator context, so only that path reaches this action
        "secret.resolved",
    }
)


class NotInstallationAction(ValueError):
    """A NULL-tenant (installation-level) audit event was asked for under an action that is not
    on `INSTALLATION_ACTIONS`."""


@contextmanager
def _installation_write(session: Session) -> Iterator[None]:
    """Hold the platform-administrator context for one insert and restore the caller's state."""
    was = get_context(session).platform_admin
    set_platform_admin(session, True)
    try:
        yield
    finally:
        if session.is_active:
            set_platform_admin(session, was)
        elif not was:  # a failed statement: the rollback discards the transaction-local setting
            forget_platform_admin(session)


@dataclass(frozen=True)
class ActivityItem:
    at: datetime
    actor: str | None
    action: str
    target: str | None


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    first_broken_id: UUID | None = None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_event(fields: Mapping[str, Any]) -> bytes:  # Any: heterogeneous column values
    doc = {
        "id": str(fields["id"]),
        "tenant_id": str(fields["tenant_id"]) if fields["tenant_id"] else None,
        "occurred_at": _iso(fields["occurred_at"]),
        "actor_type": fields["actor_type"],
        "actor_id": str(fields["actor_id"]) if fields["actor_id"] else None,
        "actor_ip": fields["actor_ip"],
        "auth_method": fields["auth_method"],
        "action": fields["action"],
        "target_type": fields["target_type"],
        "target_id": fields["target_id"],
        "workspace_id": str(fields["workspace_id"]) if fields["workspace_id"] else None,
        "project_id": str(fields["project_id"]) if fields["project_id"] else None,
        "outcome": fields["outcome"],
        "details": fields["details"],
        "trace_id": fields["trace_id"],
        "correlation_id": fields["correlation_id"],
        "chain_seq": fields["chain_seq"],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()


def event_hash(prev_hash: bytes | None, fields: Mapping[str, Any]) -> bytes:
    return hashlib.sha256((prev_hash or b"") + canonical_event(fields)).digest()


def record(
    session: Session,
    ctx: RequestContext,
    *,
    action: str,
    outcome: str = "success",
    tenant_id: UUID | None,
    actor_id: UUID | None = None,
    actor_label: str | None = None,
    auth_method: str | None = None,
    target_type: str | None = None,
    target_id: str | UUID | None = None,
    target_label: str | None = None,
    workspace_id: UUID | None = None,
    project_id: UUID | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Append one audit event. `tenant_id=None` marks an installation-level event, allowed only
    for the actions in `INSTALLATION_ACTIONS` (otherwise `NotInstallationAction`)."""
    if tenant_id is None and action not in INSTALLATION_ACTIONS:
        raise NotInstallationAction(action)
    principal = ctx.principal
    actor_type = "user" if actor_id else "anonymous"
    if actor_id is None and principal is not None:
        actor_id = principal.principal_id
        actor_label = actor_label or principal.email
        auth_method = auth_method or principal.credential
    if actor_id is not None:
        actor_type = "user"
        if (
            principal is not None
            and actor_id == principal.principal_id
            and principal.principal_type == "service_account"
        ):
            actor_type = "service_account"
    body: dict[str, Any] = scrub(dict(details or {}))
    if actor_label:
        body["actor"] = actor_label
    if target_label:
        body["target"] = target_label

    chain_seq: int | None = None
    prev_hash: bytes | None = None
    if tenant_id is not None:
        repository.lock_chain(session, tenant_id)
        head = repository.chain_head(session, tenant_id)
        chain_seq = 1 if head is None else head[0] + 1
        prev_hash = None if head is None else head[1]
    values: dict[str, Any] = {
        "id": uuid7(),
        "tenant_id": tenant_id,
        "occurred_at": utcnow(),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "actor_ip": ctx.client_ip,
        "auth_method": auth_method,
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id) if target_id is not None else None,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "outcome": outcome,
        "details": body,
        "trace_id": ctx.request_id,
        "correlation_id": ctx.request_id,
        "chain_seq": chain_seq,
    }
    values["prev_hash"] = prev_hash
    values["hash"] = event_hash(prev_hash, values)
    if tenant_id is None:
        with _installation_write(session):
            repository.insert_event(session, values)
    else:
        repository.insert_event(session, values)


def verify_chain(session: Session, tenant_id: UUID) -> VerifyResult:
    """Walk the tenant chain in order; report the first event that does not verify."""
    checked = 0
    expected_seq: int | None = None
    prev: bytes | None = None
    for ev in repository.iter_chain(session, tenant_id):
        fields = {
            "id": ev.id,
            "tenant_id": ev.tenant_id,
            "occurred_at": ev.occurred_at,
            "actor_type": ev.actor_type,
            "actor_id": ev.actor_id,
            "actor_ip": ev.actor_ip,
            "auth_method": ev.auth_method,
            "action": ev.action,
            "target_type": ev.target_type,
            "target_id": ev.target_id,
            "workspace_id": ev.workspace_id,
            "project_id": ev.project_id,
            "outcome": ev.outcome,
            "details": ev.details,
            "trace_id": ev.trace_id,
            "correlation_id": ev.correlation_id,
            "chain_seq": ev.chain_seq,
        }
        seq_ok = expected_seq is None or ev.chain_seq == expected_seq
        link_ok = (ev.prev_hash is None and prev is None) or (
            ev.prev_hash is not None and prev is not None and bytes(ev.prev_hash) == prev
        )
        hash_ok = ev.hash is not None and bytes(ev.hash) == event_hash(prev, fields)
        if expected_seq is None and ev.chain_seq != 1:
            seq_ok = False
        if not (seq_ok and link_ok and hash_ok):
            return VerifyResult(False, checked, ev.id)
        checked += 1
        expected_seq = (ev.chain_seq or 0) + 1
        prev = bytes(ev.hash) if ev.hash is not None else None
    return VerifyResult(True, checked)


@dataclass(frozen=True)
class EventRow:
    id: UUID
    occurred_at: datetime
    actor_type: str
    actor_id: UUID | None
    actor: str | None
    action: str
    target_type: str | None
    target_id: str | None
    target: str | None
    outcome: str
    auth_method: str | None
    actor_ip: str | None
    request_id: str | None
    workspace_id: UUID | None
    project_id: UUID | None
    details: dict[str, Any]  # Any: redacted free-form JSON
    chain_seq: int | None
    hash_hex: str | None


def list_events(
    session: Session,
    *,
    tenant_id: UUID,
    category: str | None,
    actor: str | None,
    action: str | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    before: UUID | None,
    limit: int,
    workspace_ids: Collection[UUID] | None = None,
    project_ids: Collection[UUID] | None = None,
) -> list[EventRow]:
    """`workspace_ids` / `project_ids` restrict the result to events of those scopes (scoped
    visibility, FR-AUD-007); both None = the whole tenant."""
    prefixes: tuple[str, ...] | None = None
    if category:
        prefixes = CATEGORY_PREFIXES.get(category)
        if prefixes is None:
            raise ValidationFailed(f"unknown category: {category}")
    actor_id: UUID | None = None
    actor_label: str | None = None
    if actor:
        try:
            actor_id = UUID(actor)
        except ValueError:
            actor_label = actor
    rows = repository.list_events(
        session,
        tenant_id=tenant_id,
        prefixes=prefixes,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        before=before,
        limit=limit,
        workspace_ids=workspace_ids,
        project_ids=project_ids,
    )
    return [_event_row(r) for r in rows]


def _event_row(r: AuditEvent) -> EventRow:
    return EventRow(
        id=r.id,
        occurred_at=r.occurred_at,
        actor_type=r.actor_type,
        actor_id=r.actor_id,
        actor=r.details.get("actor"),
        action=r.action,
        target_type=r.target_type,
        target_id=r.target_id,
        target=r.details.get("target")
        or (f"{r.target_type}:{r.target_id}" if r.target_type and r.target_id else None),
        outcome=r.outcome,
        auth_method=r.auth_method,
        actor_ip=r.actor_ip,
        request_id=r.trace_id,
        workspace_id=r.workspace_id,
        project_id=r.project_id,
        details=r.details,
        chain_seq=r.chain_seq,
        hash_hex=bytes(r.hash).hex() if r.hash is not None else None,
    )


def recent_activity(
    session: Session,
    *,
    tenant_id: UUID,
    tenant_wide: bool,
    visible_workspace_ids: Collection[UUID],
    workspace_id: UUID | None = None,
    project_id: UUID | None = None,
    limit: int = 20,
) -> list[ActivityItem]:
    rows = repository.recent_activity(
        session,
        tenant_id=tenant_id,
        tenant_wide=tenant_wide,
        visible_workspace_ids=visible_workspace_ids,
        workspace_id=workspace_id,
        project_id=project_id,
        limit=limit,
    )
    items: list[ActivityItem] = []
    for r in rows:
        target = r.target_label or (
            f"{r.target_type}:{r.target_id}" if r.target_type and r.target_id else None
        )
        items.append(ActivityItem(at=r.occurred_at, actor=r.actor, action=r.action, target=target))
    return items
