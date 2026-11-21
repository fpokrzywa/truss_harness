"""F-08 (issue #16, DECISIONS B-2653): workspace and project scoping is enforced by the PDP and
the services, not by row-level security (which is client level only). This module proves it route
by route.

`ROUTES` classifies every mounted operation, one entry per (METHOD, path), no wildcard or prefix
rule:

- `SCOPED`: the route reads, lists, changes or creates a resource of a workspace or project (its
  service hands the PDP a `ResourceRef` with a workspace/project, or filters a list with
  `visible_scopes`). The note names the case family below.
- `CLIENT`: client level only; `PLATFORM`: platform administrator only; `PUBLIC`: no
  organization. The note is the reason.

A route added to the app fails `test_every_mounted_operation_is_classified` until it is entered
here by hand. Every `SCOPED` operation has one case (`CASES`): U, a member of demo-a whose only
role binding holds every client permission at workspace W1, is refused on W2's resource and
allowed on W1's. Each family test collects every failing case and asserts once.

The `CLIENT` operations that hold workspace data (secrets, models and model routes, governance
decisions and overview, audit and usage exports) also have run-time checks, `CLIENT_CASES`: U is
refused on each, the client administrator's same request succeeds, and a refused secret write
leaves the secret's row unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.adapters.testservers import RestTestServer
from shifde.authz.pdp import PLATFORM_ONLY_ACTIONS
from shifde.core.clock import utcnow
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from tests._support.conntests import run_test
from tests._support.fullapp import World, full_client
from tests._support.p2util import login, new_user, post, tenant_id
from tests._support.p3util import (
    ADMIN,
    REST_TOKEN,
    VIEWER,
    A,
    activate,
    fake_model_stack,
    invoke_body,
    secret,
)
from tests._support.p4util import (
    agent_spec,
    art,
    builtin_tool_spec,
    edit,
    prompt_spec,
    publish,
    set_tenant_setting,
    skill_spec,
)
from tests._support.p5util import Hdr, user_session
from tests._support.runs import add_run

Op = tuple[str, str]

SCOPED, CLIENT, PLATFORM, PUBLIC = "scoped", "client", "platform", "public"
CLASSES = frozenset({SCOPED, CLIENT, PLATFORM, PUBLIC})

SOLUTIONS = "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/solutions"
DATASETS = "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}/evaluation-datasets"
EVALUATIONS = "/api/v1/orgs/{org}/releases/{release_id}/evaluations"

ROUTES: dict[Op, tuple[str, str]] = {
    # ------------------------------------------------------------------ no organization
    ("GET", "/api/auth/config"): (PUBLIC, "sign-in options, before any session"),
    ("POST", "/api/auth/local/login"): (PUBLIC, "local sign-in"),
    ("POST", "/api/auth/logout"): (PUBLIC, "ends the caller's own session"),
    ("GET", "/api/auth/oidc/{idp_id}/callback"): (PUBLIC, "OpenID Connect sign-in callback"),
    ("GET", "/api/auth/oidc/{idp_id}/login"): (PUBLIC, "OpenID Connect sign-in start"),
    ("GET", "/api/auth/session"): (PUBLIC, "the caller's own session and memberships"),
    ("POST", "/api/auth/setup/redeem"): (PUBLIC, "redeems a person's own setup token"),
    ("GET", "/api/health"): (PUBLIC, "liveness probe"),
    ("GET", "/api/metrics"): (PUBLIC, "process metrics, no client data"),
    ("GET", "/api/ready"): (PUBLIC, "readiness probe"),
    ("GET", "/healthz"): (PUBLIC, "liveness probe"),
    ("GET", "/api/v1/adapters"): (PUBLIC, "adapter catalog of the installation"),
    ("GET", "/api/v1/adapters/{key}/conformance-runs"): (PUBLIC, "adapter conformance catalog"),
    ("GET", "/api/v1/orgs"): (PUBLIC, "the organizations the caller belongs to"),
    ("GET", "/api/v1/permissions"): (PUBLIC, "permission catalog"),
    ("GET", "/api/v1/pipelines/node-types"): (PUBLIC, "pipeline node type catalog"),
    ("GET", "/api/v1/tool-executors"): (PUBLIC, "tool executor catalog"),
    # ------------------------------------------------------------------ platform administrator
    ("POST", "/api/v1/orgs"): (PLATFORM, "creating a client is the platform administrator's"),
    ("GET", "/api/v1/platform/egress-default"): (PLATFORM, "installation egress default"),
    ("PUT", "/api/v1/platform/egress-default"): (PLATFORM, "installation egress default"),
    ("GET", "/api/v1/platform/groups"): (PLATFORM, "platform groups"),
    ("POST", "/api/v1/platform/groups"): (PLATFORM, "platform groups"),
    ("GET", "/api/v1/platform/groups/{group_id}"): (PLATFORM, "platform groups"),
    ("PATCH", "/api/v1/platform/groups/{group_id}"): (PLATFORM, "platform groups"),
    ("PUT", "/api/v1/platform/groups/{group_id}/members"): (PLATFORM, "platform groups"),
    ("GET", "/api/v1/platform/health-details"): (PLATFORM, "installation health"),
    ("GET", "/api/v1/platform/idps"): (PLATFORM, "installation identity providers"),
    ("POST", "/api/v1/platform/idps"): (PLATFORM, "installation identity providers"),
    ("GET", "/api/v1/platform/idps/{idp_id}"): (PLATFORM, "installation identity providers"),
    ("PATCH", "/api/v1/platform/idps/{idp_id}"): (PLATFORM, "installation identity providers"),
    ("POST", "/api/v1/platform/idps/{idp_id}/test"): (PLATFORM, "installation identity providers"),
    ("GET", "/api/v1/platform/installation"): (PLATFORM, "installation configuration"),
    ("POST", "/api/v1/platform/orgs/{org}/group-bindings"): (
        PLATFORM,
        "platform administrator binds a group in a client",
    ),
    ("GET", "/api/v1/platform/orgs/{org}/shi-provider-access"): (
        PLATFORM,
        "a client's access to the shared SHI provider",
    ),
    ("PUT", "/api/v1/platform/orgs/{org}/shi-provider-access"): (
        PLATFORM,
        "a client's access to the shared SHI provider",
    ),
    ("POST", "/api/v1/platform/orgs/{org}/suspend"): (PLATFORM, "suspends a client"),
    ("POST", "/api/v1/platform/orgs/{org}/unsuspend"): (PLATFORM, "resumes a client"),
    ("GET", "/api/v1/platform/rate-cards"): (PLATFORM, "installation rate cards"),
    ("POST", "/api/v1/platform/rate-cards"): (PLATFORM, "installation rate cards"),
    ("GET", "/api/v1/platform/rate-cards/{card_id}/versions"): (
        PLATFORM,
        "installation rate cards",
    ),
    ("POST", "/api/v1/platform/rate-cards/{card_id}/versions"): (
        PLATFORM,
        "installation rate cards",
    ),
    ("GET", "/api/v1/platform/shi-provider"): (PLATFORM, "the shared SHI provider"),
    ("PUT", "/api/v1/platform/shi-provider"): (PLATFORM, "the shared SHI provider"),
    ("GET", "/api/v1/platform/status"): (PLATFORM, "installation status"),
    ("GET", "/api/v1/platform/vendors"): (PLATFORM, "installation vendors"),
    ("POST", "/api/v1/platform/vendors"): (PLATFORM, "installation vendors"),
    ("GET", "/api/v1/platform/vendors/{vendor_id}"): (PLATFORM, "installation vendors"),
    ("PATCH", "/api/v1/platform/vendors/{vendor_id}"): (PLATFORM, "installation vendors"),
    # ------------------------------------------------------------------ organization
    ("GET", "/api/v1/orgs/{org}"): (CLIENT, "the organization itself"),
    ("PATCH", "/api/v1/orgs/{org}"): (CLIENT, "the organization itself"),
    ("PUT", "/api/v1/orgs/{org}/capture-content"): (CLIENT, "organization content capture"),
    ("GET", "/api/v1/orgs/{org}/dashboard"): (SCOPED, "tenancy"),
    ("GET", "/api/v1/orgs/{org}/members"): (CLIENT, "member:read at the organization"),
    ("GET", "/api/v1/orgs/{org}/me/permissions"): (SCOPED, "tenancy"),
    ("GET", "/api/v1/orgs/{org}/workspaces"): (SCOPED, "tenancy"),
    ("POST", "/api/v1/orgs/{org}/workspaces"): (
        CLIENT,
        "workspace:create at the organization: a new workspace has no workspace yet",
    ),
    ("GET", "/api/v1/orgs/{org}/workspaces/{ws}"): (SCOPED, "tenancy"),
    ("PATCH", "/api/v1/orgs/{org}/workspaces/{ws}"): (SCOPED, "tenancy"),
    ("GET", "/api/v1/orgs/{org}/workspaces/{ws}/projects"): (SCOPED, "tenancy"),
    ("POST", "/api/v1/orgs/{org}/workspaces/{ws}/projects"): (SCOPED, "tenancy"),
    ("GET", "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}"): (SCOPED, "tenancy"),
    ("PATCH", "/api/v1/orgs/{org}/workspaces/{ws}/projects/{proj}"): (SCOPED, "tenancy"),
    # ------------------------------------------------------------------ identity and access
    ("GET", "/api/v1/orgs/{org}/users"): (CLIENT, "member administration at the organization"),
    ("POST", "/api/v1/orgs/{org}/users"): (CLIENT, "people are added at the organization"),
    ("PATCH", "/api/v1/orgs/{org}/users/{user_id}"): (CLIENT, "member administration"),
    ("GET", "/api/v1/orgs/{org}/groups"): (CLIENT, "groups bound in the organization"),
    ("GET", "/api/v1/orgs/{org}/groups/{group_id}"): (CLIENT, "groups bound in the organization"),
    ("GET", "/api/v1/orgs/{org}/roles"): (CLIENT, "role:read at the organization"),
    ("POST", "/api/v1/orgs/{org}/roles"): (CLIENT, "role:manage at the organization"),
    ("GET", "/api/v1/orgs/{org}/roles/{role_id}"): (CLIENT, "role:read at the organization"),
    ("PATCH", "/api/v1/orgs/{org}/roles/{role_id}"): (CLIENT, "role:manage at the organization"),
    ("GET", "/api/v1/orgs/{org}/role-bindings"): (SCOPED, "tenancy"),
    ("POST", "/api/v1/orgs/{org}/role-bindings"): (SCOPED, "tenancy"),
    ("DELETE", "/api/v1/orgs/{org}/role-bindings/{binding_id}"): (SCOPED, "tenancy"),
    ("GET", "/api/v1/orgs/{org}/service-accounts"): (CLIENT, "serviceaccount:read at the org"),
    ("POST", "/api/v1/orgs/{org}/service-accounts"): (CLIENT, "serviceaccount:manage at the org"),
    ("GET", "/api/v1/orgs/{org}/service-accounts/{sa_id}"): (
        CLIENT,
        "serviceaccount:read at the organization",
    ),
    ("PATCH", "/api/v1/orgs/{org}/service-accounts/{sa_id}"): (
        CLIENT,
        "serviceaccount:manage at the organization",
    ),
    ("GET", "/api/v1/orgs/{org}/api-keys"): (CLIENT, "apikey:read at the organization"),
    ("POST", "/api/v1/orgs/{org}/api-keys"): (
        CLIENT,
        "apikey:manage at the organization, before the key's resource scope is checked",
    ),
    ("POST", "/api/v1/orgs/{org}/api-keys/{key_id}/revoke"): (CLIENT, "apikey:manage at the org"),
    ("POST", "/api/v1/orgs/{org}/api-keys/{key_id}/rotate"): (CLIENT, "apikey:manage at the org"),
    ("GET", "/api/v1/orgs/{org}/idps"): (CLIENT, "the organization's identity providers"),
    ("POST", "/api/v1/orgs/{org}/idps"): (CLIENT, "the organization's identity providers"),
    ("GET", "/api/v1/orgs/{org}/idps/{idp_id}"): (CLIENT, "the organization's identity providers"),
    ("PATCH", "/api/v1/orgs/{org}/idps/{idp_id}"): (
        CLIENT,
        "the organization's identity providers",
    ),
    ("POST", "/api/v1/orgs/{org}/idps/{idp_id}/test"): (
        CLIENT,
        "the organization's identity providers",
    ),
    ("GET", "/api/v1/orgs/{org}/secrets"): (CLIENT, "secret:read at the organization"),
    ("POST", "/api/v1/orgs/{org}/secrets"): (SCOPED, "tenancy"),
    ("PATCH", "/api/v1/orgs/{org}/secrets/{secret_id}"): (
        CLIENT,
        "secret:manage at the organization, before the secret is read",
    ),
    ("POST", "/api/v1/orgs/{org}/secrets/{secret_id}/rotate"): (
        CLIENT,
        "secret:manage at the organization, before the secret is read",
    ),
    ("GET", "/api/v1/orgs/{org}/audit-events"): (SCOPED, "tenancy"),
    ("GET", "/api/v1/orgs/{org}/audit-events/export"): (
        CLIENT,
        "needs data:export at the organization; rows are also limited to audit:read scopes",
    ),
    ("GET", "/api/v1/orgs/{org}/audit-events/verify"): (CLIENT, "audit:read at the organization"),
    # ------------------------------------------------------------------ build: prompts
    ("GET", "/api/v1/orgs/{org}/prompts"): (SCOPED, "prompts"),
    ("POST", "/api/v1/orgs/{org}/prompts"): (SCOPED, "prompts"),
    ("GET", "/api/v1/orgs/{org}/prompts/{artifact_id}"): (SCOPED, "prompts"),
    ("PATCH", "/api/v1/orgs/{org}/prompts/{artifact_id}"): (SCOPED, "prompts"),
    ("GET", "/api/v1/orgs/{org}/prompts/{artifact_id}/diff"): (SCOPED, "prompts"),
    ("GET", "/api/v1/orgs/{org}/prompts/{artifact_id}/draft"): (SCOPED, "prompts"),
    ("PUT", "/api/v1/orgs/{org}/prompts/{artifact_id}/draft"): (SCOPED, "prompts"),
    ("POST", "/api/v1/orgs/{org}/prompts/{artifact_id}/draft/validate"): (SCOPED, "prompts"),
    ("POST", "/api/v1/orgs/{org}/prompts/{artifact_id}/publish"): (SCOPED, "prompts"),
    ("POST", "/api/v1/orgs/{org}/prompts/{artifact_id}/render"): (SCOPED, "prompts"),
    ("POST", "/api/v1/orgs/{org}/prompts/{artifact_id}/rollback"): (SCOPED, "prompts"),
    ("POST", "/api/v1/orgs/{org}/prompts/{artifact_id}/test"): (SCOPED, "prompts"),
    ("GET", "/api/v1/orgs/{org}/prompts/{artifact_id}/test-runs"): (SCOPED, "prompts"),
    ("GET", "/api/v1/orgs/{org}/prompts/{artifact_id}/versions"): (SCOPED, "prompts"),
    ("GET", "/api/v1/orgs/{org}/prompts/{artifact_id}/versions/{version_id}"): (
        SCOPED,
        "prompts",
    ),
    ("POST", "/api/v1/orgs/{org}/prompts/{artifact_id}/versions/{version_id}/state"): (
        SCOPED,
        "prompts",
    ),
    # ------------------------------------------------------------------ build: skills
    ("GET", "/api/v1/orgs/{org}/skills"): (SCOPED, "skills"),
    ("POST", "/api/v1/orgs/{org}/skills"): (SCOPED, "skills"),
    ("GET", "/api/v1/orgs/{org}/skills/{artifact_id}"): (SCOPED, "skills"),
    ("PATCH", "/api/v1/orgs/{org}/skills/{artifact_id}"): (SCOPED, "skills"),
    ("GET", "/api/v1/orgs/{org}/skills/{artifact_id}/diff"): (SCOPED, "skills"),
    ("GET", "/api/v1/orgs/{org}/skills/{artifact_id}/draft"): (SCOPED, "skills"),
    ("PUT", "/api/v1/orgs/{org}/skills/{artifact_id}/draft"): (SCOPED, "skills"),
    ("POST", "/api/v1/orgs/{org}/skills/{artifact_id}/draft/validate"): (SCOPED, "skills"),
    ("POST", "/api/v1/orgs/{org}/skills/{artifact_id}/publish"): (SCOPED, "skills"),
    ("POST", "/api/v1/orgs/{org}/skills/{artifact_id}/rollback"): (SCOPED, "skills"),
    ("GET", "/api/v1/orgs/{org}/skills/{artifact_id}/versions"): (SCOPED, "skills"),
    ("GET", "/api/v1/orgs/{org}/skills/{artifact_id}/versions/{version_id}"): (SCOPED, "skills"),
    ("POST", "/api/v1/orgs/{org}/skills/{artifact_id}/versions/{version_id}/state"): (
        SCOPED,
        "skills",
    ),
    # ------------------------------------------------------------------ build: tools
    ("GET", "/api/v1/orgs/{org}/tools"): (SCOPED, "tools"),
    ("POST", "/api/v1/orgs/{org}/tools"): (SCOPED, "tools"),
    ("GET", "/api/v1/orgs/{org}/tools/{artifact_id}"): (SCOPED, "tools"),
    ("PATCH", "/api/v1/orgs/{org}/tools/{artifact_id}"): (SCOPED, "tools"),
    ("GET", "/api/v1/orgs/{org}/tools/{artifact_id}/diff"): (SCOPED, "tools"),
    ("GET", "/api/v1/orgs/{org}/tools/{artifact_id}/draft"): (SCOPED, "tools"),
    ("PUT", "/api/v1/orgs/{org}/tools/{artifact_id}/draft"): (SCOPED, "tools"),
    ("POST", "/api/v1/orgs/{org}/tools/{artifact_id}/draft/validate"): (SCOPED, "tools"),
    ("POST", "/api/v1/orgs/{org}/tools/{artifact_id}/publish"): (SCOPED, "tools"),
    ("POST", "/api/v1/orgs/{org}/tools/{artifact_id}/rollback"): (SCOPED, "tools"),
    ("POST", "/api/v1/orgs/{org}/tools/{artifact_id}/test"): (SCOPED, "tools"),
    ("GET", "/api/v1/orgs/{org}/tools/{artifact_id}/versions"): (SCOPED, "tools"),
    ("GET", "/api/v1/orgs/{org}/tools/{artifact_id}/versions/{version_id}"): (SCOPED, "tools"),
    ("POST", "/api/v1/orgs/{org}/tools/{artifact_id}/versions/{version_id}/state"): (
        SCOPED,
        "tools",
    ),
    # ------------------------------------------------------------------ build: agents
    ("GET", "/api/v1/orgs/{org}/agents"): (SCOPED, "agents"),
    ("POST", "/api/v1/orgs/{org}/agents"): (SCOPED, "agents"),
    ("GET", "/api/v1/orgs/{org}/agents/{artifact_id}"): (SCOPED, "agents"),
    ("PATCH", "/api/v1/orgs/{org}/agents/{artifact_id}"): (SCOPED, "agents"),
    ("GET", "/api/v1/orgs/{org}/agents/{artifact_id}/diff"): (SCOPED, "agents"),
    ("GET", "/api/v1/orgs/{org}/agents/{artifact_id}/draft"): (SCOPED, "agents"),
    ("PUT", "/api/v1/orgs/{org}/agents/{artifact_id}/draft"): (SCOPED, "agents"),
    ("POST", "/api/v1/orgs/{org}/agents/{artifact_id}/draft/validate"): (SCOPED, "agents"),
    ("POST", "/api/v1/orgs/{org}/agents/{artifact_id}/publish"): (SCOPED, "agents"),
    ("POST", "/api/v1/orgs/{org}/agents/{artifact_id}/rollback"): (SCOPED, "agents"),
    ("POST", "/api/v1/orgs/{org}/agents/{artifact_id}/status"): (SCOPED, "agents"),
    ("POST", "/api/v1/orgs/{org}/agents/{artifact_id}/test"): (SCOPED, "agents"),
    ("GET", "/api/v1/orgs/{org}/agents/{artifact_id}/versions"): (SCOPED, "agents"),
    ("GET", "/api/v1/orgs/{org}/agents/{artifact_id}/versions/{version_id}"): (SCOPED, "agents"),
    ("GET", "/api/v1/orgs/{org}/agents/{artifact_id}/versions/{version_id}/resolved"): (
        SCOPED,
        "agents",
    ),
    ("POST", "/api/v1/orgs/{org}/agents/{artifact_id}/versions/{version_id}/state"): (
        SCOPED,
        "agents",
    ),
    # ------------------------------------------------------------------ build: orchestrations
    ("GET", "/api/v1/orgs/{org}/orchestrations"): (SCOPED, "orchestrations"),
    ("POST", "/api/v1/orgs/{org}/orchestrations"): (SCOPED, "orchestrations"),
    ("GET", "/api/v1/orgs/{org}/orchestrations/{artifact_id}"): (SCOPED, "orchestrations"),
    ("PATCH", "/api/v1/orgs/{org}/orchestrations/{artifact_id}"): (SCOPED, "orchestrations"),
    ("GET", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/diff"): (SCOPED, "orchestrations"),
    ("GET", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/draft"): (SCOPED, "orchestrations"),
    ("PUT", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/draft"): (SCOPED, "orchestrations"),
    ("POST", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/draft/validate"): (
        SCOPED,
        "orchestrations",
    ),
    ("POST", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/publish"): (
        SCOPED,
        "orchestrations",
    ),
    ("POST", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/rollback"): (
        SCOPED,
        "orchestrations",
    ),
    ("GET", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/versions"): (
        SCOPED,
        "orchestrations",
    ),
    ("GET", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/versions/{version_id}"): (
        SCOPED,
        "orchestrations",
    ),
    ("POST", "/api/v1/orgs/{org}/orchestrations/{artifact_id}/versions/{version_id}/state"): (
        SCOPED,
        "orchestrations",
    ),
    # ------------------------------------------------------------------ build: pipelines
    ("GET", "/api/v1/orgs/{org}/pipelines"): (SCOPED, "pipelines"),
    ("POST", "/api/v1/orgs/{org}/pipelines"): (SCOPED, "pipelines"),
    ("GET", "/api/v1/orgs/{org}/pipelines/{artifact_id}"): (SCOPED, "pipelines"),
    ("PATCH", "/api/v1/orgs/{org}/pipelines/{artifact_id}"): (SCOPED, "pipelines"),
    ("GET", "/api/v1/orgs/{org}/pipelines/{artifact_id}/diff"): (SCOPED, "pipelines"),
    ("GET", "/api/v1/orgs/{org}/pipelines/{artifact_id}/draft"): (SCOPED, "pipelines"),
    ("PUT", "/api/v1/orgs/{org}/pipelines/{artifact_id}/draft"): (SCOPED, "pipelines"),
    ("POST", "/api/v1/orgs/{org}/pipelines/{artifact_id}/draft/validate"): (SCOPED, "pipelines"),
    ("POST", "/api/v1/orgs/{org}/pipelines/{artifact_id}/publish"): (SCOPED, "pipelines"),
    ("POST", "/api/v1/orgs/{org}/pipelines/{artifact_id}/rollback"): (SCOPED, "pipelines"),
    ("GET", "/api/v1/orgs/{org}/pipelines/{artifact_id}/versions"): (SCOPED, "pipelines"),
    ("GET", "/api/v1/orgs/{org}/pipelines/{artifact_id}/versions/{version_id}"): (
        SCOPED,
        "pipelines",
    ),
    ("POST", "/api/v1/orgs/{org}/pipelines/{artifact_id}/versions/{version_id}/state"): (
        SCOPED,
        "pipelines",
    ),
    ("GET", "/api/v1/orgs/{org}/pipelines/{pipeline_id}/runs"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/pipelines/{pipeline_id}/schedules"): (SCOPED, "runs"),
    ("POST", "/api/v1/orgs/{org}/pipelines/{pipeline_id}/schedules"): (SCOPED, "runs"),
    # ------------------------------------------------------------------ build: ontologies
    ("GET", "/api/v1/orgs/{org}/ontologies"): (SCOPED, "ontologies"),
    ("POST", "/api/v1/orgs/{org}/ontologies"): (SCOPED, "ontologies"),
    ("GET", "/api/v1/orgs/{org}/ontologies/{artifact_id}"): (SCOPED, "ontologies"),
    ("PATCH", "/api/v1/orgs/{org}/ontologies/{artifact_id}"): (SCOPED, "ontologies"),
    ("GET", "/api/v1/orgs/{org}/ontologies/{artifact_id}/diff"): (SCOPED, "ontologies"),
    ("GET", "/api/v1/orgs/{org}/ontologies/{artifact_id}/draft"): (SCOPED, "ontologies"),
    ("PUT", "/api/v1/orgs/{org}/ontologies/{artifact_id}/draft"): (SCOPED, "ontologies"),
    (
        "DELETE",
        "/api/v1/orgs/{org}/ontologies/{artifact_id}/draft/mappings/{target_kind}/{target_key}",
    ): (SCOPED, "ontologies"),
    (
        "PUT",
        "/api/v1/orgs/{org}/ontologies/{artifact_id}/draft/mappings/{target_kind}/{target_key}",
    ): (SCOPED, "ontologies"),
    (
        "POST",
        "/api/v1/orgs/{org}/ontologies/{artifact_id}/draft/mappings/{target_kind}/{target_key}"
        "/check",
    ): (SCOPED, "ontologies"),
    ("POST", "/api/v1/orgs/{org}/ontologies/{artifact_id}/draft/validate"): (
        SCOPED,
        "ontologies",
    ),
    ("POST", "/api/v1/orgs/{org}/ontologies/{artifact_id}/publish"): (SCOPED, "ontologies"),
    ("POST", "/api/v1/orgs/{org}/ontologies/{artifact_id}/rollback"): (SCOPED, "ontologies"),
    ("GET", "/api/v1/orgs/{org}/ontologies/{artifact_id}/versions"): (SCOPED, "ontologies"),
    ("GET", "/api/v1/orgs/{org}/ontologies/{artifact_id}/versions/{version_id}"): (
        SCOPED,
        "ontologies",
    ),
    ("GET", "/api/v1/orgs/{org}/ontologies/{artifact_id}/versions/{version_id}/mappings"): (
        SCOPED,
        "ontologies",
    ),
    ("POST", "/api/v1/orgs/{org}/ontologies/{artifact_id}/versions/{version_id}/state"): (
        SCOPED,
        "ontologies",
    ),
    ("POST", "/api/v1/orgs/{org}/ontologies/{ontology_id}/actions/{action_key}/invoke"): (
        SCOPED,
        "ontologies",
    ),
    ("PUT", "/api/v1/orgs/{org}/ontologies/{ontology_id}/materializations/{object_type}"): (
        SCOPED,
        "ontologies",
    ),
    ("GET", "/api/v1/orgs/{org}/ontologies/{ontology_id}/objects/{object_type}/{pk}"): (
        SCOPED,
        "ontologies",
    ),
    ("GET", "/api/v1/orgs/{org}/ontologies/{ontology_id}/policies"): (SCOPED, "ontologies"),
    ("POST", "/api/v1/orgs/{org}/ontologies/{ontology_id}/policies"): (SCOPED, "ontologies"),
    ("DELETE", "/api/v1/orgs/{org}/ontologies/{ontology_id}/policies/{policy_id}"): (
        SCOPED,
        "ontologies",
    ),
    ("PATCH", "/api/v1/orgs/{org}/ontologies/{ontology_id}/policies/{policy_id}"): (
        SCOPED,
        "ontologies",
    ),
    ("POST", "/api/v1/orgs/{org}/ontologies/{ontology_id}/query"): (SCOPED, "ontologies"),
    ("GET", "/api/v1/orgs/{org}/ontology-lineage"): (SCOPED, "ontologies"),
    ("GET", "/api/v1/orgs/{org}/ontology-principal-attributes"): (
        CLIENT,
        "ontology:manage_policies at the organization",
    ),
    ("GET", "/api/v1/orgs/{org}/ontology-principal-attributes/{principal_type}/{principal_id}"): (
        CLIENT,
        "ontology:manage_policies at the organization",
    ),
    ("PUT", "/api/v1/orgs/{org}/ontology-principal-attributes/{principal_type}/{principal_id}"): (
        CLIENT,
        "ontology:manage_policies at the organization",
    ),
    # ------------------------------------------------------------------ build: publish requests
    ("GET", "/api/v1/orgs/{org}/publish-requests"): (SCOPED, "publish-requests"),
    ("POST", "/api/v1/orgs/{org}/publish-requests/{request_id}/decide"): (
        SCOPED,
        "publish-requests",
    ),
    # ------------------------------------------------------------------ connections, egress
    ("GET", "/api/v1/orgs/{org}/connections"): (SCOPED, "connections"),
    ("POST", "/api/v1/orgs/{org}/connections"): (SCOPED, "connections"),
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}"): (SCOPED, "connections"),
    ("PATCH", "/api/v1/orgs/{org}/connections/{connection_id}"): (SCOPED, "connections"),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/discover"): (SCOPED, "connections"),
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}/discoveries/latest"): (
        SCOPED,
        "connections",
    ),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/invoke"): (SCOPED, "connections"),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/status"): (SCOPED, "connections"),
    ("POST", "/api/v1/orgs/{org}/connections/{connection_id}/test"): (SCOPED, "connections"),
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}/test-jobs/{job_id}"): (
        SCOPED,
        "connections",
    ),
    ("GET", "/api/v1/orgs/{org}/connections/{connection_id}/tests"): (SCOPED, "connections"),
    ("GET", "/api/v1/orgs/{org}/egress-policies"): (CLIENT, "egress policies of the organization"),
    ("POST", "/api/v1/orgs/{org}/egress-policies"): (CLIENT, "egress:manage at the organization"),
    ("GET", "/api/v1/orgs/{org}/egress-policies/{policy_id}"): (
        CLIENT,
        "egress policies of the organization",
    ),
    ("PATCH", "/api/v1/orgs/{org}/egress-policies/{policy_id}"): (
        CLIENT,
        "egress:manage at the organization",
    ),
    # ------------------------------------------------------------------ delivery
    ("GET", "/api/v1/orgs/{org}/delivery/targets"): (SCOPED, "delivery"),
    ("POST", "/api/v1/orgs/{org}/delivery/targets"): (SCOPED, "delivery"),
    ("GET", "/api/v1/orgs/{org}/delivery/targets/{target_id}"): (SCOPED, "delivery"),
    ("POST", "/api/v1/orgs/{org}/delivery/targets/{target_id}/archive"): (SCOPED, "delivery"),
    # a plan is scoped by its target's workspace/project (B-2679)
    ("GET", "/api/v1/orgs/{org}/delivery/plans"): (SCOPED, "delivery"),
    ("POST", "/api/v1/orgs/{org}/delivery/plans"): (SCOPED, "delivery"),
    ("GET", "/api/v1/orgs/{org}/delivery/plans/{plan_id}"): (SCOPED, "delivery"),
    ("POST", "/api/v1/orgs/{org}/delivery/plans/{plan_id}/approve"): (SCOPED, "delivery"),
    ("POST", "/api/v1/orgs/{org}/delivery/plans/{plan_id}/reject"): (SCOPED, "delivery"),
    ("GET", "/api/v1/orgs/{org}/delivery/deployments"): (SCOPED, "delivery"),
    ("GET", "/api/v1/orgs/{org}/delivery/deployments/{deployment_id}"): (SCOPED, "delivery"),
    ("POST", "/api/v1/orgs/{org}/delivery/deployments/{deployment_id}/cancel"): (
        SCOPED,
        "delivery",
    ),
    ("POST", "/api/v1/orgs/{org}/delivery/deployments/{deployment_id}/reconcile"): (
        SCOPED,
        "delivery",
    ),
    ("POST", "/api/v1/orgs/{org}/delivery/deployments/{deployment_id}/mark-failed"): (
        SCOPED,
        "delivery",
    ),
    ("GET", "/api/v1/orgs/{org}/delivery/executors"): (CLIENT, "executors: organization level"),
    ("POST", "/api/v1/orgs/{org}/delivery/executors"): (CLIENT, "executors: organization level"),
    ("GET", "/api/v1/orgs/{org}/delivery/executors/{executor_id}"): (
        CLIENT,
        "executors: organization level",
    ),
    ("PATCH", "/api/v1/orgs/{org}/delivery/executors/{executor_id}"): (
        CLIENT,
        "executors: organization level",
    ),
    ("GET", "/api/v1/orgs/{org}/delivery/executors/{executor_id}/credentials"): (
        CLIENT,
        "executor credentials: organization level",
    ),
    ("POST", "/api/v1/orgs/{org}/delivery/executors/{executor_id}/credentials"): (
        CLIENT,
        "executor credentials: organization level",
    ),
    (
        "POST",
        "/api/v1/orgs/{org}/delivery/executors/{executor_id}/credentials/{credential_id}/rotate",
    ): (CLIENT, "executor credentials: organization level"),
    (
        "POST",
        "/api/v1/orgs/{org}/delivery/executors/{executor_id}/credentials/{credential_id}/revoke",
    ): (CLIENT, "executor credentials: organization level"),
    ("GET", "/api/executor/v1/whoami"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    ("POST", "/api/executor/v1/lease"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    ("POST", "/api/executor/v1/jobs/{job_id}/heartbeat"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    ("GET", "/api/executor/v1/jobs/{job_id}/artifact"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    ("GET", "/api/executor/v1/jobs/{job_id}/context"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    ("POST", "/api/executor/v1/jobs/{job_id}/credential-grant"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    ("POST", "/api/executor/v1/jobs/{job_id}/checkpoint"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    ("POST", "/api/executor/v1/jobs/{job_id}/result"): (
        PUBLIC,
        "executor credential, no user session; its own organization only",
    ),
    # ------------------------------------------------------------------ packaging
    ("GET", SOLUTIONS): (SCOPED, "packaging"),
    ("POST", SOLUTIONS): (SCOPED, "packaging"),
    ("GET", SOLUTIONS + "/{solution_id}"): (SCOPED, "packaging"),
    ("POST", SOLUTIONS + "/{solution_id}/archive"): (SCOPED, "packaging"),
    ("GET", SOLUTIONS + "/{solution_id}/versions"): (SCOPED, "packaging"),
    ("POST", SOLUTIONS + "/{solution_id}/versions"): (SCOPED, "packaging"),
    ("GET", SOLUTIONS + "/{solution_id}/versions/{version_id}"): (SCOPED, "packaging"),
    ("POST", SOLUTIONS + "/{solution_id}/versions/{version_id}/releases"): (SCOPED, "packaging"),
    ("GET", SOLUTIONS + "/{solution_id}/versions/{version_id}/releases"): (SCOPED, "packaging"),
    ("GET", "/api/v1/orgs/{org}/releases/{release_id}"): (SCOPED, "packaging"),
    ("POST", "/api/v1/orgs/{org}/releases/{release_id}/withdraw"): (SCOPED, "packaging"),
    # ------------------------------------------------------------------ evaluation
    ("GET", DATASETS): (SCOPED, "evaluation"),
    ("POST", DATASETS): (SCOPED, "evaluation"),
    ("GET", DATASETS + "/{dataset_id}"): (SCOPED, "evaluation"),
    ("POST", EVALUATIONS): (SCOPED, "evaluation"),
    ("GET", EVALUATIONS): (SCOPED, "evaluation"),
    ("GET", "/api/v1/orgs/{org}/evaluation-runs/{run_id}"): (SCOPED, "evaluation"),
    # ------------------------------------------------------------------ models
    ("GET", "/api/v1/orgs/{org}/model-allowlists"): (SCOPED, "models"),
    ("PUT", "/api/v1/orgs/{org}/model-allowlists"): (SCOPED, "models"),
    ("GET", "/api/v1/orgs/{org}/model-providers"): (CLIENT, "model:read at the organization"),
    ("POST", "/api/v1/orgs/{org}/model-providers"): (CLIENT, "model:manage at the organization"),
    ("GET", "/api/v1/orgs/{org}/model-providers/{provider_id}"): (
        CLIENT,
        "model:read at the organization",
    ),
    ("PATCH", "/api/v1/orgs/{org}/model-providers/{provider_id}"): (
        CLIENT,
        "model:manage at the organization",
    ),
    ("POST", "/api/v1/orgs/{org}/model-providers/{provider_id}/discover-models"): (
        CLIENT,
        "model:manage at the organization",
    ),
    ("POST", "/api/v1/orgs/{org}/model-providers/{provider_id}/health-check"): (
        CLIENT,
        "model:manage at the organization",
    ),
    ("GET", "/api/v1/orgs/{org}/model-routes"): (
        CLIENT,
        "model:read at the organization: routes are listed client-wide",
    ),
    ("POST", "/api/v1/orgs/{org}/model-routes"): (SCOPED, "models"),
    ("GET", "/api/v1/orgs/{org}/model-routes/{route_id}"): (
        CLIENT,
        "model:read at the organization, whatever the route's scope",
    ),
    ("PATCH", "/api/v1/orgs/{org}/model-routes/{route_id}"): (SCOPED, "models"),
    ("GET", "/api/v1/orgs/{org}/models"): (CLIENT, "model:read at the organization"),
    ("POST", "/api/v1/orgs/{org}/models"): (CLIENT, "model:manage at the organization"),
    ("GET", "/api/v1/orgs/{org}/models/effective"): (SCOPED, "models"),
    ("GET", "/api/v1/orgs/{org}/models/{model_id}"): (CLIENT, "model:read at the organization"),
    ("PATCH", "/api/v1/orgs/{org}/models/{model_id}"): (CLIENT, "model:manage at the org"),
    ("POST", "/api/v1/orgs/{org}/models/{model_id}/invoke"): (SCOPED, "models"),
    ("GET", "/api/v1/orgs/{org}/shi-provider-access"): (CLIENT, "model:read at the organization"),
    # ------------------------------------------------------------------ vendors, rate cards
    ("GET", "/api/v1/orgs/{org}/vendors"): (CLIENT, "vendors of the organization"),
    ("POST", "/api/v1/orgs/{org}/vendors"): (CLIENT, "vendors of the organization"),
    ("GET", "/api/v1/orgs/{org}/vendors/{vendor_id}"): (CLIENT, "vendors of the organization"),
    ("PATCH", "/api/v1/orgs/{org}/vendors/{vendor_id}"): (CLIENT, "vendors of the organization"),
    ("GET", "/api/v1/orgs/{org}/vendors/{vendor_id}/capabilities"): (
        CLIENT,
        "vendors of the organization",
    ),
    ("POST", "/api/v1/orgs/{org}/vendors/{vendor_id}/status"): (
        CLIENT,
        "vendors of the organization",
    ),
    ("GET", "/api/v1/orgs/{org}/rate-cards"): (CLIENT, "pricing:read at the organization"),
    ("POST", "/api/v1/orgs/{org}/rate-cards"): (CLIENT, "pricing:manage at the organization"),
    ("GET", "/api/v1/orgs/{org}/rate-cards/resolve"): (CLIENT, "pricing:read at the organization"),
    ("GET", "/api/v1/orgs/{org}/rate-cards/{card_id}/versions"): (
        CLIENT,
        "pricing:read at the organization",
    ),
    ("POST", "/api/v1/orgs/{org}/rate-cards/{card_id}/versions"): (
        CLIENT,
        "pricing:manage at the organization",
    ),
    # ------------------------------------------------------------------ operate: runs, approvals
    ("GET", "/api/v1/orgs/{org}/runs"): (SCOPED, "runs"),
    ("POST", "/api/v1/orgs/{org}/runs"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/runs/{run_id}"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/runs/{run_id}/artifacts/{artifact_id}"): (SCOPED, "runs"),
    ("POST", "/api/v1/orgs/{org}/runs/{run_id}/cancel"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/runs/{run_id}/events"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/runs/{run_id}/nodes"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/runs/{run_id}/result"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/runs/{run_id}/steps"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/approvals"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/approvals/{approval_id}"): (SCOPED, "runs"),
    ("POST", "/api/v1/orgs/{org}/approvals/{approval_id}/decide"): (SCOPED, "runs"),
    # ------------------------------------------------------------------ govern: usage, budgets
    ("GET", "/api/v1/orgs/{org}/usage/events"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/usage/export"): (
        CLIENT,
        "needs data:export at the organization; rows are also limited to usage:read scopes",
    ),
    ("POST", "/api/v1/orgs/{org}/usage/rollups/rebuild"): (
        CLIENT,
        "usage:manage at the organization",
    ),
    ("GET", "/api/v1/orgs/{org}/usage/runs/{run_id}"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/usage/summary"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/observability/performance"): (SCOPED, "runs"),
    ("GET", "/api/v1/orgs/{org}/budgets"): (SCOPED, "governance"),
    ("POST", "/api/v1/orgs/{org}/budgets"): (SCOPED, "governance"),
    ("GET", "/api/v1/orgs/{org}/budgets/{budget_id}"): (SCOPED, "governance"),
    ("PATCH", "/api/v1/orgs/{org}/budgets/{budget_id}"): (SCOPED, "governance"),
    ("GET", "/api/v1/orgs/{org}/budgets/{budget_id}/status"): (SCOPED, "governance"),
    ("GET", "/api/v1/orgs/{org}/quotas"): (SCOPED, "governance"),
    ("POST", "/api/v1/orgs/{org}/quotas"): (SCOPED, "governance"),
    ("GET", "/api/v1/orgs/{org}/quotas/{quota_id}"): (SCOPED, "governance"),
    ("PATCH", "/api/v1/orgs/{org}/quotas/{quota_id}"): (SCOPED, "governance"),
    ("GET", "/api/v1/orgs/{org}/governance/policies"): (SCOPED, "governance"),
    ("POST", "/api/v1/orgs/{org}/governance/policies"): (SCOPED, "governance"),
    ("GET", "/api/v1/orgs/{org}/governance/policies/{policy_id}"): (SCOPED, "governance"),
    ("PATCH", "/api/v1/orgs/{org}/governance/policies/{policy_id}"): (SCOPED, "governance"),
    ("GET", "/api/v1/orgs/{org}/governance/decisions"): (
        CLIENT,
        "governance:read at the organization: decisions are listed client-wide",
    ),
    ("GET", "/api/v1/orgs/{org}/governance/overview"): (
        CLIENT,
        "governance:read at the organization",
    ),
    ("GET", "/api/v1/orgs/{org}/governance/retention"): (
        CLIENT,
        "retention is set for the whole organization",
    ),
    ("PUT", "/api/v1/orgs/{org}/governance/retention"): (
        CLIENT,
        "retention is set for the whole organization",
    ),
    ("POST", "/api/v1/orgs/{org}/governance/retention/preview"): (
        CLIENT,
        "retention is set for the whole organization",
    ),
}


# ====================================================================== cases
READ, LIST, WRITE, CREATE = "read", "list", "write", "create"
REFUSED = frozenset({403, 404})
NEVER_ALLOWED = frozenset({401, 403, 404})
ORG = "/api/v1/orgs/{org}"
U_EMAIL = "w1-only@demo-a.local"
PASSWORD = "a-long-password-1"


@dataclass(frozen=True)
class Req:
    method: str
    url: str
    body: Any = None  # Any: a JSON body
    params: dict[str, str] | None = None


@dataclass(frozen=True)
class Case:
    """One scoped operation sent by U against W2's resource (refused) and W1's (allowed).

    `ids`: for a LIST, the id of each side's resource (W1's must be listed, W2's must not).
    `state`: for a WRITE/CREATE, a snapshot of W2's rows, equal before and after U's W2 request.
    `also`: statuses accepted on the W1 side besides 2xx: a request valid for authorization but
    refused on state (409/422) or a feature the deployment does not offer (501)."""

    kind: str
    w1: Req
    w2: Req
    ids: tuple[str, str] | None = None
    state: Callable[[], list[tuple[str, ...]]] | None = None
    also: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        assert not self.also & NEVER_ALLOWED
        assert (self.kind == LIST) == (self.ids is not None)
        assert (self.kind in (WRITE, CREATE)) == (self.state is not None)


@dataclass
class W:
    """The world: demo-a with workspaces w1/w2 (projects p1/p2), the client administrator and U,
    bound only at workspace W1. `ids[key][n]` is the id of side n's resource `key`."""

    owner: Session
    admin: Hdr
    user: Hdr
    user_id: str
    ids: dict[str, dict[int, Any]] = field(default_factory=dict)  # Any: ids and JSON values

    def __call__(self, key: str, n: int) -> Any:  # Any: an id or a JSON value
        return self.ids[key][n]

    def set(self, key: str, n: int, value: Any) -> None:  # Any: an id or a JSON value
        self.ids.setdefault(key, {})[n] = value

    def scope(self, n: int, kind: str = "workspace") -> dict[str, str]:
        return {"type": kind, "id": self("ws" if kind == "workspace" else "proj", n)}

    def rows(self, *sqls: str, **params: Any) -> list[tuple[str, ...]]:  # Any: bound values
        out: list[tuple[str, ...]] = []
        for sql in sqls:
            out += [
                tuple(str(v) for v in r)
                for r in self.owner.execute(
                    text(sql), {"w": self("ws", 2), "p": self("proj", 2), **params}
                ).all()
            ]
        self.owner.rollback()
        return out

    def snap(self, *sqls: str, **params: Any) -> Callable[[], list[tuple[str, ...]]]:  # Any
        """W2's rows as the schema owner (`:w` W2's workspace id, `:p` its project id)."""
        return lambda: self.rows(*sqls, **params)

    def post(self, path: str, body: Any, status: int = 201) -> Any:  # Any: JSON bodies
        return post(self.admin.client, f"{A}{path}", self.admin, body, status)


