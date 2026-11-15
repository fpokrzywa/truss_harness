"""SQL for model providers, models, allowlists, routes and the SHI-provider switch.

SETH 3ba0e06's queries, keyed by the owning client (`tenant_id`) as well as by row-level security
(DECISIONS B-1704, the connections B-1503 pattern): a read returns the client's own rows plus the
shared rows of the SHI-wide provider (tenant NULL, B-1701); a write names the client it writes
for (None = a shared row, which row-level security lets only the platform administrator context
write). Reading a shared model is not permission to call it: the call-time gate (part 2) checks
`shi_enabled` for the caller's client.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any  # Any: column values
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = Mapping[Any, Any]  # Any: SQLAlchemy RowMapping keys / column values

# The one platform-level provider (tenant NULL); pinned by `ck_model_providers__platform`.
SHI_PROVIDER_ID = UUID("00000000-0000-4000-8000-00000000051a")

PROVIDER_COLUMNS = (
    "id, tenant_id, vendor_id, name, adapter_key, connection_id, settings, status, health_status, "
    "health_checked_at, health_detail, row_version, created_at, updated_at"
)
PROVIDER_UPDATABLE = frozenset(
    {"name", "settings", "status", "health_status", "health_checked_at", "health_detail"}
)
MODEL_COLUMNS = (
    "id, tenant_id, provider_id, model_key, display_name, family, version_label, capabilities, "
    "context_window, max_output_tokens, lifecycle, deprecation_date, rate_limits, pricing_ref, "
    "row_version, created_at, updated_at"
)
MODEL_UPDATABLE = frozenset(
    {
        "display_name",
        "family",
        "version_label",
        "capabilities",
        "context_window",
        "max_output_tokens",
        "lifecycle",
        "deprecation_date",
        "rate_limits",
        "pricing_ref",
    }
)
ROUTE_COLUMNS = (
    "id, tenant_id, name, scope_type, scope_id, primary_model_id, fallback_model_ids, conditions, "
    "row_version, created_at, updated_at"
)
ROUTE_UPDATABLE = frozenset({"name", "primary_model_id", "fallback_model_ids", "conditions"})
ACCESS_COLUMNS = "tenant_id, enabled, changed_by, changed_at, row_version, created_at, updated_at"

# own rows and the shared rows of the SHI provider
_VISIBLE = "(tenant_id = :t OR tenant_id IS NULL)"


def _update_owned(
    session: Session,
    table: str,
    columns: str,
    updatable: frozenset[str],
    row_id: UUID,
    tenant_id: UUID | None,
    expected_version: int | None,
    values: Mapping[str, Any],
    json_columns: frozenset[str] = frozenset(),
) -> Row | None:
    """Optimistic-locking ``UPDATE ... RETURNING`` keyed by id AND owning client (`tenant_id`
    None = a shared row). `table`, `columns` and `updatable` are module constants; a value key
    outside `updatable` is refused before any SQL is built; every value is a bound parameter."""
    sets = ["row_version = row_version + 1"]
    params: dict[str, Any] = {"id": row_id, "t": tenant_id, "expected": expected_version}
    for column, value in values.items():
        if column not in updatable:
            raise ValueError(f"column {column!r} is not updatable")
        if column in json_columns:
            sets.append(f"{column} = CAST(:{column} AS jsonb)")
            params[column] = json.dumps(value)
        else:
            sets.append(f"{column} = :{column}")
            params[column] = value
    sql = (
        f"UPDATE shifde.{table} SET {', '.join(sets)} WHERE id = :id "
        "AND tenant_id IS NOT DISTINCT FROM CAST(:t AS uuid) "
        f"AND (CAST(:expected AS int) IS NULL OR row_version = :expected) RETURNING {columns}"
    )
    return session.execute(text(sql), params).mappings().first()


# ------------------------------------------------------------------ providers
def get_provider(session: Session, provider_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {PROVIDER_COLUMNS} FROM shifde.model_providers "
                f"WHERE id = :id AND {_VISIBLE}"
            ),
            {"id": provider_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_providers(
    session: Session, after: UUID | None, limit: int, *, tenant_id: UUID
) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {PROVIDER_COLUMNS} FROM shifde.model_providers WHERE {_VISIBLE} "
                "AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
            ),
            {"t": tenant_id, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


def insert_provider(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.model_providers (id, tenant_id, vendor_id, name, adapter_key, "
            "connection_id, settings, created_by) VALUES (:id, :tenant_id, :vendor_id, :name, "
            ":adapter_key, :connection_id, CAST(:settings AS jsonb), :created_by)"
        ),
        {**values, "settings": json.dumps(values["settings"])},
    )


def ensure_shi_provider(
    session: Session,
    *,
    adapter_key: str,
    settings: Mapping[str, Any],
    created_by: UUID | None,
    name: str = "SHI",
) -> None:
    """Create the SHI-wide provider row if it is missing (disabled until configured). Row-level
    security admits it only in the platform administrator context."""
    session.execute(
        text(
            "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, settings, "
            "status, created_by) VALUES (:id, NULL, :name, :adapter_key, "
            "CAST(:settings AS jsonb), 'disabled', :created_by) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": SHI_PROVIDER_ID,
            "name": name,
            "adapter_key": adapter_key,
            "settings": json.dumps(dict(settings)),
            "created_by": created_by,
        },
    )


def update_provider(
    session: Session,
    provider_id: UUID,
    expected: int | None,
    values: Mapping[str, Any],
    *,
    tenant_id: UUID | None,
) -> Row | None:
    return _update_owned(
        session,
        "model_providers",
        PROVIDER_COLUMNS,
        PROVIDER_UPDATABLE,
        provider_id,
        tenant_id,
        expected,
        values,
        frozenset({"settings"}),
    )


# ------------------------------------------------------------------ models
def get_model(session: Session, model_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(f"SELECT {MODEL_COLUMNS} FROM shifde.models WHERE id = :id AND {_VISIBLE}"),
            {"id": model_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def get_models(session: Session, model_ids: Sequence[UUID], *, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {MODEL_COLUMNS} FROM shifde.models "
                f"WHERE id = ANY(CAST(:ids AS uuid[])) AND {_VISIBLE}"
            ),
            {"ids": list(model_ids), "t": tenant_id},
        )
        .mappings()
        .all()
    )


def list_models(
    session: Session,
    *,
    tenant_id: UUID,
    provider_id: UUID | None,
    lifecycle: str | None,
    after: UUID | None,
    limit: int,
) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {MODEL_COLUMNS} FROM shifde.models WHERE {_VISIBLE} "
                "AND (CAST(:provider AS uuid) IS NULL OR provider_id = :provider) "
                "AND (CAST(:lifecycle AS text) IS NULL OR lifecycle = :lifecycle) "
                "AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
            ),
            {
                "t": tenant_id,
                "provider": provider_id,
                "lifecycle": lifecycle,
                "after": after,
                "n": limit,
            },
        )
        .mappings()
        .all()
    )


def insert_model(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.models (id, tenant_id, provider_id, model_key, display_name, "
            "family, version_label, capabilities, context_window, max_output_tokens, lifecycle, "
            "deprecation_date, rate_limits, pricing_ref, created_by) VALUES (:id, :tenant_id, "
            ":provider_id, :model_key, :display_name, :family, :version_label, :capabilities, "
            ":context_window, :max_output_tokens, :lifecycle, :deprecation_date, "
            "CAST(:rate_limits AS jsonb), :pricing_ref, :created_by)"
        ),
        {**values, "rate_limits": json.dumps(values["rate_limits"])},
    )


def update_model(
    session: Session,
    model_id: UUID,
    expected: int | None,
    values: Mapping[str, Any],
    *,
    tenant_id: UUID | None,
) -> Row | None:
    return _update_owned(
        session,
        "models",
        MODEL_COLUMNS,
        MODEL_UPDATABLE,
        model_id,
        tenant_id,
        expected,
        values,
        frozenset({"rate_limits"}),
    )


# ------------------------------------------------------------------ allowlists
def get_allowlist(
    session: Session, scope_type: str, scope_id: UUID, *, tenant_id: UUID
) -> Row | None:
    return (
        session.execute(
            text(
                "SELECT id, scope_type, scope_id, enabled, row_version FROM "
                "shifde.model_allowlists WHERE tenant_id = :t AND scope_type = :st "
                "AND scope_id = :si"
            ),
            {"t": tenant_id, "st": scope_type, "si": scope_id},
        )
        .mappings()
        .first()
    )


def allowlist_model_ids(session: Session, allowlist_id: UUID, *, tenant_id: UUID) -> list[UUID]:
    return list(
        session.execute(
            text(
                "SELECT model_id FROM shifde.model_allowlist_models "
                "WHERE tenant_id = :t AND allowlist_id = :a"
            ),
            {"t": tenant_id, "a": allowlist_id},
        ).scalars()
    )


def upsert_allowlist(
    session: Session,
    tenant_id: UUID,
    allowlist_id: UUID,
    scope_type: str,
    scope_id: UUID,
    enabled: bool,
) -> UUID:
    row = session.execute(
        text(
            "INSERT INTO shifde.model_allowlists (id, tenant_id, scope_type, scope_id, enabled) "
            "VALUES (:id, :t, :st, :si, :e) ON CONFLICT (tenant_id, scope_type, scope_id) "
            "DO UPDATE SET enabled = :e, row_version = model_allowlists.row_version + 1 "
            "RETURNING id"
        ),
        {"id": allowlist_id, "t": tenant_id, "st": scope_type, "si": scope_id, "e": enabled},
    ).scalar_one()
    return UUID(str(row))


def replace_allowlist_models(
    session: Session, tenant_id: UUID, allowlist_id: UUID, model_ids: Sequence[UUID]
) -> None:
    session.execute(
        text(
            "DELETE FROM shifde.model_allowlist_models WHERE tenant_id = :t AND allowlist_id = :a"
        ),
        {"t": tenant_id, "a": allowlist_id},
    )
    for model_id in model_ids:
        session.execute(
            text(
                "INSERT INTO shifde.model_allowlist_models (tenant_id, allowlist_id, model_id) "
                "VALUES (:t, :a, :m)"
            ),
            {"t": tenant_id, "a": allowlist_id, "m": model_id},
        )


# ------------------------------------------------------------------ routes
def get_route(session: Session, route_id: UUID, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(
                f"SELECT {ROUTE_COLUMNS} FROM shifde.model_routes WHERE id = :id AND tenant_id = :t"
            ),
            {"id": route_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )


def list_routes(session: Session, after: UUID | None, limit: int, *, tenant_id: UUID) -> list[Row]:
    return list(
        session.execute(
            text(
                f"SELECT {ROUTE_COLUMNS} FROM shifde.model_routes WHERE tenant_id = :t "
                "AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
            ),
            {"t": tenant_id, "after": after, "n": limit},
        )
        .mappings()
        .all()
    )


def insert_route(session: Session, values: Mapping[str, Any]) -> None:
    session.execute(
        text(
            "INSERT INTO shifde.model_routes (id, tenant_id, name, scope_type, scope_id, "
            "primary_model_id, fallback_model_ids, conditions, created_by) "
            "VALUES (:id, :tenant_id, "
            ":name, :scope_type, :scope_id, :primary_model_id, :fallback_model_ids, "
            "CAST(:conditions AS jsonb), :created_by)"
        ),
        {**values, "conditions": json.dumps(values["conditions"])},
    )


def update_route(
    session: Session,
    route_id: UUID,
    expected: int | None,
    values: Mapping[str, Any],
    *,
    tenant_id: UUID,
) -> Row | None:
    return _update_owned(
        session,
        "model_routes",
        ROUTE_COLUMNS,
        ROUTE_UPDATABLE,
        route_id,
        tenant_id,
        expected,
        values,
        frozenset({"conditions"}),
    )


# ------------------------------------------------------------------ SHI provider switch
def get_shi_access(session: Session, *, tenant_id: UUID) -> Row | None:
    return (
        session.execute(
            text(f"SELECT {ACCESS_COLUMNS} FROM shifde.shi_provider_access WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        .mappings()
        .first()
    )


def shi_enabled(session: Session, *, tenant_id: UUID) -> bool:
    """Whether the client may call the SHI provider: off unless a row says it is on."""
    row = get_shi_access(session, tenant_id=tenant_id)
    return bool(row is not None and row["enabled"])


def set_shi_access(
    session: Session,
    *,
    tenant_id: UUID,
    enabled: bool,
    changed_by: UUID | None,
    expected: int | None,
) -> Row | None:
    """Turn the SHI provider on or off for one client (row-level security admits it only in the
    platform administrator context, in that client's context). None when `expected` is stale."""
    return (
        session.execute(
            text(
                "INSERT INTO shifde.shi_provider_access AS a (tenant_id, enabled, changed_by) "
                "VALUES (:t, :e, :by) ON CONFLICT (tenant_id) DO UPDATE "
                "SET enabled = :e, changed_by = :by, changed_at = now(), "
                "row_version = a.row_version + 1 "
                "WHERE CAST(:expected AS int) IS NULL OR a.row_version = :expected "
                f"RETURNING {ACCESS_COLUMNS}"
            ),
            {"t": tenant_id, "e": enabled, "by": changed_by, "expected": expected},
        )
        .mappings()
        .first()
    )
