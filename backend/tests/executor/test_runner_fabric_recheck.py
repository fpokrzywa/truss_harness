"""The grant and the credential entry are checked before every Fabric request of every job kind, a
cached token's too (#407): once the target is archived or the entry is gone, nothing more is sent.
The token endpoint's refusals and unreadable answers end a job before any write, never as an unknown
outcome of a write that was not sent, and never with a token or a body in a log, result or error.

Each runs on the Fabric fake and on the real platform: the runner's own `FabricClient` and
`EntraTokenProvider` over a mock transport. No network is used."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any  # Any: JSON bodies, rows
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from shifde.adapters.native.contracts import CredentialGrant, JobLease, Target, utcnow
from shifde.adapters.native.fabric.adapter import FabricAdapter
from shifde.adapters.native.fabric.clients import (
    CheckedTokens,
    EntraTokenProvider,
    FabricAuthError,
    FabricClient,
)
from shifde.adapters.native.fabric.fake import FakeFabricWorkspace
from shifde.adapters.native.fabric.source import ORIGIN
from shifde.core.db import Database
from shifde.executor.client import GatewayClient, GatewayError
from shifde.executor.runner import FabricCredentialProvider, RealPlatform, Runner
from tests.connections.routes import as_user
from tests.delivery.fabric import (
    approved_deployment,
    fabric_platform,
    fabric_release,
    fabric_runner,
    fabric_target,
    workspace_of,
)
from tests.delivery.routes import (
    Flow,
    deployment_jobs,
    deployment_row,
    make_due,
    owner_sql,
    plan_row,
    register,
    sp8_body,
    targets,
)
from tests.executor.support import SP8_SECRET, ask_plan, sp8_credentials_file
from tests.tenancy.conftest import World
from tests.tenancy.routes import People, ws_slug

LOGIN_HOST = "login.microsoftonline.com"
TOKEN = "entra-fixture-token"
MARK = "unreadable-marker"


class RealOnWorkspace(RealPlatform):
    """The real platform's adapter, its client and token provider on recording transports."""

    def __init__(self, transport: Callable[[], httpx.BaseTransport]) -> None:
        self.transport = transport

    def fabric_adapter(
        self, target: Target, credential_provider: FabricCredentialProvider
    ) -> FabricAdapter:
        adapter = super().fabric_adapter(target, credential_provider)
        build = adapter.client_factory

        def on_workspace(t: Target) -> FabricClient:
            client = build(t)
            client.transport = self.transport()
            tokens = client.token_provider
            assert isinstance(tokens, CheckedTokens)
            provider_of = tokens._provider_of

            def recorded(now: Any) -> EntraTokenProvider:  # Any: the runner's FabricCredentials
                provider = provider_of(now)
                assert isinstance(provider, EntraTokenProvider)
                provider.transport = self.transport()
                return provider

            tokens._provider_of = recorded
            return client

        adapter.client_factory = on_workspace
        return adapter


def _token_answer(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})


def _short_token(_request: httpx.Request) -> httpx.Response:
    """A token inside the expiry margin at once: the provider mints again on every use."""
    return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 60})


@dataclass
class Mid:
    """A runner whose job is changed under it, on the fake or the real platform's client."""

    flow: Flow
    directory: Path
    target: dict[str, Any]  # a TargetOut body
    fake: FakeFabricWorkspace
    runner: Runner = field(init=False)
    real: bool
    requests: int = 0  # the Fabric requests of the current job
    minted: int = 0
    sent: list[str] = field(default_factory=list)  # what was requested after the change
    changed: bool = False
    change: Callable[[Mid], None] | None = None
    change_after: int = 0
    change_in_mint: int = 0
    token_answer: Callable[[httpx.Request], httpx.Response] = _token_answer

    def __post_init__(self) -> None:
        creds = sp8_credentials_file(
            self.directory, "fabric", self.target["address"], tenant=self.flow.tenant
        )
        self.fake.transport = httpx.MockTransport(self.handle)
        platform = (
            RealOnWorkspace(lambda: self.fake.transport)
            if self.real
            else fabric_platform(self.fake)
        )
        if self.real:
            self.fake.add_workspace(workspace_of(self.target))
        self.runner = fabric_runner(self.flow.client, self.flow.token, creds, platform)

    def handle(self, request: httpx.Request) -> httpx.Response:
        login = request.url.host == LOGIN_HOST
        if self.changed:
            self.sent.append("token" if login else request.url.path)
        if login:
            self.minted += 1
            answer = self.token_answer(request)
            self._change_at(self.change_in_mint, self.minted)
            return answer
        if self.real:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        self.requests += 1
        answer = self.fake.handle(request)
        self._change_at(self.change_after, self.requests)
        return answer

    def _change_at(self, when: int, now: int) -> None:
        if self.change is not None and not self.changed and now == when:
            self.change(self)
            self.changed = True

    def after(self, nth: int, change: Callable[[Mid], None]) -> None:
        """`change` happens once, right after the workspace answered the `nth` request from now."""
        self.requests, self.minted, self.sent, self.changed = 0, 0, [], False
        self.change_after, self.change_in_mint, self.change = nth, 0, change

    def in_mint(self, nth: int, change: Callable[[Mid], None]) -> None:
        """`change` happens once, inside the `nth` token request from now."""
        self.after(0, change)
        self.change_in_mint = nth

    def approved(self) -> str:
        release = fabric_release(self.flow.owner_db, self.target["id"])
        return approved_deployment(self.flow, self.runner, self.target, release.id)

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
    return Mid(
        flow, tmp_path, fabric_target(flow), FakeFabricWorkspace(), real=request.param == "real"
    )


