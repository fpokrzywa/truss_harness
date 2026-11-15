"""build part 4a: `ArtifactService` (`build/service.py`) and its production `Env`.

Every public method is called in client A's context with client B's ids and with unknown ids:
both must give the same outcome, and B's rows must be untouched. Each case runs as `shifde_app`
(row-level security applies) and as the schema owner (it does not: only the client id the
service passes confines A). The `Env` cases close build isolation review 2 F1 (a)-(i).

Moved from SETH `test_p4_review2.py` with assertions unchanged (the set-up goes through the
service instead of the API): `test_finalize_hashes_what_jsonb_stores_even_for_an_unnormalised_spec`,
`test_the_approval_audit_row_is_written_in_the_publishing_transaction`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.build import repository
from shifde.build import schemas as sch
from shifde.build.canonical import spec_hash
from shifde.build.env import VersionRef
from shifde.build.service import ArtifactService
from shifde.connections import repository as conn_repo
from shifde.core.db import Database
from shifde.core.errors import AppError, NotFound
from shifde.core.ids import uuid7
from shifde.core.settings import Settings
from shifde.models_admin import repository as models_repo
from tests.build.conftest import World
from tests.build.service_support import REGISTRY, add_person, bind_at, holder, service_for
from tests.connections.conftest import connection_values, discovery_values
from tests.models_admin.conftest import model_values, owner_connection, provider_values
from tests.tenancy.conftest import app_session

PROMPT: dict[str, Any] = {
    "sections": {"system": "You are a support assistant.", "context": "Question: {{message}}"},
    "variables": [{"name": "message", "type": "string", "required": True}],
}
PROMPT2: dict[str, Any] = {**PROMPT, "sections": {**PROMPT["sections"], "system": "Be brief."}}


@dataclass(frozen=True)
class Org:
    tenant: UUID
    admin: UUID
    approver: UUID
    prompt: UUID  # published v1, open draft v2
    version: UUID
    draft: UUID
    pending: UUID  # an artifact whose draft waits on `request`
    request: UUID
    connection: UUID
    model: UUID


def _approval(o: Session, tenant: UUID, on: bool) -> None:
    o.execute(
        text(
            "UPDATE shifde.tenants SET settings = settings || "
            "jsonb_build_object('publish_requires_approval', CAST(:on AS boolean)) WHERE id = :t"
        ),
        {"t": tenant, "on": on},
    )


def _tenant_scope(tenant: UUID) -> sch.ScopeRef:
    return sch.ScopeRef(type="tenant", id=tenant)


def _create(svc: ArtifactService, key: str, spec: dict[str, Any], **over: Any) -> sch.ArtifactOut:
    body = {"key": key, "name": key, "scope": _tenant_scope(svc.sc.tenant_id), "spec": spec}
    return svc.create("prompts", sch.ArtifactCreate(**{**body, **over}))


@pytest.fixture
def orgs(owner_db: Database, app_db: Database, world: World, settings: Settings) -> tuple[Org, Org]:
    out: list[Org] = []
    for label, tenant in (("a", world.a.tenant), ("b", world.b.tenant)):
        with owner_db.session() as o:
            admin = add_person(o, tenant, f"admin-{label}", "tenant_administrator")
            approver = add_person(o, tenant, f"approver-{label}", "tenant_administrator")
            values = connection_values(tenant, connection_type="database", adapter_key="postgres")
            conn_repo.insert_connection(o, values)
            conn_repo.insert_discovery(
                o, {**discovery_values(tenant, values["id"]), "kind": "schema", "payload": {}}
            )
            provider = provider_values(tenant, owner_connection(o, tenant))
            models_repo.insert_provider(o, provider)
            model = model_values(tenant, provider["id"])
            models_repo.insert_model(o, model)
            _approval(o, tenant, False)
            o.commit()
        with app_session(app_db, tenant) as s:
            svc = service_for(s, tenant, admin, settings)
            p = _create(svc, f"prompt-{label}", PROMPT)
            v1 = svc.publish("prompts", p.id, sch.PublishIn())
            assert isinstance(v1, sch.VersionOut)
            draft = svc.put_draft("prompts", p.id, sch.DraftPut(spec=PROMPT2, row_version=0))
            q = _create(svc, f"pending-{label}", PROMPT)
        with owner_db.session() as o:
            _approval(o, tenant, True)
            o.commit()
        with app_session(app_db, tenant) as s:
            svc = service_for(s, tenant, admin, settings)
            req = svc.publish("prompts", q.id, sch.PublishIn())
            assert isinstance(req, sch.PublishPendingOut)
        out.append(
            Org(
                tenant,
                admin,
                approver,
                p.id,
                v1.id,
                draft.id,
                q.id,
                req.publish_request.id,
                values["id"],
                model["id"],
            )
        )
    return out[0], out[1]


Opener = Callable[[UUID, UUID], AbstractContextManager[ArtifactService]]


@pytest.fixture(params=["as-app-role", "as-owner-no-rls"])
def as_user(
    request: pytest.FixtureRequest, app_db: Database, owner_db: Database, settings: Settings
) -> Opener:
    """`as_user(client, person)`: the person's service on a session of the given kind."""

    @contextmanager
    def opened(tenant: UUID, user: UUID) -> Iterator[ArtifactService]:
        cm = app_session(app_db, tenant) if request.param == "as-app-role" else owner_db.session()
        with cm as s:
            yield service_for(s, tenant, user, settings)

    return opened


