"""The `databricks` connection adapter against the in-process fake (CM-3, issue #161; design
2026-11-22 sections 7.2, 7.4 and 7.5): conformance, the eight fault switches, OAuth M2M, the row
cap, discovery caps, the read-only check, `normalize_config` and `validate_credential`."""

from __future__ import annotations

import base64
import json
from typing import Any  # Any: JSON bodies

import pytest

from shifde.adapters.base import AdapterFailure, AdapterRules, ConfigRefused, ErrorClass
from shifde.adapters.conformance import run_suite
from shifde.adapters.dataplatforms import sqlapi
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import (
    ROWS_STATEMENT,
    SELECT_ONE,
    FakeDatabricksWorkspace,
)
from shifde.adapters.dataplatforms.suites import DatabricksSqlSuite
from shifde.adapters.oauth2 import AUTH_METHOD as OAUTH2
from shifde.core.ids import uuid7
from tests.adapters.dataplatforms.helpers import (
    CLIENT_ID,
    CLIENT_SECRET,
    URL,
    conn,
    ctx_for,
)

ADAPTER = DatabricksAdapter()
STATEMENTS = "/api/2.0/sql/statements"


def _invoke(fake: FakeDatabricksWorkspace, request: dict[str, Any], **kw: Any) -> Any:  # Any
    write = kw.pop("write", False)
    session = ADAPTER.open(ctx_for(fake), conn(**kw))
    try:
        return session.invoke(request, write=write)
    finally:
        session.close()


def _submitted(fake: FakeDatabricksWorkspace) -> list[dict[str, Any]]:
    return [
        json.loads(r.content)
        for r in fake.requests
        if (r.method, r.url.path) == ("POST", STATEMENTS)
    ]


# ------------------------------------------------------------------ conformance
def test_the_conformance_suite_passes_against_the_fake() -> None:
    run = run_suite(DatabricksSqlSuite(), DatabricksAdapter.descriptor)
    assert run.outcome == "pass", run.checks
    assert set(run.validated_capabilities) == {"query.sql", "discover.schema"}


def test_descriptor_and_schema() -> None:
    d = DatabricksAdapter.descriptor
    assert (d.key, d.kind, d.connection_type, d.declared_maturity) == (
        "databricks",
        "connection",
        "data_platform",
        "experimental",
    )
    assert {str(c) for c in d.capabilities} == {"query.sql", "discover.schema"}
    assert d.secret_fields == ("token", "client_secret")
    assert d.destination_fields == ("host",)
    assert d.auth_methods == ("none", "bearer", OAUTH2)
    assert dict(d.auth_only_fields) == {"client_id": (OAUTH2,)}
    assert d.config_schema["additionalProperties"] is False
    assert all("title" in p for p in d.config_schema["properties"].values())
    assert ADAPTER.validate_config({"host": URL, "warehouse_id": "abc-123", "max_rows": 10}) == []
    for bad in (
        {"host": "ws1.cloud.databricks.com"},  # not normalized
        {"host": URL, "max_rows": 0},
        {"host": URL, "statement_timeout_seconds": 51},
        {"host": URL, "warehouse_id": "a b"},
        {"host": URL, "catalogs": ["ok"] * 51},
        {"host": URL, "token": "x"},
        {"host": URL, "unknown": 1},
    ):
        assert ADAPTER.validate_config(bad), bad


def test_a_passing_test_reports_every_check(fake: FakeDatabricksWorkspace) -> None:
    out = ADAPTER.test(ctx_for(fake), conn())
    assert out.passed, out.checks
    checks = {c.name: c for c in out.checks}
    assert [c.status for c in out.checks] == ["pass"] * 6
    assert "RUNNING" in checks["capability"].detail
    assert fake.paths() == [
        ("GET", "/api/2.0/preview/scim/v2/Me"),
        ("GET", "/api/2.1/unity-catalog/catalogs"),
        ("GET", "/api/2.0/sql/warehouses/wh-0001"),
    ]