@pytest.fixture
def real(flow: Flow, tmp_path: Path) -> Mid:
    return Mid(flow, tmp_path, fabric_target(flow), FakeFabricWorkspace(), real=True)


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


def _other_entra_tenants_entry(mid: Mid) -> None:
    entries = mid.runner.broker._entries
    for ref, entry in list(entries.items()):
        entries[ref] = entry.model_copy(update={"entra_tenant_id": str(uuid4())})


def _rotate(mid: Mid) -> None:
    (mid.directory / "fabric.json.secret").write_text("rotated-fixture-secret", encoding="utf-8")


# how the job ends; an archived target is the gateway's `target_unavailable`
GONE: dict[str, tuple[Callable[[Mid], None], str]] = {
    "target-archived": (_archive, "execution_failed"),
    "entry-removed": (_remove_entry, "credential_unavailable"),
    "entry-of-another-tenant": (_other_tenants_entry, "credential_unavailable"),
    "entry-of-another-entra-tenant": (_other_entra_tenants_entry, "credential_unavailable"),
}


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


# the apply's requests: workspace, items; lakehouse create, read; notebook create, operation,
# operation result, read, definition; run
APPLY_REQUESTS = 10
# after that many requests: the journal's complete steps and the open one
LAKE, BOOK = "lakehouse-metadata", "notebook-metadata"
JOURNAL: dict[int, tuple[list[str], list[str]]] = {
    1: ([], []),
    2: ([], []),  # the next request is the first write
    3: ([f"{LAKE}-submit"], []),
    4: ([f"{LAKE}-submit", f"{LAKE}-complete"], []),
    5: ([f"{LAKE}-submit", f"{LAKE}-complete", f"{BOOK}-submit"], []),
    # a poll turn of the accepted notebook: its `-complete` step reads only
    6: ([f"{LAKE}-submit", f"{LAKE}-complete", f"{BOOK}-submit"], [f"{BOOK}-complete"]),
    8: ([f"{LAKE}-submit", f"{LAKE}-complete", f"{BOOK}-submit", f"{BOOK}-complete"], []),
    9: ([f"{LAKE}-submit", f"{LAKE}-complete", f"{BOOK}-submit", f"{BOOK}-complete"], []),
    10: (
        [
            f"{LAKE}-submit",
            f"{LAKE}-complete",
            f"{BOOK}-submit",
            f"{BOOK}-complete",
            "fabric-verification-submit",
        ],
        [],
    ),
}


@pytest.mark.parametrize("nth", JOURNAL)
@pytest.mark.parametrize("gone", GONE)
def test_a_fabric_apply_sends_nothing_more_once_its_target_or_entry_is_gone(
    mid: Mid, owner_db: Database, monkeypatch: pytest.MonkeyPatch, gone: str, nth: int
) -> None:
    """The cached token included: no later request, poll turn or token request, no later intent
    of a write, and the job's named refusal."""
    change, code = GONE[gone]
    deployment = mid.approved()
    codes = _refusals(monkeypatch)
    mid.after(nth, change)
    row = mid.job(deployment)
    assert mid.changed and mid.sent == []
    assert mid.minted == (1 if mid.real else 0)
    complete, opened = JOURNAL[nth]
    journal = {s["step_key"]: s["state"] for s in mid.flow.get(deployment)["journal"]}
    assert journal == {**dict.fromkeys(complete, "complete"), **dict.fromkeys(opened, "intent")}
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert apply["result"] == {"status": "failed", "error_code": code}
    assert codes == (["target_unavailable"] if gone == "target-archived" else [])
    reason = f"apply failed after a platform write may have happened: {code}"
    assert (row["status"], row["status_reason"]) == (
        ("unknown_outcome", reason) if complete else ("failed", "no platform write was attempted")
    )


