"""Docker delivery test helpers (SP8-9): a Databricks target of the flow's organization (the claim a
Docker target's `workspace_host` needs), a Docker target, a sealed, passed `/2` Docker release for
it, and the delivery rows a refused request must not write."""

from __future__ import annotations

from typing import Any  # Any: JSON bodies, rows
from uuid import UUID

from sqlalchemy import text

from shifde.core.db import Database
from tests.connections.routes import as_user
from tests.delivery.app_releases import (
    DOCKER_KIND,
    AppRelease,
    add_provider,
    app_release_for_target,
)
from tests.delivery.routes import (
    DOCKER_BINDINGS,
    Flow,
    address,
    register,
    sp8_address,
    sp8_body,
    target_body,
)

TABLES = (
    "delivery_platform_claims",
    "delivery_targets",
    "delivery_artifacts",
    "delivery_plans",
    "delivery_approvals",
    "delivery_deployments",
    "delivery_jobs",
)


def workspace_target(flow: Flow, url: str | None = None) -> dict[str, Any]:  # Any: a TargetOut
    """A Databricks target of the flow's organization at `url` (a new address unless given)."""
    h = as_user(flow.client, flow.requester)
    body = target_body(flow.workspace, url or address())
    made: dict[str, Any] = register(flow.client, h, flow.slug, body)
    flow.client.cookies.clear()
    return made


def workspace(flow: Flow) -> str:
    """The address of a new Databricks target: a workspace the organization claims."""
    return str(workspace_target(flow)["address"])


def sp8_target(flow: Flow, kind: str, **overrides: Any) -> dict[str, Any]:  # Any: a TargetOut
    """A Fabric, Palantir or Docker target of the flow's organization at a new address."""
    h = as_user(flow.client, flow.requester)
    url = overrides.pop("address", None) or sp8_address(kind)
    body = sp8_body(kind, flow.workspace, url, **overrides)
    made: dict[str, Any] = register(flow.client, h, flow.slug, body)
    flow.client.cookies.clear()
    return made


def docker_target(
    flow: Flow, *, address: str | None = None, project_scope: str | None = None, **bindings: Any
) -> dict[str, Any]:  # Any: `DockerBindings` values, a TargetOut
    """A Docker target on a daemon of its own (unless `address`), in the SETH project
    `project_scope`; `bindings` override `DOCKER_BINDINGS`."""
    scoped = {"project": project_scope} if project_scope else {}
    bound = {**DOCKER_BINDINGS, **bindings}
    return sp8_target(flow, "docker", address=address, bindings=bound, **scoped)


def docker_release(
    flow: Flow, target: dict[str, Any], *, provider_id: UUID | None = None, **options: Any
) -> AppRelease:  # Any: a TargetOut, `make_app_release` options
    """A `/2` Docker release in the target's workspace, bound to a `fake` provider of the
    organization unless one is given."""
    provider = provider_id or add_provider(flow.owner_db, flow.tenant, "fake", None)
    return app_release_for_target(
        flow.owner_db, target["id"], provider_id=provider, kind=DOCKER_KIND, **options
    )


def rows(owner_db: Database, tenant: UUID) -> list[list[tuple[Any, ...]]]:
    """Every delivery row of `tenant`, whole, read as the schema owner."""
    with owner_db.session() as o:
        return [
            [
                tuple(r)
                for r in o.execute(
                    # the table names are the literals above
                    text(f"SELECT * FROM shifde.{table} WHERE tenant_id = :t ORDER BY id"),  # nosec B608
                    {"t": tenant},
                )
            ]
            for table in TABLES
        ]
