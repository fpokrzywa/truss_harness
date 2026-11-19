"""Registered ontology actions (P7 contract section 4, FR-ONT-011).

An action is a published tool version bound to an object type, with a parameter mapping from the
object's properties. Invoking it creates a P5 run of kind `ontology_action`; the worker resolves
the object as the INITIATING principal (policies, row filters and property denial apply, so a
property the principal cannot read can never feed a parameter) and runs the tool through the very
same `steps.run_tool` as agents and pipelines: Execution Gate (`tool:invoke`, the tool's
`required_permissions`), resource and policy checks, write-ahead journal, idempotency key,
retry rules and approvals. Parameters are derived from source data, which is untrusted, so every
tool with a side effect (any write class) waits for an approval by another principal; the
mapping wins over `extra_params` so a caller cannot rewrite an object-derived value.

Unified-platform port (DECISIONS B-2160..): the worker reads the action's tool through the run's
own env (`ctx.env`, the `run_of` contract of B-1933): only a version the run's pinned ontology
version pins (role `action_tool`) is reachable, so the tool's `VersionRef` and the pinned ids of
the call's `ToolPolicy` both come from the run's pin set, never from the caller (B-2162). The API
side reads it through the caller's own env (visibility and use checks). An action always runs as
its own `ontology_action` run: nothing else starts one (B-2163). Every repository call names the
client (B-2115); a new run's id is the repository's (no `id` / `root_run_id` in its values).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any  # Any: tool arguments / run input
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from shifde.authz.service import ResourceRef
from shifde.build import ontologies
from shifde.build.env import Env, VersionRef
from shifde.build.executors import WRITE_CLASSES
from shifde.build.issues import schema_errors
from shifde.core.clock import utcnow
from shifde.core.errors import (
    AppError,
    Conflict,
    Forbidden,
    IdempotencyConflict,
    NotFound,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.observability import tracing
from shifde.ontology.service import OntologyService
from shifde.runtime import gate, state, steps
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from shifde.runtime.governance import BudgetDenied, BudgetScope
from shifde.runtime.schemas import ActionInvokeIn, RunOut
from shifde.tenancy import service as tenancy


def build_args(
    action: Mapping[str, Any],  # Any: action spec
    obj: Mapping[str, Any],  # Any: the resolved object
    extra: Mapping[str, Any],  # Any: caller supplied
) -> dict[str, Any]:  # Any: tool arguments
    """`param_mapping` evaluated against the object; `extra` fills only parameters it does not
    map."""
    mapped: dict[str, Any] = {}
    for param, value in (action.get("param_mapping") or {}).items():
        if isinstance(value, str) and value.startswith(ontologies.OBJECT_REF):
            mapped[param] = obj.get(value[len(ontologies.OBJECT_REF) :])
        else:
            mapped[param] = value
    return {**dict(extra), **mapped}


def _tool(env: Env, action: Mapping[str, Any]) -> VersionRef:  # Any: action spec
    """The action's tool version read through `env` (the run's own, or the caller's)."""
    try:
        version_id = UUID(str(action["tool_version_id"]))
    except (KeyError, ValueError) as exc:
        raise NotFound("the action's tool version is not available") from exc
    tool = env.version(version_id)
    if tool is None or tool.kind != "tools":
        raise NotFound("the action's tool version is not available")
    return tool