def _outcome(call: Callable[[], object]) -> tuple[str, str]:
    try:
        result = call()
    except AppError as exc:
        return type(exc).__name__, str(exc)
    return "ok", type(result).__name__


def _b_state(owner_db: Database, b: Org) -> tuple[Any, ...]:
    with owner_db.session() as o:
        return tuple(
            o.execute(
                text(
                    "SELECT (SELECT array_agg(row_to_json(a)::text ORDER BY id) "
                    "        FROM shifde.artifacts a WHERE tenant_id = :t), "
                    "       (SELECT array_agg(row_to_json(v)::text ORDER BY id) "
                    "        FROM shifde.artifact_versions v WHERE tenant_id = :t), "
                    "       (SELECT array_agg(row_to_json(r)::text ORDER BY id) "
                    "        FROM shifde.publish_requests r WHERE tenant_id = :t)"
                ),
                {"t": b.tenant},
            ).one()
        )


Call = Callable[[ArtifactService, "Ids"], object]


@dataclass(frozen=True)
class Ids:
    prompt: UUID
    version: UUID
    pending: UUID
    request: UUID


def _ids(o: Org) -> Ids:
    return Ids(o.prompt, o.version, o.pending, o.request)


CASES: list[tuple[str, Call]] = [
    ("get", lambda v, i: v.get("prompts", i.prompt)),
    ("update", lambda v, i: v.update("prompts", i.prompt, sch.ArtifactPatch(name="hijacked"))),
    (
        "update_status",
        lambda v, i: v.update("prompts", i.prompt, sch.ArtifactPatch(status="retired")),
    ),
    ("list_versions", lambda v, i: v.list_versions("prompts", i.prompt)),
    ("get_version", lambda v, i: v.get_version("prompts", i.prompt, i.version)),
    ("get_draft", lambda v, i: v.get_draft("prompts", i.prompt)),
    (
        "put_draft",
        lambda v, i: v.put_draft("prompts", i.prompt, sch.DraftPut(spec=PROMPT, row_version=1)),
    ),
    ("validate_draft", lambda v, i: v.validate_draft("prompts", i.prompt)),
    ("publish", lambda v, i: v.publish("prompts", i.prompt, sch.PublishIn())),
    (
        "set_state",
        lambda v, i: v.set_state("prompts", i.prompt, i.version, sch.StateIn(state="deprecated")),
    ),
    ("diff", lambda v, i: v.diff("prompts", i.prompt, str(i.version), "draft")),
    (
        "rollback",
        lambda v, i: v.rollback("prompts", i.prompt, sch.RollbackIn(version_id=i.version)),
    ),
    ("decide_approve", lambda v, i: v.decide(i.request, sch.DecideIn(decision="approve"))),
    ("decide_reject", lambda v, i: v.decide(i.request, sch.DecideIn(decision="reject"))),
    ("artifact_row", lambda v, i: v.artifact_row("prompts", i.prompt)),
    ("version_spec", lambda v, i: v.version_spec(i.version)),
    ("draft_of", lambda v, i: v.draft_of({"id": i.prompt})),
]