def test_the_apply_cases_cover_the_whole_apply(mid: Mid) -> None:
    deployment = mid.approved()
    mid.after(0, lambda _mid: None)
    assert mid.job(deployment)["status"] == "observing"
    assert mid.requests == APPLY_REQUESTS == max(JOURNAL)


@pytest.mark.parametrize("gone", GONE)
def test_a_fabric_plan_job_sends_nothing_more_once_its_target_or_entry_is_gone(
    mid: Mid, monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    change, code = GONE[gone]
    release = fabric_release(mid.flow.owner_db, mid.target["id"])
    plan = ask_plan(mid.flow, mid.target["id"], release_id=release.id)
    codes = _refusals(monkeypatch)
    mid.after(1, change)
    assert mid.runner.run_once()
    assert mid.changed and mid.sent == []
    failed = plan_row(mid.flow.owner_db, plan["id"])
    assert (failed["status"], failed["status_reason"]) == (
        "plan_failed",
        f"plan job failed: {code}",
    )
    assert codes == (["target_unavailable"] if gone == "target-archived" else [])


@pytest.mark.parametrize("nth", [1, 2, 3])  # lakehouse, notebook, definition; then the job
@pytest.mark.parametrize("kind", ["observe", "verify"])
@pytest.mark.parametrize("gone", GONE)
def test_a_fabric_observe_or_verify_job_sends_nothing_more_once_its_target_or_entry_is_gone(
    mid: Mid,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    gone: str,
    kind: str,
    nth: int,
) -> None:
    """A refusal is the job's own failure, never a check that failed."""
    change, code = GONE[gone]
    deployment = mid.approved()
    for _ in ("apply",) if kind == "observe" else ("apply", "observe"):
        mid.job(deployment)
    codes = _refusals(monkeypatch)
    mid.after(nth, change)
    row = mid.job(deployment)
    assert mid.changed and mid.sent == []
    job = deployment_jobs(owner_db, deployment, kind)[0]
    assert job["result"] == {"status": "failed", "error_code": code}
    assert row["status"] != "verified" and row["verification"] is None
    assert codes == (["target_unavailable"] if gone == "target-archived" else [])


def test_bs_claim_of_an_archived_fabric_workspace_gets_no_request_of_as_apply(
    mid: Mid, client: TestClient, world: World, people: People
) -> None:
    """Cross-client: A archives its target mid-apply and B registers the same workspace; A's
    running apply sends nothing more to it."""

    def change(mid: Mid) -> None:
        _archive(mid)
        headers = as_user(client, people.owner_b)
        body = sp8_body("fabric", ws_slug(world.b), mid.target["address"])
        register(client, headers, world.b.slug, body)
        client.cookies.clear()

    deployment = mid.approved()
    mid.after(2, change)
    assert mid.job(deployment)["status"] == "failed"
    assert mid.changed and mid.sent == [] and mid.fake.writes == []


def test_a_fabric_grant_that_ran_out_is_refreshed_and_the_apply_goes_on(mid: Mid) -> None:
    deployment = mid.approved()
    mid.after(2, lambda mid: mid.expire_grant())
    assert mid.job(deployment)["status"] == "observing"
    assert len(mid.sent) == APPLY_REQUESTS - 2
    assert mid.minted == (1 if mid.real else 0)  # one token, used ten times


def test_a_rotated_fabric_secret_mints_a_new_token_mid_apply(real: Mid) -> None:
    deployment = real.approved()
    real.after(2, _rotate)
    assert real.job(deployment)["status"] == "observing"
    assert real.sent[0] == "token" and real.sent.count("token") == 1
    assert len(real.sent) == 1 + APPLY_REQUESTS - 2


# ------------------------------------------------------------------ the two checks around a mint
JOBS = {"plan": 0, "apply": 0, "observe": 1, "verify": 2}  # a kind -> the jobs before it
BEFORE = ("apply", "observe")


def _due(real: Mid, kind: str) -> Callable[[], str]:
    """What runs the flow's next job of `kind`, which fails; it answers the job's error code."""
    flow = real.flow
    if kind == "plan":
        release = fabric_release(flow.owner_db, real.target["id"])
        plan = ask_plan(flow, real.target["id"], release_id=release.id)

        def run_plan() -> str:
            assert real.runner.run_once()
            row = plan_row(flow.owner_db, plan["id"])
            assert row["status"] == "plan_failed"
            return str(row["status_reason"]).removeprefix("plan job failed: ")

        return run_plan
    deployment = real.approved()
    for _ in BEFORE[: JOBS[kind]]:
        real.job(deployment)

    def run() -> str:
        row = real.job(deployment)
        assert row["verification"] is None  # never a failed check
        job = deployment_jobs(flow.owner_db, deployment, kind)[0]
        assert job["result"]["status"] == "failed"
        return str(job["result"]["error_code"])

    return run


@pytest.mark.parametrize("kind", JOBS)
@pytest.mark.parametrize("gone", GONE)
def test_a_due_mint_of_a_gone_target_or_entry_sends_no_token_request(
    real: Mid, gone: str, kind: str
) -> None:
    """The first check: the token is due for renewal, and the secret is not sent for a job that
    may no longer use the target."""
    change, code = GONE[gone]
    run = _due(real, kind)
    real.token_answer = _short_token
    real.after(1, change)
    assert run() == code
    assert real.changed and real.sent == []


@pytest.mark.parametrize("nth", [1, 2])
@pytest.mark.parametrize("kind", JOBS)
@pytest.mark.parametrize("gone", GONE)
def test_a_target_or_entry_gone_inside_a_mint_gets_no_request_with_its_token(
    real: Mid, gone: str, kind: str, nth: int
) -> None:
    """The second check: the token came after the target or the entry was gone."""
    change, code = GONE[gone]
    run = _due(real, kind)
    real.token_answer = _short_token
    real.in_mint(nth, change)
    assert run() == code
    assert real.changed and real.sent == [] and real.requests == nth - 1


@pytest.mark.parametrize("kind", JOBS)
def test_a_secret_rotated_inside_a_mint_gets_no_request_with_its_token(
    real: Mid, kind: str
) -> None:
    run = _due(real, kind)
    real.in_mint(1, _rotate)
    assert run() == "execution_failed"
    assert real.changed and real.sent == [] and real.requests == 0


# ------------------------------------------------------------------ the stored address
@pytest.mark.parametrize("kind", JOBS)
def test_a_stored_address_of_another_origin_gets_no_request_and_no_token(
    real: Mid, owner_db: Database, kind: str
) -> None:
    """Registration never stores one. The entry's host is always the Fabric origin, so the broker's
    audience check ends the job before the artifact, the adapter, any request and any token."""
    run = _due(real, kind)
    owner_sql(
        owner_db,
        "UPDATE shifde.delivery_targets SET address = :a WHERE id = :g",
        a=f"https://other.example.com/v1/workspaces/{workspace_of(real.target)}",
        g=real.target["id"],
    )
    real.after(0, lambda _mid: None)
    assert run() == "credential_unavailable"
    assert (real.requests, real.minted) == (0, 0)


# ------------------------------------------------------------------ the token endpoint's answers
def _raises(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def answer(_request: httpx.Request) -> httpx.Response:
        raise error

    return answer


def _undecodable(status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Encoding": "gzip"},
        stream=httpx.ByteStream(f"{MARK} is not gzip".encode()),
    )


def _redirect(location: str) -> httpx.Response:
    """A 302 whose `Location` httpx cannot turn into the next request."""
    return httpx.Response(302, headers={"Location": location})


def _given(token: object, lifetime: str = "3600") -> Callable[[httpx.Request], httpx.Response]:
    body = f'{{"access_token": {token}, "expires_in": {lifetime}}}'.encode()
    return lambda _request: httpx.Response(200, content=body)


NO_TOKEN: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "4xx": lambda _r: httpx.Response(401, text=MARK),
    "5xx": lambda _r: httpx.Response(503, text=MARK),
    "redirect": lambda _r: httpx.Response(302, headers={"Location": f"https://{MARK}.example"}),
    "redirect-unparsable": lambda _r: _redirect(f"https://{MARK}\x00.example"),
    "redirect-bad-idna": lambda _r: _redirect(f"https://xn--{MARK}-zz.example/"),
    "not-json": lambda _r: httpx.Response(200, text=MARK),
    "not-utf8": lambda _r: httpx.Response(200, content=MARK.encode() + b"\xff\xfe"),
    "not-an-object": lambda _r: httpx.Response(200, json=[MARK]),
    "nested": lambda _r: httpx.Response(200, content=b"[" * 60_000),
    "no-access-token": lambda _r: httpx.Response(200, json={"expires_in": 3600, "x": MARK}),
    "undecodable": lambda _r: _undecodable(),
    "token-with-a-newline": _given(f'"{MARK}\\n"'),
    "token-with-a-space": _given(f'"{MARK} {MARK}"'),
    "token-not-ascii": _given(f'"{MARK}\\u00e9"'),
    "token-not-text": _given("7"),
    "no-lifetime": _given(f'"{MARK}"', "null"),
    "lifetime-not-a-number": _given(f'"{MARK}"', "NaN"),
    "timeout": _raises(httpx.ConnectTimeout("no answer")),
    "lost": _raises(httpx.RemoteProtocolError("connection lost")),
}


def _clean(caplog: pytest.LogCaptureFixture, flow: Flow, owner_db: Database, plan: str) -> None:
    for secret in (MARK, TOKEN, SP8_SECRET, "rotated-fixture-secret"):
        assert secret not in caplog.text
        assert secret not in str(deployment_jobs(owner_db, plan))
        assert secret not in str(flow.get(plan))


@pytest.mark.parametrize("answer", NO_TOKEN)
def test_a_fabric_token_refused_before_the_first_write_is_no_unknown_outcome(
    real: Mid, owner_db: Database, caplog: pytest.LogCaptureFixture, answer: str
) -> None:
    """The secret is rotated after the apply's own snapshot and the endpoint gives no token: no
    intent, no write, and the deployment is `failed`."""
    caplog.set_level(logging.INFO)
    deployment = real.approved()

    def change(mid: Mid) -> None:
        _rotate(mid)
        mid.token_answer = NO_TOKEN[answer]

    real.after(2, change)
    row = real.job(deployment)
    assert real.changed and real.sent == ["token"] and real.fake.writes == []
    assert real.flow.get(deployment)["journal"] == []
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert apply["result"] == {"status": "failed", "error_code": "execution_failed"}
    assert (row["status"], row["status_reason"]) == ("failed", "no platform write was attempted")
    _clean(caplog, real.flow, owner_db, deployment)


@pytest.mark.parametrize("answer", NO_TOKEN)
def test_a_fabric_token_refused_later_leaves_no_open_intent_of_a_write(
    real: Mid, owner_db: Database, caplog: pytest.LogCaptureFixture, answer: str
) -> None:
    """Between the lakehouse and the notebook: the notebook's write has no intent."""
    caplog.set_level(logging.INFO)
    deployment = real.approved()

    def change(mid: Mid) -> None:
        _rotate(mid)
        mid.token_answer = NO_TOKEN[answer]

    real.after(4, change)
    assert real.job(deployment)["status"] == "unknown_outcome"  # the lakehouse was written
    assert real.sent == ["token"] and len(real.fake.writes) == 1
    journal = {s["step_key"]: s["state"] for s in real.flow.get(deployment)["journal"]}
    assert journal == dict.fromkeys([f"{LAKE}-submit", f"{LAKE}-complete"], "complete")
    _clean(caplog, real.flow, owner_db, deployment)


def _rotating(mid: Mid) -> Callable[[httpx.Request], httpx.Response]:
    """A token answer whose mint sees the secret rotated: the second check refuses it."""

    def answer(request: httpx.Request) -> httpx.Response:
        _rotate(mid)
        return _short_token(request)

    return answer


# the token request after begin()'s mint for the lakehouse write
AFTER_BEGIN: dict[str, Callable[[Mid], Callable[[httpx.Request], httpx.Response]]] = {
    "redirect": lambda _mid: NO_TOKEN["redirect"],
    "redirect-unparsable": lambda _mid: NO_TOKEN["redirect-unparsable"],
    "secret-rotated-inside-the-mint": _rotating,
}


@pytest.mark.parametrize("refusal", AFTER_BEGIN)
def test_a_fabric_write_is_sent_with_the_token_its_begin_checked(
    real: Mid, owner_db: Database, caplog: pytest.LogCaptureFixture, refusal: str
) -> None:
    """Every use mints: the token request after begin()'s mint is refused. The write went out with
    begin's token, so the refusal comes at the next `begin`, before its intent."""
    caplog.set_level(logging.INFO)
    deployment = real.approved()
    real.token_answer = _short_token

    def change(mid: Mid) -> None:
        mid.token_answer = AFTER_BEGIN[refusal](mid)

    real.in_mint(3, change)  # the workspace, the items, then begin()'s mint for the write
    real.job(deployment)
    assert real.changed and len(real.fake.writes) == 1
    assert real.sent[1:] == ["token"] and real.sent[0] != "token"  # the write, with begin's token
    journal = {s["step_key"]: s["state"] for s in real.flow.get(deployment)["journal"]}
    assert "intent" not in journal.values()
    assert journal == {f"{LAKE}-submit": "complete"}
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert apply["result"] == {"status": "failed", "error_code": "execution_failed"}
    _clean(caplog, real.flow, owner_db, deployment)


@pytest.mark.parametrize("answer", NO_TOKEN)
def test_a_fabric_token_refused_inside_verify_is_the_jobs_failure(
    real: Mid, owner_db: Database, caplog: pytest.LogCaptureFixture, answer: str
) -> None:
    """Not a failed `native-items` or `runtime-health`: no report is stored."""
    caplog.set_level(logging.INFO)
    deployment = real.approved()
    for _ in ("apply", "observe"):
        real.job(deployment)

    def change(mid: Mid) -> None:
        _rotate(mid)
        mid.token_answer = NO_TOKEN[answer]

    real.after(1, change)
    row = real.job(deployment)
    assert real.changed and real.sent == ["token"]
    (job,) = deployment_jobs(owner_db, deployment, "verify")
    assert job["result"] == {"status": "failed", "error_code": "execution_failed"}
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")
    assert row["verification"] is None
    _clean(caplog, real.flow, owner_db, deployment)


@pytest.mark.parametrize("body", ["{}", "x"])
def test_a_malformed_grant_answer_inside_verify_is_the_jobs_failure(
    mid: Mid, owner_db: Database, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """The gateway answers the grant refresh 200 with no grant: not a failed check."""
    deployment = mid.approved()
    for _ in ("apply", "observe"):
        mid.job(deployment)
    send = GatewayClient._send

    def answered(self: GatewayClient, method: str, path: str, **options: Any) -> httpx.Response:
        if mid.changed and path.endswith("/credential-grant"):
            return httpx.Response(200, content=body.encode())
        return send(self, method, path, **options)

    monkeypatch.setattr(GatewayClient, "_send", answered)
    mid.after(1, lambda mid: mid.expire_grant())
    row = mid.job(deployment)
    assert mid.changed
    (job,) = deployment_jobs(owner_db, deployment, "verify")
    assert job["result"] == {"status": "failed", "error_code": "execution_failed"}
    assert row["verification"] is None
    assert (row["status"], row["status_reason"]) == ("failed", "verification failed")


@pytest.mark.parametrize("answer", NO_TOKEN)
@pytest.mark.parametrize("kind", ["plan", "observe"])
def test_a_fabric_token_refused_inside_a_plan_or_observe_is_the_jobs_failure(
    real: Mid, kind: str, answer: str
) -> None:
    run = _due(real, kind)

    def change(mid: Mid) -> None:
        _rotate(mid)
        mid.token_answer = NO_TOKEN[answer]

    real.after(1, change)
    assert run() == "execution_failed"
    assert real.changed and real.sent == ["token"]


@pytest.mark.parametrize("answer", NO_TOKEN)
def test_no_fabric_token_error_carries_the_answer(answer: str) -> None:
    provider = EntraTokenProvider(
        str(uuid4()),
        str(uuid4()),
        lambda: SP8_SECRET,
        transport=httpx.MockTransport(NO_TOKEN[answer]),
    )
    with pytest.raises(FabricAuthError) as failed:
        provider()
    assert failed.value.__cause__ is None and failed.value.__context__ is None
    error: BaseException | None = failed.value
    while error is not None:
        for text in (repr(error), str(error), str(vars(error))):
            assert MARK not in text and SP8_SECRET not in text
        error = error.__cause__
    assert provider._token is None


def test_a_fabric_token_is_never_kept_longer_than_a_day() -> None:
    for lifetime in ("1e999", "9" * 400):
        answer = _given('"t"', lifetime)
        provider = EntraTokenProvider(
            str(uuid4()), str(uuid4()), lambda: "s", transport=httpx.MockTransport(answer)
        )
        assert provider._fetch() == ("t", 24 * 3600)


# ------------------------------------------------------------------ an answer of the workspace
ANSWERS: dict[str, Callable[[], httpx.Response]] = {
    "not-json": lambda: httpx.Response(200, text=MARK),
    "not-utf8": lambda: httpx.Response(200, content=MARK.encode() + b"\xff\xfe"),
    "not-an-object": lambda: httpx.Response(200, json=[MARK]),
    "nested": lambda: httpx.Response(200, content=b"[" * 60_000),
    "undecodable": lambda: _undecodable(),
    "4xx": lambda: httpx.Response(403, text=MARK),
    "5xx": lambda: httpx.Response(500, text=MARK),
}


@pytest.mark.parametrize("answer", ANSWERS)
def test_an_unreadable_fabric_answer_ends_an_apply_before_any_write(
    mid: Mid,
    owner_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: str,
) -> None:
    caplog.set_level(logging.INFO)
    deployment = mid.approved()
    monkeypatch.setattr(mid.fake, "handle", lambda _request: ANSWERS[answer]())
    row = mid.job(deployment)
    assert (row["status"], row["status_reason"]) == ("failed", "no platform write was attempted")
    assert mid.fake.writes == [] and mid.flow.get(deployment)["journal"] == []
    (apply,) = deployment_jobs(owner_db, deployment, "apply")
    assert apply["result"] == {"status": "failed", "error_code": "execution_failed"}
    _clean(caplog, mid.flow, owner_db, deployment)


@pytest.mark.parametrize("answer", ANSWERS)
def test_no_fabric_answer_error_carries_the_answer(answer: str) -> None:
    client = FabricClient(
        Target(tenant_id=uuid4(), host=ORIGIN),
        lambda: TOKEN,
        transport=httpx.MockTransport(lambda _request: ANSWERS[answer]()),
    )
    with pytest.raises((RuntimeError, ValueError, httpx.DecodingError)) as failed:
        client.workspace("w")
    error: BaseException | None = failed.value
    while error is not None:
        for text in (repr(error), str(error), str(getattr(error, "doc", ""))):
            assert MARK not in text and TOKEN not in text
        error = error.__cause__ or error.__context__


# ------------------------------------------------------------------ CheckedTokens
class Refused(Exception):
    pass


def test_a_token_is_kept_only_after_the_second_check() -> None:
    answers: list[Any] = ["a", Refused(), "a", "a", "a", "a"]
    made: list[str] = []

    def authorize() -> str:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return str(answer)

    def provider_of(now: str) -> Callable[[], str]:
        made.append(now)
        token = f"token-{len(made)}"
        return lambda: token

    tokens = CheckedTokens(authorize, provider_of)
    with pytest.raises(Refused):
        tokens()
    assert tokens() == "token-2"  # the first provider's token was not kept
    assert tokens() == "token-2" and made == ["a", "a"]


def test_a_token_of_credentials_that_changed_is_not_used() -> None:
    answers = ["a", "a", "b", "b", "b", "c", "c", "c"]
    made: list[str] = []

    def provider_of(now: str) -> Callable[[], str]:
        made.append(now)
        return lambda: f"token-of-{now}"

    tokens = CheckedTokens(lambda: answers.pop(0), provider_of)
    assert [tokens(), tokens()] == ["token-of-a", "token-of-b"]
    with pytest.raises(FabricAuthError):  # changed while the token was got
        tokens()
    assert tokens() == "token-of-c" and made == ["a", "b", "c"]


def test_a_refused_first_check_mints_nothing_and_drops_the_kept_token() -> None:
    answers: list[Any] = ["a", "a", Refused(), "a", "a"]
    made: list[str] = []

    def authorize() -> str:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return str(answer)

    def provider_of(now: str) -> Callable[[], str]:
        made.append(now)
        return lambda: "token"

    tokens = CheckedTokens(authorize, provider_of)
    tokens()
    with pytest.raises(Refused):
        tokens()
    assert made == ["a"]
    assert tokens() == "token" and made == ["a", "a"]