Builder = Callable[[W], Case]
CASES: dict[Op, tuple[str, Builder]] = {}


def _register(family: str, cases: dict[Op, Builder]) -> None:
    for op, build in cases.items():
        assert op not in CASES, op
        CASES[op] = (family, build)


def _url(path: str) -> str:
    return A + path


def _case(
    kind: str,
    method: str,
    url: Callable[[int], str],
    *,
    body: Callable[[int], Any] | None = None,  # Any: JSON bodies
    params: Callable[[int], dict[str, str]] | None = None,
    ids: Callable[[int], str] | None = None,
    state: Callable[[], list[tuple[str, ...]]] | None = None,
    also: tuple[int, ...] = (),
) -> Case:
    def req(n: int) -> Req:
        return Req(method, _url(url(n)), body(n) if body else None, params(n) if params else None)

    return Case(
        kind,
        req(1),
        req(2),
        (str(ids(1)), str(ids(2))) if ids else None,
        state,
        frozenset(also),
    )


# ---------------------------------------------------------------------- snapshots of W2
ART = (
    "SELECT * FROM shifde.artifacts WHERE workspace_id = :w ORDER BY id",
    "SELECT v.* FROM shifde.artifact_versions v JOIN shifde.artifacts a ON a.id = v.artifact_id "
    "WHERE a.workspace_id = :w ORDER BY v.id",
    "SELECT r.* FROM shifde.publish_requests r JOIN shifde.artifacts a ON a.id = r.artifact_id "
    "WHERE a.workspace_id = :w ORDER BY r.id",
    "SELECT m.* FROM shifde.ontology_source_mappings m JOIN shifde.artifact_versions v "
    "ON v.id = m.ontology_version_id JOIN shifde.artifacts a ON a.id = v.artifact_id "
    "WHERE a.workspace_id = :w ORDER BY m.id",
    "SELECT o.* FROM shifde.ontology_access_policies o JOIN shifde.artifacts a "
    "ON a.id = o.ontology_id WHERE a.workspace_id = :w ORDER BY o.id",
    "SELECT count(*) FROM shifde.runs WHERE workspace_id = :w",
)
RUNS = (
    "SELECT * FROM shifde.runs WHERE workspace_id = :w ORDER BY id",
    "SELECT * FROM shifde.approvals WHERE workspace_id = :w ORDER BY id",
    "SELECT count(*) FROM shifde.usage_events WHERE workspace_id = :w",
)
CONNS = (
    "SELECT * FROM shifde.connections WHERE workspace_id = :w ORDER BY id",
    "SELECT d.id FROM shifde.connection_discoveries d JOIN shifde.connections c "
    "ON c.id = d.connection_id WHERE c.workspace_id = :w ORDER BY d.id",
    "SELECT count(*) FROM shifde.jobs",
)