class OntologyActionRunner:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def run(self) -> dict[str, Any]:  # Any: the run's output document
        ctx = self.ctx
        run = ctx.run
        payload = repo.get_artifact(ctx.session, ctx.tenant_id, run["input_ref"]) or {}
        action_key = str(run["action_key"])
        ontology_id: UUID = run["ontology_id"]
        try:
            art = ctx.artifacts.artifact_row("ontologies", ontology_id)  # the client's, else 404
            # the run's own env: its pinned ontology version and what it pins (B-1933)
            env = ctx.env(art)
        except NotFound as exc:
            raise state.RunFailed(
                state.VALIDATION, "the pinned ontology version is not available"
            ) from exc
        # the version this run row names must be the stored run's own (the env's), of `art`
        pinned = env.version(run["ontology_version_id"])
        if pinned is None or pinned.kind != "ontologies" or pinned.artifact_id != art["id"]:
            raise state.RunFailed(state.VALIDATION, "the pinned ontology version is not available")
        ref = ResourceRef(
            ctx.tenant_id, art["workspace_id"], art["project_id"], "ontology", art["id"]
        )

        def resource_state() -> gate.Problem | None:
            if art["status"] != "active":
                return ("ontology_not_active", f"the ontology is {art['status']}")
            version = ctx.artifacts.version_of(art, run["ontology_version_id"])
            if version["state"] not in ("published", "deprecated"):
                return ("version_not_published", "only a published ontology version can run")
            return None

        def visibility() -> gate.Problem | None:
            if not ctx.artifacts.visible(art):
                return ("ontology_not_visible", "the ontology is not available to this principal")
            return None

        gate.check(
            ctx,
            "ontology:query",
            ref,
            label=f"ontology:{art['key']}:action:{action_key}"[:200],
            resource_state=resource_state,
            policy=visibility,
        )
        service = OntologyService(ctx.artifacts)
        try:
            action, _, obj = service.action_inputs(
                ontology_id,
                run["ontology_version_id"],
                action_key,
                str(payload.get("object_pk", "")),
                api=False,
                run_id=ctx.run_id,
            )
        except (NotFound, Forbidden) as exc:
            raise state.GateDenied(state.AUTHZ_DENIED, exc.detail, "ontology_policy") from exc
        except AppError as exc:
            raise state.RunFailed(state.VALIDATION, steps._short(exc.detail)) from exc
        ctx.record_classification(service.read_classification)  # ARCH M11
        try:
            tool = _tool(env, action)  # only a version the run's ontology version pins
        except NotFound as exc:
            raise state.RunFailed(state.VALIDATION, exc.detail) from exc
        args = build_args(action, obj, payload.get("extra_params") or {})
        policy = steps.ToolPolicy(
            approval_classes=frozenset(WRITE_CLASSES),
            agent_id=None,
            pinned_version_ids=frozenset({tool.version_id}),
        )
        output = steps.run_tool(
            ctx,
            steps.ToolCall(tool=tool, args=args, call_ref=f"action:{action_key}"),
            policy,
        )
        return {
            "action": action_key,
            "object_pk": payload.get("object_pk"),
            "tool_version_id": str(tool.version_id),
            "output": output,
        }


def _run_scope(sc: Any, art: Mapping[str, Any], body: ActionInvokeIn) -> tuple[UUID, UUID]:  # Any
    """(workspace id, project id) of the run. A project-scoped ontology fixes both; otherwise the
    body names them and they must lie inside the ontology's own scope (a workspace-scoped
    ontology cannot be run, and its object-derived data cannot be exposed, in another workspace)."""
    if body.workspace is not None or body.project is not None or art["project_id"] is None:
        if body.workspace is None or body.project is None:
            raise ValidationFailed(
                "workspace and project (slugs) are required: the ontology is not project-scoped"
            )
        ref = tenancy.scope_ref_from_slugs(sc, body.workspace, body.project)
        if ref.workspace_id is None or ref.project_id is None:
            raise ValidationFailed("a workspace and a project are required")
        if art["workspace_id"] not in (None, ref.workspace_id) or art["project_id"] not in (
            None,
            ref.project_id,
        ):
            raise ValidationFailed("the ontology does not belong to this workspace / project")
        return ref.workspace_id, ref.project_id
    return art["workspace_id"], art["project_id"]