def test_auth_method_none_fails_the_auth_check(fake: FakeDatabricksWorkspace) -> None:
    out = ADAPTER.test(ctx_for(fake), conn("none", None))
    checks = {c.name: c for c in out.checks}
    assert out.error_class is ErrorClass.AUTH and checks["auth"].status == "fail"
    assert checks["auth"].detail == "a Databricks connection needs a credential"
    assert fake.requests == []


def test_oauth_without_client_id_is_a_config_failure(fake: FakeDatabricksWorkspace) -> None:
    c = conn(OAUTH2, CLIENT_SECRET, client_id=None)
    out = ADAPTER.test(ctx_for(fake), c)
    assert out.error_class is ErrorClass.CONFIG and out.checks[0].status == "fail"
    for call in (ADAPTER.discover, ADAPTER.open):
        with pytest.raises(AdapterFailure) as raised:
            call(ctx_for(fake), c)
        assert raised.value.error_class is ErrorClass.CONFIG


# ------------------------------------------------------------------ faults
@pytest.mark.parametrize(
    ("fault", "error_class"),
    [
        ("bad_token", ErrorClass.AUTH),
        ("forbidden", ErrorClass.AUTH),
        ("rate_limited", ErrorClass.RATE_LIMIT),
        ("server_error", ErrorClass.UPSTREAM),
        ("oversize", ErrorClass.UPSTREAM),
        ("redirect", ErrorClass.EGRESS),
        ("warehouse_missing", ErrorClass.CONFIG),
    ],
)
def test_each_fault_maps_to_its_error_class(
    fake: FakeDatabricksWorkspace, fault: str, error_class: ErrorClass
) -> None:
    fake.faults.add(fault)
    fake.retry_after = "0"
    out = ADAPTER.test(ctx_for(fake), conn())
    assert not out.passed and out.error_class is error_class, (fault, out)
    failed = [c for c in out.checks if c.status == "fail"]
    assert len(failed) == 1
    if fault == "warehouse_missing":
        assert failed[0].name == "capability" and "warehouse" in failed[0].detail


def test_rate_limit_is_retried_once_when_retry_after_fits(fake: FakeDatabricksWorkspace) -> None:
    fake.faults.add("rate_limited")
    fake.retry_after = "0"
    ADAPTER.test(ctx_for(fake), conn())
    assert fake.paths() == [("GET", "/api/2.0/preview/scim/v2/Me")] * 2
    fake.requests.clear()
    fake.retry_after = "30"  # longer than the remaining deadline: no retry
    out = ADAPTER.test(ctx_for(fake, deadline=5), conn())
    assert out.error_class is ErrorClass.RATE_LIMIT
    assert fake.paths() == [("GET", "/api/2.0/preview/scim/v2/Me")]


def test_slow_statements_are_cancelled_at_the_deadline(fake: FakeDatabricksWorkspace) -> None:
    fake.faults.add("slow")
    session = ADAPTER.open(ctx_for(fake, deadline=2.5), conn(statement_timeout_seconds=5))
    with pytest.raises(AdapterFailure) as raised:
        session.invoke({"sql": SELECT_ONE})
    assert raised.value.error_class is ErrorClass.TIMEOUT
    cancels = [p for p in fake.paths() if p[1].endswith("/cancel")]
    assert len(cancels) == 1 and cancels[0][0] == "POST"
    assert ("GET", f"{STATEMENTS}/{cancels[0][1].split('/')[-2]}") in fake.paths()


def test_statement_states_map_to_classes(fake: FakeDatabricksWorkspace) -> None:
    for state, error_class in (
        ("FAILED", ErrorClass.UPSTREAM),
        ("CANCELED", ErrorClass.TIMEOUT),
        ("CLOSED", ErrorClass.TIMEOUT),
    ):
        original = fake._result

        def ended(sid: str, statement: str, limit: Any, state: str = state) -> dict[str, Any]:
            return {
                "statement_id": sid,
                "status": {"state": state, "error": {"message": "server text SECRET"}},
            }

        fake._result = ended  # type: ignore[method-assign]
        try:
            with pytest.raises(AdapterFailure) as raised:
                _invoke(fake, {"sql": "SELECT secret_column FROM t"})
        finally:
            fake._result = original  # type: ignore[method-assign]
        assert raised.value.error_class is error_class
        assert "SECRET" not in raised.value.message and "secret_column" not in raised.value.message