# ---------------------------------------------------------------------- tenancy
_register(
    "tenancy",
    {
        ("GET", ORG + "/workspaces"): lambda w: _case(
            LIST, "GET", lambda n: "/workspaces", ids=lambda n: w("ws", n)
        ),
        ("GET", ORG + "/workspaces/{ws}"): lambda w: _case(
            READ, "GET", lambda n: f"/workspaces/w{n}"
        ),
        ("GET", ORG + "/workspaces/{ws}/projects"): lambda w: _case(
            LIST, "GET", lambda n: f"/workspaces/w{n}/projects", ids=lambda n: w("proj", n)
        ),
        ("GET", ORG + "/workspaces/{ws}/projects/{proj}"): lambda w: _case(
            READ, "GET", lambda n: f"/workspaces/w{n}/projects/p{n}"
        ),
        ("GET", ORG + "/dashboard"): lambda w: _case(
            READ, "GET", lambda n: "/dashboard", params=lambda n: {"workspace": f"w{n}"}
        ),
        ("GET", ORG + "/me/permissions"): lambda w: _case(
            READ,
            "GET",
            lambda n: "/me/permissions",
            params=lambda n: {"workspace": f"w{n}", "project": f"p{n}"},
        ),
        ("GET", ORG + "/role-bindings"): lambda w: _case(
            LIST,
            "GET",
            lambda n: "/role-bindings",
            params=lambda n: {"scope_type": "workspace", "scope_id": w("ws", n)},
            ids=lambda n: w("binding", n),
        ),
        ("GET", ORG + "/audit-events"): lambda w: _case(
            LIST,
            "GET",
            lambda n: "/audit-events",
            params=lambda n: {"action": "tenancy.workspace.created"},
            ids=lambda n: w("event", n),
        ),
        ("POST", ORG + "/workspaces/{ws}/projects"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: f"/workspaces/w{n}/projects",
            body=lambda n: {"slug": "extra", "name": "Extra"},
            state=w.snap("SELECT id FROM shifde.projects WHERE workspace_id = :w ORDER BY id"),
        ),
        ("POST", ORG + "/role-bindings"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/role-bindings",
            body=lambda n: {
                "subject_type": "user",
                "subject_id": w("viewer", 1),
                "role_id": w("reader", 1),
                "scope": w.scope(n, "project"),
            },
            state=w.snap(BINDINGS),
        ),
        ("POST", ORG + "/secrets"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/secrets",
            body=lambda n: {"name": "extra", "value": "not-a-secret", "scope": w.scope(n)},
            state=w.snap("SELECT * FROM shifde.secrets WHERE scope_id IN (:w, :p) ORDER BY id"),
        ),
        ("DELETE", ORG + "/role-bindings/{binding_id}"): lambda w: _case(
            WRITE,
            "DELETE",
            lambda n: f"/role-bindings/{w('binding', n)}",
            state=w.snap(BINDINGS),
        ),
        ("PATCH", ORG + "/workspaces/{ws}/projects/{proj}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: f"/workspaces/w{n}/projects/p{n}",
            body=lambda n: {"description": "changed", "row_version": w("proj_rv", n)},
            state=w.snap("SELECT * FROM shifde.projects WHERE id = :p"),
        ),
        ("PATCH", ORG + "/workspaces/{ws}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: f"/workspaces/w{n}",
            body=lambda n: {"description": "changed", "row_version": w("ws_rv", n)},
            state=w.snap("SELECT * FROM shifde.workspaces WHERE id = :w"),
        ),
    },
)
BINDINGS = "SELECT * FROM shifde.role_bindings WHERE scope_id IN (:w, :p) ORDER BY id"


