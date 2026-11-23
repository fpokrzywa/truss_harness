"""Egress of the `databricks` adapter (CM-3, issue #161; design 2026-11-22 section 7.4): refused
hosts at normalization, no redirects, private resolution under a production installation layer and
a client policy, the response cap, and the deadline that cancels a statement. Fakes only."""

from __future__ import annotations

import pytest

from shifde.adapters.base import AdapterFailure, AdapterRules, ConfigRefused, ErrorClass
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import SELECT_ONE, FakeDatabricksWorkspace
from shifde.core.egress import LOOPBACK_NETWORKS, EgressPolicy
from tests.adapters.dataplatforms.helpers import conn, ctx_for

ADAPTER = DatabricksAdapter()


@pytest.mark.parametrize(
    "host",
    [
        "https://ws1.evil.example",
        "https://203.0.113.7",
        "http://ws1.cloud.databricks.com",
        "https://ws1.cloud.databricks.com:8443",
        "https://reader@ws1.cloud.databricks.com",
    ],
    ids=["suffix", "ip-literal", "http", "port", "userinfo"],
)
def test_refused_hosts_never_get_stored(host: str) -> None:
    with pytest.raises(ConfigRefused):
        ADAPTER.normalize_config({"host": host}, AdapterRules())


def test_a_redirect_is_refused_not_followed(fake: FakeDatabricksWorkspace) -> None:
    fake.faults.add("redirect")
    session = ADAPTER.open(ctx_for(fake), conn())
    with pytest.raises(AdapterFailure) as raised:
        session.invoke({"sql": SELECT_ONE})
    assert raised.value.error_class is ErrorClass.EGRESS
    assert "redirect" in raised.value.message
    assert {r.headers["host"] for r in fake.requests} == {fake.host}  # nothing went elsewhere
    assert len(fake.requests) == 1


def test_private_resolution_is_refused_under_a_client_policy(fake: FakeDatabricksWorkspace) -> None:
    ctx = ctx_for(
        fake,
        env="production",
        layers=[EgressPolicy(allow_private=False)],
        resolver=lambda host: ["10.0.0.5"],
    )
    out = ADAPTER.test(ctx, conn())
    checks = {c.name: c for c in out.checks}
    assert out.error_class is ErrorClass.EGRESS and checks["egress"].status == "fail"
    assert "private" in checks["egress"].detail
    for call in (ADAPTER.discover, lambda c, k: ADAPTER.open(c, k).invoke({"sql": SELECT_ONE})):
        with pytest.raises(AdapterFailure) as raised:
            call(ctx, conn())
        assert raised.value.error_class is ErrorClass.EGRESS
    assert fake.requests == []


def test_loopback_passes_only_the_development_test_layer(fake: FakeDatabricksWorkspace) -> None:
    assert ADAPTER.test(ctx_for(fake), conn()).passed
    out = ADAPTER.test(ctx_for(fake, env="production"), conn())
    assert out.error_class is ErrorClass.EGRESS
    assert len(fake.requests) == 3  # only the development/test run reached the fake


def test_the_response_cap_applies(fake: FakeDatabricksWorkspace) -> None:
    fake.faults.add("oversize")
    fake.oversize_bytes = 5_000
    # a client policy that admits the loopback fake and caps responses at 4 KiB
    cap = EgressPolicy(allowed_cidrs=LOOPBACK_NETWORKS, max_response_bytes=4_096)
    ctx = ctx_for(fake, layers=[cap])
    with pytest.raises(AdapterFailure) as raised:
        ADAPTER.discover(ctx, conn())
    assert raised.value.error_class is ErrorClass.UPSTREAM
    assert "size" in raised.value.message


def test_the_deadline_cancels_the_statement(fake: FakeDatabricksWorkspace) -> None:
    fake.faults.add("slow")
    session = ADAPTER.open(ctx_for(fake, deadline=2.0), conn(statement_timeout_seconds=50))
    with pytest.raises(AdapterFailure) as raised:
        session.invoke({"sql": SELECT_ONE})
    assert raised.value.error_class is ErrorClass.TIMEOUT
    assert [p for p in fake.paths() if p[1].endswith("/cancel")] != []
    assert fake.paths()[-1][1].endswith("/cancel")
