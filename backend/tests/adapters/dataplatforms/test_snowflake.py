"""The `snowflake` connection adapter against the in-process fake (CM-4, issue #162; design
2026-11-22 sections 7.3, 7.4 and 7.5): conformance, the eight fault switches, the key-pair JWT
and the fake's check of it, the PAT headers, `validate_credential`, the row cap, the read-only
check, discovery and the host rule. No test opens a socket or imports a vendor SDK."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any  # Any: JSON bodies

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from shifde.adapters import oauth2
from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.conformance import run_suite
from shifde.adapters.dataplatforms import snowflake, sqlapi
from shifde.adapters.dataplatforms.fakes import ROWS_STATEMENT, SELECT_ONE, FakeSnowflakeAccount
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from shifde.adapters.dataplatforms.suites import SnowflakeSqlSuite
from shifde.core.ids import uuid7
from tests.adapters.dataplatforms.helpers import (
    SF_PAT,
    SF_PEM,
    ctx_for,
    make_snowflake,
    pkcs8,
    rsa_key,
    sf_conn,
)

ADAPTER = SnowflakeAdapter()
STATEMENTS = "/api/v2/statements"


def _invoke(fake: FakeSnowflakeAccount, request: dict[str, Any], **kw: Any) -> Any:  # Any
    write = kw.pop("write", False)
    auth = kw.pop("auth", "key_pair")
    session = ADAPTER.open(ctx_for(fake), sf_conn(auth, **kw))
    try:
        return session.invoke(request, write=write)
    finally:
        session.close()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(token: str) -> tuple[dict[str, Any], dict[str, Any]]:  # Any: JWT JSON
    head, body, _ = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    return json.loads(base64.urlsafe_b64decode(pad(head))), json.loads(
        base64.urlsafe_b64decode(pad(body))
    )


def _sent_token(fake: FakeSnowflakeAccount) -> str:
    return fake.requests[-1].headers["authorization"].removeprefix("Bearer ")


# ------------------------------------------------------------------ conformance and descriptor
def test_the_conformance_suite_passes_against_the_fake() -> None:
    run = run_suite(SnowflakeSqlSuite(), SnowflakeAdapter.descriptor)
    assert run.outcome == "pass", run.checks
    assert set(run.validated_capabilities) == {"query.sql", "discover.schema"}


def test_descriptor_and_schema() -> None:
    d = SnowflakeAdapter.descriptor
    assert (d.key, d.kind, d.connection_type, d.declared_maturity) == (
        "snowflake",
        "connection",
        "data_platform",
        "experimental",
    )
    assert {str(c) for c in d.capabilities} == {"query.sql", "discover.schema"}
    assert d.auth_methods == ("key_pair", "bearer")
    assert d.destination_fields == ("account",)
    assert d.secret_fields == ("private_key", "token")
    assert "planned" not in d.display_name.lower()
    assert d.config_schema["additionalProperties"] is False
    assert not hasattr(ADAPTER, "conformance_suite")
    good = {
        "account": "xy12345.eu-west-1.aws",
        "user": "reader",
        "role": "R",
        "warehouse": "W",
        "database": "D",
        "schema": "S",
        "databases": ["A"],
        "max_rows": 10_000,
        "statement_timeout_seconds": 120,
    }
    assert ADAPTER.validate_config(good) == []
    assert ADAPTER.validate_config({"account": "my_org-acct", "user": "u"}) == []
    for bad in (
        {"user": "u"},
        {"account": "acct1"},
        {"account": "acct1", "user": ""},
        {"account": "-acct", "user": "u"},
        {"account": "a.b.c.d", "user": "u"},
        {"account": "evil.com/x", "user": "u"},
        {"account": "acct1", "user": "u", "max_rows": 0},
        {"account": "acct1", "user": "u", "max_rows": 10_001},
        {"account": "acct1", "user": "u", "statement_timeout_seconds": 4},
        {"account": "acct1", "user": "u", "statement_timeout_seconds": 121},
        {"account": "acct1", "user": "u", "databases": ["d"] * 51},
        {"account": "acct1", "user": "u", "private_key": "x"},
        {"account": "acct1", "user": "u", "unknown": 1},
    ):
        assert ADAPTER.validate_config(bad), bad


# ------------------------------------------------------------------ test
def test_a_passing_test_reports_every_check_in_order(sf: FakeSnowflakeAccount) -> None:
    out = ADAPTER.test(ctx_for(sf), sf_conn(warehouse="COMPUTE_WH"))
    assert out.passed, out.checks
    assert [c.name for c in out.checks] == ["config", "egress", "dns", "tls", "auth", "capability"]
    assert "9.1.0" in out.checks[-1].detail
    assert sf.paths() == [("POST", STATEMENTS)]
    (body,) = sf.bodies()
    assert body["statement"] == snowflake.TEST_STATEMENT
    assert body["warehouse"] == "COMPUTE_WH"
    headers = sf.requests[0].headers
    assert headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"


def test_the_pat_headers_are_sent(sf: FakeSnowflakeAccount) -> None:
    out = ADAPTER.test(ctx_for(sf), sf_conn("bearer"))
    assert out.passed, out.checks
    headers = sf.requests[0].headers
    assert headers["authorization"] == f"Bearer {SF_PAT}"
    assert headers["x-snowflake-authorization-token-type"] == "PROGRAMMATIC_ACCESS_TOKEN"
    sf.pat = "another-token"
    assert ADAPTER.test(ctx_for(sf), sf_conn("bearer")).error_class is ErrorClass.AUTH


def test_a_bad_stored_key_is_a_config_failure_before_any_request(
    sf: FakeSnowflakeAccount,
) -> None:
    out = ADAPTER.test(ctx_for(sf), sf_conn(secret="not a key"))
    assert out.error_class is ErrorClass.CONFIG
    assert [(c.name, c.status) for c in out.checks if c.status != "skipped"] == [("config", "fail")]
    assert "not a key" not in json.dumps([c.detail for c in out.checks])
    assert sf.requests == []


@pytest.mark.parametrize(
    ("fault", "error_class"),
    [
        ("bad_token", ErrorClass.AUTH),
        ("forbidden", ErrorClass.AUTH),
        ("rate_limited", ErrorClass.RATE_LIMIT),
        ("server_error", ErrorClass.UPSTREAM),
        ("slow", ErrorClass.TIMEOUT),
        ("oversize", ErrorClass.UPSTREAM),
        ("redirect", ErrorClass.EGRESS),
        ("warehouse_missing", ErrorClass.CONFIG),
    ],
)
def test_each_fault_maps_to_its_error_class(
    sf: FakeSnowflakeAccount, fault: str, error_class: ErrorClass
) -> None:
    sf.faults.add(fault)
    sf.retry_after = "0"
    out = ADAPTER.test(ctx_for(sf, deadline=2.5), sf_conn(statement_timeout_seconds=5))
    assert not out.passed and out.error_class is error_class, (fault, out)
    failed = [c for c in out.checks if c.status == "fail"]
    assert len(failed) == 1
    if fault in ("slow", "warehouse_missing"):
        assert failed[0].name == "capability"
    if fault in ("bad_token", "forbidden"):
        assert failed[0].name == "auth"


def test_slow_statements_are_cancelled_at_the_deadline(sf: FakeSnowflakeAccount) -> None:
    sf.faults.add("slow")
    session = ADAPTER.open(ctx_for(sf, deadline=2.5), sf_conn(statement_timeout_seconds=5))
    with pytest.raises(AdapterFailure) as raised:
        session.invoke({"sql": SELECT_ONE})
    assert raised.value.error_class is ErrorClass.TIMEOUT
    cancels = [p for p in sf.paths() if p[1].endswith("/cancel")]
    assert len(cancels) == 1 and cancels[0][0] == "POST"
    handle = cancels[0][1].split("/")[-2]
    assert ("GET", f"{STATEMENTS}/{handle}") in sf.paths()


def test_a_202_is_polled_until_it_finishes(sf: FakeSnowflakeAccount) -> None:
    sf.faults.add("slow")
    original = sf._submit

    def finish_on_first_poll(body: dict[str, Any]) -> httpx.Response:
        response = original(body)
        handle = response.json()["statementHandle"]
        sf._pending.discard(handle)
        sf._statements[handle] = {
            "statementHandle": handle,
            "resultSetMetaData": {"numRows": 1, "rowType": [{"name": "1"}]},
            "data": [["1"]],
        }
        return httpx.Response(202, json={"statementHandle": handle})

    sf._submit = finish_on_first_poll  # type: ignore[method-assign]
    result = _invoke(sf, {"sql": SELECT_ONE})
    assert result.result == {
        "columns": [{"name": "1", "type": ""}],  # the answer gave no type
        "rows": [["1"]],
        "row_count": 1,
    }
    assert [p[0] for p in sf.paths()] == ["POST", "GET"]


# ------------------------------------------------------------------ key-pair JWT
def test_the_jwt_claims(sf: FakeSnowflakeAccount) -> None:
    sf.account = "xy12345.eu-west-1"
    before = int(time.time())
    out = ADAPTER.test(ctx_for(sf), sf_conn(account="xy12345.eu-west-1", user="Reader"))
    assert out.passed, out.checks
    assert sf.requests[0].headers["host"] == "xy12345.eu-west-1.snowflakecomputing.com"
    head, claims = _claims(_sent_token(sf))
    assert head == {"alg": "RS256", "typ": "JWT"}
    der = (
        rsa_key()
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    fingerprint = base64.b64encode(hashlib.sha256(der).digest()).decode()
    assert claims["sub"] == "XY12345.READER"
    assert claims["iss"] == f"XY12345.READER.SHA256:{fingerprint}"
    assert isinstance(claims["iat"], int) and before <= claims["iat"] <= int(time.time())
    assert claims["exp"] == claims["iat"] + 300
    assert "=" not in _sent_token(sf)


def test_the_host_replaces_underscores(sf: FakeSnowflakeAccount) -> None:
    sf.account = "My_Org-Acct"
    out = ADAPTER.test(ctx_for(sf), sf_conn(account="My_Org-Acct"))
    assert out.passed, out.checks
    assert sf.requests[0].headers["host"] == "my-org-acct.snowflakecomputing.com"
    assert _claims(_sent_token(sf))[1]["sub"] == "MY_ORG-ACCT.READER"


def test_a_jwt_signed_by_another_key_is_refused(sf: FakeSnowflakeAccount) -> None:
    sf.public_key = rsa_key("other").public_key()
    out = ADAPTER.test(ctx_for(sf), sf_conn())
    assert out.error_class is ErrorClass.AUTH
    assert {c.name: c.status for c in out.checks}["auth"] == "fail"
    assert "401" in out.message
    assert sf.paths() == [("POST", STATEMENTS)] * 2  # re-signed once, refused again


def test_an_expired_jwt_is_refused(
    sf: FakeSnowflakeAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snowflake, "clock", lambda: time.time() - 600)
    out = ADAPTER.test(ctx_for(sf), sf_conn())
    assert out.error_class is ErrorClass.AUTH and "401" in out.message
    assert _claims(_sent_token(sf))[1]["exp"] < time.time()


def _signed(key: rsa.RSAPrivateKey, claims: dict[str, Any]) -> str:  # Any: claim values
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(claims).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64url(signature)}"


def _status(fake: FakeSnowflakeAccount, token: str) -> int:
    request = httpx.Request(
        "POST",
        f"https://{fake.host}{STATEMENTS}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
        },
        json={"statement": SELECT_ONE, "parameters": {"MULTI_STATEMENT_COUNT": "1"}},
    )
    return fake.handle(request).status_code


def test_the_fake_checks_every_claim(sf: FakeSnowflakeAccount) -> None:
    now = int(time.time())
    good = {"iss": sf.expected_issuer(), "sub": sf.expected_subject(), "iat": now, "exp": now + 300}
    assert _status(sf, _signed(rsa_key(), good)) == 200
    assert _status(sf, _signed(rsa_key("other"), good)) == 401
    assert _status(sf, _signed(rsa_key(), {**good, "iss": "ACCT1.READER.SHA256:AAAA"})) == 401
    assert _status(sf, _signed(rsa_key(), {**good, "sub": "ACCT1.SOMEONE"})) == 401
    assert _status(sf, _signed(rsa_key(), {**good, "exp": now - 1})) == 401
    assert _status(sf, _signed(rsa_key(), good)[:-4] + "AAAA") == 401
    assert _status(sf, "not.a.jwt") == 401


def test_a_wrong_iss_from_the_adapter_is_an_auth_failure(
    sf: FakeSnowflakeAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snowflake, "public_key_fingerprint", lambda key: "SHA256:AAAA")
    out = ADAPTER.test(ctx_for(sf), sf_conn())
    assert out.error_class is ErrorClass.AUTH
    assert _claims(_sent_token(sf))[1]["iss"] == "ACCT1.READER.SHA256:AAAA"


def test_a_wrong_sub_from_the_adapter_is_an_auth_failure(sf: FakeSnowflakeAccount) -> None:
    out = ADAPTER.test(ctx_for(sf), sf_conn(user="someone"))
    assert out.error_class is ErrorClass.AUTH
    assert _claims(_sent_token(sf))[1]["sub"] == "ACCT1.SOMEONE"


def test_a_refused_cached_jwt_is_re_signed_once(sf: FakeSnowflakeAccount) -> None:
    tenant, connection_id = uuid7(), uuid7()
    c = sf_conn(connection_id=connection_id)
    assert ADAPTER.test(ctx_for(sf, tenant=tenant), c).passed
    first = _sent_token(sf)
    (key,) = list(oauth2.TOKEN_CACHE._entries)
    oauth2.TOKEN_CACHE.put(key, "stale.jwt.value", oauth2.clock() + 100)
    sf.requests.clear()
    assert ADAPTER.test(ctx_for(sf, tenant=tenant), c).passed
    tokens = [r.headers["authorization"].removeprefix("Bearer ") for r in sf.requests]
    assert tokens[0] == "stale.jwt.value" and len(tokens) == 2
    assert oauth2.TOKEN_CACHE.get(key) == tokens[1]
    assert first.count(".") == 2


# ------------------------------------------------------------------ validate_credential
def _pem(key: Any, fmt: Any, encryption: Any = None) -> str:  # Any: cryptography objects
    return key.private_bytes(  # type: ignore[no-any-return]
        serialization.Encoding.PEM, fmt, encryption or serialization.NoEncryption()
    ).decode()


REFUSED_KEYS = {
    "encrypted_pkcs8": lambda: _pem(
        rsa_key(),
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"passphrase"),
    ),
    "pkcs1": lambda: _pem(rsa_key(), serialization.PrivateFormat.TraditionalOpenSSL),
    "ec": lambda: pkcs8(ec.generate_private_key(ec.SECP256R1())),
    "rsa_1024": lambda: pkcs8(rsa_key("short", 1024)),
    "garbage": lambda: "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n",
    "not_pem": lambda: "just some text",
}


@pytest.mark.parametrize("name", sorted(REFUSED_KEYS))
def test_validate_credential_refuses_each_bad_key(name: str) -> None:
    value = REFUSED_KEYS[name]()
    errors = ADAPTER.validate_credential("key_pair", value)
    assert len(errors) == 1, (name, errors)
    assert errors[0].path == "value"
    body = value.strip().splitlines()
    for line in body[1:-1] or body:
        assert line not in errors[0].message


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_validate_credential_refuses_an_empty_pat(value: str) -> None:
    (error,) = ADAPTER.validate_credential("bearer", value)
    assert error.path == "value" and (not value.strip() or value not in error.message)


def test_validate_credential_accepts_good_values() -> None:
    assert ADAPTER.validate_credential("key_pair", SF_PEM) == []
    assert ADAPTER.validate_credential("bearer", SF_PAT) == []
    assert ADAPTER.validate_credential("none", "") == []


# ------------------------------------------------------------------ invoke
def test_the_statement_body_and_bindings(sf: FakeSnowflakeAccount) -> None:
    result = _invoke(
        sf,
        {"sql": SELECT_ONE, "params": ["north", 3, 1.5, True, None]},
        database="ANALYTICS",
        schema="PUBLIC",
        warehouse="COMPUTE_WH",
        role="READER",
        statement_timeout_seconds=20,
    )
    assert result.result == {
        "columns": [{"name": "1", "type": "text"}],
        "rows": [["1"]],
        "row_count": 1,
    }
    assert result.truncated is False
    (body,) = sf.bodies()
    assert body == {
        "statement": SELECT_ONE,
        "timeout": 20,
        "database": "ANALYTICS",
        "schema": "PUBLIC",
        "warehouse": "COMPUTE_WH",
        "role": "READER",
        "bindings": {
            "1": {"type": "TEXT", "value": "north"},
            "2": {"type": "FIXED", "value": "3"},
            "3": {"type": "REAL", "value": "1.5"},
            "4": {"type": "BOOLEAN", "value": "true"},
            "5": {"type": "TEXT", "value": None},
        },
        "parameters": {"MULTI_STATEMENT_COUNT": "1"},
    }


def test_absent_optional_keys_are_left_out(sf: FakeSnowflakeAccount) -> None:
    _invoke(sf, {"sql": SELECT_ONE})
    (body,) = sf.bodies()
    assert body == {
        "statement": SELECT_ONE,
        "timeout": 30,
        "parameters": {"MULTI_STATEMENT_COUNT": "1"},
    }


def test_named_params_are_a_config_failure_before_any_request(sf: FakeSnowflakeAccount) -> None:
    for params in ({"a": 1}, [[1]]):
        with pytest.raises(AdapterFailure) as raised:
            _invoke(sf, {"sql": SELECT_ONE, "params": params})
        assert raised.value.error_class is ErrorClass.CONFIG
    assert sf.requests == []


def test_the_row_cap_sets_truncated(sf: FakeSnowflakeAccount) -> None:
    sf.row_count = 10
    capped = _invoke(sf, {"sql": ROWS_STATEMENT}, max_rows=4)
    assert capped.result["row_count"] == 4 and len(capped.result["rows"]) == 4
    assert capped.truncated is True and capped.result["columns"] == [
        {"name": "N", "type": "text"},
        {"name": "LABEL", "type": "text"},
    ]
    sf.row_count = 4
    exact = _invoke(sf, {"sql": ROWS_STATEMENT}, max_rows=4)
    assert exact.result["row_count"] == 4 and exact.truncated is False


def test_only_the_first_partition_is_read(sf: FakeSnowflakeAccount) -> None:
    sf.row_count, sf.partition_size = 10, 3
    result = _invoke(sf, {"sql": ROWS_STATEMENT}, max_rows=100)
    assert result.result["row_count"] == 3 and result.truncated is True
    assert all(p[1] == STATEMENTS for p in sf.paths())


REFUSED_SQL = [
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "SELECT 1; DROP TABLE t",
    "/* */ DROP TABLE t",
    "-- c\nDELETE FROM t",
    "SELECT 1 -- x\n; DROP t",
    "SELECT 1 /* /* nested */ ; DROP t */",
    "SELECT 1 /* unterminated",
    # Snowflake dialect (B-3211, #231)
    "WITH p AS PROCEDURE () RETURNS VARCHAR LANGUAGE SQL AS "
    "$$ BEGIN DELETE FROM t; RETURN 'x'; END $$ CALL p()",
    "SELECT $$a$$; DELETE FROM t; SELECT $$b$$",
    "SELECT $$x$$ ; DROP TABLE t",
    "SELECT $$ ; DROP TABLE t",
    "SELECT a$$b FROM t",
    "WITH x AS (SELECT 1) SELECT * FROM x -- \n CALL p()",
    "SELECT 1 // $$\n CALL p() -- $$",
    # B-3212: `\` is no escape in `"..."`, a lone `\r` ends a line comment, no raw literals
    'WITH "p\\" AS PROCEDURE () RETURNS VARCHAR LANGUAGE SQL AS '
    '$$ BEGIN DELETE FROM t; RETURN \'x\'; END $$ CALL "p\\"() -- "',
    "SELECT 1 -- x\rCALL p()",
    "SELECT 1 // x\rCALL p()",
    "SELECT r'\\' , '  CALL p() -- '",
    "SELECT `a CALL p() -- `",
    # a write keyword anywhere in the code (#267)
    "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a",
    "WITH a AS (SELECT 1) DELETE FROM t",
    "with a as (select 1) Insert into t select * from a",
    "With a As (Select 1) dElEtE From t",
]
ACCEPTED_SQL = [
    "SELECT $$hello$$",
    "SELECT $$a;b$$",
    "SELECT $$delete from t; drop table t$$ AS note",
    'SELECT $$it\'s "quoted" -- not a comment$$',
    "SELECT call_count FROM t",
    "SELECT 'call me', \"CALL\" FROM t",
    "SELECT $1 FROM t",
    "SELECT $$x$$;",
    'SELECT 1 AS "a "" b;"',
    "SELECT 'insert' AS w -- delete",
    'SELECT "update" FROM t // drop',
    "SELECT updated_at, deleted FROM t",
]


@pytest.mark.parametrize("sql", REFUSED_SQL)
def test_refused_statements_make_no_request(sf: FakeSnowflakeAccount, sql: str) -> None:
    with pytest.raises(AdapterFailure) as raised:
        _invoke(sf, {"sql": sql})
    assert raised.value.error_class is ErrorClass.CAPABILITY
    assert sql not in raised.value.message
    assert sf.requests == []


@pytest.mark.parametrize("sql", ACCEPTED_SQL)
def test_read_statements_pass_the_snowflake_check(sql: str) -> None:
    sqlapi.check_read_only(sql, dialect="snowflake")


def test_an_accepted_dollar_quoted_select_reaches_the_account(sf: FakeSnowflakeAccount) -> None:
    _invoke(sf, {"sql": "SELECT $$a;b$$"})
    assert [b["statement"] for b in sf.bodies()] == ["SELECT $$a;b$$"]


def test_write_skips_the_keyword_check(sf: FakeSnowflakeAccount) -> None:
    result = _invoke(sf, {"sql": "INSERT INTO t VALUES (1)"}, write=True)
    assert result.result == {"columns": [], "rows": [], "row_count": 0}
    assert [b["statement"] for b in sf.bodies()] == ["INSERT INTO t VALUES (1)"]


def test_a_failed_statement_does_not_echo_sql(sf: FakeSnowflakeAccount) -> None:
    sf.faults.add("server_error")
    with pytest.raises(AdapterFailure) as raised:
        _invoke(sf, {"sql": "SELECT secret_column FROM t", "params": ["SECRET"]})
    assert raised.value.error_class is ErrorClass.UPSTREAM
    assert "secret_column" not in raised.value.message and "SECRET" not in raised.value.message


# ------------------------------------------------------------------ discover
def test_discover_payload_shape(sf: FakeSnowflakeAccount) -> None:
    found = ADAPTER.discover(ctx_for(sf), sf_conn())
    assert found.kind == "schema" and found.payload["truncated"] is False
    tables = found.payload["tables"]
    assert {(t["catalog"], t["schema"], t["name"]) for t in tables} == {
        ("ANALYTICS", "PUBLIC", "CUSTOMERS"),
        ("ANALYTICS", "PUBLIC", "ORDERS"),
        ("SALES", "RAW", "INVOICES"),
    }
    assert tables[0] == {
        "catalog": "ANALYTICS",
        "schema": "PUBLIC",
        "name": "CUSTOMERS",
        "table_type": None,
        "columns": [
            {"name": "ID", "type": "NUMBER", "nullable": False},
            {"name": "LABEL", "type": "TEXT", "nullable": True},
        ],
    }
    statements = [b["statement"] for b in sf.bodies()]
    assert statements[0] == "SHOW DATABASES"
    assert statements[1] == (
        "SELECT table_schema, table_name, column_name, data_type, is_nullable "
        'FROM "ANALYTICS".INFORMATION_SCHEMA.COLUMNS '
        "WHERE table_schema <> 'INFORMATION_SCHEMA' ORDER BY 1, 2 LIMIT 20000"
    )


def test_discover_quotes_a_database_name_with_a_quote(sf: FakeSnowflakeAccount) -> None:
    sf.tree = {'we"ird': {"S": {"T": [("C", "TEXT", True)]}}}
    found = ADAPTER.discover(ctx_for(sf), sf_conn(databases=['we"ird']))
    assert [(t["catalog"], t["name"]) for t in found.payload["tables"]] == [('we"ird', "T")]
    (statement,) = [b["statement"] for b in sf.bodies()]
    assert 'FROM "we""ird".INFORMATION_SCHEMA.COLUMNS' in statement
    assert "SHOW DATABASES" not in statement


def test_discover_caps_the_databases(sf: FakeSnowflakeAccount) -> None:
    sf.tree = {f"DB{i}": {} for i in range(60)}
    found = ADAPTER.discover(ctx_for(sf), sf_conn())
    assert found.payload == {"tables": [], "truncated": True}
    assert len(sf.bodies()) == 1 + 50


def test_discover_sets_truncated_at_the_column_cap(
    sf: FakeSnowflakeAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snowflake, "MAX_COLUMN_ROWS", 2)
    found = ADAPTER.discover(ctx_for(sf), sf_conn(databases=["SALES"]))
    assert found.payload["truncated"] is True


# ------------------------------------------------------------------ host rule
@pytest.mark.parametrize(
    "account",
    ["evil.com/x", "a@evil.com", "acct1.evil.example.com", "acct1:8443", "", "acct1#x", "a b"],
)
def test_an_account_that_leaves_the_suffix_is_refused_before_any_request(
    sf: FakeSnowflakeAccount, account: str
) -> None:
    stored = sf_conn(account=account)  # a row of the planned adapter took any account
    for call in (
        lambda: ADAPTER.discover(ctx_for(sf), stored),
        lambda: ADAPTER.open(ctx_for(sf), stored),
    ):
        with pytest.raises(AdapterFailure) as raised:
            call()
        assert raised.value.error_class is ErrorClass.CONFIG
        assert not account or account not in raised.value.message
    out = ADAPTER.test(ctx_for(sf), stored)
    assert out.error_class is ErrorClass.CONFIG
    assert sf.requests == []


def test_the_derived_host_has_the_only_suffix() -> None:
    assert snowflake.host_of("Acct_1.us-east-2.aws") == (
        "https://acct-1.us-east-2.aws.snowflakecomputing.com"
    )
    with pytest.raises(AdapterFailure):
        snowflake.host_of("acct1.snowflakecomputing.com.evil")


def test_a_fake_for_another_account_never_answers() -> None:
    other = make_snowflake(account="acct2")
    out = ADAPTER.test(ctx_for(other), sf_conn())
    assert out.error_class is ErrorClass.CONFIG  # 404 from a host it does not serve