# ---------------------------------------------------------------------- artifacts
def _artifact_cases(
    kind: str,
    middle: dict[Op, Builder] | None = None,
    end: dict[Op, Builder] | None = None,
    *,
    publish_also: tuple[int, ...] = (),
) -> dict[Op, Builder]:
    """The route set every artifact kind shares, in an order that keeps W1's later cases valid
    (reads, then edits of the draft, then create, publish, state and rollback). `middle` and `end`
    are the kind's own routes."""
    base, item = f"{ORG}/{kind}", f"{ORG}/{kind}/{{artifact_id}}"

    def at(w: W, n: int, tail: str = "") -> str:
        return f"/{kind}/{w(kind, n)}{tail}"

    cases: dict[Op, Builder] = {
        ("GET", base): lambda w: _case(
            LIST,
            "GET",
            lambda n: f"/{kind}",
            params=lambda n: {
                "scope_type": w(f"{kind}.scope", n)["type"],
                "scope_id": w(f"{kind}.scope", n)["id"],
            },
            ids=lambda n: w(kind, n),
        ),
        ("GET", item): lambda w: _case(READ, "GET", lambda n: at(w, n)),
        ("GET", item + "/versions"): lambda w: _case(READ, "GET", lambda n: at(w, n, "/versions")),
        ("GET", item + "/versions/{version_id}"): lambda w: _case(
            READ, "GET", lambda n: at(w, n, f"/versions/{w(f'{kind}.v1', n)}")
        ),
        ("GET", item + "/draft"): lambda w: _case(READ, "GET", lambda n: at(w, n, "/draft")),
        ("GET", item + "/diff"): lambda w: _case(
            READ,
            "GET",
            lambda n: at(w, n, "/diff"),
            params=lambda n: {"from": w(f"{kind}.v1", n), "to": "draft"},
        ),
        ("PUT", item + "/draft"): lambda w: _case(
            WRITE,
            "PUT",
            lambda n: at(w, n, "/draft"),
            body=lambda n: {"spec": w(f"{kind}.spec", n), "row_version": w(f"{kind}.rv", n)},
            state=w.snap(*ART),
        ),
        ("POST", item + "/draft/validate"): lambda w: _case(
            WRITE, "POST", lambda n: at(w, n, "/draft/validate"), state=w.snap(*ART)
        ),
        ("PATCH", item): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: at(w, n),
            body=lambda n: {"description": "changed"},
            state=w.snap(*ART),
        ),
        **(middle or {}),
        ("POST", base): lambda w: _case(
            CREATE,
            "POST",
            lambda n: f"/{kind}",
            body=lambda n: {
                "key": "extra",
                "name": "Extra",
                "scope": w(f"{kind}.scope", n),
                "spec": w(f"{kind}.spec", n),
            },
            state=w.snap(*ART),
        ),
        ("POST", item + "/publish"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: at(w, n, "/publish"),
            body=lambda n: {"change_note": "next"},
            state=w.snap(*ART),
            also=publish_also,
        ),
        ("POST", item + "/versions/{version_id}/state"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: at(w, n, f"/versions/{w(f'{kind}.v1', n)}/state"),
            body=lambda n: {"state": "deprecated"},
            state=w.snap(*ART),
            also=(409,),  # publishing the next version may already have deprecated v1
        ),
        ("POST", item + "/rollback"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: at(w, n, "/rollback"),
            body=lambda n: {"version_id": w(f"{kind}.v1", n)},
            state=w.snap(*ART),
            also=(409, 422),  # a draft left by a refused publish blocks a rollback
        ),
        **(end or {}),
    }
    return cases


def _prompt_extras() -> dict[Op, Builder]:
    item = ORG + "/prompts/{artifact_id}"
    return {
        ("POST", item + "/render"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/prompts/{w('prompts', n)}/render",
            body=lambda n: {"version_id": w("prompts.v1", n), "variables": {"message": "hi"}},
            state=w.snap(*ART),
        ),
        ("POST", item + "/test"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/prompts/{w('prompts', n)}/test",
            body=lambda n: {
                "version_id": w("prompts.v1", n),
                "variables": {"message": "hi"},
                "model_id": w("model", 1),
            },
            state=w.snap(*ART),
        ),
        ("GET", item + "/test-runs"): lambda w: _case(
            READ, "GET", lambda n: f"/prompts/{w('prompts', n)}/test-runs"
        ),
    }


def _tool_extras() -> dict[Op, Builder]:
    return {
        ("POST", ORG + "/tools/{artifact_id}/test"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/tools/{w('tools', n)}/test",
            body=lambda n: {"version_id": w("tools.v1", n), "args": {"text": "hi"}},
            state=w.snap(*ART),
        ),
    }