# ------------------------------------------------------------------ OAuth M2M
def test_oauth_fetches_a_token_with_basic_credentials(fake: FakeDatabricksWorkspace) -> None:
    out = ADAPTER.test(ctx_for(fake, tenant=uuid7()), conn(OAUTH2, CLIENT_SECRET))
    assert out.passed, out.checks
    token_calls = [r for r in fake.requests if r.url.path == "/oidc/v1/token"]
    assert len(token_calls) == 1
    basic = token_calls[0].headers["authorization"].removeprefix("Basic ")
    assert base64.b64decode(basic).decode() == f"{CLIENT_ID}:{CLIENT_SECRET}"
    form = token_calls[0].content.decode()
    assert "grant_type=client_credentials" in form and "scope=all-apis" in form
    assert fake.issued and all(
        r.headers["authorization"] == f"Bearer {fake.issued[0]}"
        for r in fake.requests
        if r.url.path != "/oidc/v1/token"
    )


def test_a_wrong_client_secret_is_an_auth_failure(fake: FakeDatabricksWorkspace) -> None:
    out = ADAPTER.test(ctx_for(fake), conn(OAUTH2, "not-the-secret"))
    assert out.error_class is ErrorClass.AUTH
    assert {c.name: c.status for c in out.checks}["auth"] == "fail"


# ------------------------------------------------------------------ invoke
def test_select_one_and_the_statement_body(fake: FakeDatabricksWorkspace) -> None:
    result = _invoke(
        fake,
        {"sql": SELECT_ONE, "params": {"region": "north", "n": 3, "none": None}},
        default_catalog="main",
        default_schema="default",
        max_rows=10,
        statement_timeout_seconds=20,
    )
    assert result.result == {"columns": ["1"], "rows": [["1"]], "row_count": 1}
    assert result.truncated is False
    (body,) = _submitted(fake)
    assert body == {
        "warehouse_id": "wh-0001",
        "statement": SELECT_ONE,
        "parameters": [
            {"name": "region", "value": "north"},
            {"name": "n", "value": "3"},
            {"name": "none", "value": None},
        ],
        "catalog": "main",
        "schema": "default",
        "wait_timeout": "20s",
        "on_wait_timeout": "CANCEL",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
        "row_limit": 11,
    }


def test_absent_optional_keys_are_left_out(fake: FakeDatabricksWorkspace) -> None:
    _invoke(fake, {"sql": SELECT_ONE})
    (body,) = _submitted(fake)
    assert not {"parameters", "catalog", "schema"} & set(body)
    assert body["row_limit"] == 1001 and body["wait_timeout"] == "30s"


def test_positional_params_and_a_missing_warehouse_are_config_failures(
    fake: FakeDatabricksWorkspace,
) -> None:
    for request, kw in (
        ({"sql": SELECT_ONE, "params": [1]}, {}),
        ({"sql": SELECT_ONE}, {"warehouse_id": None}),
    ):
        with pytest.raises(AdapterFailure) as raised:
            _invoke(fake, request, **kw)
        assert raised.value.error_class is ErrorClass.CONFIG
    assert fake.requests == []


def test_the_row_cap_sets_truncated(fake: FakeDatabricksWorkspace) -> None:
    fake.row_count = 10
    capped = _invoke(fake, {"sql": ROWS_STATEMENT}, max_rows=4)
    assert capped.result["row_count"] == 4 and len(capped.result["rows"]) == 4
    assert capped.truncated is True
    assert capped.result["columns"] == ["n", "label"]
    fake.row_count = 4
    exact = _invoke(fake, {"sql": ROWS_STATEMENT}, max_rows=4)
    assert exact.result["row_count"] == 4 and exact.truncated is False


