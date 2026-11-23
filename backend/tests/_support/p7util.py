"""Helpers for the P7 ontology tests: the reference customer-service ontology over the sample
database, mapping / policy / query shortcuts and policy-restricted users."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from tests._support.conntests import discovery_of
from tests._support.fullapp import World
from tests._support.p2util import login, system_role
from tests._support.p3util import ADMIN, A, activate, connection, pg_body, secret
from tests._support.p4util import art, publish, set_tenant_setting, tenant_scope
from tests._support.p5util import (
    Hdr,
    Stack,
    limited_user,
    rest_connection,
    rest_tool_spec,
    user_session,
    wait_for,
    wait_run,
)

NS = "cs"
CUSTOMER, ACCOUNT, REQUEST, EMPLOYEE = (
    "cs.customer",
    "cs.account",
    "cs.service_request",
    "cs.employee",
)
R_ACCOUNTS, R_REQUESTS, R_EMPLOYEE = (
    "cs.customer_accounts",
    "cs.customer_requests",
    "cs.request_employee",
)
ACTION = "cs.escalate_request"
QUERY_PERMS = [
    "workspace:read",
    "project:read",
    "ontology:read",
    "ontology:query",
    "run:read",
    "run:cancel",
    "pipeline:read",
    "pipeline:execute",
    "agent:read",
    "agent:execute",
    "tool:read",
    "tool:invoke",
    "model:read",
    "model:invoke",
    "approval:read",
]


def _prop(
    key: str, ptype: str, *, nullable: bool = False, classification: str = "internal"
) -> dict[str, Any]:
    return {
        "key": key,
        "type": ptype,
        "nullable": nullable,
        "classification": classification,
        "label": key,
    }


def reference_spec(tool_version_id: str | None = None) -> dict[str, Any]:
    """The generic customer-service ontology (build prompt 08): Customer, Account, Service
    Request, Employee (the assigned employee)."""
    spec: dict[str, Any] = {
        "namespaces": [{"key": NS, "label": "Customer service"}],
        "object_types": [
            {
                "key": CUSTOMER,
                "label": "Customer",
                "namespace": NS,
                "properties": [
                    _prop("id", "integer"),
                    _prop("name", "string"),
                    _prop("email", "string", classification="confidential"),
                    _prop("region", "string"),
                    _prop("segment", "string"),
                    _prop("tax_id", "string", nullable=True, classification="restricted"),
                    _prop("created_at", "datetime"),
                ],
                "primary_key": ["id"],
                "display_property": "name",
            },
            {
                "key": ACCOUNT,
                "label": "Account",
                "namespace": NS,
                "properties": [
                    _prop("id", "integer"),
                    _prop("customer_id", "integer"),
                    _prop("account_number", "string"),
                    _prop("status", "string"),
                    _prop("balance", "number", classification="confidential"),
                    _prop("opened_at", "datetime"),
                ],
                "primary_key": ["id"],
                "display_property": "account_number",
            },
            {
                "key": REQUEST,
                "label": "Service Request",
                "namespace": NS,
                "properties": [
                    _prop("id", "integer"),
                    _prop("account_id", "integer"),
                    _prop("customer_id", "integer"),
                    _prop("region", "string"),
                    _prop("status", "string"),
                    _prop("priority", "string"),
                    _prop("assigned_employee_id", "integer", nullable=True),
                    _prop("summary", "string"),
                    _prop("opened_at", "datetime"),
                    _prop("closed_at", "datetime", nullable=True),
                ],
                "primary_key": ["id"],
                "display_property": "summary",
            },
            {
                "key": EMPLOYEE,
                "label": "Assigned Employee",
                "namespace": NS,
                "properties": [
                    _prop("id", "integer"),
                    _prop("name", "string"),
                    _prop("email", "string", classification="confidential"),
                    _prop("department", "string"),
                    _prop("region", "string"),
                    _prop("title", "string"),
                ],
                "primary_key": ["id"],
                "display_property": "name",
            },
        ],
        "relationships": [
            {
                "key": R_ACCOUNTS,
                "label": "has accounts",
                "from": CUSTOMER,
                "to": ACCOUNT,
                "cardinality": "one_to_many",
                "constraints": {"required": False},
            },
            {
                "key": R_REQUESTS,
                "label": "has service requests",
                "from": CUSTOMER,
                "to": REQUEST,
                "cardinality": "one_to_many",
                "constraints": {"required": False},
            },
            {
                "key": R_EMPLOYEE,
                "label": "assigned to",
                "from": REQUEST,
                "to": EMPLOYEE,
                "cardinality": "many_to_one",
                "constraints": {"required": False},
            },
        ],
        "actions": [],
        "ui": {"positions": {CUSTOMER: {"x": 0, "y": 0}}},
    }
    if tool_version_id:
        spec["actions"] = [
            {
                "key": ACTION,
                "object_type": REQUEST,
                "tool_version_id": tool_version_id,
                "param_mapping": {"item": "$object.summary"},
            }
        ]
    return spec


TABLES = {
    CUSTOMER: "customers",
    ACCOUNT: "accounts",
    REQUEST: "service_requests",
    EMPLOYEE: "employees",
}
EDGES = {
    R_ACCOUNTS: ("accounts", "customer_id", "id"),
    R_REQUESTS: ("service_requests", "customer_id", "id"),
    R_EMPLOYEE: ("service_requests", "id", "assigned_employee_id"),
}


def mapping_body(connection_id: str, kind: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    if kind == "object_type":
        ot = next(t for t in spec["object_types"] if t["key"] == key)
        return {
            "target_kind": kind,
            "target_key": key,
            "connection_id": connection_id,
            "relation": {"schema": "", "name": TABLES[key]},
            "column_map": {p["key"]: p["key"] for p in ot["properties"]},
            "filters": [],
        }
    table, start, end = EDGES[key]
    return {
        "target_kind": kind,
        "target_key": key,
        "connection_id": connection_id,
        "relation": {"schema": "public", "name": table},
        "key_map": {"from_column": start, "to_column": end},
        "filters": [],
    }


def rows_of(sample_db_url: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(sample_db_url) as conn:
        return conn.execute(sql, params).fetchall()


def sample_admin_exec(sample_db_url: str, *statements: str) -> None:
    """Run DDL / DML on the sample database as the superuser (tests only)."""
    from sqlalchemy.engine import make_url

    from tests.conftest import _env

    admin = make_url(_env("SHIFDE_TEST_ADMIN_DATABASE_URL"))
    target = make_url(sample_db_url)
    with psycopg.connect(
        host=admin.host,
        port=admin.port,
        user=admin.username,
        password=admin.password,
        dbname=target.database,
    ) as conn:
        for statement in statements:
            conn.execute(statement)  # type: ignore[call-overload]  # str, not LiteralString: test DDL


def user_id(owner: Session, email: str) -> str:
    return str(
        owner.execute(
            text("SELECT id FROM shifde.users WHERE primary_email = :e"), {"e": email}
        ).scalar_one()
    )


class World7:
    """Admin session, the sample-database connection (active + discovered), and helpers to build,
    map, publish, restrict and query the reference ontology."""

    def __init__(
        self, client: TestClient, world: World, owner: Session, sample_db_url: str
    ) -> None:
        set_tenant_setting(owner, "demo-a", "capture_content", "full")
        self.client, self.world, self.owner = client, world, owner
        self.sample_db_url = sample_db_url
        self.h = login(client, world.passwords, ADMIN)
        self.scope = tenant_scope(owner)
        password = sample_db_url.split(":")[2].split("@")[0]
        self.pg = str(
            activate(
                client,
                self.h,
                connection(
                    client,
                    self.h,
                    pg_body(sample_db_url, secret(client, self.h, "pg-pw", password)),
                )["id"],
            )["id"]
        )
        self.discover()
        self.admin_id = user_id(owner, ADMIN)
        self.admin_role = str(system_role(owner, "tenant_administrator"))

    # ---------------------------------------------------------------- connection
    def discover(self, connection_id: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = discovery_of(self.client, self.h, A, connection_id or self.pg)
        return out

    # ---------------------------------------------------------------- ontology
    def create(
        self,
        key: str = "customer-service",
        spec: dict[str, Any] | None = None,
        *,
        scope: dict[str, str] | None = None,
        visibility: str = "scope",
    ) -> dict[str, Any]:
        out: dict[str, Any] = art(
            self.client,
            self.h,
            "ontologies",
            key,
            spec or reference_spec(),
            scope or self.scope,
            visibility=visibility,
        )
        return out

    def map_all(
        self, oid: str, spec: dict[str, Any] | None = None, connection_id: str | None = None
    ) -> None:
        spec = spec or reference_spec()
        cid = connection_id or self.pg
        for ot in spec["object_types"]:
            self.put_mapping(oid, mapping_body(cid, "object_type", ot["key"], spec))
        for rel in spec["relationships"]:
            self.put_mapping(oid, mapping_body(cid, "relationship", rel["key"], spec))

    def put_mapping(
        self, oid: str, body: dict[str, Any], status: int = 200, h: dict[str, str] | None = None
    ) -> Any:
        r = self.client.put(
            f"{A}/ontologies/{oid}/draft/mappings/{body['target_kind']}/{body['target_key']}",
            json=body,
            headers=h or self.h,
        )
        assert r.status_code == status, f"{r.status_code} {r.text}"
        return r.json() if r.content else None

    def validate(self, oid: str) -> dict[str, Any]:
        r = self.client.post(f"{A}/ontologies/{oid}/draft/validate", headers=self.h)
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    def ready(
        self,
        key: str = "customer-service",
        spec: dict[str, Any] | None = None,
        *,
        scope: dict[str, str] | None = None,
        visibility: str = "scope",
    ) -> dict[str, Any]:
        """Create, map and publish the reference ontology; returns the artifact with `version`."""
        spec = spec or reference_spec()
        created = self.create(key, spec, scope=scope, visibility=visibility)
        self.map_all(created["id"], spec)
        created["version"] = publish(self.client, self.h, "ontologies", created["id"])
        return created

    # ---------------------------------------------------------------- policies
    def policy(
        self,
        oid: str,
        object_type: str,
        subject: tuple[str, str],
        props: list[str] | None = None,
        row_filter: str | None = None,
        status: int = 201,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "object_type_key": object_type,
            "property_keys": props,
            "subject": {"type": subject[0], "id": subject[1]},
            "effect": "allow",
            **({"row_filter": row_filter} if row_filter else {}),
        }
        r = self.client.post(f"{A}/ontologies/{oid}/policies", json=body, headers=self.h)
        assert r.status_code == status, f"{r.status_code} {r.text}"
        out: dict[str, Any] = r.json()
        return out

    def allow_all(self, oid: str, subject: tuple[str, str]) -> None:
        """Every object type, every non-restricted property."""
        for ot in (CUSTOMER, ACCOUNT, REQUEST, EMPLOYEE):
            self.policy(oid, ot, subject)

    # ---------------------------------------------------------------- users
    def member(self, email: str, permissions: list[str] | None = None) -> tuple[Hdr, str, str]:
        """A member holding exactly `permissions` through a custom role; (headers, user id, role id)."""
        uid, rid = limited_user(
            self.client,
            self.owner,
            self.h,
            self.world.passwords,
            email,
            permissions if permissions is not None else QUERY_PERMS,
        )
        hdr = user_session(self.client, self.world.passwords, email)
        self.h = login(self.client, self.world.passwords, ADMIN)  # restore the admin cookie
        return hdr, uid, rid

    # ---------------------------------------------------------------- queries
    def query(
        self,
        oid: str,
        body: dict[str, Any],
        *,
        h: dict[str, str] | None = None,
        client: TestClient | None = None,
        status: int = 200,
    ) -> Any:
        c = client or self.client
        r = c.post(f"{A}/ontologies/{oid}/query", json=body, headers=h or self.h)
        assert r.status_code == status, f"{r.status_code} {r.text}"
        return r.json()

    def lineage(self, oid: str, run_id: str | None = None) -> list[dict[str, Any]]:
        q = f"?ontology_id={oid}" + (f"&run_id={run_id}" if run_id else "")
        r = self.client.get(f"{A}/ontology-lineage{q}", headers=self.h)
        assert r.status_code == 200, r.text
        out: list[dict[str, Any]] = r.json()["items"]
        return out


def q(
    object_type: str,
    select: list[str],
    *,
    filter: str | None = None,  # noqa: A002 - the API field name
    traverse: list[dict[str, Any]] | None = None,
    limit: int = 100,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"object_type": object_type, "select": select, "limit": limit, **extra}
    if filter is not None:
        body["filter"] = filter
    if traverse is not None:
        body["traverse"] = traverse
    return body


def clone(spec: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(spec)


__all__ = [
    "ACTION",
    "ACCOUNT",
    "CUSTOMER",
    "EMPLOYEE",
    "QUERY_PERMS",
    "REQUEST",
    "R_ACCOUNTS",
    "R_EMPLOYEE",
    "R_REQUESTS",
    "RestTestServer",
    "Stack",
    "World7",
    "clone",
    "mapping_body",
    "q",
    "reference_spec",
    "rest_connection",
    "rest_tool_spec",
    "rows_of",
    "user_id",
    "wait_for",
    "wait_run",
]