def _agent_extras() -> tuple[dict[Op, Builder], dict[Op, Builder]]:
    item = ORG + "/agents/{artifact_id}"
    middle: dict[Op, Builder] = {
        ("GET", item + "/versions/{version_id}/resolved"): lambda w: _case(
            READ,
            "GET",
            lambda n: f"/agents/{w('agents', n)}/versions/{w('agents.v1', n)}/resolved",
        ),
        ("POST", item + "/test"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/agents/{w('agents', n)}/test",
            body=lambda n: {"version_id": w("agents.v1", n), "input": "hello"},
            state=w.snap(*ART),
        ),
    }
    end: dict[Op, Builder] = {
        ("POST", item + "/status"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/agents/{w('agents', n)}/status",
            body=lambda n: {"status": "disabled"},
            state=w.snap(*ART),
        ),
    }
    return middle, end


def _ontology_extras() -> tuple[dict[Op, Builder], dict[Op, Builder]]:
    item, oid = ORG + "/ontologies/{artifact_id}", ORG + "/ontologies/{ontology_id}"
    mapping = item + "/draft/mappings/{target_kind}/{target_key}"

    def at(w: W, n: int, tail: str) -> str:
        return f"/ontologies/{w('ontologies', n)}{tail}"

    def policy(w: W, n: int) -> dict[str, Any]:  # Any: JSON body
        return {
            "object_type_key": "thing",
            "subject": {"type": "user", "id": w.user_id},
            "row_version": w("policy_rv", n),
        }

    middle: dict[Op, Builder] = {
        ("GET", item + "/versions/{version_id}/mappings"): lambda w: _case(
            READ, "GET", lambda n: at(w, n, f"/versions/{w('ontologies.v1', n)}/mappings")
        ),
        ("PUT", mapping): lambda w: _case(
            WRITE,
            "PUT",
            lambda n: at(w, n, "/draft/mappings/object_type/thing"),
            body=lambda n: _mapping_body(w("conn", 1)),  # U may use W1's connection only
            state=w.snap(*ART),
        ),
        ("POST", mapping + "/check"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: at(w, n, "/draft/mappings/object_type/thing/check"),
            state=w.snap(*ART),
        ),
        ("DELETE", mapping): lambda w: _case(
            WRITE,
            "DELETE",
            lambda n: at(w, n, "/draft/mappings/object_type/thing"),
            state=w.snap(*ART),
        ),
        ("GET", oid + "/policies"): lambda w: _case(READ, "GET", lambda n: at(w, n, "/policies")),
        ("POST", oid + "/policies"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: at(w, n, "/policies"),
            body=lambda n: {
                "object_type_key": "thing",
                "subject": {"type": "role", "id": w("role", 1)},
            },
            state=w.snap(*ART),
        ),
        ("PATCH", oid + "/policies/{policy_id}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: at(w, n, f"/policies/{w('policy', n)}"),
            body=lambda n: {**policy(w, n), "property_keys": ["id"]},
            state=w.snap(*ART),
        ),
        ("DELETE", oid + "/policies/{policy_id}"): lambda w: _case(
            WRITE, "DELETE", lambda n: at(w, n, f"/policies/{w('policy', n)}"), state=w.snap(*ART)
        ),
        # v1 is published without frozen mappings, so a query reaches the PDP and then stops at
        # "mapping unavailable" (409); the refusal of W2 comes first
        ("POST", oid + "/query"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: at(w, n, "/query"),
            body=lambda n: {"object_type": "thing", "select": ["id"]},
            state=w.snap(*ART),
            also=(409,),
        ),
        ("GET", oid + "/objects/{object_type}/{pk}"): lambda w: _case(
            READ, "GET", lambda n: at(w, n, "/objects/thing/1"), also=(409,)
        ),
        ("POST", oid + "/actions/{action_key}/invoke"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: at(w, n, "/actions/act/invoke"),
            body=lambda n: {"object_pk": "1"},
            state=w.snap(*ART),
            also=(409,),
        ),
        ("PUT", oid + "/materializations/{object_type}"): lambda w: _case(
            WRITE,
            "PUT",
            lambda n: at(w, n, "/materializations/thing"),
            state=w.snap(*ART),
            also=(501,),  # materialization is not offered (the route answers 501 by design)
        ),
        ("GET", ORG + "/ontology-lineage"): lambda w: _case(
            READ,
            "GET",
            lambda n: "/ontology-lineage",
            params=lambda n: {"ontology_id": w("ontologies", n)},
        ),
    }
    return middle, {}


def _mapping_body(connection_id: str) -> dict[str, Any]:  # Any: JSON body
    return {
        "target_kind": "object_type",
        "target_key": "thing",
        "connection_id": connection_id,
        "relation": {"name": "things"},
        "column_map": {"id": "id"},
    }


_register("prompts", _artifact_cases("prompts", _prompt_extras()))
_register("skills", _artifact_cases("skills"))
_register("tools", _artifact_cases("tools", _tool_extras()))
_register("agents", _artifact_cases("agents", *_agent_extras()))
# specs of these kinds are not complete here: v1 is published by the schema owner and a publish
# through the API is refused on validation (422) after the PDP allowed it
_register("orchestrations", _artifact_cases("orchestrations", publish_also=(422,)))
_register("pipelines", _artifact_cases("pipelines", publish_also=(422,)))
_register("ontologies", _artifact_cases("ontologies", *_ontology_extras(), publish_also=(422,)))

_register(
    "publish-requests",
    {
        ("GET", ORG + "/publish-requests"): lambda w: _case(
            LIST, "GET", lambda n: "/publish-requests", ids=lambda n: w("request", n)
        ),
        ("POST", ORG + "/publish-requests/{request_id}/decide"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/publish-requests/{w('request', n)}/decide",
            body=lambda n: {"decision": "approve"},
            state=w.snap(*ART),
        ),
    },
)


# ---------------------------------------------------------------------- connections, delivery
def _conn(w: W, n: int, tail: str = "") -> str:
    return f"/connections/{w('conn', n)}{tail}"


_register(
    "connections",
    {
        ("GET", ORG + "/connections"): lambda w: _case(
            LIST, "GET", lambda n: "/connections", ids=lambda n: w("conn", n)
        ),
        ("GET", ORG + "/connections/{connection_id}"): lambda w: _case(
            READ, "GET", lambda n: _conn(w, n)
        ),
        ("GET", ORG + "/connections/{connection_id}/tests"): lambda w: _case(
            READ, "GET", lambda n: _conn(w, n, "/tests")
        ),
        ("GET", ORG + "/connections/{connection_id}/test-jobs/{job_id}"): lambda w: _case(
            READ, "GET", lambda n: _conn(w, n, f"/test-jobs/{w('job', n)}")
        ),
        ("GET", ORG + "/connections/{connection_id}/discoveries/latest"): lambda w: _case(
            READ, "GET", lambda n: _conn(w, n, "/discoveries/latest")
        ),
        ("POST", ORG + "/connections/{connection_id}/discover"): lambda w: _case(
            WRITE, "POST", lambda n: _conn(w, n, "/discover"), state=w.snap(*CONNS)
        ),
        ("POST", ORG + "/connections/{connection_id}/invoke"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: _conn(w, n, "/invoke"),
            body=lambda n: {"method": "GET", "path": "/health"},
            state=w.snap(*CONNS),
        ),
        ("POST", ORG + "/connections/{connection_id}/test"): lambda w: _case(
            WRITE, "POST", lambda n: _conn(w, n, "/test"), state=w.snap(*CONNS)
        ),
        ("PATCH", ORG + "/connections/{connection_id}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: _conn(w, n),
            body=lambda n: {"name": "renamed"},
            state=w.snap(*CONNS),
        ),
        ("POST", ORG + "/connections"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/connections",
            body=lambda n: _rest_connection(w("rest", 1), n, f"extra-{n}"),
            state=w.snap(*CONNS),
        ),
        ("POST", ORG + "/connections/{connection_id}/status"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: _conn(w, n, "/status"),
            body=lambda n: {"status": "disabled"},
            state=w.snap(*CONNS),
        ),
    },
)

TARGETS = "SELECT * FROM shifde.delivery_targets WHERE workspace_id = :w ORDER BY id"
PLANS = (
    "SELECT p.* FROM shifde.delivery_plans p JOIN shifde.delivery_targets t "
    "ON t.id = p.target_id WHERE t.workspace_id = :w ORDER BY p.id",
    "SELECT j.* FROM shifde.delivery_jobs j JOIN shifde.delivery_targets t "
    "ON t.id = j.target_id WHERE t.workspace_id = :w ORDER BY j.id",
    "SELECT a.* FROM shifde.delivery_approvals a JOIN shifde.delivery_plans p "
    "ON p.id = a.plan_id JOIN shifde.delivery_targets t ON t.id = p.target_id "
    "WHERE t.workspace_id = :w ORDER BY a.id",
    "SELECT count(*) FROM shifde.delivery_artifacts",
)
DEPLOYMENTS = (
    "SELECT d.* FROM shifde.delivery_deployments d JOIN shifde.delivery_targets t "
    "ON t.id = d.target_id WHERE t.workspace_id = :w ORDER BY d.id",
    "SELECT j.* FROM shifde.delivery_jobs j JOIN shifde.delivery_targets t "
    "ON t.id = j.target_id WHERE t.workspace_id = :w ORDER BY j.id",
)
_register(
    "delivery",
    {
        ("GET", ORG + "/delivery/targets"): lambda w: _case(
            LIST, "GET", lambda n: "/delivery/targets", ids=lambda n: w("target", n)
        ),
        ("GET", ORG + "/delivery/targets/{target_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/delivery/targets/{w('target', n)}"
        ),
        ("POST", ORG + "/delivery/targets"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/delivery/targets",
            body=lambda n: _target_body(n),
            state=w.snap(TARGETS),
        ),
        # plans before the archive case, so W1's request finds an active target; a plan the
        # administrator requested is not sealed: U's decision on W1 is 409 `plan_not_sealed`
        ("GET", ORG + "/delivery/plans"): lambda w: _case(
            LIST, "GET", lambda n: "/delivery/plans", ids=lambda n: w("plan", n)
        ),
        ("GET", ORG + "/delivery/plans/{plan_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/delivery/plans/{w('plan', n)}"
        ),
        ("POST", ORG + "/delivery/plans"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/delivery/plans",
            body=lambda n: _plan_body(w("target", n), w("release", n)),
            state=w.snap(*PLANS),
        ),
        ("POST", ORG + "/delivery/plans/{plan_id}/approve"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/delivery/plans/{w('plan', n)}/approve",
            body=lambda n: {"plan_digest": "a" * 64},
            state=w.snap(*PLANS),
            also=(409,),
        ),
        ("POST", ORG + "/delivery/plans/{plan_id}/reject"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/delivery/plans/{w('plan', n)}/reject",
            body=lambda n: {},
            state=w.snap(*PLANS),
            also=(409,),
        ),
        # deployments inserted as the owner, `queued` with no apply job: U's cancel on W1 is 409
        # `deployment_not_cancellable`, reconcile and mark-failed 409 `deployment_not_unknown`
        ("GET", ORG + "/delivery/deployments"): lambda w: _case(
            LIST, "GET", lambda n: "/delivery/deployments", ids=lambda n: w("deployment", n)
        ),
        ("GET", ORG + "/delivery/deployments/{deployment_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/delivery/deployments/{w('deployment', n)}"
        ),
        ("POST", ORG + "/delivery/deployments/{deployment_id}/cancel"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/delivery/deployments/{w('deployment', n)}/cancel",
            state=w.snap(*DEPLOYMENTS),
            also=(409,),
        ),
        ("POST", ORG + "/delivery/deployments/{deployment_id}/reconcile"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/delivery/deployments/{w('deployment', n)}/reconcile",
            state=w.snap(*DEPLOYMENTS),
            also=(409,),
        ),
        ("POST", ORG + "/delivery/deployments/{deployment_id}/mark-failed"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/delivery/deployments/{w('deployment', n)}/mark-failed",
            body=lambda n: {"note": "resolved by hand"},
            state=w.snap(*DEPLOYMENTS),
            also=(409,),
        ),
        ("POST", ORG + "/delivery/targets/{target_id}/archive"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/delivery/targets/{w('target', n)}/archive",
            state=w.snap(TARGETS),
        ),
    },
)


# ---------------------------------------------------------------------- packaging
PACKAGING = (
    "SELECT * FROM shifde.solutions WHERE workspace_id = :w ORDER BY id",
    "SELECT v.* FROM shifde.solution_versions v JOIN shifde.solutions s "
    "ON s.id = v.solution_id WHERE s.workspace_id = :w ORDER BY v.id",
    "SELECT r.* FROM shifde.releases r JOIN shifde.solution_versions v "
    "ON v.id = r.solution_version_id JOIN shifde.solutions s ON s.id = v.solution_id "
    "WHERE s.workspace_id = :w ORDER BY r.id",
)


def _sol(w: W, n: int, tail: str = "") -> str:
    return f"/workspaces/w{n}/projects/p{n}/solutions/{w('solution', n)}{tail}"


def _solution_version_body(w: W, n: int) -> dict[str, Any]:  # Any: JSON body
    return {"entrypoint_kind": "agent", "entrypoint_version_id": w("agents.v1", n)}


def _sealable_body(w: W, n: int) -> dict[str, Any]:  # Any: JSON body
    """A version body with an evaluation contract naming its project's dataset, so it passes the
    compatibility check."""
    return {
        **_solution_version_body(w, n),
        "evaluation": {
            "dataset_id": w("dataset", n),
            "min_accuracy": "0.9",
            "max_p95_latency_ms": 2000,
            "max_total_cost_usd": "1",
        },
    }


def _seal_body(w: W) -> dict[str, Any]:  # Any: JSON body
    return {
        "environment_bindings": {
            "model_bindings": {w("model", 0): {"provider_id": w("provider", 0), "model": "chat"}},
            "connection_bindings": {},
        }
    }