# ------------------------------------------------------------------ read-only
REFUSED = [
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "SELECT 1; DROP TABLE t",
    "/* */ DROP TABLE t",
    "SELECT 1 -- x\n; DROP t",
    "SELECT 1 /* unterminated",
    "SELECT 'unterminated",
    "SELECT 1 /* /* nested */ ; DROP t */",
    "SELECT '\\'' ; DROP t --'",
    'SELECT R"\\"; DROP TABLE t; --"',
    "SELECT r'\\'; DROP TABLE t; --'",
    "-- only a comment",
    "",
]
ACCEPTED = [
    "SELECT ';'",
    "SELECT 1;",
    "  select 1  ;  ",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SHOW TABLES",
    "DESCRIBE t",
    "DESC t",
    "EXPLAIN SELECT 1",
    "/* lead */ SELECT `a;b` FROM t -- trailing",
    "SELECT \"x;y\", 'it''s'",
    "SELECT r'\\', 'a;b'",
    "SELECT attr'\\';'",
]


@pytest.mark.parametrize("sql", REFUSED)
def test_refused_statements_make_no_request(fake: FakeDatabricksWorkspace, sql: str) -> None:
    with pytest.raises(AdapterFailure) as raised:
        _invoke(fake, {"sql": sql or " "})
    assert raised.value.error_class in (ErrorClass.CAPABILITY, ErrorClass.CONFIG)
    assert fake.requests == []


@pytest.mark.parametrize("sql", ACCEPTED)
def test_read_statements_pass_the_check(sql: str) -> None:
    sqlapi.check_read_only(sql)


def test_refusals_are_capability_failures() -> None:
    for sql in REFUSED:
        with pytest.raises(AdapterFailure) as raised:
            sqlapi.check_read_only(sql)
        assert raised.value.error_class is ErrorClass.CAPABILITY, sql


def test_accepted_statements_reach_the_warehouse(fake: FakeDatabricksWorkspace) -> None:
    assert _invoke(fake, {"sql": "SELECT ';'"}).result["row_count"] == 0
    assert _invoke(fake, {"sql": "SELECT 1;"}).result["rows"] == [["1"]]
    assert len(_submitted(fake)) == 2


def test_write_skips_the_keyword_check(fake: FakeDatabricksWorkspace) -> None:
    result = _invoke(fake, {"sql": "INSERT INTO t VALUES (1)"}, write=True)
    assert result.result == {"columns": [], "rows": [], "row_count": 0}
    assert [b["statement"] for b in _submitted(fake)] == ["INSERT INTO t VALUES (1)"]


# ------------------------------------------------------------------ discover
def test_discover_payload_shape(fake: FakeDatabricksWorkspace) -> None:
    found = ADAPTER.discover(ctx_for(fake), conn())
    assert found.kind == "schema" and found.payload["truncated"] is False
    tables = found.payload["tables"]
    assert {(t["catalog"], t["schema"], t["name"]) for t in tables} == {
        ("main", "default", "customers"),
        ("main", "default", "orders"),
        ("main", "sales", "invoices"),
        ("samples", "nyctaxi", "trips"),
    }
    assert tables[0] == {
        "catalog": "main",
        "schema": "default",
        "name": "customers",
        "table_type": "MANAGED",
        "columns": [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "label", "type": "string", "nullable": True},
        ],
    }


def test_discover_honours_the_catalog_filter(fake: FakeDatabricksWorkspace) -> None:
    found = ADAPTER.discover(ctx_for(fake), conn(catalogs=["samples"]))
    assert {t["catalog"] for t in found.payload["tables"]} == {"samples"}
    assert ("GET", "/api/2.1/unity-catalog/catalogs") not in fake.paths()


@pytest.mark.parametrize(
    ("tree", "tables"),
    [
        ({f"c{i}": {} for i in range(60)}, 0),  # 50 catalogs at most
        ({"main": {f"s{i}": [] for i in range(210)}}, 0),  # 200 schemas per catalog
        ({"main": {"a": [f"t{i}" for i in range(1500)], "b": [f"u{i}" for i in range(900)]}}, 2000),
    ],
    ids=["catalogs", "schemas", "tables"],
)
def test_discover_caps_set_truncated(
    fake: FakeDatabricksWorkspace, tree: dict[str, Any], tables: int
) -> None:
    fake.tree = tree
    found = ADAPTER.discover(ctx_for(fake), conn())
    assert found.payload["truncated"] is True
    assert len(found.payload["tables"]) == tables
    schema_calls = [p for p in fake.paths() if p[1] == "/api/2.1/unity-catalog/schemas"]
    table_calls = {
        r.url.params["schema_name"]
        for r in fake.requests
        if r.url.path == "/api/2.1/unity-catalog/tables"
    }
    if len(tree) == 60:
        assert len(schema_calls) == 50
    if len(tree) == 1 and len(tree["main"]) == 210:
        assert len(table_calls) == 200


