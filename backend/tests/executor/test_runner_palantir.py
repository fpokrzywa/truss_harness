"""The runner on a Palantir target (SP8-7): a Marketplace release through plan, apply, observe and
verify on `FakeFoundryStack` over the in-process gateway, a second release on the same
installation, `plan_stale` at plan and at apply, a target or entry gone in the middle of a job (no
later request, a cached token's included), the descriptor's refusals, a fake-made plan on the real
toolchain, an uncertain write, the extra host suffixes, and every credential refusal ending
`credential_unavailable` with no token, request or checkpoint. Nothing contacts a Foundry stack and
no secret reaches a log, a result or a receipt."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: JSON bodies, recorded calls
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import shifde
from shifde.adapters.native.base import create_archive, read_archive
from shifde.adapters.native.contracts import (
    ContextPlan,
    CredentialGrant,
    JobContext,
    JobLease,
    LeasedJob,
    Target,
    utcnow,
)
from shifde.adapters.native.palantir import source as palantir_source
from shifde.adapters.native.palantir.adapter import PalantirMarketplaceAdapter
from shifde.adapters.native.palantir.clients import (
    INSTALLATIONS,
    JOBS,
    TOKEN_PATH,
    FoundryCredentials,
    PalantirClient,
)
from shifde.adapters.native.palantir.fake import FakeFoundryStack
from shifde.adapters.native.planning import bindings_digest
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.executor import main
from shifde.executor.client import GatewayClient, GatewayError
from shifde.executor.credentials import CredentialBroker, CredentialUnavailable
from shifde.executor.runner import (
    FakePlatform,
    FoundryCredentialProvider,
    JobFailure,
    LeaseState,
    Platform,
    RealPlatform,
    Runner,
    toolchain_digest,
)
from tests.connections.routes import as_user
from tests.delivery.palantir import (
    CHECKS,
    DEPLOYABLE,
    RESOURCE,
    change_target,
    palantir_release,
    palantir_target,
)
from tests.delivery.routes import (
    Flow,
    delivery_client,
    deployment_jobs,
    deployment_row,
    journal_rows,
    make_due,
    owner_sql,
    plan_row,
    register,
    sp8_body,
    targets,
)
from tests.executor.support import SP8_SECRET, ask_plan, sp8_credentials_file, sp8_entry
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, ws_slug

STEPS = ("palantir-installation-create", "palantir-job-submit", "palantir-finalize")


def _stack(target: dict[str, Any]) -> FakeFoundryStack:  # Any: a TargetOut body
    """A stack the target's bindings satisfy."""
    bindings = target["bindings"]
    return FakeFoundryStack(
        environment="test",
        host=target["address"],
        frontend_origin=bindings["frontend_origin"],
        ontology_rid=bindings["ontology_rid"],
        namespace_rid=bindings["namespace_rid"],
        hang_timeout=0.05,
    )


def _runner(
    flow: Flow,
    creds: Path,
    platform: Platform,
    *,
    suffixes: tuple[str, ...] = (),
    gateway: GatewayClient | None = None,
) -> Runner:
    gateway = gateway or GatewayClient("https://testserver", flow.token, http=flow.client)
    me = gateway.whoami()
    broker = CredentialBroker.from_file(creds, palantir_host_suffixes=suffixes)
    return Runner(gateway, broker, me.executor_id, platform, tenant_id=me.tenant_id)


def _started(
    flow: Flow, tmp_path: Path
) -> tuple[dict[str, Any], FakeFoundryStack, Runner]:  # Any: a TargetOut body
    """A target, the stack behind it and a runner on that stack."""
    target = palantir_target(flow)
    stack = _stack(target)
    creds = sp8_credentials_file(tmp_path, "palantir", target["address"], tenant=flow.tenant)
    return target, stack, _runner(flow, creds, FakePlatform(palantir=stack, environment="test"))


def _ask(flow: Flow, target: dict[str, Any]) -> dict[str, Any]:  # Any: JSON bodies
    asked: dict[str, Any] = ask_plan(
        flow, target["id"], release_id=palantir_release(flow, target["id"]).id
    )
    return asked


def _approved(flow: Flow, target: dict[str, Any], runner: Runner) -> str:  # Any: a TargetOut body
    """An approved plan sealed by `runner`; its queued deployment's id."""
    plan = _ask(flow, target)
    assert runner.run_once()
    sealed = flow.get_plan(plan["id"])
    assert sealed["status"] == "sealed", sealed
    deployment: str = flow.approve(sealed)["deployment_id"]
    return deployment


def _run_to_end(flow: Flow, runner: Runner, deployment: str) -> Any:  # Any: a row
    for _ in ("apply", "observe", "verify"):
        make_due(flow.owner_db, deployment)
        assert runner.run_once()
    return deployment_row(flow.owner_db, deployment)