# the archive case last, so W1's version create and seal still find an active solution; the
# withdraw case after every other release case (W1's seal of the same bindings returns its
# existing release either way)
_register(
    "packaging",
    {
        ("GET", SOLUTIONS): lambda w: _case(
            LIST,
            "GET",
            lambda n: f"/workspaces/w{n}/projects/p{n}/solutions",
            ids=lambda n: w("solution", n),
        ),
        ("GET", SOLUTIONS + "/{solution_id}"): lambda w: _case(READ, "GET", lambda n: _sol(w, n)),
        ("GET", SOLUTIONS + "/{solution_id}/versions"): lambda w: _case(
            READ, "GET", lambda n: _sol(w, n, "/versions")
        ),
        ("GET", SOLUTIONS + "/{solution_id}/versions/{version_id}"): lambda w: _case(
            READ, "GET", lambda n: _sol(w, n, f"/versions/{w('solution_version', n)}")
        ),
        ("POST", SOLUTIONS): lambda w: _case(
            CREATE,
            "POST",
            lambda n: f"/workspaces/w{n}/projects/p{n}/solutions",
            body=lambda n: {"name": "extra", "target_kind": "databricks.app/v1"},
            state=w.snap(*PACKAGING),
        ),
        ("POST", SOLUTIONS + "/{solution_id}/versions"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: _sol(w, n, "/versions"),
            body=lambda n: _solution_version_body(w, n),
            state=w.snap(*PACKAGING),
        ),
        ("GET", SOLUTIONS + "/{solution_id}/versions/{version_id}/releases"): lambda w: _case(
            LIST,
            "GET",
            lambda n: _sol(w, n, f"/versions/{w('sealable', n)}/releases"),
            ids=lambda n: w("release", n),
        ),
        ("GET", "/api/v1/orgs/{org}/releases/{release_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/releases/{w('release', n)}"
        ),
        ("POST", SOLUTIONS + "/{solution_id}/versions/{version_id}/releases"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: _sol(w, n, f"/versions/{w('sealable', n)}/releases"),
            body=lambda n: _seal_body(w),
            state=w.snap(*PACKAGING),
        ),
        ("POST", "/api/v1/orgs/{org}/releases/{release_id}/withdraw"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/releases/{w('release', n)}/withdraw",
            body=lambda n: {"note": "scoping"},
            state=w.snap(*PACKAGING),
        ),
        ("POST", SOLUTIONS + "/{solution_id}/archive"): lambda w: _case(
            WRITE, "POST", lambda n: _sol(w, n, "/archive"), state=w.snap(*PACKAGING)
        ),
    },
)


# ---------------------------------------------------------------------- evaluation
EVALUATION = (
    "SELECT * FROM shifde.evaluation_datasets WHERE workspace_id = :w ORDER BY id",
    "SELECT e.* FROM shifde.evaluation_runs e JOIN shifde.releases r ON r.id = e.release_id "
    "JOIN shifde.solution_versions v ON v.id = r.solution_version_id "
    "JOIN shifde.solutions s ON s.id = v.solution_id WHERE s.workspace_id = :w ORDER BY e.id",
    "SELECT count(*) FROM shifde.runs WHERE workspace_id = :w",
)


def _datasets(n: int, tail: str = "") -> str:
    return f"/workspaces/w{n}/projects/p{n}/evaluation-datasets{tail}"


def _dataset_body(name: str) -> dict[str, Any]:  # Any: JSON body
    return {
        "name": name,
        "cases": [
            {
                "case_id": "c1",
                "input": {"message": "hello"},
                "assertions": [{"id": "a1", "kind": "contains", "value": "hello"}],
            }
        ],
    }


_register(
    "evaluation",
    {
        ("GET", DATASETS): lambda w: _case(
            LIST, "GET", lambda n: _datasets(n), ids=lambda n: w("dataset", n)
        ),
        ("GET", DATASETS + "/{dataset_id}"): lambda w: _case(
            READ, "GET", lambda n: _datasets(n, f"/{w('dataset', n)}")
        ),
        ("GET", EVALUATIONS): lambda w: _case(
            LIST,
            "GET",
            lambda n: f"/releases/{w('release', n)}/evaluations",
            ids=lambda n: w("evaluation_run", n),
        ),
        ("GET", "/api/v1/orgs/{org}/evaluation-runs/{run_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/evaluation-runs/{w('evaluation_run', n)}"
        ),
        ("POST", DATASETS): lambda w: _case(
            CREATE,
            "POST",
            lambda n: _datasets(n),
            body=lambda n: _dataset_body("extra"),
            state=w.snap(*EVALUATION),
        ),
        ("POST", EVALUATIONS): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/releases/{w('release', n)}/evaluations",
            body=lambda n: {},
            state=w.snap(*EVALUATION),
        ),
    },
)


def _rest_connection(base_url: str, n: int, name: str) -> dict[str, Any]:  # Any: JSON body
    return {
        "name": name,
        "connection_type": "rest",
        "adapter_key": "rest.http",
        "scope": {"workspace": f"w{n}", "project": f"p{n}"},
        "auth_method": "none",
        "access_mode": "read",
        "config": {"base_url": base_url, "health_path": "/health"},
    }


def _target_body(n: int) -> dict[str, Any]:  # Any: JSON body
    return {
        "workspace": f"w{n}",
        "project": f"p{n}",
        "name": f"target-{uuid7().hex[-8:]}",
        "address": f"https://adb-{uuid7().hex[-16:]}.cloud.databricks.com",
        "environment": "dev",
        "bindings": {"warehouse_id": "abc123def456"},
        "credential_ref": "databricks-dev",
    }


def _plan_body(target_id: str, release_id: str) -> dict[str, str]:
    return {
        "target_id": target_id,
        "release_id": release_id,
        "application_key": "app",
        "display_name": "App",
    }


def _passed_evaluation(w: W, owner: Session, n: int) -> None:
    """A passed evaluation run of side `n`'s release on its manifest's digests, as the owner."""
    owner.execute(
        text(
            "INSERT INTO shifde.evaluation_runs (tenant_id, id, release_id, release_digest, "
            "dataset_id, dataset_digest, thresholds, status, requested_by) SELECT r.tenant_id, "
            ":i, r.id, r.digest, CAST(r.manifest->'evaluation'->>'dataset_id' AS uuid), "
            "r.manifest->'evaluation'->>'dataset_digest', '{}'::jsonb, 'passed', r.sealed_by "
            "FROM shifde.releases r WHERE r.id = :r"
        ),
        {"i": uuid7(), "r": w("release", n)},
    )
    owner.commit()


# ---------------------------------------------------------------------- models
ALLOWLISTS = (
    "SELECT * FROM shifde.model_allowlists WHERE scope_id = :w ORDER BY id",
    "SELECT m.* FROM shifde.model_allowlist_models m JOIN shifde.model_allowlists a "
    "ON a.id = m.allowlist_id WHERE a.scope_id = :w ORDER BY 1, 2",
)
MODEL_ROUTES = "SELECT * FROM shifde.model_routes WHERE scope_id = :w ORDER BY id"
_register(
    "models",
    {
        ("GET", ORG + "/model-allowlists"): lambda w: _case(
            READ,
            "GET",
            lambda n: "/model-allowlists",
            params=lambda n: {"scope_type": "workspace", "scope_id": w("ws", n)},
        ),
        ("GET", ORG + "/models/effective"): lambda w: _case(
            READ, "GET", lambda n: "/models/effective", params=lambda n: {"workspace": f"w{n}"}
        ),
        ("POST", ORG + "/models/{model_id}/invoke"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/models/{w('model', 1)}/invoke",
            params=lambda n: {"workspace": f"w{n}", "project": f"p{n}"},
            body=lambda n: invoke_body(),
            state=w.snap("SELECT count(*) FROM shifde.usage_events WHERE workspace_id = :w"),
        ),
        ("POST", ORG + "/model-routes"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/model-routes",
            body=lambda n: {
                "name": f"extra-{n}",
                "scope": w.scope(n),
                "primary_model_id": w("model", 1),
            },
            state=w.snap(MODEL_ROUTES),
        ),
        ("PATCH", ORG + "/model-routes/{route_id}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: f"/model-routes/{w('route', n)}",
            body=lambda n: {"name": f"renamed-{n}"},
            state=w.snap(MODEL_ROUTES),
        ),
        ("PUT", ORG + "/model-allowlists"): lambda w: _case(
            WRITE,
            "PUT",
            lambda n: "/model-allowlists",
            params=lambda n: {"scope_type": "workspace", "scope_id": w("ws", n)},
            body=lambda n: {"enabled": False, "model_ids": [w("model", 1)]},
            state=w.snap(*ALLOWLISTS),
        ),
    },
)


# ---------------------------------------------------------------------- runs, approvals, usage
def _run(w: W, n: int, tail: str = "") -> str:
    return f"/runs/{w('run', n)}{tail}"


_register(
    "runs",
    {
        ("GET", ORG + "/runs"): lambda w: _case(
            LIST, "GET", lambda n: "/runs", ids=lambda n: w("run", n)
        ),
        ("GET", ORG + "/runs/{run_id}"): lambda w: _case(READ, "GET", lambda n: _run(w, n)),
        ("GET", ORG + "/runs/{run_id}/steps"): lambda w: _case(
            READ, "GET", lambda n: _run(w, n, "/steps")
        ),
        ("GET", ORG + "/runs/{run_id}/events"): lambda w: _case(
            READ, "GET", lambda n: _run(w, n, "/events")
        ),
        ("GET", ORG + "/runs/{run_id}/result"): lambda w: _case(
            READ, "GET", lambda n: _run(w, n, "/result")
        ),
        ("GET", ORG + "/runs/{run_id}/artifacts/{artifact_id}"): lambda w: _case(
            READ, "GET", lambda n: _run(w, n, f"/artifacts/{w('run_artifact', n)}")
        ),
        ("GET", ORG + "/runs/{run_id}/nodes"): lambda w: _case(
            READ, "GET", lambda n: f"/runs/{w('pipeline_run', n)}/nodes"
        ),
        ("GET", ORG + "/pipelines/{pipeline_id}/runs"): lambda w: _case(
            READ, "GET", lambda n: f"/pipelines/{w('pipeline', n)}/runs"
        ),
        ("GET", ORG + "/pipelines/{pipeline_id}/schedules"): lambda w: _case(
            READ, "GET", lambda n: f"/pipelines/{w('pipeline', n)}/schedules"
        ),
        ("POST", ORG + "/pipelines/{pipeline_id}/schedules"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: f"/pipelines/{w('pipeline', n)}/schedules",
            body=lambda n: {"cron": "0 3 * * *", "run_as_service_account_id": w("sa", 1)},
            state=w.snap(
                "SELECT * FROM shifde.pipeline_schedules WHERE pipeline_id = :pl ORDER BY id",
                pl=w("pipeline", 2),
            ),
        ),
        ("GET", ORG + "/approvals"): lambda w: _case(
            LIST, "GET", lambda n: "/approvals", ids=lambda n: w("approval", n)
        ),
        ("GET", ORG + "/approvals/{approval_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/approvals/{w('approval', n)}"
        ),
        ("GET", ORG + "/usage/events"): lambda w: _case(
            LIST, "GET", lambda n: "/usage/events", ids=lambda n: w("usage", n)
        ),
        ("GET", ORG + "/usage/summary"): lambda w: _case(
            LIST,
            "GET",
            lambda n: "/usage/summary",
            params=lambda n: {"group_by": "run"},
            ids=lambda n: w("run", n),
        ),
        ("GET", ORG + "/usage/runs/{run_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/usage/runs/{w('run', n)}"
        ),
        ("GET", ORG + "/observability/performance"): lambda w: _case(
            LIST,
            "GET",
            lambda n: "/observability/performance",
            params=lambda n: {"group_by": "agent"},
            ids=lambda n: w("agents", n),
        ),
        ("POST", ORG + "/approvals/{approval_id}/decide"): lambda w: _case(
            WRITE,
            "POST",
            lambda n: f"/approvals/{w('approval', n)}/decide",
            body=lambda n: {"decision": "approve"},
            state=w.snap(*RUNS),
            also=(409,),  # the seeded approval's run is not parked on it
        ),
        ("POST", ORG + "/runs"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/runs",
            body=lambda n: {
                "kind": "agent",
                "agent_id": w("agents", n),
                "workspace": f"w{n}",
                "project": f"p{n}",
                "input": "hello",
            },
            state=w.snap(*RUNS),
        ),
        ("POST", ORG + "/runs/{run_id}/cancel"): lambda w: _case(
            WRITE, "POST", lambda n: _run(w, n, "/cancel"), state=w.snap(*RUNS)
        ),
    },
)


# ---------------------------------------------------------------------- budgets, policies
BUDGETS = "SELECT * FROM shifde.budgets WHERE scope_id = :w ORDER BY id"
QUOTAS = "SELECT * FROM shifde.quotas WHERE scope_id = :w ORDER BY id"
POLICIES = "SELECT * FROM shifde.governance_policies WHERE scope_id = :w ORDER BY id"
RULE = {"when": {"side_effect_classes": ["destructive"]}}
_register(
    "governance",
    {
        ("GET", ORG + "/budgets"): lambda w: _case(
            LIST, "GET", lambda n: "/budgets", ids=lambda n: w("budget", n)
        ),
        ("GET", ORG + "/budgets/{budget_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/budgets/{w('budget', n)}"
        ),
        ("GET", ORG + "/budgets/{budget_id}/status"): lambda w: _case(
            READ, "GET", lambda n: f"/budgets/{w('budget', n)}/status"
        ),
        ("POST", ORG + "/budgets"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/budgets",
            body=lambda n: _budget(w, n, "monthly"),
            state=w.snap(BUDGETS),
        ),
        ("PATCH", ORG + "/budgets/{budget_id}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: f"/budgets/{w('budget', n)}",
            body=lambda n: {"name": "renamed"},
            state=w.snap(BUDGETS),
        ),
        ("GET", ORG + "/quotas"): lambda w: _case(
            LIST, "GET", lambda n: "/quotas", ids=lambda n: w("quota", n)
        ),
        ("GET", ORG + "/quotas/{quota_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/quotas/{w('quota', n)}"
        ),
        ("POST", ORG + "/quotas"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/quotas",
            body=lambda n: {"scope": w.scope(n), "metric": "tokens_per_day", "limit": 1000},
            state=w.snap(QUOTAS),
        ),
        ("PATCH", ORG + "/quotas/{quota_id}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: f"/quotas/{w('quota', n)}",
            body=lambda n: {"limit": 50},
            state=w.snap(QUOTAS),
        ),
        ("GET", ORG + "/governance/policies"): lambda w: _case(
            LIST, "GET", lambda n: "/governance/policies", ids=lambda n: w("gov_policy", n)
        ),
        ("GET", ORG + "/governance/policies/{policy_id}"): lambda w: _case(
            READ, "GET", lambda n: f"/governance/policies/{w('gov_policy', n)}"
        ),
        ("POST", ORG + "/governance/policies"): lambda w: _case(
            CREATE,
            "POST",
            lambda n: "/governance/policies",
            body=lambda n: {
                "kind": "approval_rule",
                "name": "extra",
                "scope": w.scope(n),
                "definition": RULE,
            },
            state=w.snap(POLICIES),
        ),
        ("PATCH", ORG + "/governance/policies/{policy_id}"): lambda w: _case(
            WRITE,
            "PATCH",
            lambda n: f"/governance/policies/{w('gov_policy', n)}",
            body=lambda n: {"name": "renamed"},
            state=w.snap(POLICIES),
        ),
    },
)


