"""`shifde-admin dev-sample-ontology`: seed the reference ontology `customer-service` (P7, build
prompt 08 example) into a development organization.

Like `dev-sample-pipeline` it drives the PUBLIC API in-process (login as the org administrator,
then ordinary requests), so everything it creates went through the same validation, permission
checks and audit as a hand-made ontology: the sample PostgreSQL connection (secret + active
connection + schema discovery), the ontology with its four object types (Customer, Account,
Service Request, Employee as the assigned employee), three relationships, source mappings to the
`shifde_sample` tables, a harmless registered action (the builtin `util.echo` tool), the published
version, and the access policies:

* Developer: every non-restricted property of Customer, Account and Employee, and of Service
  Request limited by a ROW FILTER to the developer's own region
  (`row.region == principal.attributes.region`; set the attribute with
  `PUT /ontology-principal-attributes/user/{id}`; until then the policy does not apply);
* Tenant Administrator: every non-restricted property of every object type, plus the restricted
  `tax_id` of Customer (explicit allow).

No customer data is copied anywhere: the ontology only holds metadata. The command is idempotent.
Credentials come from `.devdata/` and are never printed or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON API documents
from uuid import UUID

from sqlalchemy.engine import make_url

from shifde.cli.sample_pipeline import (
    SeedError,
    _Api,
    _ensure_active,
    _ensure_secret,
    _read_password,
)
from shifde.core.devenv import read_dev_env
from shifde.core.settings import Settings

ONTOLOGY_KEY = "customer-service"
ACTION_KEY = "cs.show_request_summary"
NS = "cs"
CUSTOMER, ACCOUNT, REQUEST, EMPLOYEE = (
    "cs.customer",
    "cs.account",
    "cs.service_request",
    "cs.employee",
)
TABLES = {
    CUSTOMER: "customers",
    ACCOUNT: "accounts",
    REQUEST: "service_requests",
    EMPLOYEE: "employees",
}
# relationship key -> (edge relation, column holding the FROM key, column holding the TO key)
EDGES = {
    "cs.customer_accounts": ("accounts", "customer_id", "id"),
    "cs.customer_requests": ("service_requests", "customer_id", "id"),
    "cs.request_employee": ("service_requests", "id", "assigned_employee_id"),
}


@dataclass(frozen=True)
class SeededOntology:
    ontology_id: str
    version_id: str
    connection_id: str
    created: bool
    policies_created: int


def _prop(
    key: str, ptype: str, *, nullable: bool = False, classification: str = "internal"
) -> dict[str, Any]:  # Any: JSON
    return {
        "key": key,
        "type": ptype,
        "nullable": nullable,
        "classification": classification,
        "label": key.replace("_", " ").title(),
    }


def _object_type(
    key: str, label: str, display: str, props: list[dict[str, Any]]
) -> dict[str, Any]:  # Any: JSON
    return {
        "key": key,
        "label": label,
        "namespace": NS,
        "properties": props,
        "primary_key": ["id"],
        "display_property": display,
    }


def ontology_spec(tool_version_id: str) -> dict[str, Any]:  # Any: JSON
    """The logical model: business meaning only, no tables or columns."""
    return {
        "namespaces": [{"key": NS, "label": "Customer service"}],
        "object_types": [
            _object_type(
                CUSTOMER,
                "Customer",
                "name",
                [
                    _prop("id", "integer"),
                    _prop("name", "string"),
                    _prop("email", "string", classification="confidential"),
                    _prop("region", "string"),
                    _prop("segment", "string"),
                    _prop("tax_id", "string", nullable=True, classification="restricted"),
                    _prop("created_at", "datetime"),
                ],
            ),
            _object_type(
                ACCOUNT,
                "Account",
                "account_number",
                [
                    _prop("id", "integer"),
                    _prop("customer_id", "integer"),
                    _prop("account_number", "string"),
                    _prop("status", "string"),
                    _prop("balance", "number", classification="confidential"),
                    _prop("opened_at", "datetime"),
                ],
            ),
            _object_type(
                REQUEST,
                "Service Request",
                "summary",
                [
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
            ),
            _object_type(
                EMPLOYEE,
                "Assigned Employee",
                "name",
                [
                    _prop("id", "integer"),
                    _prop("name", "string"),
                    _prop("email", "string", classification="confidential"),
                    _prop("department", "string"),
                    _prop("region", "string"),
                    _prop("title", "string"),
                ],
            ),
        ],
        "relationships": [
            {
                "key": "cs.customer_accounts",
                "label": "has accounts",
                "from": CUSTOMER,
                "to": ACCOUNT,
                "cardinality": "one_to_many",
                "constraints": {"required": False},
            },
            {
                "key": "cs.customer_requests",
                "label": "has service requests",
                "from": CUSTOMER,
                "to": REQUEST,
                "cardinality": "one_to_many",
                "constraints": {"required": False},
            },
            {
                "key": "cs.request_employee",
                "label": "assigned to",
                "from": REQUEST,
                "to": EMPLOYEE,
                "cardinality": "many_to_one",
                "constraints": {"required": False},
            },
        ],
        "actions": [
            {
                "key": ACTION_KEY,
                "object_type": REQUEST,
                "tool_version_id": tool_version_id,
                "param_mapping": {"text": "$object.summary"},
            }
        ],
        "ui": {
            "positions": {
                CUSTOMER: {"x": 0, "y": 120},
                ACCOUNT: {"x": 420, "y": 0},
                REQUEST: {"x": 420, "y": 260},
                EMPLOYEE: {"x": 840, "y": 260},
            }
        },
    }


def mapping_bodies(connection_id: str, spec: dict[str, Any]) -> list[dict[str, Any]]:  # Any: JSON
    """The physical layer: one mapping per object type and relationship."""
    out: list[dict[str, Any]] = []
    for ot in spec["object_types"]:
        out.append(
            {
                "target_kind": "object_type",
                "target_key": ot["key"],
                "connection_id": connection_id,
                "relation": {"schema": "public", "name": TABLES[ot["key"]]},
                "column_map": {p["key"]: p["key"] for p in ot["properties"]},
                "filters": [],
            }
        )
    for rel in spec["relationships"]:
        table, start, end = EDGES[rel["key"]]
        out.append(
            {
                "target_kind": "relationship",
                "target_key": rel["key"],
                "connection_id": connection_id,
                "relation": {"schema": "public", "name": table},
                "key_map": {"from_column": start, "to_column": end},
                "filters": [],
            }
        )
    return out


def _non_restricted(spec: dict[str, Any], object_type: str) -> list[str]:  # Any: JSON
    ot = next(t for t in spec["object_types"] if t["key"] == object_type)
    return [p["key"] for p in ot["properties"] if p["classification"] != "restricted"]


def policy_plan(
    spec: dict[str, Any],  # Any: JSON
    roles: dict[str, str],
) -> list[dict[str, Any]]:  # Any: JSON
    plan: list[dict[str, Any]] = []
    region_filter = "row.region == principal.attributes.region"
    for ot in spec["object_types"]:
        plan.append(_policy(ot["key"], roles["tenant_administrator"], None, None))
        plan.append(
            _policy(
                ot["key"],
                roles["developer"],
                None,
                region_filter if ot["key"] == REQUEST else None,
            )
        )
    plan.append(_policy(CUSTOMER, roles["tenant_administrator"], ["tax_id"], None))
    return plan


def _policy(
    object_type: str, role_id: str, props: list[str] | None, row_filter: str | None
) -> dict[str, Any]:  # Any: JSON
    return {
        "object_type_key": object_type,
        "property_keys": props,
        "subject": {"type": "role", "id": role_id},
        "effect": "allow",
        **({"row_filter": row_filter} if row_filter else {}),
    }


def _same(existing: dict[str, Any], wanted: dict[str, Any]) -> bool:  # Any: JSON
    return (
        existing["object_type_key"] == wanted["object_type_key"]
        and existing["property_keys"] == wanted["property_keys"]
        and existing["subject"] == {k: str(v) for k, v in wanted["subject"].items()}
        and (existing.get("row_filter") or None) == wanted.get("row_filter")
    )


def seed_sample_ontology(
    settings: Settings,
    *,
    org: str,
    admin_email: str,
    admin_password: str | None = None,
    sample_url: str | None = None,
) -> SeededOntology:
    url = sample_url or read_dev_env().get("SHIFDE_SAMPLE_DATABASE_URL")
    if not url:
        raise SeedError("the sample database is not set up: run `shifde-admin dev-sample-data`")
    pg = make_url(url)
    api = _Api(settings, org, admin_email, admin_password or _read_password(admin_email))
    try:
        tenant_id = next(
            o["id"]
            for o in api.client.get("/api/v1/orgs", headers=api.headers).json()["items"]
            if o["slug"] == org
        )
        scope = {"type": "tenant", "id": str(UUID(str(tenant_id)))}
        secret = _ensure_secret(api, "dev-sample-db-password", str(pg.password))
        conn = _ensure_active(
            api,
            "dev-sample-postgres",
            {
                "name": "dev-sample-postgres",
                "connection_type": "database",
                "adapter_key": "postgres",
                "scope": {},
                "access_mode": "read",
                # the dev sample reader is SELECT-only and read-only by default (B-2430, B-1507)
                "read_only_confirmed": True,
                "auth_method": "basic",
                "secret_id": secret,
                "config": {
                    "host": pg.host,
                    "port": pg.port,
                    "database": pg.database,
                    "user": pg.username,
                },
            },
        )
        api.call("POST", f"/connections/{conn}/discover")  # schema metadata only: no rows are read
        tool_version = _ensure_echo_tool(api, scope)
        spec = ontology_spec(tool_version)
        existing = api.find("/ontologies", "key", ONTOLOGY_KEY)
        created = existing is None
        onto = existing or api.call(
            "POST",
            "/ontologies",
            {
                "key": ONTOLOGY_KEY,
                "name": "Customer service",
                "description": "Customers, accounts, service requests and their assigned "
                "employees over the sample database (generic example).",
                "scope": scope,
                "spec": spec,
            },
        )
        if onto["latest_published_version"] is None:
            draft = api.call("GET", f"/ontologies/{onto['id']}/draft")
            api.call(
                "PUT",
                f"/ontologies/{onto['id']}/draft",
                {"spec": spec, "row_version": draft["row_version"]},
            )
            for body in mapping_bodies(conn, spec):
                api.call(
                    "PUT",
                    f"/ontologies/{onto['id']}/draft/mappings/"
                    f"{body['target_kind']}/{body['target_key']}",
                    body,
                )
            api.call("POST", f"/ontologies/{onto['id']}/publish", {"change_note": "dev sample"})
            onto = api.call("GET", f"/ontologies/{onto['id']}")
        roles = _role_ids(api)
        have = api.call("GET", f"/ontologies/{onto['id']}/policies")
        made = 0
        for wanted in policy_plan(spec, roles):
            if not any(_same(p, wanted) for p in have):
                api.call("POST", f"/ontologies/{onto['id']}/policies", wanted)
                made += 1
        return SeededOntology(
            ontology_id=str(onto["id"]),
            version_id=str(onto["latest_published_version"]["id"]),
            connection_id=conn,
            created=created,
            policies_created=made,
        )
    finally:
        api.close()


def _role_ids(api: _Api) -> dict[str, str]:
    items = api.call("GET", "/roles?limit=200")["items"]
    wanted = ("developer", "tenant_administrator")
    found = {r["key"]: str(r["id"]) for r in items if r["key"] in wanted}
    missing = [k for k in wanted if k not in found]
    if missing:
        raise SeedError(f"the system roles {missing} are not available in this organization")
    return found


def _ensure_echo_tool(api: _Api, scope: dict[str, str]) -> str:
    """A harmless action target: the builtin `util.echo` function (no side effect)."""
    spec: dict[str, Any] = {  # Any: JSON
        "executor": "builtin.function",
        "binding": {"function_key": "util.echo"},
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "additionalProperties": False,
            "properties": {"text": {"type": "string", "maxLength": 100000}},
        },
        "output_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "side_effect_class": "none",
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": 5,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "none",
    }
    tool = api.find("/tools", "key", "echo-request-summary")
    if tool is None:
        tool = api.call(
            "POST",
            "/tools",
            {
                "key": "echo-request-summary",
                "name": "Echo request summary",
                "scope": scope,
                "spec": spec,
            },
        )
    if tool["latest_published_version"] is None:
        api.call("POST", f"/tools/{tool['id']}/publish", {"change_note": "dev sample"})
        tool = api.call("GET", f"/tools/{tool['id']}")
    return str(tool["latest_published_version"]["id"])