@pytest.mark.parametrize(("label", "call"), CASES, ids=[c[0] for c in CASES])
def test_a_acting_on_bs_ids_gets_the_unknown_id_outcome(
    label: str, call: Call, orgs: tuple[Org, Org], as_user: Opener, owner_db: Database
) -> None:
    a, b = orgs
    unknown = Ids(uuid7(), uuid7(), uuid7(), uuid7())
    before = _b_state(owner_db, b)
    with as_user(a.tenant, a.approver) as svc:
        theirs = _outcome(lambda: call(svc, _ids(b)))
        svc.session.rollback()
        nothing = _outcome(lambda: call(svc, unknown))
        svc.session.rollback()
    assert theirs == nothing, label
    assert theirs[0] in ("NotFound", "ok"), theirs
    if theirs[0] == "ok":  # version_spec / draft_of: None, never B's row
        with as_user(a.tenant, a.approver) as svc:
            assert call(svc, _ids(b)) is None
    assert _b_state(owner_db, b) == before


MIXED: list[tuple[str, Callable[[ArtifactService, Org, UUID], object]]] = [
    ("get_version", lambda v, a, x: v.get_version("prompts", a.prompt, x)),
    ("diff", lambda v, a, x: v.diff("prompts", a.prompt, str(x), "draft")),
    (
        "set_state",
        lambda v, a, x: v.set_state("prompts", a.prompt, x, sch.StateIn(state="deprecated")),
    ),
    (
        "rollback",
        lambda v, a, x: v.rollback("prompts", a.prompt, sch.RollbackIn(version_id=x)),
    ),
]


@pytest.mark.parametrize(("label", "call"), MIXED, ids=[c[0] for c in MIXED])
def test_as_own_artifact_with_bs_version_id_gets_the_unknown_id_outcome(
    label: str,
    call: Callable[[ArtifactService, Org, UUID], object],
    orgs: tuple[Org, Org],
    as_user: Opener,
    owner_db: Database,
) -> None:
    a, b = orgs
    before = _b_state(owner_db, b)
    with as_user(a.tenant, a.admin) as svc:
        theirs = _outcome(lambda: call(svc, a, b.version))
        svc.session.rollback()
        nothing = _outcome(lambda: call(svc, a, uuid7()))
        svc.session.rollback()
        own = _outcome(lambda: svc.get_version("prompts", a.prompt, a.version))
    assert theirs == nothing and theirs[0] == "NotFound", label
    assert own == ("ok", "VersionOut")
    assert _b_state(owner_db, b) == before


def test_lists_name_only_the_callers_client(orgs: tuple[Org, Org], as_user: Opener) -> None:
    a, b = orgs
    with as_user(a.tenant, a.approver) as svc:
        listed = svc.list_artifacts(
            "prompts", scope_type=None, scope_id=None, status=None, q=None, limit=50, cursor=None
        )
        requests = svc.list_requests(None, 50, None)
    assert {x.id for x in listed.items} == {a.prompt, a.pending}
    assert [r.id for r in requests.items] == [a.request]
    assert b.prompt not in {x.id for x in listed.items}


def test_create_takes_ids_owner_and_client_from_the_session(
    orgs: tuple[Org, Org], as_user: Opener, owner_db: Database
) -> None:
    """The client comes from the scope: a scope id naming B's client (or B's workspace) is the
    unknown-scope answer; the ids and the owner are minted here."""
    a, b = orgs
    with as_user(a.tenant, a.admin) as svc:
        theirs = _outcome(lambda: _create(svc, "x1", PROMPT, scope=_tenant_scope(b.tenant)))
        svc.session.rollback()
        nothing = _outcome(lambda: _create(svc, "x1", PROMPT, scope=_tenant_scope(uuid7())))
        svc.session.rollback()
        made = _create(svc, "mine", PROMPT)
    assert theirs == nothing and theirs[0] == "NotFound"
    with owner_db.session() as o:
        row = repository.get_artifact(o, a.tenant, made.id)
        assert row is not None and row["tenant_id"] == a.tenant
        draft = repository.get_draft(o, a.tenant, made.id)
        assert row["owner_principal_id"] == a.admin
        assert draft is not None and draft["created_by"] == a.admin
        assert repository.get_artifact(o, b.tenant, made.id) is None


