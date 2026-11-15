"""Governance use cases: pricing (part 1 of the port), then usage reporting, budgets / quotas,
policies, retention, the decision log, performance and platform health (parts 2 and 3).

Honesty rules (contract section 0): every money figure is reported per cost kind; nothing is
presented as an invoice; what is stored but not enforced says so (`enforcement`,
`on_exceed_effective`, retention `enforcement: not_implemented`).

`GovernanceService` (usage, budgets, quotas, policies, retention, decisions, audit export,
performance) and `health_details` are not ported yet: parts 2 and 3 (DECISIONS B-1801).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any  # Any: rows / JSON documents
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import PDP, ResourceRef
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import Conflict, Forbidden, NotFound, ScopeArchived, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page
from shifde.governance import repository as repo
from shifde.governance import schemas as s
from shifde.tenancy.scope import TenantScope

MAX_WINDOW_DAYS = 366
EXPORT_ROW_LIMIT = 100_000
ON_EXCEED_ENFORCED = "deny"  # queue_until_reset / require_approval are stored, enforced as deny
ON_EXCEED_SOFT = "alert_only"  # a soft budget never denies: it only raises threshold alerts


def _on_exceed_effective(row: Mapping[str, Any]) -> str:  # Any: budget row
    return ON_EXCEED_SOFT if row["enforcement"] == "soft" else ON_EXCEED_ENFORCED


NOT_AN_INVOICE = (
    "Costs are calculated from rate cards (or reported by the provider where marked); they are "
    "not invoices."
)


def _num(value: object) -> float | None:
    return None if value is None else float(Decimal(str(value)))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _csv_cell(value: object) -> str:
    """CSV-injection safe cell (a leading = + - @ would be executed by a spreadsheet). Numbers
    are not formulas: a negative figure (a correction row) stays a number."""
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        return str(value)
    text_value = "" if value is None else str(value)
    return "'" + text_value if text_value[:1] in ("=", "+", "-", "@", "\t", "\r") else text_value


def _csv(rows: list[list[object]], header: list[str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_cell(c) for c in row])
    return out.getvalue()


# ====================================================================== pricing
@dataclass
class PricingService:
    """Rate cards: tenant context (`pricing:*`) or platform context (`platform:admin`)."""

    session: Session
    ctx: RequestContext
    principal: Principal
    pdp: PDP
    tenant_id: UUID | None
    tenant_status: str = "active"

    @classmethod
    def for_tenant(cls, scope: TenantScope) -> PricingService:
        return cls(
            scope.session,
            scope.ctx,
            scope.principal,
            scope.pdp,
            scope.tenant_id,
            scope.tenant_status,
        )

    @classmethod
    def for_platform(
        cls, session: Session, ctx: RequestContext, principal: Principal
    ) -> PricingService:
        return cls(session, ctx, principal, authz.new_pdp(session, principal, None), None)

    def _require(self, action: str) -> None:
        if self.tenant_id is None:
            self.pdp.require("platform:admin")
            return
        self.pdp.require(action, ResourceRef(self.tenant_id))
        if action.endswith(":manage") and self.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    @staticmethod
    def _card_out(r: Mapping[str, Any]) -> s.RateCardOut:  # Any: row
        return s.RateCardOut(
            id=r["id"],
            provider_key=r["provider_key"],
            model_key=r["model_key"],
            name=r["name"],
            scope="global" if r["tenant_id"] is None else "tenant",
            created_at=r["created_at"],
        )

    @staticmethod
    def version_out(r: Mapping[str, Any]) -> s.RateCardVersionOut:  # Any: row
        return s.RateCardVersionOut(
            id=r["id"],
            rate_card_id=r["rate_card_id"],
            version_no=r["version_no"],
            effective_from=r["effective_from"],
            effective_to=r["effective_to"],
            currency=r["currency"],
            unit=r["unit"],
            input_rate=float(r["input_rate"]),
            output_rate=float(r["output_rate"]),
            cached_read_rate=_num(r["cached_read_rate"]),
            cached_write_rate=_num(r["cached_write_rate"]),
            other_rates=dict(r["other_rates"] or {}),
            source=r["source"],
            revision=r["revision"],
            created_at=r["created_at"],
        )

    def _audit(
        self, action: str, target_type: str, target_id: UUID, label: str, d: dict[str, Any]
    ) -> None:  # Any: details
        def record() -> None:
            audit.record(
                self.session,
                self.ctx,
                action=action,
                tenant_id=self.tenant_id,
                target_type=target_type,
                target_id=target_id,
                target_label=label,
                details=d,
            )

        if self.tenant_id is None:  # a global rate card: an installation-level event (B-1803)
            with db.platform_admin_context(self.session):
                record()
        else:
            record()

    def _card(self, card_id: UUID) -> Mapping[str, Any]:  # Any: row
        row = repo.get_card(self.session, self.tenant_id, card_id)
        if row is None or (self.tenant_id is None and row["tenant_id"] is not None):
            raise NotFound("rate card not found")
        return row

    # ------------------------------------------------------------------ cards
    def list_cards(self) -> Page[s.RateCardOut]:
        self._require("pricing:read")
        rows = repo.list_cards(self.session, self.tenant_id)
        return Page(items=[self._card_out(r) for r in rows])

    def create_card(self, body: s.RateCardCreate) -> s.RateCardOut:
        self._require("pricing:manage")
        card_id = uuid7()
        values = {
            "id": card_id,
            "tenant_id": self.tenant_id,
            "provider_key": body.provider_key,
            "model_key": body.model_key,
            "name": body.name,
            "created_by": self.principal.principal_id,
        }
        try:
            if self.tenant_id is None:
                with db.platform_admin_context(self.session):
                    repo.insert_card(self.session, values)
                    self.session.flush()
            else:
                repo.insert_card(self.session, values)
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict("a rate card for this provider and model already exists") from exc
        row = self._card(card_id)
        self._audit(
            "governance.rate_card.created",
            "rate_card",
            card_id,
            f"rate_card:{body.provider_key}/{body.model_key}",
            {"provider_key": body.provider_key, "model_key": body.model_key},
        )
        self.session.commit()
        return self._card_out(row)

    # ------------------------------------------------------------------ versions
    def list_versions(self, card_id: UUID) -> Page[s.RateCardVersionOut]:
        self._require("pricing:read")
        self._card(card_id)
        return Page(
            items=[
                self.version_out(r)
                for r in repo.list_versions(self.session, self.tenant_id, card_id)
            ]
        )

    def create_version(self, card_id: UUID, body: s.RateCardVersionCreate) -> s.RateCardVersionOut:
        self._require("pricing:manage")
        card = self._card(card_id)
        if card["tenant_id"] != self.tenant_id:
            raise Forbidden("global rate cards can only be versioned by a platform administrator")
        start = _aware(body.effective_from)
        end = _aware(body.effective_to) if body.effective_to else None
        if end is not None and end <= start:
            raise ValidationFailed("effective_to must be after effective_from")
        version_id = uuid7()
        values = {
            "id": version_id,
            "rate_card_id": card_id,
            "tenant_id": card["tenant_id"],
            "version_no": repo.next_version_no(self.session, self.tenant_id, card_id),
            "effective_from": start,
            "effective_to": end,
            "currency": body.currency,
            "unit": body.unit,
            "input_rate": Decimal(str(body.input_rate)),
            "output_rate": Decimal(str(body.output_rate)),
            "cached_read_rate": None
            if body.cached_read_rate is None
            else Decimal(str(body.cached_read_rate)),
            "cached_write_rate": None
            if body.cached_write_rate is None
            else Decimal(str(body.cached_write_rate)),
            "other_rates": body.other_rates,
            "source": body.source,
            "revision": body.revision,
            "created_by": self.principal.principal_id,
        }
        try:
            if self.tenant_id is None:
                with db.platform_admin_context(self.session):
                    row = repo.insert_version(self.session, values)
            else:
                row = repo.insert_version(self.session, values)
        except IntegrityError as exc:
            self.session.rollback()
            if getattr(exc.orig, "sqlstate", None) == "23P01":
                if "cannot start in the past" in str(exc.orig):
                    raise Conflict(
                        "a new version that supersedes the current open-ended one cannot start "
                        "in the past (it would rewrite earlier resolutions); start it now or later"
                    ) from exc
                raise Conflict("the effective range overlaps an existing version") from exc
            raise Conflict("the rate card version conflicts with an existing one") from exc
        self._audit(
            "governance.rate_card_version.created",
            "rate_card",
            card_id,
            f"rate_card:{card['provider_key']}/{card['model_key']}",
            {
                "version_no": row["version_no"],
                "currency": body.currency,
                "unit": body.unit,
                "effective_from": start.isoformat(),
                "source": body.source,
                "revision": body.revision,
            },
        )
        self.session.commit()
        return self.version_out(row)

    # ------------------------------------------------------------------ resolve
    def resolve(self, provider_key: str, model_key: str, at: datetime | None) -> s.ResolvedRateOut:
        self._require("pricing:read")
        if self.tenant_id is None:
            raise NotFound("rate card not found")
        moment = _aware(at) if at else utcnow()
        version = ledger.pricing.resolve(
            self.session, self.tenant_id, provider_key, model_key, moment
        )
        if version is None:
            raise NotFound("no rate card version is in force for this model at that time")
        card = self._card(version.rate_card_id)
        row = next(
            r
            for r in repo.list_versions(self.session, self.tenant_id, version.rate_card_id)
            if r["id"] == version.id
        )
        return s.ResolvedRateOut(
            card=self._card_out(card), version=self.version_out(row), at=moment
        )


# ====================================================================== governance