def start_action_run(
    svc: Any,  # Any: RunService (runtime.service imports this module lazily)
    ontology_id: UUID,
    action_key: str,
    body: ActionInvokeIn,
    idempotency_key: str | None,
) -> tuple[RunOut, bool]:
    """Authorize and queue one action invocation; returns (run, created)."""
    svc._active_tenant()
    sc = svc.sc
    arts = svc.artifacts
    onto = OntologyService(arts)
    art = arts.artifact("ontologies", ontology_id)  # 404 unless visible
    sc.pdp.require("ontology:query", arts.ref_of(art))
    version = onto.resolve_version(art, body.version_id)
    spec = dict(version["spec"])
    action = next((a for a in spec.get("actions") or [] if a.get("key") == action_key), None)
    if action is None:
        raise NotFound("action not found")
    tool = _tool(arts.env(art), action)  # the caller's env: visibility and use checks
    sc.pdp.require(
        "tool:invoke",
        ResourceRef(sc.tenant_id, tool.workspace_id, tool.project_id, "tool", tool.artifact_id),
        hide_unless="tool:read",
    )
    principal = sc.principal
    workspace_id, project_id = _run_scope(sc, art, body)
    fingerprint = hashlib.sha256(
        json.dumps(
            {"o": str(ontology_id), "a": action_key, **body.model_dump(mode="json")},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if idempotency_key is not None:  # a replay answers before the object is resolved again
        existing = repo.run_by_idempotency(
            svc.session, sc.tenant_id, principal.principal_id, idempotency_key
        )
        if existing is not None:
            if existing["request_hash"] != fingerprint:
                raise IdempotencyConflict("this Idempotency-Key was used with a different request")
            if not sc.pdp.authorize("run:read", svc._ref(existing)).allowed:
                raise NotFound("run not found")
            return svc.run_out(existing), False
    svc._active_scope(workspace_id, project_id)  # findings Item 12: not in an archived scope
    # resolve the object as the caller: unknown / hidden objects and denied properties fail HERE
    _, _, obj = onto.action_inputs(ontology_id, version["id"], action_key, body.object_pk, api=True)
    problems = [
        m
        for _, m in schema_errors(
            tool.spec["input_schema"], build_args(action, obj, body.extra_params)
        )
    ]
    if problems:
        raise ValidationFailed(f"the action's tool arguments are invalid: {problems[0]}")
    now = utcnow()
    # the budget pre-flight of a run not yet stored (no usage can carry this id); the run's own id
    # is the repository's (B-2115)
    pending_id = uuid7()
    values: dict[str, Any] = {  # Any: column values
        "tenant_id": sc.tenant_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "kind": "ontology_action",
        "initiated_by_principal_id": principal.principal_id,
        "principal_type": principal.principal_type,
        "api_key_id": principal.api_key_id,
        "idempotency_key": idempotency_key,
        "request_hash": fingerprint,
        "trace_id": tracing.current_trace_id() or uuid7().hex,
        "correlation_id": sc.ctx.request_id,
        "deadline_at": now + timedelta(seconds=svc.runtime.settings.run_default_deadline_seconds),
        "is_test": False,
        "ontology_id": art["id"],
        "ontology_version_id": version["id"],
        "action_key": action_key,
    }
    try:
        svc.hooks.preflight(
            svc.session,
            BudgetScope(
                sc.tenant_id,
                workspace_id,
                project_id,
                None,
                pending_id,
                None,
                principal.principal_id,
            ),
        )
    except BudgetDenied as exc:
        svc.session.rollback()
        return svc._budget_denied(values, {"object_pk": body.object_pk}, exc), True
    try:
        row = repo.create_run(
            svc.session,
            values,
            {"object_pk": body.object_pk, "extra_params": body.extra_params},
        )
        repo.enqueue(svc.session, sc.tenant_id, row["id"])
        svc._audit(
            "run.created",
            row,
            {
                "target_version_id": str(version["id"]),
                "action": action_key,
                "idempotency_key": idempotency_key,
            },
        )
        svc.session.commit()
    except IntegrityError as exc:
        svc.session.rollback()
        raise Conflict("the run could not be created") from exc
    return svc.run_out(row), True
