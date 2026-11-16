"""Phase 8 CLI helpers (owner credentials): sample rate cards and build-operation usage events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import ledger
from shifde.core.errors import NotFound, ValidationFailed
from shifde.core.ids import uuid7
from shifde.ledger import pricing

SAMPLE_SOURCE = "SAMPLE - not vendor pricing"
SAMPLE_REVISION = "sample-1"
SAMPLE_ADAPTERS = ("fake", "openai_compatible")


@dataclass(frozen=True)
class SampleRates:
    cards_created: int
    versions_created: int
    models: list[tuple[str, str]]


def seed_sample_rates(
    session: Session,
    *,
    currency: str,
    input_rate: Decimal,
    output_rate: Decimal,
    extra_models: list[tuple[str, str]],
) -> SampleRates:
    """Global SAMPLE rate cards for the fake / openai_compatible models that exist (plus any
    given explicitly). The numbers are placeholders chosen by the caller, labelled as such in
    `source` and the card name; nothing here is vendor pricing."""
    rows = session.execute(
        text(
            "SELECT DISTINCT p.adapter_key, m.model_key FROM shifde.models m "
            "JOIN shifde.model_providers p ON p.id = m.provider_id AND p.tenant_id = m.tenant_id "
            "WHERE p.adapter_key = ANY (:a)"
        ),
        {"a": list(SAMPLE_ADAPTERS)},
    )
    wanted = sorted({(r[0], r[1]) for r in rows} | set(extra_models))
    cards = versions = 0
    for provider_key, model_key in wanted:
        if provider_key not in SAMPLE_ADAPTERS:
            raise ValidationFailed("sample rates are only seeded for fake / openai_compatible")
        card = session.execute(
            text(
                "SELECT id FROM shifde.rate_cards WHERE tenant_id IS NULL "
                "AND provider_key = :p AND model_key = :m"
            ),
            {"p": provider_key, "m": model_key},
        ).first()
        if card is None:
            card_id = uuid7()
            session.execute(
                text(
                    "INSERT INTO shifde.rate_cards (id, tenant_id, provider_key, model_key, name) "
                    "VALUES (:id, NULL, :p, :m, :n)"
                ),
                {"id": card_id, "p": provider_key, "m": model_key, "n": f"SAMPLE {model_key}"},
            )
            cards += 1
        else:
            card_id = card[0]
        has_version = session.execute(
            text("SELECT 1 FROM shifde.rate_card_versions WHERE rate_card_id = :c LIMIT 1"),
            {"c": card_id},
        ).first()
        if has_version is None:
            session.execute(
                text(
                    "INSERT INTO shifde.rate_card_versions (id, rate_card_id, tenant_id, "
                    "version_no, effective_from, currency, unit, input_rate, output_rate, source, "
                    "revision) VALUES (:id, :c, NULL, 1, :f, :cur, 'per_1m_tokens', :i, :o, :s, :r)"
                ),
                {
                    "id": uuid7(),
                    "c": card_id,
                    "f": datetime(2000, 1, 1, tzinfo=UTC),
                    "cur": currency,
                    "i": input_rate,
                    "o": output_rate,
                    "s": SAMPLE_SOURCE,
                    "r": SAMPLE_REVISION,
                },
            )
            versions += 1
    session.commit()
    return SampleRates(cards, versions, wanted)


def record_build_op(
    session: Session,
    *,
    org: str,
    workspace: str | None,
    project: str | None,
    build_ref: str,
    duration_ms: int,
    key: str | None,
    cost: Decimal | None,
    currency: str | None,
) -> bool:
    """One `build_op` usage event (CI hook). Cost is `estimated_infra` when an amount is given,
    else unmeasured. Idempotent per `key` (default: the build reference). Returns True if new."""
    tenant = session.execute(
        text("SELECT id FROM shifde.tenants WHERE slug = :s"), {"s": org}
    ).first()
    if tenant is None:
        raise NotFound(f"organization {org} not found")
    tenant_id: UUID = tenant[0]
    ws_id: UUID | None = None
    project_id: UUID | None = None
    if workspace:
        row = session.execute(
            text("SELECT id FROM shifde.workspaces WHERE tenant_id = :t AND slug = :s"),
            {"t": tenant_id, "s": workspace},
        ).first()
        if row is None:
            raise NotFound(f"workspace {workspace} not found")
        ws_id = row[0]
        if project:
            prow = session.execute(
                text(
                    "SELECT id FROM shifde.projects WHERE tenant_id = :t AND workspace_id = :w "
                    "AND slug = :s"
                ),
                {"t": tenant_id, "w": ws_id, "s": project},
            ).first()
            if prow is None:
                raise NotFound(f"project {project} not found")
            project_id = prow[0]
    if cost is not None and not currency:
        raise ValidationFailed("a cost needs a currency")
    costed = (
        pricing.Costed("estimated_infra", cost.quantize(pricing.QUANT), currency)
        if cost is not None
        else pricing.UNMEASURED
    )
    result = ledger.record(
        session,
        {
            "tenant_id": tenant_id,
            "event_key": f"build_op:{key or build_ref}",
            "event_type": "build_op",
            "workspace_id": ws_id,
            "project_id": project_id,
            "build_ref": build_ref[:200],
            "call_count": 1,
            "duration_ms": duration_ms,
            "provider_reported_usage": {},
            "costed": costed,
        },
    )
    session.commit()
    return result.inserted