def test_a_private_artifact_is_its_owners_only(orgs: tuple[Org, Org], as_user: Opener) -> None:
    a, _ = orgs
    with as_user(a.tenant, a.admin) as svc:
        private = _create(svc, "secret", PROMPT, visibility="private")
    with as_user(a.tenant, a.approver) as other:
        theirs = _outcome(lambda: other.get("prompts", private.id))
        nothing = _outcome(lambda: other.get("prompts", uuid7()))
        env = other.env(holder(a.tenant, "prompts"))
        draft_owner = repository.get_draft(other.session, a.tenant, private.id)
        assert draft_owner is not None
        assert env.version(draft_owner["id"]) is None  # F1 (a): same as an unknown version
    assert theirs == nothing and theirs[0] == "NotFound"


# ------------------------------------------------------------------ publish requests / approvals
def test_an_approver_of_another_workspace_neither_sees_nor_decides_the_request(
    orgs: tuple[Org, Org], as_user: Opener, owner_db: Database, world: World
) -> None:
    """SETH `test_p4_review2.py` case, at the service: `operator` (approval:decide, prompt:read)
    bound at workspace 2 does not see or decide a request of an artifact in workspace 1."""
    a, _ = orgs
    with owner_db.session() as o:
        other = add_person(o, a.tenant, "ws2-approver", None)
        bind_at(o, a.tenant, other, "operator", "workspace", world.a.ws2)
        o.commit()
    with as_user(a.tenant, a.admin) as svc:
        art = svc.create(
            "prompts",
            sch.ArtifactCreate(
                key="in-w1",
                name="in-w1",
                scope=sch.ScopeRef(type="workspace", id=world.a.ws),
                spec=PROMPT,
            ),
        )
        req = svc.publish("prompts", art.id, sch.PublishIn())
        assert isinstance(req, sch.PublishPendingOut)
    with as_user(a.tenant, other) as svc:
        assert svc.list_requests(None, 50, None).items == []
        refused = _outcome(
            lambda: svc.decide(req.publish_request.id, sch.DecideIn(decision="approve"))
        )
        assert refused == _outcome(lambda: svc.decide(uuid7(), sch.DecideIn(decision="approve")))
        assert refused[0] == "NotFound"
    with owner_db.session() as o:
        still = repository.get_request(o, a.tenant, req.publish_request.id)
        assert still is not None and still["status"] == "pending"


def test_the_approver_decides_only_inside_the_requesters_client(
    orgs: tuple[Org, Org], as_user: Opener, owner_db: Database
) -> None:
    a, b = orgs
    with as_user(a.tenant, a.approver) as svc:
        done = svc.decide(a.request, sch.DecideIn(decision="approve"))
    assert done.status == "approved"
    with owner_db.session() as o:
        theirs = repository.get_request(o, b.tenant, b.request)
        assert theirs is not None and theirs["status"] == "pending"
        assert repository.get_request(o, b.tenant, a.request) is None


def test_the_approver_bypass_needs_a_request_of_the_callers_client(
    orgs: tuple[Org, Org], as_user: Opener, owner_db: Database
) -> None:
    """F1 (g): the env skips the caller's checks only for a request confirmed to be this
    client's and of this artifact; then names stay hidden."""
    a, b = orgs
    with owner_db.session() as o:
        own_req = repository.get_request(o, a.tenant, a.request)
        their_req = repository.get_request(o, b.tenant, b.request)
        own_art = repository.get_artifact(o, a.tenant, a.pending)
        other_art = repository.get_artifact(o, a.tenant, a.prompt)
        their_art = repository.get_artifact(o, b.tenant, b.pending)
    assert own_req and their_req and own_art and other_art and their_art
    forged = {**their_req, "tenant_id": a.tenant, "artifact_id": a.pending}
    with as_user(a.tenant, a.approver) as svc:
        env = svc.env(own_art, approval_of=own_req)
        assert not env.reveal_names and not env.check_visibility
        refusals = [
            _outcome(lambda: svc.env(own_art, approval_of=their_req)),
            _outcome(lambda: svc.env(own_art, approval_of=forged)),
            _outcome(lambda: svc.env(other_art, approval_of=own_req)),
            _outcome(lambda: svc.env(their_art, approval_of=their_req)),
            _outcome(lambda: svc.env(their_art)),
        ]
    assert {r[0] for r in refusals} == {"NotFound"}