def _budget(w: W, n: int, period: str) -> dict[str, Any]:  # Any: JSON body
    return {"scope": w.scope(n), "period": period, "amount": 100000, "metric": "tokens"}


# ====================================================================== the world
def _world(client: TestClient, owner: Session, passwords: dict[str, str]) -> W:
    """W1 and W2 (each with a project) in demo-a, and U: an active member whose only role binding
    is one custom role holding every client permission, bound at workspace W1."""
    admin = user_session(client, passwords, ADMIN)
    w = W(owner=owner, admin=admin, user=admin, user_id="")
    for n in (1, 2):
        ws = w.post("/workspaces", {"slug": f"w{n}", "name": f"W{n}"})
        proj = w.post(f"/workspaces/w{n}/projects", {"slug": f"p{n}", "name": f"P{n}"})
        w.set("ws", n, ws["id"])
        w.set("ws_rv", n, ws["row_version"])
        w.set("proj", n, proj["id"])
        w.set("proj_rv", n, proj["row_version"])
    catalog = admin.client.get("/api/v1/permissions", headers=admin)
    assert catalog.status_code == 200, catalog.text
    perms = [p["key"] for p in catalog.json()["items"] if p["key"] not in PLATFORM_ONLY_ACTIONS]
    role = w.post("/roles", {"key": "w1-everything", "name": "W1 everything", "permissions": perms})
    w.set("role", 1, role["id"])
    login(client, passwords, ADMIN)  # `new_user` signs in as the platform administrator
    w.user_id = new_user(client, {}, "demo-a", U_EMAIL, PASSWORD)
    passwords[U_EMAIL] = PASSWORD
    w.post(
        "/role-bindings",
        {
            "subject_type": "user",
            "subject_id": w.user_id,
            "role_id": role["id"],
            "scope": w.scope(1),
        },
    )
    bindings = w.rows(
        "SELECT b.scope_type, b.scope_id::text, m.status FROM shifde.role_bindings b "
        "JOIN shifde.memberships m ON m.user_id = b.subject_id AND m.tenant_id = b.tenant_id "
        "WHERE b.subject_id = :u",
        u=w.user_id,
    )
    assert bindings == [("workspace", w("ws", 1), "active")], bindings
    w.user = user_session(client, passwords, U_EMAIL)
    return w


def _check(w: W, *families: str) -> None:
    """Every case of `families`, in table order; one assertion naming every failure."""
    failures: list[str] = []
    ran = 0
    for op, (family, build) in CASES.items():
        if family in families:
            failures += _run_case(w, op, build(w))
            ran += 1
    assert ran, families
    assert not failures, "\n".join(failures)


def _send(who: Hdr, req: Req) -> Response:
    sent: Response = who.client.request(
        req.method, req.url, json=req.body, params=req.params, headers=dict(who)
    )
    return sent


def _allowed(status: int, case: Case) -> bool:
    return status < 300 or status in case.also


def _run_case(w: W, op: Op, case: Case) -> list[str]:
    name = f"{op[0]} {op[1]}"
    out: list[str] = []
    if case.kind in (READ, LIST):  # W2's resource exists: the client administrator reads it
        got = _send(w.admin, case.w2)
        if not _allowed(got.status_code, case) or (case.ids and case.ids[1] not in got.text):
            out.append(f"{name}: W2 control as the administrator: {got.status_code} {got.text}")
    before = case.state() if case.state else None
    if case.kind == WRITE and not before:
        out.append(f"{name}: W2 has no rows to protect")
    r2 = _send(w.user, case.w2)
    if case.ids and r2.status_code < 300:
        if case.ids[1] in r2.text:
            out.append(f"{name}: W2 refused: its resource is listed")
    elif r2.status_code not in REFUSED:
        out.append(f"{name}: W2 refused: {r2.status_code} {r2.text[:300]}")
    if case.state and case.state() != before:
        out.append(f"{name}: W2 refused: its rows changed")
    r1 = _send(w.user, case.w1)
    if not _allowed(r1.status_code, case):
        out.append(f"{name}: W1 allowed: {r1.status_code} {r1.text[:300]}")
    elif case.ids and case.ids[0] not in r1.text:
        out.append(f"{name}: W1 allowed: its resource is not listed")
    return out


