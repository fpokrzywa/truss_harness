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

# own rows, and the shared rows only when `:shared` (the service: the client's switch is on)
_OWN_OR_SHARED = "(tenant_id = :t OR (tenant_id IS NULL AND CAST(:shared AS boolean)))"


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
        # SET names checked against the code allowlist; values are bound
        f"UPDATE shifde.{table} SET {', '.join(sets)} WHERE id = :id "  # nosec B608
        "AND tenant_id IS NOT DISTINCT FROM CAST(:t AS uuid) "
        f"AND (CAST(:expected AS int) IS NULL OR row_version = :expected) RETURNING {columns}"
    )
    return session.execute(text(sql), params).mappings().first()


# ------------------------------------------------------------------ providers
def get_provider(
    session: Session, provider_id: UUID, *, tenant_id: UUID, shared: bool
) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {PROVIDER_COLUMNS} FROM shifde.model_providers "  # nosec B608
                f"WHERE id = :id AND {_OWN_OR_SHARED}"
            ),
            {"id": provider_id, "t": tenant_id, "shared": shared},
        )
        .mappings()
        .first()
    )


def list_providers(
    session: Session, after: UUID | None, limit: int, *, tenant_id: UUID, shared: bool
) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {PROVIDER_COLUMNS} FROM shifde.model_providers WHERE "  # nosec B608
                f"{_OWN_OR_SHARED} "
                "AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
            ),
            {"t": tenant_id, "shared": shared, "after": after, "n": limit},
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
    session: Session, *, adapter_key: str, created_by: UUID | None, name: str = "SHI"
) -> Row:
    """Create the SHI-wide provider row if it is missing (disabled until configured, no settings:
    `ck_model_providers__shared_client_safe`) and return it. Row-level security admits the insert
    only in the platform administrator context; the row read back must be the shared one (tenant
    NULL), which `ck_model_providers__platform` alone may give the reserved id (review 1 item 1)."""
    session.execute(
        text(
            "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, settings, "
            "status, created_by) VALUES (:id, NULL, :name, :adapter_key, '{}'::jsonb, "
            "'disabled', :created_by) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": SHI_PROVIDER_ID, "name": name, "adapter_key": adapter_key, "created_by": created_by},
    )
    row = get_shi_provider(session)
    if row is None:
        raise RuntimeError("the SHI provider id is held by a row that is not the shared provider")
    return row


def get_shi_provider(session: Session) -> Row | None:
    """The shared SHI provider row (tenant NULL), or None if it does not exist yet."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {PROVIDER_COLUMNS} FROM shifde.model_providers "  # nosec B608
                "WHERE id = :id AND tenant_id IS NULL"
            ),
            {"id": SHI_PROVIDER_ID},
        )
        .mappings()
        .first()
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
def get_model(session: Session, model_id: UUID, *, tenant_id: UUID, shared: bool) -> Row | None:
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {MODEL_COLUMNS} FROM shifde.models WHERE id = :id AND "  # nosec B608
                f"{_OWN_OR_SHARED}"
            ),
            {"id": model_id, "t": tenant_id, "shared": shared},
        )
        .mappings()
        .first()
    )


def own_model_id_by_key(
    session: Session, provider_id: UUID, model_key: str, *, tenant_id: UUID
) -> UUID | None:
    """The id of the client's own model `model_key` of `provider_id` (never a shared model)."""
    found = session.execute(
        text(
            "SELECT id FROM shifde.models WHERE tenant_id = :t AND provider_id = :p "
            "AND model_key = :k"
        ),
        {"t": tenant_id, "p": provider_id, "k": model_key},
    ).scalar_one_or_none()
    return UUID(str(found)) if found is not None else None


def get_models(
    session: Session, model_ids: Sequence[UUID], *, tenant_id: UUID, shared: bool
) -> list[Row]:
    """The client's own models among `model_ids`, and the shared SHI models among them only when
    `shared` (the service passes the caller's switch, or False where only own models count)."""
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {MODEL_COLUMNS} FROM shifde.models "  # nosec B608
                f"WHERE id = ANY(CAST(:ids AS uuid[])) AND {_OWN_OR_SHARED}"
            ),
            {"ids": list(model_ids), "t": tenant_id, "shared": shared},
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
    shared: bool,
) -> list[Row]:
    return list(
        session.execute(
            text(
                # columns are module constants; values are bound
                f"SELECT {MODEL_COLUMNS} FROM shifde.models WHERE {_OWN_OR_SHARED} "  # nosec B608
                "AND (CAST(:provider AS uuid) IS NULL OR provider_id = :provider) "
                "AND (CAST(:lifecycle AS text) IS NULL OR lifecycle = :lifecycle) "
                "AND (CAST(:after AS uuid) IS NULL OR id > :after) ORDER BY id LIMIT :n"
            ),
            {
                "t": tenant_id,
                "shared": shared,
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
                # columns are module constants; values are bound
                f"SELECT {ROUTE_COLUMNS} FROM shifde.model_routes WHERE id = :id AND "  # nosec B608
                "tenant_id = :t"
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
                # columns are module constants; values are bound
                f"SELECT {ROUTE_COLUMNS} FROM shifde.model_routes WHERE tenant_id = "  # nosec B608
                ":t "
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
            text(
                # columns are module constants; values are bound
                f"SELECT {ACCESS_COLUMNS} FROM shifde.shi_provider_access WHERE "  # nosec B608
                "tenant_id = :t"
            ),
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
    expected: int | None,
) -> Row | None:
    """Turn the SHI provider on or off for one client (row-level security admits it only in the
    platform administrator context, in that client's context). The author is the database's
    acting principal (`current_principal_id()`, required by the policies, review 1 item 5). None
    when `expected` is stale."""
    return (
        session.execute(
            text(
                # columns are module constants; values are bound
                "INSERT INTO shifde.shi_provider_access AS a (tenant_id, enabled, "  # nosec B608
                "changed_by) "
                "VALUES (:t, :e, shifde.current_principal_id()) ON CONFLICT (tenant_id) DO UPDATE "
                "SET enabled = :e, changed_by = shifde.current_principal_id(), "
                "changed_at = now(), row_version = a.row_version + 1 "
                "WHERE CAST(:expected AS int) IS NULL OR a.row_version = :expected "
                f"RETURNING {ACCESS_COLUMNS}"
            ),
            {"t": tenant_id, "e": enabled, "expected": expected},
        )
        .mappings()
        .first()
    )