# ------------------------------------------------------------------ the production Env (F1)
def test_the_env_answers_only_for_the_client_it_was_built_for(
    orgs: tuple[Org, Org], as_user: Opener
) -> None:
    """F1 (a), (d), (h), (i): B's version, artifact, model, route and connection answer as unknown
    ones; an env never follows its scope to another client."""
    a, b = orgs
    with as_user(a.tenant, a.admin) as svc:
        env = svc.env(holder(a.tenant))
        own = env.version(a.version)
        assert own is not None and own.workspace_id is None and own.project_id is None
        assert env.latest(a.prompt) is not None
        assert env.version(b.version) is None and env.latest(b.prompt) is None
        assert env.connection(b.connection) is None and env.connection(uuid7()) is None
        assert env.schema_discovery(b.connection) is None
        assert env.mcp_tool(b.connection, "x") == env.mcp_tool(uuid7(), "x")
        assert env.route_model_ids(uuid7()) is None
        problems = env.model_problems([b.model, (gone := uuid7())])
        assert problems[b.model] == problems[gone] == "model_not_found"
        assert svc.version_spec(b.version) is None and svc.version_spec(a.version) is not None
        svc.sc.tenant_id = b.tenant  # a scope reused for another client: the env refuses
        assert env.version(a.version) is None and env.version(b.version) is None
        assert env.connection(a.connection) is None


def test_connection_lookups_follow_read_and_use_and_the_registry(
    orgs: tuple[Org, Org], as_user: Opener, owner_db: Database, world: World
) -> None:
    """F1 (b), (c), (e): no `connection:read`, no connection (nor its discovery); `connection:use`
    decides `usable_by_publisher`; `implemented`/`capabilities` come from the registry; only an
    `mcp_tools` discovery lists MCP tools."""
    a, _ = orgs
    with owner_db.session() as o:
        viewer = add_person(o, a.tenant, "viewer", "viewer")
        blind = add_person(o, a.tenant, "blind", None)
        bind_at(o, a.tenant, blind, "developer", "project", world.a.project)
        conn_repo.insert_discovery(
            o,
            {
                **discovery_values(a.tenant, a.connection),
                "kind": "openapi",
                "payload": {"tools": [{"name": "lookup"}]},
            },
        )
        o.commit()
    desc = REGISTRY.descriptor("postgres")
    assert desc is not None
    with as_user(a.tenant, a.admin) as svc:
        info = svc.env(holder(a.tenant)).connection(a.connection)
        assert info is not None and info.usable_by_publisher
        assert info.implemented == desc.implemented
        assert info.capabilities == frozenset(str(c) for c in desc.capabilities)
        # the latest discovery is not an MCP one: no tool, whatever its payload says
        assert not svc.env(holder(a.tenant)).mcp_tool(a.connection, "lookup").exists
    with as_user(a.tenant, viewer) as svc:
        info = svc.env(holder(a.tenant)).connection(a.connection)
        assert info is not None and not info.usable_by_publisher
    with as_user(a.tenant, blind) as svc:
        env = svc.env(holder(a.tenant))
        assert env.connection(a.connection) is None
        assert env.schema_discovery(a.connection) is None
        assert env.mcp_tool(a.connection, "lookup") == env.mcp_tool(uuid7(), "lookup")


def test_permits_decides_at_the_artifacts_own_scope(
    orgs: tuple[Org, Org], as_user: Opener, owner_db: Database, world: World
) -> None:
    """F1 (f): `permits` reads the artifact with the client id and decides at its stored scope;
    a reference claiming another scope, kind or client gets False."""
    a, b = orgs
    with as_user(a.tenant, a.admin) as svc:
        env = svc.env(holder(a.tenant))
        ref = env.version(a.version)
        assert ref is not None and env.permits("prompt:read", ref)
        moved = VersionRef(**{**ref.__dict__, "workspace_id": world.a.ws})
        relabelled = VersionRef(**{**ref.__dict__, "kind": "tools"})
        foreign = VersionRef(**{**ref.__dict__, "artifact_id": b.prompt})
        assert not env.permits("prompt:read", moved)
        assert not env.permits("tool:read", relabelled)
        assert not env.permits("prompt:read", foreign)