# ====================================================================== the table
def test_every_mounted_operation_is_classified(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    mounted = {(m.upper(), p) for p, ops in paths.items() for m in ops}
    missing = sorted(mounted - set(ROUTES))
    stale = sorted(set(ROUTES) - mounted)
    assert not missing and not stale, (
        f"unclassified operations: {missing}; classified but not mounted: {stale}"
    )


def test_every_entry_has_a_class_and_a_reason() -> None:
    bad = [op for op, (cls, note) in ROUTES.items() if cls not in CLASSES or not note.strip()]
    assert not bad, bad


def test_paths_naming_a_workspace_or_project_are_scoped() -> None:
    bad = [
        op
        for op, (cls, _) in ROUTES.items()
        if ("{ws}" in op[1] or "{proj}" in op[1]) and cls != SCOPED
    ]
    assert not bad, bad


def test_every_scoped_operation_has_exactly_one_case() -> None:
    scoped = {op for op, (cls, _) in ROUTES.items() if cls == SCOPED}
    assert set(CASES) == scoped, (sorted(scoped - set(CASES)), sorted(set(CASES) - scoped))
    wrong = [op for op, (family, _) in CASES.items() if ROUTES[op][1] != family]
    assert not wrong, wrong


# ====================================================================== the families
def test_workspaces_projects_bindings_audit_and_secrets(
    client: TestClient, world: World, owner_session: Session
) -> None:
    w = _world(client, owner_session, world.passwords)
    reader = w.post(
        "/roles", {"key": "ws-reader", "name": "WS reader", "permissions": ["workspace:read"]}
    )
    w.set("reader", 1, reader["id"])
    viewer = w.rows("SELECT id::text FROM shifde.users WHERE primary_email = :e", e=VIEWER)[0][0]
    w.set("viewer", 1, viewer)
    for n in (1, 2):
        bound = w.post(
            "/role-bindings",
            {
                "subject_type": "user",
                "subject_id": viewer,
                "role_id": reader["id"],
                "scope": w.scope(n),
            },
        )
        w.set("binding", n, bound["id"])
        event = w.rows(
            "SELECT id::text FROM shifde.audit_events "
            "WHERE action = 'tenancy.workspace.created' AND workspace_id = :x",
            x=w("ws", n),
        )
        w.set("event", n, event[0][0])
        w.post("/secrets", {"name": f"s{n}", "value": "not-a-secret", "scope": w.scope(n)})
    _check(w, "tenancy")


def _publish_by_owner(w: W, kind: str, n: int, artifact_id: str) -> str:
    """Mark the artifact's first version published (the schema owner): kinds whose full spec this
    test does not build still get a published version for the version routes."""
    version = w.owner.execute(
        text(
            "UPDATE shifde.artifact_versions SET state = 'published', spec_hash = 'h', "
            "published_at = now() WHERE artifact_id = :a RETURNING id"
        ),
        {"a": artifact_id},
    ).scalar_one()
    w.owner.execute(
        text("UPDATE shifde.artifacts SET latest_published_version_id = :v WHERE id = :a"),
        {"v": version, "a": artifact_id},
    )
    w.owner.commit()
    return str(version)


def _artifact(
    w: W, kind: str, n: int, spec: dict[str, Any], *, by_api: bool, project: bool = False
) -> None:
    """Side n's artifact of `kind` with a published v1 and a saved draft v2."""
    scope = w.scope(n, "project" if project else "workspace")
    made = art(w.admin.client, w.admin, kind, f"{kind}-{n}", spec, scope)
    if by_api:
        v1 = publish(w.admin.client, w.admin, kind, made["id"])["id"]
    else:
        v1 = _publish_by_owner(w, kind, n, made["id"])
    draft = edit(w.admin.client, w.admin, kind, made["id"], spec, 0)
    w.set(kind, n, made["id"])
    w.set(f"{kind}.v1", n, str(v1))
    w.set(f"{kind}.rv", n, draft["row_version"])
    w.set(f"{kind}.spec", n, spec)
    w.set(f"{kind}.scope", n, scope)


def test_prompts_skills_tools_and_agents(
    client: TestClient, world: World, owner_session: Session
) -> None:
    w = _world(client, owner_session, world.passwords)
    w.set("model", 1, fake_model_stack(w.admin.client, w.admin)["model"])
    for n in (1, 2):
        _artifact(w, "prompts", n, prompt_spec(), by_api=True)
        _artifact(w, "skills", n, skill_spec(), by_api=True)
        _artifact(w, "tools", n, builtin_tool_spec(), by_api=True)
        spec = agent_spec(f"latest:{w('prompts', n)}", w("model", 1))
        _artifact(w, "agents", n, spec, by_api=True)
    _check(w, "prompts", "skills", "tools", "agents")


def test_publish_requests(client: TestClient, world: World, owner_session: Session) -> None:
    w = _world(client, owner_session, world.passwords)
    set_tenant_setting(owner_session, "demo-a", "publish_requires_approval", True)
    for n in (1, 2):
        made = art(w.admin.client, w.admin, "prompts", f"asks-{n}", prompt_spec(), w.scope(n))
        asked = w.post(f"/prompts/{made['id']}/publish", {}, 202)
        w.set("request", n, asked["publish_request"]["id"])
    _check(w, "publish-requests")


def _ontology_spec(tool_version_id: str) -> dict[str, Any]:  # Any: spec JSON
    return {
        "object_types": [
            {
                "key": "thing",
                "name": "Thing",
                "primary_key": ["id"],
                "properties": [{"key": "id", "type": "string"}],
            }
        ],
        "relationships": [],
        "actions": [
            {
                "key": "act",
                "object_type": "thing",
                "tool_version_id": tool_version_id,
                "param_mapping": {},
            }
        ],
    }


def _rest(w: W, n: int, base_url: str, secret_id: str) -> str:
    """Side n's active REST connection (in project p{n}) to the local test server."""
    body = {
        **_rest_connection(base_url, n, f"rest-{n}"),
        "auth_method": "bearer",
        "secret_id": secret_id,
        "read_only_confirmed": True,
    }
    made = w.post("/connections", body)
    activate(w.admin.client, w.admin, made["id"])
    return str(made["id"])


def test_ontologies(
    client: TestClient, world: World, owner_session: Session, rest_server: RestTestServer
) -> None:
    w = _world(client, owner_session, world.passwords)
    token = secret(w.admin.client, w.admin, "rest-token", REST_TOKEN)
    for n in (1, 2):
        w.set("conn", n, _rest(w, n, rest_server.base_url, token))
        tool = art(w.admin.client, w.admin, "tools", "act", builtin_tool_spec(), w.scope(n))
        tool_v = publish(w.admin.client, w.admin, "tools", tool["id"])["id"]
        _artifact(w, "ontologies", n, _ontology_spec(tool_v), by_api=False, project=True)
        policy = w.post(
            f"/ontologies/{w('ontologies', n)}/policies",
            {"object_type_key": "thing", "subject": {"type": "user", "id": w.user_id}},
        )
        w.set("policy", n, policy["id"])
        w.set("policy_rv", n, policy["row_version"])
    mapped = w.admin.client.put(  # W2's draft mapping, so a refused write has a row to protect
        f"{A}/ontologies/{w('ontologies', 2)}/draft/mappings/object_type/thing",
        json=_mapping_body(w("conn", 2)),
        headers=w.admin,
    )
    assert mapped.status_code == 200, mapped.text
    _check(w, "ontologies")


def test_orchestrations_and_pipelines(
    client: TestClient, world: World, owner_session: Session
) -> None:
    w = _world(client, owner_session, world.passwords)
    for n in (1, 2):
        _artifact(w, "orchestrations", n, {"steps": []}, by_api=False, project=True)
        _artifact(w, "pipelines", n, {"nodes": [], "edges": []}, by_api=False, project=True)
    _check(w, "orchestrations", "pipelines")


def test_connections_and_delivery_targets(
    client: TestClient, world: World, owner_session: Session, rest_server: RestTestServer
) -> None:
    w = _world(client, owner_session, world.passwords)
    w.set("rest", 1, rest_server.base_url)
    token = secret(w.admin.client, w.admin, "rest-token", REST_TOKEN)
    _solutions_world(w)  # each side's sealed release, in the side's project
    for n in (1, 2):
        _passed_evaluation(w, owner_session, n)
        cid = _rest(w, n, rest_server.base_url, token)
        w.set("conn", n, cid)
        tested = run_test(w.admin.client, w.admin, A, cid)
        w.set("job", n, tested["id"])
        w.post(f"/connections/{cid}/discover", None, 200)
        w.set("target", n, w.post("/delivery/targets", _target_body(n))["id"])
        plan = w.post("/delivery/plans", _plan_body(w("target", n), w("release", n)))
        w.set("plan", n, plan["id"])
        w.set("deployment", n, uuid7())
        owner_session.execute(
            text(
                "INSERT INTO shifde.delivery_deployments (tenant_id, id, plan_id, target_id, "
                "application_key) SELECT tenant_id, :d, id, target_id, application_key "
                "FROM shifde.delivery_plans WHERE id = :p"
            ),
            {"d": w("deployment", n), "p": w("plan", n)},
        )
        owner_session.commit()
    _check(w, "connections", "delivery")


def _solutions_world(w: W) -> None:
    """Per side: a published agent, a dataset, a solution with a version and a sealable version,
    and its release."""
    models = fake_model_stack(w.admin.client, w.admin)
    model = models["model"]
    w.set("model", 0, model)
    w.set("provider", 0, models["provider"])
    for n in (1, 2):
        scope = w.scope(n, "project")
        prompt = art(w.admin.client, w.admin, "prompts", f"sol-prompt-{n}", prompt_spec(), scope)
        publish(w.admin.client, w.admin, "prompts", prompt["id"])
        spec = agent_spec(f"latest:{prompt['id']}", model)
        agent = art(w.admin.client, w.admin, "agents", f"sol-agent-{n}", spec, scope)
        w.set("agents.v1", n, publish(w.admin.client, w.admin, "agents", agent["id"])["id"])
        w.set("dataset", n, w.post(_datasets(n), _dataset_body(f"golden-{n}"))["id"])
        base = f"/workspaces/w{n}/projects/p{n}/solutions"
        made = w.post(base, {"name": f"solution-{n}", "target_kind": "databricks.app/v1"})
        w.set("solution", n, made["id"])
        version = w.post(f"{base}/{made['id']}/versions", _solution_version_body(w, n))
        w.set("solution_version", n, version["id"])
        # a sealable version and its release on each side
        sealable = w.post(f"{base}/{made['id']}/versions", _sealable_body(w, n))
        w.set("sealable", n, sealable["id"])
        release = w.post(f"{base}/{made['id']}/versions/{sealable['id']}/releases", _seal_body(w))
        w.set("release", n, release["id"])


def test_solutions_and_solution_versions(
    client: TestClient, world: World, owner_session: Session
) -> None:
    w = _world(client, owner_session, world.passwords)
    _solutions_world(w)
    # U's version on W1's solution naming W2's agent version: the 422 an unknown id gets
    url = _url(_sol(w, 1, "/versions"))
    answers = []
    for entry in (w("agents.v1", 2), str(uuid7())):
        body = {"entrypoint_kind": "agent", "entrypoint_version_id": entry}
        r = w.user.client.post(url, json=body, headers=dict(w.user))
        answers.append((r.status_code, {k: v for k, v in r.json().items() if k != "request_id"}))
    assert answers[0][0] == 422 and answers[0] == answers[1], answers
    # U's version on W1's solution naming W2's dataset: the 422 an unknown dataset id gets
    answers = []
    for dataset_id in (w("dataset", 2), str(uuid7())):
        body = {**_sealable_body(w, 1)}
        body["evaluation"] = {**body["evaluation"], "dataset_id": dataset_id}
        r = w.user.client.post(url, json=body, headers=dict(w.user))
        answers.append((r.status_code, {k: v for k, v in r.json().items() if k != "request_id"}))
    assert answers[0][0] == 422 and answers[0] == answers[1], answers
    _check(w, "packaging")


def test_evaluation_datasets_and_runs(
    client: TestClient, world: World, owner_session: Session
) -> None:
    w = _world(client, owner_session, world.passwords)
    _solutions_world(w)
    owner_session.execute(
        text(
            "UPDATE shifde.tenants SET settings = coalesce(settings, '{}'::jsonb) || "
            "'{\"capture_content\": \"full\"}'::jsonb WHERE slug = 'demo-a'"
        )
    )
    owner_session.commit()
    for n in (1, 2):
        w.set("evaluation_run", n, w.post(f"/releases/{w('release', n)}/evaluations", {})["id"])
    _check(w, "evaluation")


def test_models(client: TestClient, world: World, owner_session: Session) -> None:
    w = _world(client, owner_session, world.passwords)
    w.set("model", 1, fake_model_stack(w.admin.client, w.admin)["model"])
    for n in (1, 2):
        route = w.post(
            "/model-routes",
            {"name": f"route-{n}", "scope": w.scope(n), "primary_model_id": w("model", 1)},
        )
        w.set("route", n, route["id"])
    put = w.admin.client.put(
        f"{A}/model-allowlists",
        params={"scope_type": "workspace", "scope_id": w("ws", 2)},
        json={"enabled": False, "model_ids": []},
        headers=w.admin,
    )
    assert put.status_code == 200, put.text
    _check(w, "models")


def _seed_runs(w: W, n: int, tenant: Any) -> None:  # Any: the tenant id
    """Side n's rows that only a worker or a run in progress would write: an approval, an output
    artifact and a usage event of its run, a finished run of its agent and a pipeline run."""
    o, run = w.owner, w("run", n)
    w.set(
        "approval",
        n,
        str(
            o.execute(
                text(
                    "INSERT INTO shifde.approvals (id, tenant_id, run_id, workspace_id, "
                    "project_id, call_ref, reason, tool_key, tool_version_no, side_effect_class, "
                    "requested_by, expires_at) SELECT gen_random_uuid(), tenant_id, id, "
                    "workspace_id, project_id, '1:0', 'tool_side_effect', 'echo', 1, "
                    "'write_non_idempotent', initiated_by_principal_id, now() + interval '1 day' "
                    "FROM shifde.runs WHERE id = :r RETURNING id"
                ),
                {"r": run},
            ).scalar_one()
        ),
    )
    w.set(
        "run_artifact",
        n,
        str(
            o.execute(
                text(
                    "INSERT INTO shifde.run_artifacts (id, tenant_id, run_id, kind, size_bytes, "
                    "content, sha256) VALUES (gen_random_uuid(), :t, :r, 'output', 2, '{}', 'x') "
                    "RETURNING id"
                ),
                {"t": tenant, "r": run},
            ).scalar_one()
        ),
    )
    w.set(
        "usage",
        n,
        str(
            o.execute(
                text(
                    "INSERT INTO shifde.usage_events (id, tenant_id, occurred_at, event_key, "
                    "event_type, status, workspace_id, project_id, principal_id, run_id, "
                    "root_run_id, attempt, agent_id, model_id, provider_key, model_key, is_test, "
                    "input_tokens, output_tokens, call_count, duration_ms, cost_kind, "
                    "cost_amount, currency) SELECT gen_random_uuid(), tenant_id, now(), "
                    "'scoping-' || id, 'model_call', 'succeeded', workspace_id, project_id, "
                    "initiated_by_principal_id, id, id, 1, agent_id, :m, 'fake', 'fake-chat', "
                    "false, 10, 5, 1, 100, 'calculated', 0.02, 'XTS' FROM shifde.runs "
                    "WHERE id = :r RETURNING id"
                ),
                {"r": run, "m": w("model", 1)},
            ).scalar_one()
        ),
    )
    ws, proj = w("ws", n), w("proj", n)
    now = utcnow()
    add_run(
        o,
        tenant,
        workspace_id=ws,
        project_id=proj,
        agent_id=w("agents", n),
        agent_version_id=w("agents.v1", n),
        status="completed",
        started_at=now - timedelta(minutes=1),
        finished_at=now,
    )
    pipeline = art(w.admin.client, w.admin, "pipelines", f"flow-{n}", {}, w.scope(n, "project"))[
        "id"
    ]
    w.set("pipeline", n, pipeline)
    version = o.execute(
        text("SELECT id FROM shifde.artifact_versions WHERE artifact_id = :a"), {"a": pipeline}
    ).scalar_one()
    w.set(
        "pipeline_run",
        n,
        str(
            add_run(
                o,
                tenant,
                workspace_id=ws,
                project_id=proj,
                kind="pipeline",
                pipeline_id=pipeline,
                pipeline_version_id=version,
            )
        ),
    )
    o.commit()


def test_runs_approvals_usage_and_schedules(
    world: World, owner_session: Session, settings: Settings
) -> None:
    flagged = settings.model_copy(update={"feature_pipeline_schedules": True})
    with full_client(flagged) as client:
        w = _world(client, owner_session, world.passwords)
        w.set("model", 1, fake_model_stack(w.admin.client, w.admin)["model"])
        tenant = tenant_id(owner_session, "demo-a")
        owner_session.rollback()
        w.set("sa", 1, w.post("/service-accounts", {"name": "scheduler"})["id"])
        for n in (1, 2):
            _artifact(w, "prompts", n, prompt_spec(), by_api=True)
            spec = agent_spec(f"latest:{w('prompts', n)}", w("model", 1))
            _artifact(w, "agents", n, spec, by_api=True)
            started = w.post(
                "/runs",
                {
                    "kind": "agent",
                    "agent_id": w("agents", n),
                    "workspace": f"w{n}",
                    "project": f"p{n}",
                    "input": "hello",
                },
                202,
            )
            w.set("run", n, started["id"])
            _seed_runs(w, n, tenant)
        w.post(
            f"/pipelines/{w('pipeline', 2)}/schedules",
            {"cron": "0 3 * * *", "run_as_service_account_id": w("sa", 1)},
        )
        _check(w, "runs")


def test_budgets_quotas_and_policies(
    client: TestClient, world: World, owner_session: Session
) -> None:
    w = _world(client, owner_session, world.passwords)
    for n in (1, 2):
        w.set("budget", n, w.post("/budgets", _budget(w, n, "daily"))["id"])
        quota = w.post("/quotas", {"scope": w.scope(n), "metric": "runs_per_day", "limit": 100})
        w.set("quota", n, quota["id"])
        policy = w.post(
            "/governance/policies",
            {"kind": "approval_rule", "name": f"rule-{n}", "scope": w.scope(n), "definition": RULE},
        )
        w.set("gov_policy", n, policy["id"])
    _check(w, "governance")


# ====================================================================== client routes, workspace data
@dataclass(frozen=True)
class ClientCase:
    """One CLIENT operation sent by U (refused) and then by the client administrator (2xx).

    `state`: for a write, a schema-owner snapshot of the target's rows, equal before and after
    U's request. `control`: the administrator's request when it differs from U's (a current
    `row_version`), built after that comparison."""

    req: Req
    state: Callable[[], list[tuple[str, ...]]] | None = None
    control: Callable[[], Req] | None = None


SECRET_W1 = "SELECT * FROM shifde.secrets WHERE id = :s"


def _secret_rv(w: W) -> int:
    rows = w.rows("SELECT row_version FROM shifde.secrets WHERE id = :s", s=w("secret", 1))
    return int(rows[0][0])


def _secret_write(
    w: W,
    method: str,
    tail: str,
    body: Callable[[int], dict[str, Any]],  # Any: JSON body
) -> ClientCase:
    """A write to W1's secret: U sends the current `row_version`, the administrator the one
    current after U's refused call."""
    url = _url(f"/secrets/{w('secret', 1)}{tail}")
    return ClientCase(
        Req(method, url, body(_secret_rv(w))),
        state=w.snap(SECRET_W1, s=w("secret", 1)),
        control=lambda: Req(method, url, body(_secret_rv(w))),
    )


ClientBuilder = Callable[[W], ClientCase]
# CLIENT operations that hold workspace data: U, bound only at W1, is refused on each.
CLIENT_CASES: dict[Op, ClientBuilder] = {
    ("GET", ORG + "/secrets"): lambda w: ClientCase(Req("GET", _url("/secrets"))),
    ("POST", ORG + "/secrets/{secret_id}/rotate"): lambda w: _secret_write(
        w, "POST", "/rotate", lambda rv: {"value": "rotated-not-a-secret", "row_version": rv}
    ),
    ("PATCH", ORG + "/secrets/{secret_id}"): lambda w: _secret_write(
        w, "PATCH", "", lambda rv: {"status": "disabled", "row_version": rv}
    ),
    ("GET", ORG + "/model-routes"): lambda w: ClientCase(Req("GET", _url("/model-routes"))),
    ("GET", ORG + "/model-routes/{route_id}"): lambda w: ClientCase(
        Req("GET", _url(f"/model-routes/{w('route', 1)}"))
    ),
    ("GET", ORG + "/models"): lambda w: ClientCase(Req("GET", _url("/models"))),
    ("GET", ORG + "/models/{model_id}"): lambda w: ClientCase(
        Req("GET", _url(f"/models/{w('model', 1)}"))
    ),
    ("GET", ORG + "/governance/decisions"): lambda w: ClientCase(
        Req("GET", _url("/governance/decisions"))
    ),
    ("GET", ORG + "/governance/overview"): lambda w: ClientCase(
        Req("GET", _url("/governance/overview"))
    ),
    ("GET", ORG + "/audit-events/export"): lambda w: ClientCase(
        Req("GET", _url("/audit-events/export"), params={"format": "csv"})
    ),
    ("GET", ORG + "/usage/export"): lambda w: ClientCase(
        Req("GET", _url("/usage/export"), params={"format": "csv"})
    ),
}


def _run_client_case(w: W, op: Op, case: ClientCase) -> list[str]:
    name = f"{op[0]} {op[1]}"
    out: list[str] = []
    before = case.state() if case.state else None
    if case.state and not before:
        out.append(f"{name}: no rows to protect")
    got = _send(w.user, case.req)
    if got.status_code not in REFUSED:
        out.append(f"{name}: U refused: {got.status_code} {got.text[:300]}")
    if case.state and case.state() != before:
        out.append(f"{name}: U refused: its rows changed")
    control = _send(w.admin, case.control() if case.control else case.req)
    if control.status_code >= 300:
        out.append(f"{name}: control as the administrator: {control.status_code} {control.text}")
    return out


def test_every_client_case_is_a_client_operation() -> None:
    bad = [f"{op[0]} {op[1]}" for op in CLIENT_CASES if ROUTES.get(op, ("",))[0] != CLIENT]
    assert not bad, f"not classed CLIENT in ROUTES (or missing): {bad}"


def test_client_operations_holding_workspace_data_refuse_u(
    client: TestClient, world: World, owner_session: Session
) -> None:
    w = _world(client, owner_session, world.passwords)
    w.set("model", 1, fake_model_stack(w.admin.client, w.admin)["model"])
    for n in (1, 2):
        made = w.post("/secrets", {"name": f"s{n}", "value": "not-a-secret", "scope": w.scope(n)})
        w.set("secret", n, made["id"])
        route = w.post(
            "/model-routes",
            {"name": f"route-{n}", "scope": w.scope(n), "primary_model_id": w("model", 1)},
        )
        w.set("route", n, route["id"])
    failures: list[str] = []
    for op, build in CLIENT_CASES.items():
        failures += _run_client_case(w, op, build(w))
    assert not failures, "\n".join(failures)
