"""Versioned rate cards: resolution and cost calculation (FR-COST-003/004/005).

No vendor price is built in anywhere: a rate exists only because someone created a rate card
version (or the dev seed command wrote a clearly labelled SAMPLE one). A calculated cost always
carries the `rate_snapshot` of the version it used, so changing or adding rate cards later never
changes an event that was already priced.

Units (the contract's `unit`):
* `per_1m_tokens`: input_rate / output_rate / cached_*_rate are prices per one million tokens.
  `input_tokens` are the NON-cached input tokens; cached reads / writes are priced with their own
  rate and fall back to `input_rate` when the card has none.
* `per_call`: `input_rate` is the price of one call (`output_rate` is ignored, keep it 0).
* `per_second`: `input_rate` is the price of one second of execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any  # Any: SQL parameter values / JSON documents
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

MILLION = Decimal(1_000_000)
QUANT = Decimal("0.0000000001")  # numeric(24,10)

VERSION_COLUMNS = (
    "v.id, v.rate_card_id, v.tenant_id, v.version_no, v.effective_from, v.effective_to, "
    "v.currency, v.unit, v.input_rate, v.output_rate, v.cached_read_rate, v.cached_write_rate, "
    "v.other_rates, v.source, v.revision, v.created_by, v.created_at"
)


@dataclass(frozen=True)
class RateVersion:
    id: UUID
    rate_card_id: UUID
    scope: str  # global | tenant
    provider_key: str
    model_key: str
    version_no: int
    effective_from: datetime
    effective_to: datetime | None
    currency: str
    unit: str
    input_rate: Decimal
    output_rate: Decimal
    cached_read_rate: Decimal | None
    cached_write_rate: Decimal | None
    other_rates: Mapping[str, Any]  # Any: free-form extra rates
    source: str
    revision: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    calls: int = 1
    duration_ms: int | None = None


@dataclass(frozen=True)
class Costed:
    """How an event is priced. `kind == 'unmeasured'` means no amount (no effective rate)."""

    kind: str
    amount: Decimal | None = None
    currency: str | None = None
    rate_card_version_id: UUID | None = None
    snapshot: Mapping[str, Any] | None = None  # Any: JSON snapshot of the rate used


UNMEASURED = Costed("unmeasured")


def _version(row: Mapping[Any, Any]) -> RateVersion:  # Any: RowMapping
    return RateVersion(
        id=row["id"],
        rate_card_id=row["rate_card_id"],
        scope="global" if row["card_tenant_id"] is None else "tenant",
        provider_key=row["provider_key"],
        model_key=row["model_key"],
        version_no=row["version_no"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        currency=row["currency"],
        unit=row["unit"],
        input_rate=Decimal(row["input_rate"]),
        output_rate=Decimal(row["output_rate"]),
        cached_read_rate=None
        if row["cached_read_rate"] is None
        else Decimal(row["cached_read_rate"]),
        cached_write_rate=(
            None if row["cached_write_rate"] is None else Decimal(row["cached_write_rate"])
        ),
        other_rates=dict(row["other_rates"] or {}),
        source=row["source"],
        revision=row["revision"],
    )


def resolve(
    session: Session, tenant_id: UUID, provider_key: str, model_key: str, at: datetime
) -> RateVersion | None:
    """The version in force at `at` for the model: a tenant card overrides the global one."""
    row = (
        session.execute(
            text(
                f"SELECT {VERSION_COLUMNS}, c.provider_key, c.model_key, "
                "c.tenant_id AS card_tenant_id "
                "FROM shifde.rate_card_versions v "
                "JOIN shifde.rate_cards c ON c.id = v.rate_card_id "
                "WHERE c.provider_key = :p AND c.model_key = :m "
                "AND (c.tenant_id = :t OR c.tenant_id IS NULL) "
                "AND v.effective_from <= :at AND (v.effective_to IS NULL OR v.effective_to > :at) "
                "ORDER BY (c.tenant_id IS NULL), v.version_no DESC LIMIT 1"
            ),
            {"p": provider_key, "m": model_key, "t": tenant_id, "at": at},
        )
        .mappings()
        .first()
    )
    return _version(row) if row else None


def snapshot(v: RateVersion) -> dict[str, Any]:  # Any: JSON document
    """Everything a later reader needs to re-derive the cost (stored on the event)."""
    return {
        "rate_card_id": str(v.rate_card_id),
        "rate_card_version_id": str(v.id),
        "version_no": v.version_no,
        "scope": v.scope,
        "provider_key": v.provider_key,
        "model_key": v.model_key,
        "unit": v.unit,
        "currency": v.currency,
        "input_rate": str(v.input_rate),
        "output_rate": str(v.output_rate),
        "cached_read_rate": None if v.cached_read_rate is None else str(v.cached_read_rate),
        "cached_write_rate": None if v.cached_write_rate is None else str(v.cached_write_rate),
        "other_rates": dict(v.other_rates),
        "source": v.source,
        "revision": v.revision,
        "effective_from": v.effective_from.isoformat(),
        "effective_to": v.effective_to.isoformat() if v.effective_to else None,
    }


def calculate(v: RateVersion, usage: Usage) -> Decimal:
    if v.unit == "per_call":
        amount = v.input_rate * usage.calls
    elif v.unit == "per_second":
        amount = v.input_rate * (Decimal(usage.duration_ms or 0) / Decimal(1000))
    else:
        cached_read = v.cached_read_rate if v.cached_read_rate is not None else v.input_rate
        cached_write = v.cached_write_rate if v.cached_write_rate is not None else v.input_rate
        amount = (
            v.input_rate * usage.input_tokens
            + v.output_rate * usage.output_tokens
            + cached_read * usage.cached_read_tokens
            + cached_write * usage.cached_write_tokens
        ) / MILLION
    return amount.quantize(QUANT)


def price(v: RateVersion | None, usage: Usage, *, estimated: bool = False) -> Costed:
    """`calculated` cost from a rate version (`estimated` when the token counts are the
    platform's own estimate, the provider having reported none), or `unmeasured` when no rate was
    in force."""
    if v is None:
        return UNMEASURED
    kind = "estimated" if estimated else "calculated"
    return Costed(kind, calculate(v, usage), v.currency, v.id, snapshot(v))


def model_keys(session: Session, tenant_id: UUID, model_id: UUID) -> tuple[str, str] | None:
    """(provider_key, model_key) of a model the tenant may name: its own, or a shared model of the
    SHI-wide provider (tenant NULL, DECISIONS B-1705). The provider's adapter key and the model's
    own key are what a rate card names. Another client's model gives None."""
    row = session.execute(
        text(
            "SELECT p.adapter_key, m.model_key FROM shifde.models m "
            "JOIN shifde.model_providers p ON p.id = m.provider_id "
            "AND p.tenant_id IS NOT DISTINCT FROM m.tenant_id "
            "WHERE m.id = :id AND (m.tenant_id = :t OR m.tenant_id IS NULL)"
        ),
        {"id": model_id, "t": tenant_id},
    ).first()
    return (str(row[0]), str(row[1])) if row else None
