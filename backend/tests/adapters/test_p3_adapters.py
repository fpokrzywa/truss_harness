"""Adapter registry, conformance suites, recorded evidence and derived maturity (FR-VND-004/005/006)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import timedelta
from importlib.metadata import entry_points
from typing import Any, ClassVar

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.adapters import evidence
from shifde.adapters.base import (
    AdapterDescriptor,
    Capability,
    ConfigError,
    ConnectionConfig,
    DiscoveryResult,
    ExecContext,
    ProbeOutcome,
)
from shifde.adapters.conformance import (
    Check,
    ConformanceSuite,
    SuiteSkipped,
    expect,
    run_suite,
)
from shifde.adapters.maturity import adapter_capabilities, capability_maturity
from shifde.adapters.registry import BUILTIN, ENTRY_POINT_GROUP, AdapterRegistry
from shifde.adapters.suites import SUITES, AnthropicLiveSmoke, PostgresSuite
from shifde.core.clock import utcnow
from shifde.core.db import Database

REGISTRY = AdapterRegistry.load(include_fake=True)


# ------------------------------------------------------------------ registry
def test_builtin_adapters_are_declared_as_entry_points_and_all_load() -> None:
    declared = {ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)}
    assert set(BUILTIN) <= declared  # pyproject.toml declares every built-in
    assert set(REGISTRY.keys()) >= {
        "rest.http",
        "mcp_http",
        "postgres",
        "fake",
        "openai_compatible",
        "anthropic",
        "databricks",
        "snowflake",
        "fabric",
    }


def test_descriptors_carry_connection_type_secret_fields_and_schema() -> None:
    types = {d.key: d.connection_type for d in REGISTRY.descriptors()}
    assert types == {
        "rest.http": "rest",
        "mcp_http": "mcp",
        "postgres": "database",
        "fake": "model_api",
        "openai_compatible": "model_api",
        "anthropic": "model_api",
        "databricks": "data_platform",
        "snowflake": "data_platform",
        "fabric": "data_platform",
    }
    for d in REGISTRY.descriptors():
        assert d.config_schema["type"] == "object"
        assert "validated" not in (d.declared_maturity,)  # code can never claim `validated`
    assert REGISTRY.descriptor("postgres").secret_fields == ("password",)  # type: ignore[union-attr]
    assert not REGISTRY.descriptor("fabric").implemented  # type: ignore[union-attr]
    assert REGISTRY.descriptor("anthropic").declared_maturity == "experimental"  # type: ignore[union-attr]


def test_duplicate_or_malformed_adapters_are_refused() -> None:
    from shifde.adapters.rest_http import RestHttpAdapter

    with pytest.raises(ValueError, match="duplicate"):
        AdapterRegistry([RestHttpAdapter, RestHttpAdapter])
    with pytest.raises(ValueError, match="no AdapterDescriptor"):
        AdapterRegistry([object])


def test_planned_adapters_refuse_to_operate() -> None:
    from shifde.core.errors import AdapterNotImplemented

    adapter = REGISTRY.get("fabric")
    cfg = ConnectionConfig(None, "x", "fabric", {"endpoint": "e"})
    for call in (adapter.test, adapter.discover, adapter.open):
        with pytest.raises(AdapterNotImplemented):
            call(None, cfg)  # type: ignore[arg-type]


# ------------------------------------------------------------------ maturity derivation
def _run(
    *, outcome: str = "pass", version: str | None = None, caps: Sequence[str] = ("rest.call",)
) -> evidence.ConformanceRun:
    d = REGISTRY.descriptor("rest.http")
    assert d is not None
    now = utcnow()
    return evidence.ConformanceRun(
        adapter_key="rest.http",
        adapter_version=version or d.version,
        suite_version="1",
        started_at=now - timedelta(seconds=2),
        finished_at=now,
        outcome=outcome,
        validated_capabilities=tuple(caps),
    )


def test_maturity_is_derived_only_from_evidence() -> None:
    rest = REGISTRY.descriptor("rest.http")
    db = REGISTRY.descriptor("fabric")
    assert rest is not None and db is not None
    # planned: no implementation, whatever the evidence says
    forged = _run(caps=("query.sql",))
    assert {m.maturity for m in adapter_capabilities(db, forged)} == {"planned"}
    # implemented without evidence: experimental
    assert {m.maturity for m in adapter_capabilities(rest, None)} == {"experimental"}
    # a passing run on this version validates exactly the capabilities it proved
    caps = {m.capability: m for m in adapter_capabilities(rest, _run())}
    assert caps["rest.call"].maturity == "validated" and caps["rest.call"].evidence is not None
    assert caps["discover.openapi"].maturity == "experimental"
    # a failing latest run demotes; so does a newer adapter version than the evidence
    assert capability_maturity(rest, "rest.call", _run(outcome="fail")).maturity == "experimental"
    assert capability_maturity(rest, "rest.call", _run(version="0.0.1")).maturity == "experimental"


# ------------------------------------------------------------------ the suites
@pytest.mark.parametrize("key", ["fake", "openai_compatible", "rest.http", "mcp_http"])
def test_conformance_suites_pass_and_prove_every_declared_capability(key: str) -> None:
    descriptor = REGISTRY.descriptor(key)
    suite = REGISTRY.suite_for(key)
    assert descriptor is not None and suite is not None
    run = run_suite(suite, descriptor)
    failed = [c for c in run.checks if c["status"] != "pass"]
    assert run.outcome == "pass", failed
    assert set(run.validated_capabilities) == {str(c) for c in descriptor.capabilities}
    assert run.adapter_version == descriptor.version and len(run.checks) >= 4


def test_postgres_suite_is_skipped_without_a_sample_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHIFDE_SAMPLE_DATABASE_URL", raising=False)
    monkeypatch.delenv("SHIFDE_ENV", raising=False)
    descriptor = REGISTRY.descriptor("postgres")
    assert descriptor is not None
    with pytest.raises(SuiteSkipped):
        run_suite(PostgresSuite(), descriptor)


def test_anthropic_is_experimental_and_only_a_live_smoke_can_validate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    descriptor = REGISTRY.descriptor("anthropic")
    assert descriptor is not None
    with pytest.raises(SuiteSkipped):
        run_suite(AnthropicLiveSmoke(), descriptor)
    assert {m.maturity for m in adapter_capabilities(descriptor, None)} == {"experimental"}
    assert SUITES["anthropic"] is AnthropicLiveSmoke


def test_a_failing_check_fails_the_run_and_validates_nothing() -> None:
    class Broken(ConformanceSuite):
        adapter_key = "fake"

        def target(self) -> AbstractContextManager[Any]:
            return _noop()

        def checks(self) -> Sequence[Check]:
            return [
                Check("ok", (Capability.MODEL_CHAT,), lambda _t: "fine"),
                Check("bad", (Capability.MODEL_LIST,), lambda _t: expect(False, "nope")),
            ]

    descriptor = REGISTRY.descriptor("fake")
    assert descriptor is not None
    run = run_suite(Broken(), descriptor)
    assert run.outcome == "fail" and run.validated_capabilities == ()
    assert [c["status"] for c in run.checks] == ["pass", "fail"]
    assert "nope" in run.checks[1]["detail"]


@contextmanager
def _noop() -> Iterator[None]:
    yield None


def test_third_party_adapters_reuse_the_harness() -> None:
    class Toy:
        descriptor: ClassVar[AdapterDescriptor] = AdapterDescriptor(
            key="acme.toy",
            kind="connection",
            version="2.0.0",
            display_name="Toy",
            capabilities=frozenset({Capability.REST_CALL}),
            config_schema={"type": "object"},
            connection_type="rest",
        )

        def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
            return []

        def test(self, ctx: ExecContext, conn: ConnectionConfig) -> ProbeOutcome:
            return ProbeOutcome([])

        def discover(self, ctx: ExecContext, conn: ConnectionConfig) -> DiscoveryResult:
            return DiscoveryResult("openapi", {"paths": []})

        def open(self, ctx: ExecContext, conn: ConnectionConfig) -> Any:
            return None

        @classmethod
        def conformance_suite(cls) -> ConformanceSuite:
            class Suite(ConformanceSuite):
                adapter_key = "acme.toy"
                suite_version = "toy-1"

                def target(self) -> AbstractContextManager[Any]:
                    return _noop()

                def checks(self) -> Sequence[Check]:
                    return [Check("works", (Capability.REST_CALL,), lambda _t: "ok")]

            return Suite()

    registry = AdapterRegistry([Toy])
    suite = registry.suite_for("acme.toy")
    assert suite is not None
    run = run_suite(suite, Toy.descriptor)
    assert run.outcome == "pass" and run.validated_capabilities == ("rest.call",)
    assert run.suite_version == "toy-1"


# ------------------------------------------------------------------ evidence storage
def test_evidence_is_owner_written_append_only_and_app_cannot_forge_it(
    owner_session: Session, app_db: Database
) -> None:
    run = _run()
    evidence.record_run(owner_session, run)
    owner_session.commit()
    latest = evidence.latest_runs(owner_session)["rest.http"]
    assert latest.id == run.id and latest.validated_capabilities == ("rest.call",)
    assert [r.id for r in evidence.list_runs(owner_session, "rest.http")] == [run.id]
    with app_db.session() as s:  # the application role can read but never write evidence
        assert (
            s.execute(text("SELECT count(*) FROM shifde.adapter_conformance_runs")).scalar_one()
            == 1
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            s.execute(
                text(
                    "INSERT INTO shifde.adapter_conformance_runs (id, adapter_key, adapter_version, "
                    "suite_version, started_at, finished_at, outcome) VALUES (gen_random_uuid(), "
                    "'rest.http', '1', '1', now(), now(), 'pass')"
                )
            )
        s.rollback()
        for stmt in (
            "UPDATE shifde.adapter_conformance_runs SET outcome = 'fail'",
            "DELETE FROM shifde.adapter_conformance_runs",
        ):
            with pytest.raises(DBAPIError):
                s.execute(text(stmt))
            s.rollback()
    with pytest.raises(DBAPIError, match="append-only|reject|not allowed"):
        owner_session.execute(text("DELETE FROM shifde.adapter_conformance_runs"))
    owner_session.rollback()
    owner_session.execute(text("TRUNCATE shifde.adapter_conformance_runs"))
    owner_session.commit()
