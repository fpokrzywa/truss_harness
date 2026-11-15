"""Helpers shared by the ledger tests: a stand-in for a table of a module not ported yet, and
direct inserts (as the schema owner) of models, rate cards and budgets."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from tests._support import tenants

PAST = utcnow() - timedelta(days=30)

# The ledger reads `shifde.runs` (runtime). Until runtime is ported, a test that evaluates
# budgets creates a minimal stand-in in its own transaction, which the test rolls back. Delete this
# when the real table exists (carry_forward). `tenants` is real since tenancy_0001; `models` and
# `model_providers` since models_admin_0001.
STAND_INS = (
    "CREATE TABLE IF NOT EXISTS shifde.runs (id uuid PRIMARY KEY, tenant_id uuid, "
    "root_run_id uuid, workspace_id uuid, project_id uuid, agent_id uuid, pipeline_id uuid, "
    "orchestration_id uuid, initiated_by_principal_id uuid)",
)


def create_stand_ins(s: Session) -> None:
    for stmt in STAND_INS:
        s.execute(text(stmt))


def add_tenant(s: Session, tenant_id: UUID | None = None, settings: str = "{}") -> UUID:
    return tenants.add_tenant(s, tenant_id, settings=json.loads(settings))


def add_model(
    s: Session, tenant_id: UUID, adapter_key: str = "openai", model_key: str = "m1"
) -> UUID:
    """A registered model of `tenant_id` (connection, provider, model), as the schema owner."""
    connection, provider, model = uuid7(), uuid7(), uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.connections (id, tenant_id, name, connection_type, adapter_key) "
            "VALUES (:c, :t, :n, 'model_api', :a)"
        ),
        {"c": connection, "t": tenant_id, "n": f"conn-{connection.hex}", "a": adapter_key},
    )
    s.execute(
        text(
            "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, connection_id) "
            "VALUES (:p, :t, :n, :a, :c)"
        ),
        {
            "p": provider,
            "t": tenant_id,
            "n": f"prov-{provider.hex}",
            "a": adapter_key,
            "c": connection,
        },
    )
    s.execute(
        text(
            "INSERT INTO shifde.models (id, tenant_id, provider_id, model_key, display_name) "
            "VALUES (:m, :t, :p, :k, :k)"
        ),
        {"m": model, "t": tenant_id, "p": provider, "k": model_key},
    )
    return model


def add_run(s: Session, tenant_id: UUID, **cols: Any) -> UUID:  # Any: column values
    run = uuid7()
    values = {"root_run_id": run, **cols}
    names = ["id", "tenant_id", *values]
    s.execute(
        text(
            f"INSERT INTO shifde.runs ({', '.join(names)}) "
            f"VALUES ({', '.join(':' + n for n in names)})"
        ),
        {"id": run, "tenant_id": tenant_id, **values},
    )
    return run


def add_rate_card(
    s: Session, tenant_id: UUID | None, provider_key: str = "openai", model_key: str = "m1"
) -> UUID:
    card = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.rate_cards (id, tenant_id, provider_key, model_key, name) "
            "VALUES (:i, :t, :p, :m, 'card')"
        ),
        {"i": card, "t": tenant_id, "p": provider_key, "m": model_key},
    )
    return card


def _seed_context(s: Session, tenant_id: UUID | None) -> None:
    """The version guard (isolation review 1, item 1) refuses a row outside the caller's context,
    owner sessions included. A session without a client context gets the row's own
    (transaction-local) and the helper re-points what it set for the next row; a context the test
    set itself is kept, so the tests that expect the guard to refuse still do."""
    seeded = s.info.get("seeded_context")  # what this helper last set on the session
    current = s.execute(text("SELECT shifde.current_tenant_id()")).scalar_one()
    admin = s.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one()
    if (current is not None or admin) and seeded is None:
        return  # the test's own context
    s.info["seeded_context"] = tenant_id or "admin"
    s.execute(
        text(
            "SELECT set_config('app.tenant_id', :t, true), set_config('app.platform_admin', :a, true)"
        ),
        {"t": str(tenant_id) if tenant_id else "", "a": "off" if tenant_id else "on"},
    )


def add_version(
    s: Session,
    card: UUID,
    tenant_id: UUID | None,
    version_no: int,
    *,
    frm: datetime = PAST,
    to: datetime | None = None,
    input_rate: str = "1",
    output_rate: str = "2",
    unit: str = "per_1m_tokens",
    currency: str = "USD",
) -> UUID:
    version = uuid7()
    _seed_context(s, tenant_id)
    s.execute(
        text(
            "INSERT INTO shifde.rate_card_versions (id, rate_card_id, tenant_id, version_no, "
            "effective_from, effective_to, currency, unit, input_rate, output_rate, source, "
            "revision) VALUES (:i, :c, :t, :n, :f, :e, :cur, :u, :ir, :orate, 'test', 'r1')"
        ),
        {
            "i": version,
            "c": card,
            "t": tenant_id,
            "n": version_no,
            "f": frm,
            "e": to,
            "cur": currency,
            "u": unit,
            "ir": Decimal(input_rate),
            "orate": Decimal(output_rate),
        },
    )
    return version


def add_rate(
    s: Session,
    tenant_id: UUID | None,
    provider_key: str = "openai",
    model_key: str = "m1",
    input_rate: str = "1",
    output_rate: str = "2",
    frm: datetime = PAST,
) -> UUID:
    """A card with one open-ended version; returns the version id."""
    card = add_rate_card(s, tenant_id, provider_key, model_key)
    return add_version(
        s, card, tenant_id, 1, frm=frm, input_rate=input_rate, output_rate=output_rate
    )


def add_budget(
    s: Session,
    tenant_id: UUID,
    *,
    scope_type: str = "tenant",
    scope_id: UUID | None = None,
    period: str = "daily",
    metric: str = "tokens",
    amount: str | None = "1000",
    currency: str | None = None,
    enforcement: str = "hard",
) -> UUID:
    budget = uuid7()
    s.execute(
        text(
            "INSERT INTO shifde.budgets (id, tenant_id, scope_type, scope_id, period, metric, "
            "amount, currency, enforcement) VALUES (:i, :t, :st, :si, :p, :m, :a, :c, :e)"
        ),
        {
            "i": budget,
            "t": tenant_id,
            "st": scope_type,
            "si": scope_id or tenant_id,
            "p": period,
            "m": metric,
            "a": None if amount is None else Decimal(amount),
            "c": currency,
            "e": enforcement,
        },
    )
    return budget
