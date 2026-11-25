"""Palantir delivery test helpers (SP8-7): a target whose bindings the fake Foundry stack satisfies,
a sealed, passed `palantir.marketplace/v1` release in its scope, the target changes that make a plan
stale, and an administrator of one project only."""

from __future__ import annotations

import hashlib
from typing import Any  # Any: JSON bodies
from uuid import UUID, uuid4

from sqlalchemy import text

from shifde.adapters.native.palantir.fake import PUBLISHED_ASSET
from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.packaging.schemas import PALANTIR_TARGET_KIND
from tests.connections.routes import as_user
from tests.delivery.releases import SealedRelease, release_for_target
from tests.delivery.routes import (
    PALANTIR_BINDINGS,
    Flow,
    owner_sql,
    register,
    sp8_address,
    sp8_body,
)
from tests.identity import harness
from tests.tenancy.conftest import owner_member

DEPLOYABLE: dict[str, Any] = {
    **PALANTIR_BINDINGS,
    "frontend_asset_sha256": hashlib.sha256(PUBLISHED_ASSET).hexdigest(),
    "allow_preview_apis": True,
}
CHECKS = ("native-version", "ontology-read", "frontend-asset")
RESOURCE = "palantir.marketplace-installation"


def palantir_target(
    flow: Flow, url: str | None = None, **overrides: Any
) -> dict[str, Any]:  # Any: a TargetOut body, target body fields
    """A Palantir target of the flow's organization (`DEPLOYABLE` bindings), on a stack of its own
    unless `url` is given."""
    h = as_user(flow.client, flow.requester)
    body = sp8_body(
        "palantir",
        flow.workspace,
        url or sp8_address("palantir"),
        **{"bindings": dict(DEPLOYABLE), **overrides},
    )
    made: dict[str, Any] = register(flow.client, h, flow.slug, body)
    flow.client.cookies.clear()
    return made


def palantir_release(flow: Flow, target_id: object, **options: Any) -> SealedRelease:
    """A `/1` Marketplace release in the target's scope (`make_release` options), evaluation
    passed."""
    return release_for_target(
        flow.owner_db, target_id, **{"target_kind": PALANTIR_TARGET_KIND, **options}
    )


def change_target(flow: Flow, target_id: object, how: str) -> None:
    """Change the target as the schema owner (no route can): its `version_id` binding, or its
    `revision`."""
    if how == "revision":
        owner_sql(
            flow.owner_db,
            "UPDATE shifde.delivery_targets SET revision = revision + 1 WHERE id = :g",
            g=target_id,
        )
        return
    assert how == "bindings", how
    owner_sql(
        flow.owner_db,
        "UPDATE shifde.delivery_targets SET bindings = bindings || "
        "jsonb_build_object('version_id', CAST(:v AS text)) WHERE id = :g",
        g=target_id,
        v=str(uuid4()),
    )


def project_administrator(owner_db: Database, tenant: UUID, project: UUID) -> str:
    """A member of `tenant` whose only binding is `tenant_administrator` at `project`; the e-mail."""
    email = f"project-admin-{uuid7().hex[-12:]}@route.test"
    with owner_db.session() as o:
        uid = harness.person(o, email)
        owner_member(o, tenant, uid)
        o.execute(
            text(
                "INSERT INTO shifde.role_bindings (id, tenant_id, subject_type, subject_id, "
                "role_id, scope_type, scope_id) SELECT :i, :t, 'user', :u, r.id, 'project', :p "
                "FROM shifde.roles r WHERE r.tenant_id IS NULL AND r.key = 'tenant_administrator'"
            ),
            {"i": uuid7(), "t": tenant, "u": uid, "p": project},
        )
        o.commit()
    return email
