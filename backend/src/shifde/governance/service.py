"""Governance use cases: pricing (part 1 of the port), then usage reporting, budgets / quotas,
policies, retention, the decision log, performance and platform health (parts 2 and 3).

Honesty rules (contract section 0): every money figure is reported per cost kind; nothing is
presented as an invoice; what is stored but not enforced says so (`enforcement`,
`on_exceed_effective`, retention `enforcement: not_implemented`).

`GovernanceService` here holds usage, budgets, quotas and the audit export (part 2, B-1811);
policies, retention, decisions, overview, performance and `health_details` are part 3.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast  # Any: rows / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.adapters.runtime import IntegrationRuntime
from shifde.audit import service as audit
from shifde.authz import service as authz
from shifde.authz.service import PDP, ResourceRef
from shifde.core import db
from shifde.core.clock import utcnow
from shifde.core.context import Principal, RequestContext
from shifde.core.errors import Conflict, Forbidden, NotFound, ScopeArchived, ValidationFailed
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.governance import repository as repo
from shifde.governance import schemas as s
from shifde.governance.validation import (
    RETENTION_DEFAULTS,
    RETENTION_FLOORS,
    enforcement_of,
    validate_definition,
)
from shifde.ledger import budgeting
from shifde.ledger.usage import ALL, Visibility
from shifde.models_admin import schemas as ms
from shifde.models_admin.service import ModelService
from shifde.observability import tracing
from shifde.tenancy.scope import TenantScope

MAX_WINDOW_DAYS = 366
EXPORT_ROW_LIMIT = 100_000
ON_EXCEED_ENFORCED = "deny"  # queue_until_reset / require_approval are stored, enforced as deny
ON_EXCEED_SOFT = "alert_only"  # a soft budget never denies: it only raises threshold alerts


_BUDGET_AUDITED = ("amount", "limit_runs", "enforcement", "on_exceed", "status")


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


def export_row_cap(session: Session, tenant_id: UUID) -> int | None:
    """Active `data_export` policies can forbid exports (403) or cap their size: the smallest
    `max_rows`, None without a cap. Shared by the usage, audit and ontology exports."""
    cap: int | None = None
    for pol in repo.list_policies(session, tenant_id, "data_export"):
        if pol["status"] != "active":
            continue
        d = pol["definition"] or {}
        if d.get("allow_export") is False:
            raise Forbidden("a data_export policy forbids exports")
        if isinstance(d.get("max_rows"), int):
            cap = d["max_rows"] if cap is None else min(cap, d["max_rows"])
    return cap


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
    def resolve(
        self,
        provider_key: str | None,
        model_key: str | None,
        at: datetime | None,
        model_id: UUID | None = None,
    ) -> s.ResolvedRateOut:
        """The rate in force for adapter + model key, or for a model of this client.

        By `model_id` the answer is what pricing really uses (B-1822): a shared SHI model is
        priced from global cards only, so the client's own card for the same keys is not shown
        for it. By keys the client's own card wins over the global one, as for its own models.
        """
        self._require("pricing:read")
        if self.tenant_id is None:
            raise NotFound("rate card not found")
        global_only = False
        if model_id is not None:
            keys = ledger.pricing.rate_keys(self.session, self.tenant_id, model_id)
            if keys is None:
                raise NotFound("model not found")
            provider_key, model_key, global_only = keys
        elif provider_key is None or model_key is None:
            raise ValidationFailed("give model_id, or provider_key and model_key")
        moment = _aware(at) if at else utcnow()
        version = ledger.pricing.resolve(
            self.session, self.tenant_id, provider_key, model_key, moment, global_only=global_only
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


# ====================================================================== governance
class GovernanceService:
    def __init__(self, scope: TenantScope, runtime: IntegrationRuntime | None = None) -> None:
        """`runtime` is only needed by model policies, which write through to the model
        allowlist (`models_admin`); without it they answer 422 (SETH's behaviour)."""
        self.sc = scope
        self.runtime = runtime

    @property
    def session(self) -> Session:
        return self.sc.session

    @property
    def tenant_id(self) -> UUID:
        return self.sc.tenant_id

    # ------------------------------------------------------------------ helpers
    def _require(self, action: str, ref: ResourceRef | None = None) -> None:
        self.sc.pdp.require(action, ref or self.sc.tenant_ref)

    def _require_manage(self, action: str, ref: ResourceRef | None = None) -> None:
        self._require(action, ref)
        if self.sc.tenant_status != "active":
            raise ScopeArchived("organization is archived")

    def _vis(self, action: str) -> Visibility:
        """Rows the caller may see for a read permission: the whole tenant, or its scopes."""
        sf = self.sc.pdp.visible_scopes(action)
        if sf.tenant_wide:
            return ALL
        if not sf.workspace_ids and not sf.project_ids:
            self.sc.pdp.require(action, self.sc.tenant_ref)  # raises the standard 403
        return Visibility(False, tuple(sf.workspace_ids), tuple(sf.project_ids))

    def _audit(
        self,
        action: str,
        target_type: str,
        target_id: UUID | str,
        label: str,
        details: Mapping[str, Any],  # Any: audit details
        *,
        workspace_id: UUID | None = None,
        project_id: UUID | None = None,
        outcome: str = "success",
    ) -> None:
        audit.record(
            self.session,
            self.sc.ctx,
            action=action,
            outcome=outcome,
            tenant_id=self.tenant_id,
            target_type=target_type,
            target_id=target_id,
            target_label=label,
            workspace_id=workspace_id,
            project_id=project_id,
            details=details,
        )

    def _scope_ref(
        self, scope_type: str, scope_id: UUID
    ) -> tuple[ResourceRef, UUID | None, UUID | None]:
        """(resource ref for permission checks, workspace id, project id) of a rule's scope."""
        if scope_type == "tenant":
            if scope_id != self.tenant_id:
                raise NotFound("scope not found")
            return self.sc.tenant_ref, None, None
        found = repo.scope_workspace_project(self.session, self.tenant_id, scope_type, scope_id)
        if found is None:
            raise NotFound("scope not found")
        ws, proj = found
        return ResourceRef(self.tenant_id, ws, proj), ws, proj

    @staticmethod
    def _immutable(*checks: tuple[str, object, object]) -> None:
        """A client may echo immutable fields back; a different value is refused."""
        for name, given, current in checks:
            if given is not None and given != current:
                raise ValidationFailed(f"{name} cannot be changed; create a new one instead")

    def _row_visible(self, vis: Visibility, scope_type: str, scope_id: UUID) -> bool:
        if vis.all:
            return True
        if scope_type == "tenant":
            return False
        found = repo.scope_workspace_project(self.session, self.tenant_id, scope_type, scope_id)
        if found is None:
            return False
        ws, proj = found
        return (ws is not None and ws in vis.workspace_ids) or (
            proj is not None and proj in vis.project_ids
        )

    def _window(
        self, start: datetime | None, end: datetime | None, default_days: int
    ) -> tuple[date, date]:
        to_day = _aware(end).astimezone(UTC).date() if end else utcnow().date()
        from_day = (
            _aware(start).astimezone(UTC).date() if start else to_day - timedelta(days=default_days)
        )
        if from_day > to_day:
            raise ValidationFailed("`from` must not be after `to`")
        if (to_day - from_day).days > MAX_WINDOW_DAYS:
            raise ValidationFailed(f"the window is limited to {MAX_WINDOW_DAYS} days")
        return from_day, to_day

    def _ws_filter(self, value: str | None) -> UUID | None:
        if not value:
            return None
        row = self.session.execute(
            text(
                "SELECT id FROM shifde.workspaces WHERE tenant_id = :t "
                "AND (slug = :v OR id::text = :v)"
            ),
            {"v": value, "t": self.tenant_id},
        ).first()
        if row is None:
            raise NotFound("workspace not found")
        return UUID(str(row[0]))

    def _project_filter(self, value: str | None, ws_id: UUID | None = None) -> UUID | None:
        """A project by id or slug. Slugs are unique per workspace only: without a workspace
        filter an ambiguous slug is refused rather than resolved to an arbitrary project."""
        if not value:
            return None
        rows = self.session.execute(
            text(
                "SELECT id FROM shifde.projects WHERE tenant_id = :t "
                "AND (slug = :v OR id::text = :v) "
                "AND (CAST(:ws AS uuid) IS NULL OR workspace_id = :ws) ORDER BY id LIMIT 2"
            ),
            {"v": value, "ws": ws_id, "t": self.tenant_id},
        ).all()
        if not rows:
            raise NotFound("project not found")
        if len(rows) > 1:
            raise ValidationFailed(
                "the project slug exists in several workspaces: pass `workspace` as well"
            )
        return UUID(str(rows[0][0]))

    # ================================================================== usage
    @staticmethod
    def event_out(r: Mapping[str, Any]) -> s.UsageEventOut:  # Any: row
        return s.UsageEventOut(
            id=r["id"],
            event_key=r["event_key"],
            occurred_at=r["occurred_at"],
            event_type=r["event_type"],
            status=r["status"],
            run_id=r["run_id"],
            root_run_id=r["root_run_id"],
            step_id=r["step_id"],
            step_name=r["step_name"],
            attempt=r["attempt"],
            workspace_id=r["workspace_id"],
            project_id=r["project_id"],
            principal_id=r["principal_id"],
            agent_id=r["agent_id"],
            model_id=r["model_id"],
            provider_key=r["provider_key"],
            model_key=r["model_key"],
            is_test=r["is_test"],
            input_tokens=int(r["input_tokens"]),
            output_tokens=int(r["output_tokens"]),
            cached_read_tokens=int(r["cached_read_tokens"]),
            cached_write_tokens=int(r["cached_write_tokens"]),
            call_count=int(r["call_count"]),
            duration_ms=r["duration_ms"],
            cost_amount=_num(r["cost_amount"]),
            currency=r["currency"],
            cost_kind=r["cost_kind"],
            rate_card_version_id=r["rate_card_version_id"],
            rate_snapshot=dict(r["rate_snapshot"]) if r["rate_snapshot"] else None,
            corrects_event_id=r["corrects_event_id"],
        )

    def list_events(
        self,
        *,
        run_id: UUID | None,
        start: datetime | None,
        end: datetime | None,
        event_type: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[s.UsageEventOut]:
        vis = self._vis("usage:read")
        rows = repo.list_events(
            self.session,
            self.tenant_id,
            vis=vis,
            run_id=run_id,
            start=_aware(start) if start else None,
            end=_aware(end) if end else None,
            event_type=event_type,
            before=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self.event_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    def summary(
        self,
        *,
        group_by: str,
        start: datetime | None,
        end: datetime | None,
        workspace: str | None,
        project: str | None,
        cost_kind: str | None,
        currency: str | None,
        deep: bool = False,
    ) -> s.UsageSummaryOut:
        if group_by not in (*repo.DIM_COLUMNS, "run"):
            raise ValidationFailed(
                f"group_by must be one of {', '.join([*repo.DIM_COLUMNS, 'run'])}"
            )
        if cost_kind is not None and cost_kind not in ledger.COST_KINDS:
            raise ValidationFailed(f"cost_kind must be one of {', '.join(ledger.COST_KINDS)}")
        vis = self._vis("usage:read")
        from_day, to_day = self._window(start, end, 30)
        with tracing.start_span("governance.usage_summary", {"group_by": group_by}):
            ws_id = self._ws_filter(workspace)
            pj_id = self._project_filter(project, ws_id)
            rows = repo.summary_rows(
                self.session,
                self.tenant_id,
                group_by,
                from_day,
                to_day,
                vis=vis,
                workspace_id=ws_id,
                project_id=pj_id,
                cost_kind=cost_kind,
                limit=500,
            )
            whole = repo.totals_rows(
                self.session,
                self.tenant_id,
                from_day,
                to_day,
                vis=vis,
                workspace_id=ws_id,
                project_id=pj_id,
                cost_kind=cost_kind,
            )
            report_currency, other = self._pick_currency(whole, currency)
            groups, _ = self._merge(rows, report_currency)
            whole_groups, _ = self._merge(whole, report_currency)
            totals_group = whole_groups[0] if whole_groups else self._merge([], report_currency)[1]
            groups.sort(
                key=lambda g: (-g.cost.total_known, -(g.input_tokens + g.output_tokens), g.key)
            )
            groups = groups[:500]
            names = repo.labels(self.session, self.tenant_id, group_by, [g.key for g in groups])
            wanted = {
                UUID(v["id"])
                for g in [*groups, *whole_groups]
                for v in g.rate_card_versions
                if "id" in v
            }
            version_nos = repo.rate_versions(self.session, self.tenant_id, sorted(wanted))
            for g in [*groups, *whole_groups]:
                g.label = names.get(g.key)
                g.rate_card_versions = [
                    {"id": v["id"], "version_no": version_nos.get(UUID(v["id"]))}
                    for v in g.rate_card_versions
                ]
            totals = s.UsageTotalsOut(**totals_group.model_dump(exclude={"key", "label"}))
            rec = ledger.reconcile(self.session, self.tenant_id, from_day, to_day, vis, deep=deep)
        notes = [
            NOT_AN_INVOICE,
            "Unmeasured counts model calls with no rate card in force; they are NOT priced and "
            "are not part of 'total known'.",
            "Usage is bucketed by whole UTC days; the window is [from, to] inclusive.",
        ]
        if group_by == "run":
            notes.append(
                "Grouped by run: the most recent 500 runs are listed; totals cover the window."
            )
        if other:
            notes.append(
                "Other currencies in this window are excluded from the cost figures: "
                + ", ".join(f"{c} {a:g}" for c, a in sorted(other.items()))
                + ". Pass `currency` to report one of them."
            )
        return s.UsageSummaryOut(
            groups=groups,
            totals=totals,
            reconciliation=s.ReconciliationOut(
                events_sum=rec.events_sum,
                rollup_sum=rec.rollup_sum,
                consistent=rec.consistent,
                mismatched_rows=rec.mismatched,
                detail=rec.detail,
                level=rec.level,
            ),
            group_by=group_by,
            from_day=from_day.isoformat(),
            to_day=to_day.isoformat(),
            notes=notes,
        )

    @staticmethod
    def _pick_currency(
        rows: list[Mapping[str, Any]],
        wanted: str | None,  # Any: rows
    ) -> tuple[str | None, dict[str, float]]:
        totals: dict[str, float] = defaultdict(float)
        for r in rows:
            if r["currency"] and r["cost_kind"] != "unmeasured":
                totals[r["currency"]] += abs(float(r["cost_amount"]))
        if not totals:
            return wanted, {}
        chosen = wanted or max(totals, key=lambda c: totals[c])
        return chosen, {c: a for c, a in totals.items() if c != chosen}

    @staticmethod
    def _merge(
        rows: list[Mapping[str, Any]],
        currency: str | None,  # Any: rows
    ) -> tuple[list[s.UsageGroupOut], s.UsageTotalsOut]:
        def blank(key: str) -> s.UsageGroupOut:
            return s.UsageGroupOut(
                key=key,
                label=None,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                calls=0,
                duration_ms=0,
                cost=s.CostBreakdownOut(
                    provider_reported=0,
                    calculated=0,
                    estimated_infra=0,
                    unmeasured_count=0,
                    total_known=0,
                ),
                currency=currency,
            )

        by_key: dict[str, s.UsageGroupOut] = {}
        total = blank("")
        versions: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            for g in (by_key.setdefault(r["key"], blank(r["key"])), total):
                g.input_tokens += int(r["input_tokens"])
                g.output_tokens += int(r["output_tokens"])
                g.cached_tokens += int(r["cached_tokens"])
                g.calls += int(r["calls"])
                g.duration_ms += int(r["duration_ms"])
                g.cost.unmeasured_count += int(r["unmeasured_priceable"])
                if r["cost_kind"] != "unmeasured" and r["currency"] == currency:
                    amount = float(r["cost_amount"])
                    setattr(g.cost, r["cost_kind"], getattr(g.cost, r["cost_kind"]) + amount)
                    g.cost.total_known += amount
            if r["rate_card_version_id"] is not None and r["cost_kind"] in (
                "calculated",
                "provider_reported",
            ):
                versions[r["key"]].add(str(r["rate_card_version_id"]))
        groups = list(by_key.values())
        for g in groups:
            g.rate_card_versions = [{"id": v} for v in sorted(versions[g.key])]
        return groups, s.UsageTotalsOut(
            input_tokens=total.input_tokens,
            output_tokens=total.output_tokens,
            cached_tokens=total.cached_tokens,
            calls=total.calls,
            duration_ms=total.duration_ms,
            cost=total.cost,
            currency=currency,
        )

    def run_usage(self, run_id: UUID) -> s.RunUsageOut:
        vis = self._vis("usage:read")
        events = repo.run_events(self.session, self.tenant_id, run_id, vis)
        if not events:
            # A run with no usage yet is still a run of this client.
            run = self.session.execute(
                text(
                    "SELECT workspace_id, project_id FROM shifde.runs "
                    "WHERE id = :r AND tenant_id = :t"
                ),
                {"r": run_id, "t": self.tenant_id},
            ).first()
            if run is None or not (
                vis.all or run[0] in vis.workspace_ids or run[1] in vis.project_ids
            ):
                raise NotFound("run not found")
        out = [self.event_out(r) for r in events]
        cost = s.CostBreakdownOut(
            provider_reported=0, calculated=0, estimated_infra=0, unmeasured_count=0, total_known=0
        )
        currencies = {e.currency for e in out if e.currency}
        currency = next(iter(currencies)) if len(currencies) == 1 else None
        for e in out:
            if e.cost_kind == "unmeasured":
                if e.event_type in ledger.PRICEABLE:
                    cost.unmeasured_count += 1
            elif e.cost_amount is not None and e.currency == (currency or e.currency):
                setattr(cost, e.cost_kind, getattr(cost, e.cost_kind) + e.cost_amount)
                cost.total_known += e.cost_amount
        reconciled = cost.provider_reported + cost.calculated
        basis = (
            "mixed"
            if cost.provider_reported and cost.calculated
            else "provider_reported"
            if cost.provider_reported
            else "calculated"
            if cost.calculated
            else "none"
        )
        return s.RunUsageOut(
            run_id=run_id,
            events=out,
            totals=s.UsageTotalsOut(
                input_tokens=sum(e.input_tokens for e in out),
                output_tokens=sum(e.output_tokens for e in out),
                cached_tokens=sum(e.cached_read_tokens + e.cached_write_tokens for e in out),
                calls=sum(e.call_count for e in out),
                duration_ms=sum(e.duration_ms or 0 for e in out),
                cost=cost,
                currency=currency,
            ),
            final_reconciled_cost={
                "amount": reconciled,
                "currency": currency,
                "basis": basis,
                "flags": [
                    f
                    for f, on in (
                        ("includes_calculated_not_invoice", cost.calculated > 0),
                        ("excludes_unmeasured_events", cost.unmeasured_count > 0),
                        ("excludes_estimated_infrastructure", cost.estimated_infra > 0),
                    )
                    if on
                ],
            },
        )

    def project_costs(self, project_id: UUID, days: int) -> dict[str, Any]:  # Any: JSON
        """The project's usage of the last `days` days by UTC day, model and root run kind, each
        per currency (`usage:read`; read only: no commit, no rollback, for assist)."""
        vis = self._vis("usage:read")
        since = utcnow() - timedelta(days=days)
        rows = repo.project_cost_rows(self.session, self.tenant_id, project_id, since, vis)

        def grouped(key: str) -> list[dict[str, Any]]:  # Any: JSON
            sums: dict[tuple[str, str | None], dict[str, Any]] = {}
            for r in rows:
                g = sums.setdefault(
                    (str(r[key]), r["currency"]),
                    {key: r[key], "currency": r["currency"], "input_tokens": 0,
                     "output_tokens": 0, "calls": 0, "cost": Decimal(0)},
                )  # fmt: skip
                g["input_tokens"] += int(r["input_tokens"])
                g["output_tokens"] += int(r["output_tokens"])
                g["calls"] += int(r["calls"])
                g["cost"] += Decimal(r["cost"])
            return [{**g, "cost": format(g["cost"], "f")} for _, g in sorted(sums.items())]

        by_model = grouped("model_id")
        names = repo.labels(
            self.session,
            self.tenant_id,
            "model",
            sorted({g["model_id"] for g in by_model if g["model_id"]}),
        )
        for g in by_model:
            g["model_name"] = names.get(g["model_id"]) if g["model_id"] else None
        return {
            "since": since.isoformat(),
            "by_day": grouped("day"),
            "by_model": by_model,
            "by_run_kind": grouped("run_kind"),
        }

    def rebuild_rollups(self, start: datetime | None, end: datetime | None) -> s.ReconciliationOut:
        self._require_manage("usage:manage")
        from_day, to_day = self._window(start, end, 30)
        rec = ledger.rebuild(self.session, self.tenant_id, from_day, to_day)
        self._audit(
            "governance.usage_rollups.rebuilt",
            "usage",
            self.tenant_id,
            "usage:rollups",
            {"from": from_day.isoformat(), "to": to_day.isoformat(), "consistent": rec.consistent},
        )
        self.session.commit()
        return s.ReconciliationOut(
            events_sum=rec.events_sum,
            rollup_sum=rec.rollup_sum,
            consistent=rec.consistent,
            mismatched_rows=rec.mismatched,
            detail=rec.detail,
        )

    def _export_allowed(self) -> int | None:
        return export_row_cap(self.session, self.tenant_id)

    def export_usage(
        self,
        *,
        fmt: str,
        start: datetime | None,
        end: datetime | None,
        workspace: str | None,
        project: str | None,
        cost_kind: str | None,
    ) -> tuple[str, bool]:
        """The CSV and whether it was cut at the row cap (also flagged by a trailer row)."""
        if fmt != "csv":
            raise ValidationFailed("format must be csv")
        if cost_kind is not None and cost_kind not in ledger.COST_KINDS:
            raise ValidationFailed(f"cost_kind must be one of {', '.join(ledger.COST_KINDS)}")
        vis = self._vis("usage:read")
        self._require("data:export")
        cap = self._export_allowed()
        from_day, to_day = self._window(start, end, 30)
        ws_id = self._ws_filter(workspace)
        pj_id = self._project_filter(project, ws_id)
        limit = min(EXPORT_ROW_LIMIT, cap or EXPORT_ROW_LIMIT)
        rows = repo.export_events(
            self.session,
            self.tenant_id,
            vis=vis,
            start=datetime(from_day.year, from_day.month, from_day.day, tzinfo=UTC),
            end=datetime(to_day.year, to_day.month, to_day.day, tzinfo=UTC) + timedelta(days=1),
            limit=limit + 1,  # one extra row tells "exactly at the cap" from "cut"
            workspace_id=ws_id,
            project_id=pj_id,
            cost_kind=cost_kind,
        )
        truncated = len(rows) > limit
        rows = rows[:limit]
        header = [
            "occurred_at", "event_type", "status", "run_id", "root_run_id", "workspace_id",
            "project_id", "principal_id", "agent_id", "model_id", "provider_key", "model_key",
            "input_tokens", "output_tokens", "cached_read_tokens", "cached_write_tokens",
            "call_count", "duration_ms", "cost_amount", "currency", "cost_kind",
            "rate_card_version_id", "rate_card_version_no", "corrects_event_id", "is_test",
        ]  # fmt: skip
        body = _csv(
            [
                [
                    r["occurred_at"].isoformat(), r["event_type"], r["status"], r["run_id"],
                    r["root_run_id"], r["workspace_id"], r["project_id"], r["principal_id"],
                    r["agent_id"], r["model_id"], r["provider_key"], r["model_key"],
                    r["input_tokens"], r["output_tokens"], r["cached_read_tokens"],
                    r["cached_write_tokens"], r["call_count"], r["duration_ms"], r["cost_amount"],
                    r["currency"], r["cost_kind"], r["rate_card_version_id"],
                    (r["rate_snapshot"] or {}).get("version_no"), r["corrects_event_id"],
                    r["is_test"],
                ]
                for r in rows
            ],
            header,
        )  # fmt: skip
        if truncated:
            body += (
                f"# TRUNCATED: only the first {limit} rows are included; "
                "narrow the date range or filters and export again\n"
            )
        self._audit(
            "governance.usage.exported",
            "usage",
            self.tenant_id,
            "usage:export",
            {
                "rows": len(rows),
                "from": from_day.isoformat(),
                "to": to_day.isoformat(),
                "workspace": workspace,
                "project": project,
                "cost_kind": cost_kind,
                "truncated": truncated,
                "row_cap": limit,
                "scope": "tenant" if vis.all else "limited",
            },
        )
        self.session.commit()
        return body, truncated

    # ================================================================== audit export
    def export_audit(
        self,
        *,
        fmt: str,
        category: str | None,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[str, str, bool]:
        """Filtered audit export (FR-AUD-007): needs `audit:read` (limited to the caller's scopes)
        AND tenant-level `data:export`; honours `data_export` policies; is itself audited.
        Returns (body, media type, truncated); a cut export carries an in-band marker (CSV
        `# TRUNCATED` trailer / final NDJSON `{"truncated": true}` object)."""
        if fmt not in ("csv", "json"):
            raise ValidationFailed("format must be csv or json")
        sf = self.sc.pdp.visible_scopes("audit:read")
        limited = not sf.tenant_wide
        if limited and not sf.workspace_ids and not sf.project_ids:
            self._require("audit:read")
        self._require("data:export")
        cap = min(self._export_allowed() or EXPORT_ROW_LIMIT, EXPORT_ROW_LIMIT)
        events: list[audit.EventRow] = []
        before: UUID | None = None
        while len(events) < cap + 1:  # one extra row tells "exactly at the cap" from "cut"
            page = audit.list_events(
                self.session,
                tenant_id=self.tenant_id,
                category=category,
                actor=None,
                action=None,
                occurred_from=_aware(start) if start else None,
                occurred_to=_aware(end) if end else None,
                before=before,
                limit=min(1000, cap + 1 - len(events)),
                workspace_ids=sf.workspace_ids if limited else None,
                project_ids=sf.project_ids if limited else None,
            )
            if not page:
                break
            events.extend(page)
            before = page[-1].id
        truncated = len(events) > cap
        events = events[:cap]
        self._audit(
            "audit.exported",
            "audit",
            self.tenant_id,
            "audit:export",
            {"rows": len(events), "format": fmt, "category": category,
             "truncated": truncated, "row_cap": cap,
             "scope": "limited" if limited else "tenant"},
        )  # fmt: skip
        self.session.commit()
        if fmt == "json":
            body = "\n".join(
                json.dumps(
                    {
                        "id": str(e.id),
                        "occurred_at": e.occurred_at.isoformat(),
                        "actor": e.actor,
                        "action": e.action,
                        "outcome": e.outcome,
                        "target": e.target,
                        "workspace_id": str(e.workspace_id) if e.workspace_id else None,
                        "project_id": str(e.project_id) if e.project_id else None,
                        "chain_seq": e.chain_seq,
                        "hash": e.hash_hex,
                        "request_id": e.request_id,
                        "details": e.details,
                    },
                    default=str,
                )
                for e in events
            )
            if truncated:
                marker = json.dumps({"truncated": True, "row_cap": cap})
                body = body + "\n" + marker if body else marker
            return body + ("\n" if body else ""), "application/x-ndjson", truncated
        header = ["occurred_at", "actor", "action", "outcome", "target", "workspace_id",
                  "project_id", "chain_seq", "hash", "request_id", "details"]  # fmt: skip
        csv_body = (
            _csv(
                [
                    [e.occurred_at.isoformat(), e.actor, e.action, e.outcome, e.target,
                     e.workspace_id, e.project_id, e.chain_seq, e.hash_hex, e.request_id,
                     json.dumps(e.details, default=str)]
                    for e in events
                ],
                header,
            )
        )  # fmt: skip
        if truncated:
            csv_body += (
                f"# TRUNCATED: only the first {cap} rows are included; "
                "narrow the date range or filters and export again\n"
            )
        return csv_body, "text/csv", truncated

    # ================================================================== budgets
    def _budget_out(self, r: Mapping[str, Any]) -> s.BudgetOut:  # Any: row
        metric = str(r["metric"])
        legacy_runs = (
            metric == "tokens"
            and r["amount"] is None
            and r["limit_tokens"] is None
            and r["limit_runs"] is not None
        )
        if legacy_runs:
            metric = "runs"
        limit = budgeting.effective_limit(r, metric)
        return s.BudgetOut(
            id=r["id"],
            name=r["name"],
            scope=s.GovScope(type=r["scope_type"], id=r["scope_id"]),
            period=budgeting.period_name(str(r["period"])),
            amount=None if limit is None else float(limit),
            currency=r["currency"],
            metric=metric,
            enforcement=r["enforcement"],
            on_exceed=r["on_exceed"],
            on_exceed_effective=_on_exceed_effective(r),
            alert_thresholds=[float(t) for t in r["alert_thresholds"] or []],
            status=r["status"],
            row_version=r["row_version"],
            created_at=r["created_at"],
        )

    def list_budgets(self) -> Page[s.BudgetOut]:
        vis = self._vis("budget:read")
        rows = [
            r
            for r in repo.list_budgets(self.session, self.tenant_id)
            if self._row_visible(vis, r["scope_type"], r["scope_id"])
        ]
        return Page(items=[self._budget_out(r) for r in rows])

    def _budget(self, budget_id: UUID) -> Mapping[str, Any]:  # Any: row
        row = repo.get_budget(self.session, self.tenant_id, budget_id)
        if row is None:
            raise NotFound("budget not found")
        return row

    def get_budget(self, budget_id: UUID) -> s.BudgetOut:
        row = self._budget(budget_id)
        ref, _, _ = self._scope_ref(row["scope_type"], row["scope_id"])
        self._require("budget:read", ref)
        return self._budget_out(row)

    @staticmethod
    def _thresholds(values: list[float]) -> list[Decimal]:
        return sorted({Decimal(str(v)) for v in values})

    def create_budget(self, body: s.BudgetCreate) -> s.BudgetOut:
        ref, ws, proj = self._scope_ref(body.scope.type, body.scope.id)
        self._require_manage("budget:manage", ref)
        if body.metric == "cost" and not body.currency:
            raise ValidationFailed("a cost budget needs a currency")
        amount = Decimal(str(body.amount))
        if body.metric == "tokens" and amount != amount.to_integral_value():
            raise ValidationFailed("a token budget needs a whole number of tokens")
        budget_id = uuid7()
        row = repo.insert_budget(
            self.session,
            {
                "id": budget_id,
                "tenant_id": self.tenant_id,
                "name": body.name or f"{body.scope.type} {body.period} {body.metric}",
                "scope_type": body.scope.type,
                "scope_id": body.scope.id,
                "period": body.period,
                "metric": body.metric,
                "limit_tokens": int(amount) if body.metric == "tokens" else None,
                "amount": amount,
                "currency": body.currency if body.metric == "cost" else None,
                "enforcement": body.enforcement,
                "on_exceed": body.on_exceed,
                "alert_thresholds": self._thresholds(body.alert_thresholds),
                "status": body.status,
                "created_by": self.sc.principal.principal_id,
            },
        )
        self._audit(
            "governance.budget.created",
            "budget",
            budget_id,
            f"budget:{budget_id}",
            {
                "scope_type": body.scope.type,
                "scope_id": str(body.scope.id),
                "metric": body.metric,
                "period": body.period,
                "amount": str(amount),
                "currency": body.currency,
                "enforcement": body.enforcement,
                "on_exceed": body.on_exceed,
            },
            workspace_id=ws,
            project_id=proj,
        )
        self.session.commit()
        return self._budget_out(row)

    def update_budget(self, budget_id: UUID, body: s.BudgetPatch) -> s.BudgetOut:
        before = self._budget(budget_id)
        ref, ws, proj = self._scope_ref(before["scope_type"], before["scope_id"])
        self._require_manage("budget:manage", ref)
        legacy_runs = (
            before["metric"] == "tokens"
            and before["amount"] is None
            and before["limit_tokens"] is None
            and before["limit_runs"] is not None
        )  # same detection as _budget_out: a P5 run-count budget is reported as metric "runs"
        self._immutable(
            ("scope", body.scope, s.GovScope(type=before["scope_type"], id=before["scope_id"])),
            ("metric", body.metric, "runs" if legacy_runs else before["metric"]),
        )
        sent = body.model_fields_set - {"row_version", "scope", "metric"}
        if not sent:
            raise ValidationFailed("no updatable fields supplied")
        values: dict[str, Any] = {}  # Any: column values
        for field in sent:
            value = getattr(body, field)
            if value is None and field != "currency":
                continue
            if field == "amount":
                amount = Decimal(str(value))
                if legacy_runs:  # a P5 run-count budget stays a run-count budget
                    if amount != amount.to_integral_value():
                        raise ValidationFailed("a run budget needs a whole number of runs")
                    values["limit_runs"] = int(amount)
                    continue
                if before["metric"] == "tokens":
                    if amount != amount.to_integral_value():
                        raise ValidationFailed("a token budget needs a whole number of tokens")
                    values["limit_tokens"] = int(amount)
                values["amount"] = amount
            elif field == "alert_thresholds":
                values["alert_thresholds"] = self._thresholds(value)
            elif field == "currency":
                if before["metric"] != "cost":
                    continue
                if not value:
                    raise ValidationFailed("a cost budget needs a currency")
                values["currency"] = value
            else:
                values[field] = value
        if not values:
            raise ValidationFailed("no updatable fields supplied")
        row = repo.update_row(
            self.session,
            self.tenant_id,
            "budgets",
            repo.BUDGET_COLUMNS,
            repo.BUDGET_UPDATABLE,
            budget_id,
            body.row_version,
            values,
        )
        if row is None:
            self.session.rollback()
            raise Conflict("the budget was changed by someone else; reload and retry")
        self._audit(
            "governance.budget.updated",
            "budget",
            budget_id,
            f"budget:{budget_id}",
            {
                "changed": sorted(values),
                "before": {k: str(before[k]) for k in _BUDGET_AUDITED},
                "after": {k: str(row[k]) for k in _BUDGET_AUDITED},
            },
            workspace_id=ws,
            project_id=proj,
        )
        self.session.commit()
        return self._budget_out(row)

    def budget_status(self, budget_id: UUID) -> s.BudgetStatusOut:
        row = self._budget(budget_id)
        ref, _, _ = self._scope_ref(row["scope_type"], row["scope_id"])
        self._require("budget:read", ref)
        out = self._budget_out(row)
        metric = out.metric
        period = budgeting.period_name(str(row["period"]))
        since = budgeting.period_start(str(row["period"]))
        limit = budgeting.effective_limit(row, metric)
        note: str | None = None
        if period == "run":
            settled = reserved = Decimal(0)
            note = "a per-run budget is evaluated separately for every root run"
        elif metric == "runs":
            settled = Decimal(self._count_runs(row, since))
            reserved = Decimal(0)
        else:
            settled, reserved = budgeting.used_and_held(
                self.session, self.tenant_id, None, row, metric=metric, since=since
            )
        utilization = float(settled / limit) if limit else 0.0
        start = since or datetime(1970, 1, 1, tzinfo=UTC)
        return s.BudgetStatusOut(
            budget_id=budget_id,
            period_start=since,
            metric=metric,
            currency=row["currency"],
            amount=None if limit is None else float(limit),
            reserved=float(reserved),
            settled=float(settled),
            remaining=None if limit is None else float(max(limit - settled - reserved, Decimal(0))),
            utilization=utilization,
            alerts_fired=repo.budget_alerts(self.session, self.tenant_id, budget_id, start),
            stale_holds=repo.stale_holds(self.session, self.tenant_id),
            on_exceed_effective=_on_exceed_effective(row),
            note=note,
        )

    def _count_runs(self, rule: Mapping[str, Any], since: datetime | None) -> int:  # Any: row
        return int(
            self.session.execute(
                text(
                    # predicate from the RUN_ENTITY constant map; values bound
                    "SELECT count(*) FROM shifde.runs r WHERE r.tenant_id = :t "  # nosec B608
                    "AND r.parent_run_id IS NULL AND (CAST(:since AS timestamptz) IS NULL "
                    "OR r.created_at >= :since) AND COALESCE(r.error_class, '') <> 'budget_denied' "
                    f"AND {budgeting.RUN_ENTITY[str(rule['scope_type'])]}"
                ),
                {"t": self.tenant_id, "since": since, "scope_id": rule["scope_id"]},
            ).scalar_one()
        )

    # ================================================================== quotas
    @staticmethod
    def _quota_out(r: Mapping[str, Any]) -> s.QuotaOut:  # Any: row
        return s.QuotaOut(
            id=r["id"],
            scope=s.GovScope(type=r["scope_type"], id=r["scope_id"]),
            metric=r["metric"],
            limit=int(r["limit_value"]),
            status=r["status"],
            row_version=r["row_version"],
        )

    def list_quotas(self) -> Page[s.QuotaOut]:
        vis = self._vis("budget:read")
        rows = [
            r
            for r in repo.list_quotas(self.session, self.tenant_id)
            if self._row_visible(vis, r["scope_type"], r["scope_id"])
        ]
        return Page(items=[self._quota_out(r) for r in rows])

    def _quota(self, quota_id: UUID) -> Mapping[str, Any]:  # Any: row
        row = repo.get_quota(self.session, self.tenant_id, quota_id)
        if row is None:
            raise NotFound("quota not found")
        return row

    def get_quota(self, quota_id: UUID) -> s.QuotaOut:
        row = self._quota(quota_id)
        ref, _, _ = self._scope_ref(row["scope_type"], row["scope_id"])
        self._require("budget:read", ref)
        return self._quota_out(row)

    def create_quota(self, body: s.QuotaCreate) -> s.QuotaOut:
        ref, ws, proj = self._scope_ref(body.scope.type, body.scope.id)
        self._require_manage("budget:manage", ref)
        quota_id = uuid7()
        try:
            row = repo.insert_quota(
                self.session,
                {
                    "id": quota_id,
                    "tenant_id": self.tenant_id,
                    "scope_type": body.scope.type,
                    "scope_id": body.scope.id,
                    "metric": body.metric,
                    "limit_value": body.limit,
                    "created_by": self.sc.principal.principal_id,
                },
            )
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict("a quota for this scope and metric already exists") from exc
        self._audit(
            "governance.quota.created",
            "quota",
            quota_id,
            f"quota:{quota_id}",
            {"scope_type": body.scope.type, "scope_id": str(body.scope.id),
             "metric": body.metric, "limit": body.limit},
            workspace_id=ws,
            project_id=proj,
        )  # fmt: skip
        self.session.commit()
        return self._quota_out(row)

    def update_quota(self, quota_id: UUID, body: s.QuotaPatch) -> s.QuotaOut:
        before = self._quota(quota_id)
        ref, ws, proj = self._scope_ref(before["scope_type"], before["scope_id"])
        self._require_manage("budget:manage", ref)
        self._immutable(
            ("scope", body.scope, s.GovScope(type=before["scope_type"], id=before["scope_id"])),
            ("metric", body.metric, before["metric"]),
        )
        values: dict[str, Any] = {}  # Any: column values
        if body.limit is not None:
            values["limit_value"] = body.limit
        if body.status is not None:
            values["status"] = body.status
        if not values:
            raise ValidationFailed("no updatable fields supplied")
        row = repo.update_row(
            self.session,
            self.tenant_id,
            "quotas",
            repo.QUOTA_COLUMNS,
            repo.QUOTA_UPDATABLE,
            quota_id,
            body.row_version,
            values,
        )
        if row is None:
            self.session.rollback()
            raise Conflict("the quota was changed by someone else; reload and retry")
        self._audit(
            "governance.quota.updated",
            "quota",
            quota_id,
            f"quota:{quota_id}",
            {"changed": sorted(values), "before": str(before["limit_value"]),
             "after": str(row["limit_value"])},
            workspace_id=ws,
            project_id=proj,
        )  # fmt: skip
        self.session.commit()
        return self._quota_out(row)

    # ================================================================== policies
    def _policy_out(self, r: Mapping[str, Any]) -> s.PolicyOut:  # Any: row
        definition = dict(r["definition"] or {})
        if r["kind"] == "model_policy" and self.runtime is not None:
            definition = self._live_model_policy(r, definition)
        return s.PolicyOut(
            id=r["id"],
            kind=r["kind"],
            name=r["name"],
            scope=None
            if r["scope_type"] is None
            else s.GovScope(type=r["scope_type"], id=r["scope_id"]),
            definition=definition,
            status=r["status"],
            version=r["version"],
            row_version=r["row_version"],
            enforcement=enforcement_of(r["kind"]),
            created_at=r["created_at"],
        )

    def _live_model_policy(
        self, r: Mapping[str, Any], definition: dict[str, Any]
    ) -> dict[str, Any]:  # Any: JSON
        """The P3 allowlist is the source of truth for model policies: show what is in force."""
        assert self.runtime is not None
        st = r["scope_type"] or "tenant"
        sid = r["scope_id"] or self.tenant_id
        try:
            current = ModelService(self.sc, self.runtime).get_allowlist(cast(ms.ScopeType, st), sid)
        except (Forbidden, NotFound):
            return definition
        return {
            **definition,
            "enabled": current.enabled,
            "allowed_model_ids": [str(m) for m in current.model_ids],
        }

    def list_policies(self, kind: str | None) -> Page[s.PolicyOut]:
        vis = self._vis("governance:read")
        rows = [
            r
            for r in repo.list_policies(self.session, self.tenant_id, kind)
            if (r["scope_type"] is None and vis.all)
            or (
                r["scope_type"] is not None
                and self._row_visible(vis, r["scope_type"], r["scope_id"])
            )
        ]
        return Page(items=[self._policy_out(r) for r in rows])

    def _policy(self, policy_id: UUID) -> Mapping[str, Any]:  # Any: row
        row = repo.get_policy(self.session, self.tenant_id, policy_id)
        if row is None:
            raise NotFound("policy not found")
        return row

    def get_policy(self, policy_id: UUID) -> s.PolicyOut:
        row = self._policy(policy_id)
        ref = self._policy_ref(row)[0]
        self._require("governance:read", ref)
        return self._policy_out(row)

    def _policy_ref(
        self, row: Mapping[str, Any]
    ) -> tuple[ResourceRef, UUID | None, UUID | None]:  # Any: row
        if row["scope_type"] is None:
            return self.sc.tenant_ref, None, None
        return self._scope_ref(row["scope_type"], row["scope_id"])

    def _check_policy(
        self, kind: str, definition: dict[str, Any], scope: s.GovScope | None
    ) -> dict[str, Any]:  # Any: JSON
        if kind == "model_policy" and (
            scope is not None and scope.type not in ("tenant", "workspace", "project")
        ):
            raise ValidationFailed("a model_policy scope must be tenant, workspace or project")
        known = {r[0] for r in self._permission_keys()}
        return validate_definition(kind, definition, known_permissions=known)

    def _permission_keys(self) -> list[tuple[str]]:
        rows = self.session.execute(
            text("SELECT key FROM shifde.permissions WHERE key <> 'platform:admin'")
        )
        return [(r[0],) for r in rows]

    def _sync_model_policy(
        self, scope: s.GovScope | None, definition: dict[str, Any]
    ) -> None:  # Any: JSON
        """Write the policy through to the P3 allowlist (needs `model:manage` as well)."""
        if self.runtime is None:
            raise ValidationFailed("model policies are not available in this context")
        # `_check_policy` has already limited a model policy's scope to these three types
        scope_type = cast(ms.ScopeType, scope.type if scope else "tenant")
        scope_id = scope.id if scope else self.tenant_id
        ModelService(self.sc, self.runtime).put_allowlist(
            scope_type,
            scope_id,
            ms.AllowlistPut(
                enabled=bool(definition.get("enabled", True)),
                model_ids=[UUID(str(m)) for m in definition.get("allowed_model_ids", [])],
            ),
        )

    def create_policy(self, body: s.PolicyCreate) -> s.PolicyOut:
        if body.scope is not None:
            ref, ws, proj = self._scope_ref(body.scope.type, body.scope.id)
        else:
            ref, ws, proj = self.sc.tenant_ref, None, None
        self._require_manage("governance:manage", ref)
        definition = self._check_policy(body.kind, body.definition, body.scope)
        if body.kind == "model_policy":
            self._sync_model_policy(body.scope, definition)
        policy_id = uuid7()
        row = repo.insert_policy(
            self.session,
            {
                "id": policy_id,
                "tenant_id": self.tenant_id,
                "kind": body.kind,
                "name": body.name,
                "scope_type": body.scope.type if body.scope else None,
                "scope_id": body.scope.id if body.scope else None,
                "definition": definition,
                "status": body.status,
                "created_by": self.sc.principal.principal_id,
            },
        )
        self._audit(
            "governance.policy.created",
            "governance_policy",
            policy_id,
            f"policy:{body.kind}:{policy_id}",
            {"kind": body.kind, "status": body.status, "definition": definition,
             "scope": None if body.scope is None else f"{body.scope.type}:{body.scope.id}"},
            workspace_id=ws,
            project_id=proj,
        )  # fmt: skip
        self.session.commit()
        return self._policy_out(row)

    def update_policy(self, policy_id: UUID, body: s.PolicyPatch) -> s.PolicyOut:
        before = self._policy(policy_id)
        ref, ws, proj = self._policy_ref(before)
        self._require_manage("governance:manage", ref)
        scope = (
            None
            if before["scope_type"] is None
            else s.GovScope(type=before["scope_type"], id=before["scope_id"])
        )
        self._immutable(("kind", body.kind, before["kind"]), ("scope", body.scope, scope))
        values: dict[str, Any] = {}  # Any: column values
        if body.name is not None:
            values["name"] = body.name
        if body.status is not None:
            values["status"] = body.status
        if body.definition is not None:
            definition = self._check_policy(before["kind"], body.definition, scope)
            values["definition"] = definition
            values["version"] = before["version"] + 1
            if before["kind"] == "model_policy":
                self._sync_model_policy(scope, definition)
        if not values:
            raise ValidationFailed("no updatable fields supplied")
        row = repo.update_row(
            self.session,
            self.tenant_id,
            "governance_policies",
            repo.POLICY_COLUMNS,
            repo.POLICY_UPDATABLE,
            policy_id,
            body.row_version,
            values,
            json_columns=frozenset({"definition"}),
        )
        if row is None:
            self.session.rollback()
            raise Conflict("the policy was changed by someone else; reload and retry")
        self._audit(
            "governance.policy.updated",
            "governance_policy",
            policy_id,
            f"policy:{before['kind']}:{policy_id}",
            {
                "kind": before["kind"],
                "changed": sorted(values),
                "before": {"status": before["status"], "definition": before["definition"]},
                "after": {"status": row["status"], "definition": row["definition"]},
            },
            workspace_id=ws,
            project_id=proj,
        )
        self.session.commit()
        return self._policy_out(row)

    # ================================================================== decisions / overview
    def list_decisions(
        self,
        *,
        run_id: UUID | None,
        decision: str | None,
        action: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[s.DecisionOut]:
        self._require("governance:read")
        if decision is not None and decision not in ("allow", "deny"):
            raise ValidationFailed("decision must be allow or deny")
        rows = repo.list_decisions(
            self.session,
            self.tenant_id,
            run_id=run_id,
            decision=decision,
            action=action,
            before=decode_cursor(cursor),
            limit=limit + 1,
        )
        page, more = paginate(rows, limit)
        items = [
            s.DecisionOut(
                id=r["id"],
                at=r["at"],
                occurred_at=r["at"],
                decision=r["decision"],
                action=r["action"],
                resource=(
                    f"{r['resource_type']}:{r['resource_id']}"
                    if r["resource_type"]
                    else r["resource_id"]
                ),
                reason_codes=list(r["reason_codes"] or []),
                reason_code=(r["reason_codes"] or [None])[0],
                policy_ids=list(r["policy_ids"] or []),
                run_id=r["run_id"],
                principal=r["principal"],
            )
            for r in page
        ]
        return Page(
            items=items, next_cursor=encode_cursor(page[-1]["id"]) if more and page else None
        )

    def overview(self) -> s.OverviewOut:
        self._require("governance:read")
        budgets: list[s.OverviewBudget] = []
        for r in repo.list_budgets(self.session, self.tenant_id)[:200]:
            if r["status"] != "active":
                continue
            st = self.budget_status(r["id"])
            out = self._budget_out(r)
            budgets.append(
                s.OverviewBudget(
                    id=r["id"],
                    name=out.name,
                    scope=out.scope,
                    metric=out.metric,
                    currency=out.currency,
                    utilization=st.utilization,
                    enforcement=out.enforcement,
                    alert_thresholds=out.alert_thresholds,
                )
            )
        end = utcnow()
        summary = self.summary(
            group_by="project", start=end - timedelta(days=30), end=end, workspace=None,
            project=None, cost_kind=None, currency=None,
        )  # fmt: skip
        spenders = [
            s.OverviewSpender(
                key=g.key,
                label=g.label,
                cost_total_known=g.cost.total_known,
                cost=g.cost,
                currency=g.currency,
                tokens=g.input_tokens + g.output_tokens,
            )
            for g in summary.groups[:5]
        ]
        return s.OverviewOut(
            budgets=budgets,
            top_spenders=spenders,
            denials_7d=repo.denials_since(self.session, self.tenant_id, end - timedelta(days=7)),
            pending_approvals=repo.pending_approvals(self.session, self.tenant_id),
            policy_counts=repo.policy_counts(self.session, self.tenant_id),
        )

    # ================================================================== retention
    def _retention_items(
        self, override: list[s.RetentionItem] | None = None
    ) -> list[s.RetentionItemOut]:
        stored = {r["data_class"]: r for r in repo.retention_rows(self.session, self.tenant_id)}
        given = {i.data_class: i for i in override or []}
        out: list[s.RetentionItemOut] = []
        for data_class, default in RETENTION_DEFAULTS.items():
            if data_class in given:
                days, hold, is_default = (
                    given[data_class].retain_days,
                    given[data_class].legal_hold,
                    False,
                )
            elif data_class in stored:
                days, hold, is_default = (
                    stored[data_class]["retain_days"],
                    stored[data_class]["legal_hold"],
                    False,
                )
            else:
                days, hold, is_default = default, False, True
            out.append(
                s.RetentionItemOut(
                    data_class=data_class,
                    retain_days=days,
                    legal_hold=hold,
                    floor_days=RETENTION_FLOORS[data_class],
                    default_days=default,
                    is_default=is_default,
                )
            )
        return out

    RETENTION_NOTE = (
        "These are SETTINGS. The purge job is not implemented in this release: nothing is deleted "
        "automatically; the preview counts what a purge would remove."
    )

    def get_retention(self) -> s.RetentionOut:
        self._require("governance:read")
        return s.RetentionOut(items=self._retention_items(), note=self.RETENTION_NOTE)

    @staticmethod
    def _check_retention(items: list[s.RetentionItem]) -> None:
        seen: set[str] = set()
        for i in items:
            if i.data_class in seen:
                raise ValidationFailed(f"data_class {i.data_class} is listed twice")
            seen.add(i.data_class)
            floor = RETENTION_FLOORS[i.data_class]
            if i.retain_days < floor:
                raise ValidationFailed(
                    f"retain_days for {i.data_class} must be at least {floor} (platform floor)"
                )

    def put_retention(self, body: s.RetentionPut) -> s.RetentionOut:
        self._require_manage("governance:manage")
        self._check_retention(body.items)
        before = {i.data_class: (i.retain_days, i.legal_hold) for i in self._retention_items()}
        for item in body.items:
            repo.upsert_retention(
                self.session,
                self.tenant_id,
                item.data_class,
                item.retain_days,
                item.legal_hold,
                self.sc.principal.principal_id,
            )
        self._audit(
            "governance.retention.updated",
            "retention",
            self.tenant_id,
            "retention",
            {
                "changes": {
                    i.data_class: {
                        "before": list(before[i.data_class]),
                        "after": [i.retain_days, i.legal_hold],
                    }
                    for i in body.items
                },
                "purge": "not_implemented",
            },
        )
        self.session.commit()
        return s.RetentionOut(items=self._retention_items(), note=self.RETENTION_NOTE)

    def preview_retention(self, body: s.RetentionPut | None) -> s.RetentionPreviewOut:
        self._require("governance:read")
        items = body.items if body else []
        self._check_retention(items)
        now = utcnow()
        out: list[s.RetentionPreviewItem] = []
        for it in self._retention_items(items):
            cutoff = now - timedelta(days=it.retain_days)
            count = (
                0
                if it.legal_hold
                else repo.count_older_than(self.session, self.tenant_id, it.data_class, cutoff)
            )
            out.append(
                s.RetentionPreviewItem(
                    data_class=it.data_class,
                    count=count,
                    cutoff=cutoff,
                    on_legal_hold=it.legal_hold,
                )
            )
        return s.RetentionPreviewOut(items=out, note=self.RETENTION_NOTE)

    # ================================================================== observability
    def performance(
        self, *, group_by: str, start: datetime | None, end: datetime | None
    ) -> s.PerformanceOut:
        if group_by not in ("agent", "model", "pipeline"):
            raise ValidationFailed("group_by must be agent, model or pipeline")
        vis = self._vis("ops:read")
        end_at = _aware(end) if end else utcnow()
        start_at = _aware(start) if start else end_at - timedelta(days=7)
        if start_at >= end_at:
            raise ValidationFailed("`from` must be before `to`")
        rows = repo.performance(self.session, self.tenant_id, group_by, start_at, end_at, vis)
        names = repo.labels(self.session, self.tenant_id, group_by, [str(r["key"]) for r in rows])
        groups = [
            s.PerfGroupOut(
                key=str(r["key"]),
                label=names.get(str(r["key"])),
                runs=int(r["n"]),
                count=int(r["n"]),
                error_count=int(r["errors"]),
                p50_ms=float(r["p50"] or 0),
                p95_ms=float(r["p95"] or 0),
                p99_ms=float(r["p99"] or 0),
                error_rate=(int(r["errors"]) / int(r["n"])) if int(r["n"]) else 0.0,
            )
            for r in rows
        ]
        groups.sort(key=lambda g: -g.runs)
        return s.PerformanceOut(
            groups=groups,
            group_by=group_by,
            note=(
                "Latency is wall-clock from start to finish of "
                + ("each model call attempt" if group_by == "model" else "each finished run")
                + "; cancelled runs are excluded; errors are failed / timed-out runs or failed "
                "attempts."
            ),
        )


def health_details(
    session: Session,
    settings: Any,  # Any: Settings
    principal: Principal,
) -> s.HealthDetailsOut:
    """Platform health for `platform:admin` (the route has already checked the permission).

    The worker, queue and run-failure sections read `workers` and `shifde_ops_stats(int)`
    (runtime_0001); the function refuses a client context, and this view runs without one.
    """
    from shifde.core.health import database_reachable, migration_state
    from shifde.observability import metrics

    reachable = database_reachable(session)
    state = migration_state(session)
    http = {
        "api_process_requests_total": metrics.registry.total("shifde_http_requests_total"),
        "api_process_http_errors_total": metrics.registry.total("shifde_http_errors_total"),
        "api_process_note": "request counters are for this API process since it started",
    }
    telemetry = tracing.tracer.status() | {"metrics": "prometheus text at /api/metrics"}
    migrations = {"status": state.status, "revision": state.revision, "head": state.head}
    stats = repo.ops_stats(session, 900)
    workers = repo.workers(session)
    now = utcnow()
    horizon = max(settings.worker_lease_seconds * 3, 15)
    live = [
        w
        for w in workers
        if w["status"] == "running" and (now - w["heartbeat_at"]).total_seconds() <= horizon
    ]
    reaps = [w["last_reap_at"] for w in workers if w["last_reap_at"] is not None]
    finished = sum(int(r["count"]) for r in stats["runs"])
    failed = sum(int(r["count"]) for r in stats["runs"] if r["status"] in ("failed", "timed_out"))
    return s.HealthDetailsOut(
        database={"reachable": reachable},
        migrations=migrations,
        workers={
            "running": len(live),
            "known": len(workers),
            "items": [
                {
                    "id": w["id"],
                    "host": w["hostname"],
                    "status": w["status"],
                    "version": w["version"],
                    "last_heartbeat_at": w["heartbeat_at"].isoformat(),
                }
                for w in workers[:20]
            ],
        },
        queue={
            "queued": stats["queue"]["queued"],
            "leased": stats["queue"]["leased"],
            "dead_in_window": stats["queue"]["dead"],
            "oldest_queued_age_seconds": stats["queue"]["oldest_queued_age_seconds"],
        },
        reaper={
            "last_run_at": max(reaps).isoformat() if reaps else None,
            "status": "ok" if reaps and (now - max(reaps)).total_seconds() <= horizon else "stale",
        },
        recent_error_rates={
            "window_seconds": 900,
            "runs_finished": finished,
            "runs_failed": failed,
            "run_failure_rate": (failed / finished) if finished else 0.0,
            "budget_denials": stats["budget_denials"],
            **http,
        },
        telemetry=telemetry,
    )


__all__ = ["GovernanceService", "PricingService", "health_details"]