def test_visibility_in_version_and_latest_uses_the_artifacts_scope(
    orgs: tuple[Org, Org], as_user: Opener, owner_db: Database, world: World
) -> None:
    """F1 (a): a person bound only in project 1 sees a workspace-2 artifact as an unknown id."""
    a, _ = orgs
    with owner_db.session() as o:
        narrow = add_person(o, a.tenant, "narrow", None)
        bind_at(o, a.tenant, narrow, "developer", "project", world.a.project)
        o.commit()
    with as_user(a.tenant, a.admin) as svc:
        made = svc.create(
            "prompts",
            sch.ArtifactCreate(
                key="ws2",
                name="ws2",
                scope=sch.ScopeRef(type="workspace", id=world.a.ws2),
                spec=PROMPT,
            ),
        )
        draft = repository.get_draft(svc.session, a.tenant, made.id)
        assert draft is not None
        seen = svc.env(holder(a.tenant)).version(draft["id"])
        assert seen is not None and seen.workspace_id == world.a.ws2
    with as_user(a.tenant, narrow) as svc:
        env = svc.env(holder(a.tenant))
        assert env.version(draft["id"]) is None and env.latest(made.id) is None


# ------------------------------------------------------------------ moved from SETH
def test_finalize_hashes_what_jsonb_stores_even_for_an_unnormalised_spec(
    orgs: tuple[Org, Org],
    owner_session: Session,
    app_db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    odd = {"n": 1e16, "z": -0.0}
    normalised = repository.normalize_json(owner_session, odd)
    owner_session.rollback()
    assert spec_hash(normalised) != spec_hash(odd)  # the premise: jsonb changes these numbers

    a, _ = orgs
    uid = a.admin
    in_memory = {**PROMPT, "examples": [{"inputs": {"message": "x", **odd}, "output": "o"}]}

    def finalize(key: str) -> tuple[str, Any]:
        with app_session(app_db, a.tenant) as s:
            svc = service_for(s, a.tenant, uid, settings)
            created = _create(svc, key, PROMPT)
            row = repository.get_artifact(svc.session, a.tenant, created.id)
            draft = repository.get_draft(svc.session, a.tenant, created.id)
            assert row is not None and draft is not None
            done = svc._finalize(
                row,
                frozen_draft=draft,
                spec=in_memory,
                pins=[],
                note=None,
                published_by=uid,
                approved_by=None,
                approval_id=None,
            )
            return str(done["spec_hash"]), done["spec"]

    digest, stored = finalize("norm-ok")
    assert digest == spec_hash(stored)
    monkeypatch.setattr(repository, "normalize_json", lambda _s, v: v)  # sabotage
    bad_digest, bad_stored = finalize("norm-bad")
    assert bad_digest != spec_hash(bad_stored)  # without normalisation the invariant breaks


def test_the_approval_audit_row_is_written_in_the_publishing_transaction(
    orgs: tuple[Org, Org],
    owner_session: Session,
    as_user: Opener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = orgs
    original = ArtifactService._audit

    def failing(self: ArtifactService, action: str, *args: Any, **kw: Any) -> None:
        if action.endswith("publish_approved"):
            raise RuntimeError("audit store unavailable")
        original(self, action, *args, **kw)

    monkeypatch.setattr(ArtifactService, "_audit", failing)
    with as_user(a.tenant, a.approver) as svc, pytest.raises(RuntimeError):
        svc.decide(a.request, sch.DecideIn(decision="approve"))
    row = owner_session.execute(
        text(
            "SELECT v.state, r.status FROM shifde.artifact_versions v "
            "JOIN shifde.publish_requests r ON r.version_id = v.id WHERE r.id = :r"
        ),
        {"r": a.request},
    ).one()
    owner_session.rollback()
    assert (row.state, row.status) == ("draft", "pending")  # nothing half-applied
    monkeypatch.undo()
    with as_user(a.tenant, a.approver) as svc:
        ok = svc.decide(a.request, sch.DecideIn(decision="approve"))
    assert ok.status == "approved"


def test_an_unknown_kind_or_another_clients_artifact_has_no_env(
    orgs: tuple[Org, Org], as_user: Opener
) -> None:
    a, b = orgs
    with as_user(a.tenant, a.admin) as svc, pytest.raises(NotFound):
        svc.env(holder(b.tenant, "prompts"))