# ------------------------------------------------------------------ end to end
def test_plan_apply_observe_and_verify_run_on_the_palantir_fake(
    flow: Flow, tmp_path: Path, owner_db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    target = palantir_target(flow)
    creds = sp8_credentials_file(tmp_path, "palantir", target["address"], tenant=flow.tenant)
    platform = FakePlatform(environment="test")
    runner = _runner(flow, creds, platform)
    assert platform.palantir is None

    plan = _ask(flow, target)
    assert runner.run_once()
    sealed = plan_row(owner_db, plan["id"])
    assert sealed["status"] == "sealed", sealed["status_reason"]
    assert [s["key"] for s in sealed["steps"]] == list(STEPS)
    assert sealed["toolchain_digest"] == toolchain_digest("fake", "palantir")
    stack = platform.palantir
    assert isinstance(stack, FakeFoundryStack) and stack.posts == []  # planning only reads
    assert stack.namespace_rid == DEPLOYABLE["namespace_rid"]

    deployment = flow.approve(flow.get_plan(plan["id"]))["deployment_id"]
    row = _run_to_end(flow, runner, deployment)
    assert row["status"] == "verified", row["status_reason"]
    body = flow.get(deployment)
    assert {s["step_key"]: s["state"] for s in body["journal"]} == dict.fromkeys(STEPS, "complete")
    assert all(s["receipt"]["platform"] == "fake" for s in body["journal"])
    assert body["receipt"]["operation_handles"]["platform"] == "fake"
    assert [r["resource_type"] for r in body["receipt"]["resources"]] == [RESOURCE]
    checks = {c["name"]: c for c in row["verification"]["checks"]}
    assert {name: c["status"] for name, c in checks.items()} == {
        **dict.fromkeys(CHECKS, "passed"),
        "content-check": "not_run",
    }
    assert checks["content-check"]["required"] is False
    assert "not derived from the release" in checks["content-check"]["evidence"]
    assert ("GET", "/index.html", False) in stack.requests

    # a second release: the same installation, no second one
    second = _run_to_end(flow, runner, _approved(flow, target, runner))
    assert second["status"] == "verified", second["status_reason"]
    assert len(stack.installations) == 1
    assert [path for path, _ in stack.posts] == [INSTALLATIONS, JOBS, JOBS]

    results = [j["result"] for j in deployment_jobs(owner_db, deployment)]
    for leak in (SP8_SECRET, str(tmp_path), "palantir.json.secret", *stack.tokens):
        for blob in (caplog.text, str(results), str(body), str(dict(row))):
            assert leak not in blob


def test_an_asset_mismatch_or_a_failed_job_never_verifies_a_palantir_deployment(
    flow: Flow, tmp_path: Path, owner_db: Database
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    stack.asset_mismatch = True
    row = _run_to_end(flow, runner, _approved(flow, target, runner))
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")
    checks = {c["name"]: c["status"] for c in row["verification"]["checks"]}
    assert checks["frontend-asset"] == "failed" and checks["content-check"] == "not_run"
    stack.asset_mismatch, stack.job_failed = False, True
    deployment = _approved(flow, target, runner)
    assert runner.run_once()
    make_due(owner_db, deployment)
    assert runner.run_once()
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "the platform reported a failure")


@pytest.mark.parametrize("step", STEPS[:2])
def test_a_palantir_fault_after_acceptance_is_an_unknown_outcome_without_a_replay(
    flow: Flow, tmp_path: Path, owner_db: Database, step: Any
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    deployment = _approved(flow, target, runner)
    stack.fail_after_accept = step
    assert runner.run_once()
    assert deployment_row(owner_db, deployment)["status"] == "unknown_outcome"
    posts, requests = len(stack.posts), len(stack.requests)
    assert posts == 1 + STEPS.index(step)
    stack.fail_after_accept = None
    assert not runner.run_once()  # no second apply
    assert flow.act(deployment, "reconcile").status_code == 200
    assert runner.run_once()
    row = deployment_row(owner_db, deployment)
    assert row["status"] == "unknown_outcome"
    assert row["reconcile_evidence"]["state"] == "ambiguous"
    assert (len(stack.posts), len(stack.requests)) == (posts, requests)


# ------------------------------------------------------------------ plan_stale
@pytest.mark.parametrize("how", ["bindings", "revision"])
def test_a_palantir_plan_whose_target_changed_is_plan_stale_at_the_plan_job(
    flow: Flow, tmp_path: Path, owner_db: Database, how: str
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    plan = _ask(flow, target)
    change_target(flow, target["id"], how)
    assert runner.run_once()
    row = plan_row(owner_db, plan["id"])
    assert (row["status"], row["status_reason"]) == ("plan_failed", "plan job failed: plan_stale")
    assert stack.tokens == set() and stack.requests == []


@pytest.mark.parametrize("how", ["bindings", "revision"])
def test_an_approved_palantir_plan_whose_target_changed_is_refused_plan_stale(
    flow: Flow, tmp_path: Path, owner_db: Database, how: str
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    deployment = _approved(flow, target, runner)
    change_target(flow, target["id"], how)
    stack.tokens.clear()
    stack.requests.clear()
    assert runner.run_once()
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "apply refused: plan_stale")
    assert stack.tokens == set() and stack.requests == []
    assert journal_rows(owner_db, flow.tenant) == []
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert apply["result"] == {"status": "failed", "error_code": "plan_stale"}


def test_palantir_schema_drift_between_plan_and_apply_writes_nothing(
    flow: Flow, tmp_path: Path, owner_db: Database
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    deployment = _approved(flow, target, runner)
    stack.object_type_schema["revision"] = 2
    assert runner.run_once()
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == (
        "failed",
        "no platform write was attempted",
    )
    assert stack.posts == [] and journal_rows(owner_db, flow.tenant) == []


# ------------------------------------------------------------------ unreadable bindings (#406)
def _non_public_origin(flow: Flow, target: dict[str, Any]) -> None:  # Any: a TargetOut body
    """The stored row as a version before the public `frontend_origin` rule could have it."""
    owner_sql(
        flow.owner_db,
        "UPDATE shifde.delivery_targets SET bindings = bindings || "
        "jsonb_build_object('frontend_origin', 'https://localhost') WHERE id = :g",
        g=target["id"],
    )


def test_a_plan_job_of_an_unreadable_palantir_target_fails_with_no_token_or_request(
    flow: Flow, tmp_path: Path, owner_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    plan = _ask(flow, target)
    _non_public_origin(flow, target)
    codes: list[str] = []
    read = GatewayClient.context

    def recorded(self: GatewayClient, lease: JobLease) -> JobContext:
        try:
            return read(self, lease)
        except GatewayError as exc:
            codes.append(exc.code)
            raise

    monkeypatch.setattr(GatewayClient, "context", recorded)
    assert runner.run_once()
    assert codes == ["target_bindings_invalid"]
    row = plan_row(owner_db, plan["id"])
    assert (row["status"], row["status_reason"]) == (
        "plan_failed",
        "plan job failed: execution_failed",
    )
    assert stack.tokens == set() and stack.requests == []


def test_an_apply_job_of_an_unreadable_palantir_target_writes_nothing(
    flow: Flow, tmp_path: Path, owner_db: Database
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    deployment = _approved(flow, target, runner)
    _non_public_origin(flow, target)
    stack.tokens.clear()
    stack.requests.clear()
    assert runner.run_once()
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "no platform write was attempted")
    assert stack.tokens == set() and stack.requests == []
    assert journal_rows(owner_db, flow.tenant) == []


# ------------------------------------------------------------------ the toolchain
def test_the_palantir_toolchain_digest_is_its_own() -> None:
    fake, real = toolchain_digest("fake", "palantir"), toolchain_digest("palantir", "palantir")
    assert fake != real
    # the real platform's kind is `databricks`, whatever its target's
    assert toolchain_digest(RealPlatform.kind, "palantir") == real
    others = {
        toolchain_digest(platform, kind)
        for platform in ("fake", "databricks", "snowflake")
        for kind in ("databricks", "snowflake", "fabric", "docker")
    }
    assert not {fake, real} & others
    assert palantir_source.TOOLCHAIN_DIGEST not in (fake, real)


class RealOnStack(RealPlatform):
    """The real platform's adapter (its toolchain), its client on a recording stack."""

    def __init__(self, stack: FakeFoundryStack) -> None:
        self.stack = stack

    def palantir_adapter(
        self, target: Target, credential_provider: FoundryCredentialProvider
    ) -> PalantirMarketplaceAdapter:
        adapter = super().palantir_adapter(target, credential_provider)
        build = adapter.client_factory

        def on_stack(t: Target) -> PalantirClient:
            client = build(t)
            client.transport = self.stack.transport
            return client

        adapter.client_factory = on_stack
        return adapter


def test_a_fake_made_palantir_plan_is_refused_by_the_real_toolchain(
    flow: Flow, tmp_path: Path, owner_db: Database
) -> None:
    target, stack, runner = _started(flow, tmp_path)
    deployment = _approved(flow, target, runner)
    stack.tokens.clear()
    stack.requests.clear()
    creds = tmp_path / "palantir.json"
    assert _runner(flow, creds, RealOnStack(stack)).run_once()
    row = deployment_row(owner_db, deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "no platform write was attempted")
    assert stack.tokens == set() and stack.requests == []
    assert journal_rows(owner_db, flow.tenant) == []


# ------------------------------------------------------------------ the target or entry gone mid-job
@dataclass
class Mid:
    """A runner whose job is changed under it, on the fake or the real platform's client."""

    flow: Flow
    target: dict[str, Any]  # a TargetOut body
    stack: FakeFoundryStack
    runner: Runner
    sent: list[str] = field(default_factory=list)  # the paths requested after the change
    changed: bool = False

    def after(self, path: str, change: Callable[[Mid], None], nth: int = 1) -> None:
        """`change` happens once, right after the stack answered its `nth` request from now whose
        path starts with `path`; `sent` holds the paths requested from then on."""
        left = nth

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal left
            if self.changed:
                self.sent.append(request.url.path)
            answer = self.stack.handle(request)
            if request.url.path.startswith(path) and not self.changed:
                left -= 1
                if left == 0:
                    change(self)
                    self.changed = True
            return answer

        self.stack.transport = httpx.MockTransport(handle)

    def job(self, deployment: str) -> Any:  # Any: the deployment row
        make_due(self.flow.owner_db, deployment)
        assert self.runner.run_once()
        return deployment_row(self.flow.owner_db, deployment)

    def expire_grant(self) -> None:
        """The job in flight's grant has run out (30 seconds)."""
        state = self.runner._state
        assert state is not None and state.grant is not None
        state.grant = state.grant.model_copy(update={"expires_at": utcnow()})


@pytest.fixture(params=["fake", "real"])
def mid(request: pytest.FixtureRequest, flow: Flow, tmp_path: Path) -> Mid:
    target = palantir_target(flow)
    stack = _stack(target)
    creds = sp8_credentials_file(tmp_path, "palantir", target["address"], tenant=flow.tenant)
    platform: Platform = (
        FakePlatform(palantir=stack, environment="test")
        if request.param == "fake"
        else RealOnStack(stack)
    )
    return Mid(flow, target, stack, _runner(flow, creds, platform))


def _archive(mid: Mid) -> None:
    """An administrator archives the target; the grant runs out."""
    flow = mid.flow
    headers = as_user(flow.client, flow.requester)
    url = f"{targets(flow.slug)}/{mid.target['id']}/archive"
    assert flow.client.post(url, headers=headers).status_code == 200
    flow.client.cookies.clear()
    mid.expire_grant()


def _remove_entry(mid: Mid) -> None:
    mid.runner.broker._entries.clear()


def _other_tenants_entry(mid: Mid) -> None:
    entries = mid.runner.broker._entries
    for ref, entry in list(entries.items()):
        entries[ref] = entry.model_copy(update={"tenant_id": uuid4()})


# how the job ends; an archived target is the gateway's `target_unavailable`
GONE: dict[str, tuple[Callable[[Mid], None], str]] = {
    "target-archived": (_archive, "execution_failed"),
    "entry-removed": (_remove_entry, "credential_unavailable"),
    "entry-of-another-tenant": (_other_tenants_entry, "credential_unavailable"),
}
ONTOLOGIES = "/api/v2/ontologies/"


def _refusals(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The code of every credential grant the gateway refuses."""
    codes: list[str] = []
    grant = GatewayClient.credential_grant

    def recorded(self: GatewayClient, lease: JobLease) -> CredentialGrant:
        try:
            return grant(self, lease)
        except GatewayError as exc:
            codes.append(exc.code)
            raise

    monkeypatch.setattr(GatewayClient, "credential_grant", recorded)
    return codes


@pytest.mark.parametrize(
    ("path", "nth", "posts", "steps"),
    [
        (TOKEN_PATH, 1, [], []),
        (ONTOLOGIES, 1, [], []),
        (ONTOLOGIES, 4, [], []),  # the apply's own snapshot: the next request is the first write
        (INSTALLATIONS, 1, [INSTALLATIONS], STEPS[:1]),
        (INSTALLATIONS, 2, [INSTALLATIONS], STEPS[:1]),  # the next request submits the job
    ],
)
@pytest.mark.parametrize("gone", GONE)
def test_a_palantir_apply_sends_nothing_more_once_its_target_or_entry_is_gone(
    mid: Mid,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    gone: str,
    path: str,
    nth: int,
    posts: list[str],
    steps: tuple[str, ...],
) -> None:
    """The cached token included: no later request, no later intent, and the job's named refusal."""
    change, code = GONE[gone]
    deployment = _approved(mid.flow, mid.target, mid.runner)
    codes = _refusals(monkeypatch)
    mid.after(path, change, nth)
    row = mid.job(deployment)
    assert mid.changed and mid.sent == []
    assert [p for p, _ in mid.stack.posts] == posts
    journal = {s["step_key"]: s["state"] for s in mid.flow.get(deployment)["journal"]}
    assert journal == dict.fromkeys(steps, "complete")
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert apply["result"] == {"status": "failed", "error_code": code}
    assert codes == (["target_unavailable"] if gone == "target-archived" else [])
    reason = f"apply failed after a platform write may have happened: {code}"
    assert (row["status"], row["status_reason"]) == (
        ("unknown_outcome", reason) if steps else ("failed", "no platform write was attempted")
    )


@pytest.mark.parametrize("path", [TOKEN_PATH, ONTOLOGIES])
@pytest.mark.parametrize("gone", GONE)
def test_a_palantir_plan_job_sends_nothing_more_once_its_target_or_entry_is_gone(
    mid: Mid, monkeypatch: pytest.MonkeyPatch, gone: str, path: str
) -> None:
    change, code = GONE[gone]
    plan = _ask(mid.flow, mid.target)
    codes = _refusals(monkeypatch)
    mid.after(path, change)
    assert mid.runner.run_once()
    assert mid.changed and mid.sent == []
    failed = plan_row(mid.flow.owner_db, plan["id"])
    assert (failed["status"], failed["status_reason"]) == (
        "plan_failed",
        f"plan job failed: {code}",
    )
    assert codes == (["target_unavailable"] if gone == "target-archived" else [])


@pytest.mark.parametrize(
    ("kind", "path", "nth"),
    [
        ("observe", TOKEN_PATH, 1),
        ("observe", INSTALLATIONS, 1),
        ("observe", JOBS, 1),
        ("verify", JOBS, 1),
        ("verify", ONTOLOGIES, 3),  # inside `ontology-read`
        ("verify", ONTOLOGIES, 5),  # the next request is the frontend asset
    ],
)
@pytest.mark.parametrize("gone", GONE)
def test_a_palantir_observe_or_verify_job_sends_nothing_more_once_its_target_or_entry_is_gone(
    mid: Mid,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    gone: str,
    kind: str,
    path: str,
    nth: int,
) -> None:
    """A refusal is the job's own failure, never a check that failed."""
    change, code = GONE[gone]
    deployment = _approved(mid.flow, mid.target, mid.runner)
    for _ in ("apply",) if kind == "observe" else ("apply", "observe"):
        mid.job(deployment)
    codes = _refusals(monkeypatch)
    mid.after(path, change, nth)
    row = mid.job(deployment)
    assert mid.changed and mid.sent == []
    job = deployment_jobs(owner_db, deployment, kind)[0]
    assert job["result"] == {"status": "failed", "error_code": code}
    assert row["status"] != "verified" and row["verification"] is None
    assert codes == (["target_unavailable"] if gone == "target-archived" else [])


def test_bs_claim_of_an_archived_palantir_stack_gets_no_request_of_as_apply(
    mid: Mid, client: TestClient, world: World, people: People
) -> None:
    """Cross-client: A archives its target mid-apply and B registers the same stack; A's running
    apply sends nothing more to it."""

    def change(mid: Mid) -> None:
        _archive(mid)
        headers = as_user(client, people.owner_b)
        body = sp8_body("palantir", ws_slug(world.b), mid.target["address"], bindings=DEPLOYABLE)
        register(client, headers, world.b.slug, body)
        client.cookies.clear()

    deployment = _approved(mid.flow, mid.target, mid.runner)
    mid.after(ONTOLOGIES, change, 4)
    assert mid.job(deployment)["status"] == "failed"
    assert mid.changed and mid.sent == [] and mid.stack.posts == []


def test_a_palantir_grant_that_ran_out_is_refreshed_and_the_apply_goes_on(mid: Mid) -> None:
    deployment = _approved(mid.flow, mid.target, mid.runner)
    mid.after(INSTALLATIONS, Mid.expire_grant)
    assert mid.job(deployment)["status"] == "observing"
    assert mid.changed and [p for p, _ in mid.stack.posts] == [INSTALLATIONS, JOBS]


def test_a_rotated_palantir_secret_mints_a_new_token_mid_apply(mid: Mid, tmp_path: Path) -> None:
    """A token of the entry's earlier secret is not used again."""

    def rotate(_mid: Mid) -> None:
        secret = tmp_path / "palantir.json.secret"
        assert secret.read_text(encoding="utf-8").strip() == SP8_SECRET
        secret.write_text("rotated-fixture-secret", encoding="utf-8")

    deployment = _approved(mid.flow, mid.target, mid.runner)
    mid.after(ONTOLOGIES, rotate, 4)
    assert mid.job(deployment)["status"] == "observing"
    assert mid.sent[:2] == [TOKEN_PATH, INSTALLATIONS] and mid.sent.count(TOKEN_PATH) == 1


def _rotate(tmp_path: Path) -> None:
    (tmp_path / "palantir.json.secret").write_text("rotated-fixture-secret", encoding="utf-8")


def _verify_refused(
    mid: Mid, owner_db: Database, change: Callable[[Mid], None], path: str, nth: int
) -> None:
    """Verify with its second client refused a token: the job fails and no report is stored."""
    deployment = _approved(mid.flow, mid.target, mid.runner)
    for _ in ("apply", "observe"):
        mid.job(deployment)
    mid.after(path, change, nth)
    row = mid.job(deployment)
    assert mid.changed
    (job,) = deployment_jobs(owner_db, deployment, "verify")
    assert job["result"] == {"status": "failed", "error_code": "execution_failed"}
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")
    assert row["verification"] is None


def test_a_palantir_secret_rotated_inside_verifys_second_mint_is_the_jobs_failure(
    mid: Mid, owner_db: Database, tmp_path: Path
) -> None:
    """Not a failed `ontology-read`; nothing is sent after the mint."""
    _verify_refused(mid, owner_db, lambda _mid: _rotate(tmp_path), TOKEN_PATH, 2)
    assert mid.sent == []


NO_TOKEN = {
    "4xx": (401, b""),
    "5xx": (503, b""),
    "not-json": (200, b"not json"),
    "no-access-token": (200, b'{"token_type": "bearer"}'),
}


@pytest.mark.parametrize("answer", NO_TOKEN)
def test_a_token_endpoint_refusing_verifys_second_mint_is_the_jobs_failure(
    mid: Mid, owner_db: Database, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """Not a failed `ontology-read`; nothing is sent after the refused token request."""
    status, content = NO_TOKEN[answer]

    def refuse(mid: Mid) -> None:
        monkeypatch.setattr(
            mid.stack, "_token", lambda _request: httpx.Response(status, content=content)
        )

    _verify_refused(mid, owner_db, refuse, ONTOLOGIES, 2)  # the first client's last read
    assert mid.sent == [TOKEN_PATH]


@pytest.mark.parametrize(
    ("path", "nth", "posts", "steps"),
    [
        (ONTOLOGIES, 4, [], []),  # the next request is the first write
        (INSTALLATIONS, 2, [INSTALLATIONS], STEPS[:1]),  # the next request submits the job
    ],
)
def test_a_palantir_token_refused_before_a_write_leaves_no_open_intent(
    mid: Mid,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    nth: int,
    posts: list[str],
    steps: tuple[str, ...],
) -> None:
    """The secret is rotated between two steps and the stack refuses the new one."""

    def change(mid: Mid) -> None:
        _rotate(tmp_path)
        monkeypatch.setattr(mid.stack, "_token", lambda _request: httpx.Response(401))

    deployment = _approved(mid.flow, mid.target, mid.runner)
    mid.after(path, change, nth)
    row = mid.job(deployment)
    assert mid.changed and mid.sent == [TOKEN_PATH]
    assert [p for p, _ in mid.stack.posts] == posts
    journal = {s["step_key"]: s["state"] for s in mid.flow.get(deployment)["journal"]}
    assert journal == dict.fromkeys(steps, "complete")
    reason = "apply failed after a platform write may have happened: execution_failed"
    assert (row["status"], row["status_reason"]) == (
        ("unknown_outcome", reason) if steps else ("failed", "no platform write was attempted")
    )


# ------------------------------------------------------------------ the descriptor
class Tampered(GatewayClient):
    """The in-process gateway, serving another artifact, whose digest the context agrees with."""

    def __init__(self, flow: Flow, edit: Callable[[dict[str, bytes]], dict[str, bytes]]) -> None:
        super().__init__("https://testserver", flow.token, http=flow.client)
        self.edit = edit

    def artifact(self, lease: JobLease) -> bytes:
        return create_archive(self.edit(read_archive(super().artifact(lease))))

    def context(self, lease: JobLease) -> JobContext:
        ctx = super().context(lease)
        assert ctx.plan is not None
        digest = hashlib.sha256(self.artifact(lease)).hexdigest()
        return ctx.model_copy(
            update={"plan": ctx.plan.model_copy(update={"artifact_digest": digest})}
        )


def _edited(files: dict[str, bytes], **changes: str) -> dict[str, bytes]:
    value = {**json.loads(files["deployment.json"]), **changes}
    return {
        **files,
        "deployment.json": json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        + b"\n",
    }


TAMPERED: dict[str, tuple[Callable[[dict[str, bytes]], dict[str, bytes]], str]] = {
    "missing": (
        lambda f: {"release.json": f["release.json"], "other.json": b"{}"},
        "execution_failed",
    ),
    "spec-only": (
        lambda f: {**f, **palantir_source.files_for(palantir_source.DELIVERY_SPEC)},
        "execution_failed",
    ),
    "other-release": (lambda f: _edited(f, release_digest="0" * 64), "execution_failed"),
    "other-target": (lambda f: _edited(f, bindings_digest="0" * 64), "plan_stale"),
    "extra-file": (lambda f: {**f, "extra.json": b"{}"}, "execution_failed"),
    "not-canonical": (
        lambda f: {**f, "deployment.json": f["deployment.json"] + b"\n"},
        "execution_failed",
    ),
}


@pytest.mark.parametrize("case", sorted(TAMPERED))
def test_a_tampered_palantir_descriptor_is_refused_before_any_token(
    flow: Flow, tmp_path: Path, owner_db: Database, case: str
) -> None:
    edit, code = TAMPERED[case]
    target, stack, runner = _started(flow, tmp_path)
    runner.client = Tampered(flow, edit)
    plan = _ask(flow, target)
    assert runner.run_once()
    row = plan_row(owner_db, plan["id"])
    assert (row["status"], row["status_reason"]) == ("plan_failed", f"plan job failed: {code}")
    assert stack.tokens == set() and stack.requests == []


# ------------------------------------------------------------------ extra host suffixes
def test_a_palantir_target_under_an_extra_suffix_needs_the_executors_list(
    flow: Flow, settings: Settings, tmp_path: Path, owner_db: Database
) -> None:
    url = f"https://stack-{uuid4().hex[-12:]}.foundry.corp.example"
    cfg = settings.model_copy(update={"delivery_palantir_host_suffixes": ["foundry.corp.example"]})
    with delivery_client(cfg) as client:
        flow = replace(flow, client=client)
        target = palantir_target(flow, url)
        stack = _stack(target)
        creds = sp8_credentials_file(tmp_path, "palantir", url, tenant=flow.tenant)
        platform = FakePlatform(palantir=stack, environment="test")
        for suffixes, status in (((), "plan_failed"), (("foundry.corp.example",), "sealed")):
            plan = _ask(flow, target)
            assert _runner(flow, creds, platform, suffixes=suffixes).run_once()
            row = plan_row(owner_db, plan["id"])
            assert row["status"] == status, row["status_reason"]
            if not suffixes:
                assert row["status_reason"] == "plan job failed: credential_unavailable"
                assert stack.tokens == set() and stack.requests == []
        assert len(stack.tokens) == 1


NESTED = (
    "acme.palantirfoundry.com",
    "a.b.palantirfoundry.com",
    "foundry.corp.example,stack-1.foundry.corp.example",
    "stack-1.foundry.corp.example,foundry.corp.example",
    " Stack-1.Foundry.Corp.Example , foundry.corp.example",
    "*.example.com",
)


def _environ(tmp_path: Path, suffixes: str | None) -> dict[str, str]:
    host = "https://stack-1.palantirfoundry.com"
    return {
        main.API_URL: "https://seth.example.com",
        main.TOKEN: "shx_prefix_executor-token-never-logged",
        main.CREDENTIALS_FILE: str(
            sp8_credentials_file(tmp_path, "palantir", host, tenant=uuid4())
        ),
        **({main.PALANTIR_SUFFIXES: suffixes} if suffixes is not None else {}),
    }


@pytest.mark.parametrize("value", NESTED)
def test_the_executor_refuses_an_invalid_palantir_suffix_list_naming_the_variable_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        main.configure([], _environ(tmp_path, value))
    assert raised.value.code == 2
    err = capsys.readouterr().err.splitlines()[-1]  # the message, after the usage
    assert "SHIFDE_DELIVERY_PALANTIR_HOST_SUFFIXES" in err
    for entry in value.split(","):
        assert entry.strip().lower() not in err.lower()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()),
        ("", ()),
        (" Foundry.Corp.Example , a.b-c.io ", ("foundry.corp.example", "a.b-c.io")),
        ("notpalantirfoundry.com", ("notpalantirfoundry.com",)),
        ("corp.example,xcorp.example", ("corp.example", "xcorp.example")),
    ],
)
def test_the_executor_reads_the_palantir_suffix_list_into_its_broker(
    tmp_path: Path, value: str | None, expected: tuple[str, ...]
) -> None:
    broker = main.configure([], _environ(tmp_path, value)).broker
    assert broker.palantir_host_suffixes == expected


def test_the_palantir_suffixes_count_for_a_palantir_host_only(tmp_path: Path) -> None:
    """A Databricks entry under a listed suffix stays refused."""
    host = "https://adb-1.foundry.corp.example"
    ids = {"job_id": uuid4(), "executor_id": uuid4(), "target_id": uuid4()}
    tenant = uuid4()
    path = tmp_path / "credentials.json"
    entry = {
        "tenant_id": str(tenant),
        "host": host,
        "token": "dapi-never-logged",
        "service_principal": "deployer@example.com",
    }
    path.write_text(json.dumps({"ref": entry}), encoding="utf-8")
    os.chmod(path, 0o600)
    broker = CredentialBroker.from_file(path, palantir_host_suffixes=["foundry.corp.example"])
    grant = CredentialGrant(
        **ids, credential_ref="ref", audience=host, expires_at=utcnow() + timedelta(seconds=30)
    )
    with pytest.raises(CredentialUnavailable):
        broker.token_for(grant, **ids, tenant_id=tenant)


# ------------------------------------------------------------------ credential isolation
HOST_A = "https://stack-a.palantirfoundry.com"
HOST_B = "https://stack-b.palantirfoundry.com"


class Stub:
    """A gateway of one job, in memory: its context and grants; any other call fails the test."""

    def __init__(self, ctx: JobContext, *grants: CredentialGrant) -> None:
        self.ctx, self.grants, self.asked = ctx, list(grants), 0

    def context(self, _lease: JobLease) -> JobContext:
        return self.ctx

    def credential_grant(self, _lease: JobLease) -> CredentialGrant:
        self.asked += 1
        return self.grants.pop(0) if len(self.grants) > 1 else self.grants[0]

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the gateway was called: {name}")


class Isolated:
    """Organization B's executor with a plan job whose target and grant name `HOST_A`, and a file
    whose one Palantir entry is organization B's own for that host, unless changed."""

    def __init__(self, directory: Path, entry_tenant: UUID | None = None) -> None:
        self.tenant, self.executor = uuid4(), uuid4()
        self.target = Target(
            tenant_id=self.tenant,
            id=uuid4(),
            host=HOST_A,
            bindings={**DEPLOYABLE, "allow_preview_apis": "true"},
        )
        self.job = LeasedJob(
            id=uuid4(),
            kind="plan",
            plan_id=uuid4(),
            deployment_id=None,
            target_id=self.target.id,
            deadline=None,
            attempts=1,
            max_attempts=1,
        )
        self.stack = FakeFoundryStack(
            environment="test",
            host=HOST_A,
            frontend_origin=DEPLOYABLE["frontend_origin"],
            ontology_rid=DEPLOYABLE["ontology_rid"],
            namespace_rid=DEPLOYABLE["namespace_rid"],
        )
        self.creds = sp8_credentials_file(
            directory, "palantir", HOST_A, tenant=entry_tenant or self.tenant
        )
        self.runner: Runner | None = None

    def grant(self, **changes: Any) -> CredentialGrant:
        return CredentialGrant.model_validate(
            {
                "job_id": self.job.id,
                "executor_id": self.executor,
                "target_id": self.target.id,
                "credential_ref": "palantir-dev",
                "audience": HOST_A,
                "expires_at": utcnow() + timedelta(seconds=30),
                **changes,
            }
        )

    def plan(self) -> ContextPlan:
        return ContextPlan(
            id=self.job.plan_id or uuid4(),
            tenant_id=self.tenant,
            application_key="sales-app",
            display_name="Sales app",
            artifact_id=uuid4(),
            artifact_digest="e" * 64,
            target_revision=1,
            status="requested",
            steps=None,
            snapshot=None,
            adapter_version=None,
            toolchain_digest=None,
            digest=None,
            expires_at=None,
            release_digest="f" * 64,
        )

    def state(self, *grants: CredentialGrant) -> LeaseState:
        """A lease, and this runner over a stub gateway giving `grants` in order."""
        until = utcnow() + timedelta(seconds=45)
        ctx = JobContext(
            job=self.job,
            target=self.target,
            platform_kind="palantir",
            plan=self.plan(),
            deployment=None,
            owned_resources=[],
        )
        self.stub = Stub(ctx, *grants)
        gateway: Any = self.stub  # Any: stands in for a GatewayClient
        self.runner = Runner(
            gateway,
            CredentialBroker.from_file(self.creds),
            self.executor,
            FakePlatform(palantir=self.stack, environment="test"),
            tenant_id=self.tenant,
        )
        lease = JobLease(
            job=self.job, lease_id=uuid4(), fence=1, lease_until=until, request_id=uuid4()
        )
        return LeaseState(lease=lease, lease_until=until)

    def provider(self, grant: CredentialGrant) -> Callable[[str], FoundryCredentials]:
        """The runner's credential provider with `grant` held."""
        state = self.state(grant)
        state.grant = grant
        assert self.runner is not None
        return self.runner._foundry_provider(state, self.target)

    def refused(self, grant: CredentialGrant) -> None:
        """The job, and a token of a client over the provider alone, are `credential_unavailable`,
        with no token, request or checkpoint."""
        state = self.state(grant)
        assert self.runner is not None
        with pytest.raises(CredentialUnavailable):
            self.runner._execute(state)
        client = self.stack.client(self.target, self.provider(grant))
        with pytest.raises(CredentialUnavailable):
            client.schema(palantir_source.bindings(self.target))
        assert self.stack.tokens == set() and self.stack.requests == []


def test_a_palantir_target_naming_another_organizations_foundry_host_gets_no_token(
    tmp_path: Path,
) -> None:
    """Cross-client: B's target and grant name A's host, and the file holds A's entry for it."""
    side = Isolated(tmp_path, entry_tenant=uuid4())
    side.refused(side.grant())


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": uuid4()},
        {"executor_id": uuid4()},
        {"target_id": uuid4()},
        {"expires_at": utcnow() - timedelta(seconds=1)},
        {"audience": HOST_B},
        {"credential_ref": "another"},
    ],
    ids=lambda c: next(iter(c)),
)
def test_a_mismatched_palantir_grant_gets_no_token(tmp_path: Path, changes: dict[str, Any]) -> None:
    side = Isolated(tmp_path)
    side.refused(side.grant(**changes))


@pytest.mark.parametrize("platform", ["fabric", "docker"])
def test_an_entry_of_another_platform_gets_no_palantir_token(tmp_path: Path, platform: str) -> None:
    side = Isolated(tmp_path)
    entry = sp8_entry(tmp_path, platform, HOST_A, tenant=side.tenant)
    side.creds.write_text(json.dumps({"palantir-dev": entry}), encoding="utf-8")
    side.refused(side.grant())


def test_the_palantir_provider_answers_for_the_grants_audience_only(tmp_path: Path) -> None:
    side = Isolated(tmp_path)
    provider = side.provider(side.grant())
    for host in (HOST_B, "https://example.com", "http://stack-a.palantirfoundry.com", ""):
        with pytest.raises(CredentialUnavailable):
            provider(host)
    assert provider(HOST_A).client_secret == SP8_SECRET  # its own host, normalized
    assert provider("https://STACK-A.palantirfoundry.com:443/").client_secret == SP8_SECRET
    assert SP8_SECRET not in repr(provider(HOST_A))
    assert side.stack.tokens == set()


def test_the_palantir_provider_refreshes_an_expired_grant(tmp_path: Path) -> None:
    side = Isolated(tmp_path)
    expired = side.grant(expires_at=utcnow() - timedelta(seconds=1))
    state = side.state(side.grant())
    state.grant = expired
    assert side.runner is not None
    provider = side.runner._foundry_provider(state, side.target)
    assert provider(HOST_A).client_secret == SP8_SECRET
    assert side.stub.asked == 1 and state.grant is not expired
    provider(HOST_A)
    assert side.stub.asked == 1


def test_every_palantir_job_kind_checks_its_credential_first(tmp_path: Path) -> None:
    for kind in ("plan", "apply", "observe", "verify", "reconcile"):
        side = Isolated(tmp_path, entry_tenant=uuid4())
        side.job = side.job.model_copy(update={"kind": kind})
        side.refused(side.grant())


def test_a_palantir_context_of_another_organization_is_refused(tmp_path: Path) -> None:
    side = Isolated(tmp_path)
    state = side.state(side.grant())
    assert side.runner is not None
    side.runner.tenant_id = uuid4()
    with pytest.raises(ValueError, match="another organization"):
        side.runner._execute(state)
    assert side.stack.tokens == set() and side.stub.asked == 0


def test_a_stale_palantir_context_is_plan_stale_in_the_runner(tmp_path: Path) -> None:
    """The bindings digest is the context target's, its revision included."""
    side = Isolated(tmp_path)
    state = side.state(side.grant())
    files = palantir_source.delivery_files(
        bindings_digest=bindings_digest(side.target.model_copy(update={"revision": 2})),
        release_digest="f" * 64,
    )
    data = create_archive({**files, "release.json": b"{}"})
    side.stub.ctx = side.stub.ctx.model_copy(
        update={
            "plan": side.plan().model_copy(
                update={"artifact_digest": hashlib.sha256(data).hexdigest()}
            )
        }
    )
    side.stub.artifact = lambda _lease: data  # type: ignore[attr-defined]
    assert side.runner is not None
    with pytest.raises(JobFailure) as raised:
        side.runner._execute(state)
    assert raised.value.code == "plan_stale"
    assert side.stack.tokens == set() and side.stack.requests == []


# ------------------------------------------------------------------ the fake and the API
@pytest.mark.parametrize("environment", ["", "production", "staging"])
def test_the_fake_platform_builds_no_palantir_fake_outside_development_and_test(
    environment: str, tmp_path: Path
) -> None:
    side = Isolated(tmp_path)
    platform = FakePlatform(environment=environment)
    with pytest.raises(RuntimeError, match="only in development or test"):
        platform.palantir_adapter(side.target, lambda _host: FoundryCredentials("id", "x"))
    assert platform.palantir is None
    assert FakePlatform(environment="development").palantir_adapter(
        side.target, lambda _host: FoundryCredentials("id", "x")
    )


def test_a_refused_credential_mints_no_palantir_fake_token(tmp_path: Path) -> None:
    side = Isolated(tmp_path)

    def refused(_host: str) -> FoundryCredentials:
        raise CredentialUnavailable

    platform = FakePlatform(palantir=side.stack, environment="test")
    client = platform.palantir_adapter(side.target, refused).client_factory(side.target)
    with pytest.raises(CredentialUnavailable):
        client.schema(palantir_source.bindings(side.target))
    assert side.stack.tokens == set() and side.stack.requests == []


SRC = Path(shifde.__file__).parent


def _imports(path: Path) -> set[str]:
    """Every module `path` imports by name (`from a import b` gives `a` and `a.b`)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_only_the_executor_imports_the_palantir_client_adapter_and_fake() -> None:
    """No API, worker or delivery module reaches a Foundry stack; `palantir.source` is allowed."""
    package = "shifde.adapters.native.palantir"
    closed = {f"{package}.{name}" for name in ("clients", "adapter", "fake")}
    allowed = {SRC / "executor", SRC / "adapters" / "native" / "palantir"}
    importers = set()
    for path in SRC.rglob("*.py"):
        if not allowed & set(path.parents) and _imports(path) & closed:
            importers.add(path.relative_to(SRC).as_posix())
    assert importers == set()
    executor = {p.name for p in (SRC / "executor").glob("*.py") if _imports(p) & closed}
    assert executor == {"credentials.py", "runner.py"}  # the scan finds what it must
    assert f"{package}.source" in _imports(SRC / "delivery" / "plans.py")


def test_only_the_fake_platform_flag_builds_the_fake_platform(tmp_path: Path) -> None:
    """`FakePlatform` (the only builder of the Palantir fake) is built only in `main.py`, behind
    `--fake-platform`; without the flag the platform is real."""
    named = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "FakePlatform(" in path.read_text(encoding="utf-8")
    }
    assert named == {"executor/main.py"}
    environ = {**_environ(tmp_path, None), "SHIFDE_ENV": "development"}
    assert isinstance(main.configure([], environ).platform, RealPlatform)
    built = main.configure(["--fake-platform"], environ).platform
    assert isinstance(built, FakePlatform) and built.environment == "development"


def test_a_palantir_client_error_never_carries_the_secret(tmp_path: Path) -> None:
    side = Isolated(tmp_path)
    provider = side.provider(side.grant())
    client = PalantirClient(
        side.target,
        credential_provider=provider,
        authorize=lambda: provider(HOST_A),
        transport=httpx.MockTransport(lambda _r: httpx.Response(401, text=SP8_SECRET)),
    )
    with pytest.raises(PermissionError) as raised:
        client.schema(palantir_source.bindings(side.target))
    assert SP8_SECRET not in str(raised.value) and SP8_SECRET not in repr(client)