# ------------------------------------------------------------------ hooks
RULES = AdapterRules()


@pytest.mark.parametrize(
    ("host", "rules", "expected"),
    [
        ("ws1.cloud.databricks.com", RULES, "https://ws1.cloud.databricks.com"),
        ("https://WS1.cloud.databricks.com/", RULES, "https://ws1.cloud.databricks.com"),
        ("https://adb-1.2.azuredatabricks.net:443", RULES, "https://adb-1.2.azuredatabricks.net"),
        (
            "ws9.databricks.example.org",
            AdapterRules(databricks_host_suffixes=("databricks.example.org",)),
            "https://ws9.databricks.example.org",
        ),
    ],
)
def test_normalize_config_accepts(host: str, rules: AdapterRules, expected: str) -> None:
    cfg = {"host": host, "warehouse_id": "w1", "max_rows": 5}
    assert ADAPTER.normalize_config(cfg, rules) == {**cfg, "host": expected}
    assert cfg["host"] == host  # a copy


@pytest.mark.parametrize(
    "host",
    [
        "ws1.example.com",
        "ws9.databricks.example.org",  # an extra suffix only counts when the rules list it
        "https://10.0.0.1",
        "10.0.0.1",
        "http://ws1.cloud.databricks.com",
        "https://ws1.cloud.databricks.com:8443",
        "https://user@ws1.cloud.databricks.com",
        "https://user:pw@ws1.cloud.databricks.com",
        "https://ws1.cloud.databricks.com/path",
        "https://ws1.cloud.databricks.com?x=1",
    ],
)
def test_normalize_config_refuses_without_echo(host: str) -> None:
    with pytest.raises(ConfigRefused) as raised:
        ADAPTER.normalize_config({"host": host}, RULES)
    (error,) = raised.value.errors
    assert error.path == "host"
    assert host not in error.message and "10.0.0.1" not in error.message


@pytest.mark.parametrize("value", ["", "   ", "two words", "tab\there", "nul\x00byte", "line\n"])
def test_validate_credential_refuses_without_echo(value: str) -> None:
    for method in ("bearer", OAUTH2):
        errors = ADAPTER.validate_credential(method, value)
        assert len(errors) == 1, (method, value)
        assert value.strip() == "" or value not in errors[0].message


def test_validate_credential_accepts_tokens() -> None:
    assert ADAPTER.validate_credential("bearer", "dapi0123456789abcdef") == []
    assert ADAPTER.validate_credential(OAUTH2, "secret-value") == []
    assert ADAPTER.validate_credential("none", "") == []


# ------------------------------------------------------------------ stored host (B-3116)
LEGACY_HOSTS = [
    "ws1.cloud.databricks.com",
    "http://ws1.cloud.databricks.com",
    "https://ws1.cloud.databricks.com:8443",
    "https://ws1.cloud.databricks.com/path",
    "https://user@ws1.cloud.databricks.com",
    "https://10.0.0.1",
    "https://localhost",
    "https://WS1.cloud.databricks.com",
    "",
]


@pytest.mark.parametrize("host", LEGACY_HOSTS)
def test_a_non_normalized_stored_host_is_refused_before_any_request(
    fake: FakeDatabricksWorkspace, host: str
) -> None:
    stored = conn(host=host)
    for call in (
        lambda: ADAPTER.discover(ctx_for(fake), stored),
        lambda: ADAPTER.open(ctx_for(fake), stored),
    ):
        with pytest.raises(AdapterFailure) as raised:
            call()
        assert raised.value.error_class is ErrorClass.CONFIG
        assert host not in raised.value.message or not host
    assert fake.requests == []
