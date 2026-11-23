"""The Snowflake SQL API client and the pinned CLI's stage copy (FDE SDK 1fd902b
`tests/adapters/test_snowflake_clients.py`, rewritten for the SQL API over httpx and the key-pair
CLI): fresh JWT per call, one statement per request, bounded polling and rows, no redirect, no
secret in an error; the CLI is version-checked, copies once, never sees the key on its argv or
environment, and its key file lives only for the call."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any  # Any: JSON bodies, subprocess keyword arguments
from uuid import uuid4

import httpx
import pytest

from shifde.adapters import oauth2
from shifde.adapters.native.contracts import Target
from shifde.adapters.native.snowflake import clients
from shifde.adapters.native.snowflake.clients import (
    SnowflakeSqlApi,
    SnowflakeStageCopier,
)
from tests.adapters.native.conftest import SF_HOST, SF_PEM, sf_credential

DIGEST = "e" * 64
KEY_LINE = SF_PEM.splitlines()[1]  # a line of the key material
ALLOWED_ENV = {
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "TMPDIR",
}


def _target(host: str = SF_HOST) -> Target:
    return Target(tenant_id=uuid4(), host=host)


def _answer(rows: list[list[Any]], columns: list[str], handle: str = "01b0-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "statementHandle": handle,
            "resultSetMetaData": {"numRows": len(rows), "rowType": [{"name": c} for c in columns]},
            "data": rows,
        },
    )


def _api(handler: Any, **kw: Any) -> SnowflakeSqlApi:  # Any: handler and overrides
    credential = sf_credential(**kw)
    return SnowflakeSqlApi(
        _target(), credential_provider=lambda: credential, transport=httpx.MockTransport(handler)
    )


# ------------------------------------------------------------------ SQL API
def test_each_call_is_one_statement_with_a_fresh_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter(range(1_900_000_000, 1_900_000_100))
    monkeypatch.setattr(clients, "clock", lambda: float(next(ticks)))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _answer([["ACCT1", "DEPLOY_ROLE"]], ["ACCOUNT", "ROLE"])

    api = _api(handler)
    cached = len(oauth2.TOKEN_CACHE._entries)
    first = api.execute("SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_ROLE() AS ROLE")
    api.execute("SELECT 2", warehouse="app_wh")
    assert first.rows == [{"account": "ACCT1", "role": "DEPLOY_ROLE"}]
    assert first.handle == "01b0-1"
    tokens = [r.headers["authorization"] for r in seen]
    assert len(set(tokens)) == 2 and all(t.startswith("Bearer ") for t in tokens)
    assert all(r.headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT" for r in seen)
    assert len(oauth2.TOKEN_CACHE._entries) == cached  # no cache
    bodies = [json.loads(r.content) for r in seen]
    assert bodies[0]["statement"].startswith("SELECT CURRENT_ACCOUNT()")
    assert bodies[0]["role"] == "DEPLOY_ROLE" and "warehouse" not in bodies[0]
    assert bodies[1]["warehouse"] == "APP_WH"
    assert all(b["parameters"]["MULTI_STATEMENT_COUNT"] == "1" for b in bodies)
    assert [str(r.url) for r in seen] == [f"{SF_HOST}/api/v2/statements"] * 2


def test_a_running_statement_is_polled_with_a_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clients, "sleep", lambda _s: None)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if len(calls) < 3:
            return httpx.Response(202, json={"statementHandle": "01b0-7", "code": "333334"})
        return _answer([["OK"]], ["STATUS"], "01b0-7")

    assert _api(handler).execute("SELECT 1").rows == [{"status": "OK"}]
    assert calls == [
        "POST /api/v2/statements",
        "GET /api/v2/statements/01b0-7",
        "GET /api/v2/statements/01b0-7",
    ]
    forever = _api(lambda r: httpx.Response(202, json={"statementHandle": "01b0-8"}))
    with pytest.raises(TimeoutError):
        forever.execute("SELECT 1")


def test_more_than_1000_rows_is_refused() -> None:
    rows = [[str(i)] for i in range(1001)]
    with pytest.raises(ValueError, match="bound"):
        _api(lambda r: _answer(rows, ["NAME"])).execute("SHOW STAGES")
    assert len(_api(lambda r: _answer(rows[:1000], ["NAME"])).execute("SHOW STAGES").rows) == 1000


def test_redirects_are_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example.com/"})

    with pytest.raises(RuntimeError, match="HTTP 302"):
        _api(handler).execute("SELECT 1")
    assert seen == [f"{SF_HOST}/api/v2/statements"]


def test_errors_never_carry_the_body_the_key_or_the_jwt() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["authorization"].removeprefix("Bearer "))
        return httpx.Response(422, json={"message": "secret-native-detail"})

    with pytest.raises(RuntimeError) as failure:
        _api(handler).execute("SELECT 1")
    message = str(failure.value)
    assert "secret-native-detail" not in message
    assert sent[0] not in message and KEY_LINE not in message


def test_a_bad_key_is_refused_with_a_fixed_reason_and_no_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _answer([], [])

    api = _api(handler, pem="not a pem")
    with pytest.raises(ValueError, match="PKCS#8"):
        api.execute("SELECT 1")
    assert seen == []


def test_the_key_pair_repr_hides_the_key() -> None:
    credential = sf_credential()
    assert KEY_LINE not in repr(credential) and "acct1" in repr(credential)
    assert KEY_LINE not in repr(_api(lambda r: _answer([], [])))


# ------------------------------------------------------------------ CLI
class _Run:
    """A patched `subprocess.run` recording each call and the key file while it exists."""

    def __init__(self, *, version: str = "Snowflake CLI version: 3.33.0", code: int = 0) -> None:
        self.version, self.code = version, code
        self.calls: list[tuple[list[str], dict[str, Any]]] = []  # Any: run keyword arguments
        self.key_file: Path | None = None
        self.key_text = ""
        self.key_mode = 0

    def __call__(self, argv: list[str], **kwargs: Any) -> CompletedProcess[bytes]:  # Any
        self.calls.append((argv, kwargs))
        if "--version" in argv:
            return CompletedProcess(argv, 0, stdout=self.version.encode(), stderr=b"")
        self.key_file = Path(argv[argv.index("--private-key-file") + 1])
        self.key_text = self.key_file.read_text(encoding="utf-8")
        self.key_mode = stat.S_IMODE(self.key_file.stat().st_mode)
        return CompletedProcess(argv, self.code, stdout=b"secret cli output", stderr=b"x")


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> _Run:
    recorder = _Run()
    monkeypatch.setattr(clients.subprocess, "run", recorder)
    monkeypatch.setattr(clients.shutil, "which", lambda name: f"/opt/snowcli/bin/{name}")
    return recorder


def _copier(credential_host: str = SF_HOST) -> SnowflakeStageCopier:
    credential = sf_credential(credential_host)
    return SnowflakeStageCopier(_target(), credential_provider=lambda: credential)


def test_the_copier_runs_one_hook_free_stage_copy(run: _Run, tmp_path: Path) -> None:
    _copier().copy(tmp_path, "shifde_pkg.SHIFDE.SOURCE", DIGEST)
    (version, _), (argv, kwargs) = run.calls
    assert version[1:] == ["--version"]
    assert argv[0] == "/opt/snowcli/bin/snow"
    at = argv.index("stage")
    assert argv[at : at + 5] == [
        "stage",
        "copy",
        str(tmp_path),
        f"@SHIFDE_PKG.SHIFDE.SOURCE/{DIGEST}/",
        "--no-overwrite",
    ]
    for flag in ("--no-auto-compress", "--temporary-connection", "--config-file"):
        assert flag in argv
    assert argv[argv.index("--host") + 1] == "acct1.snowflakecomputing.com"
    assert argv[argv.index("--authenticator") + 1] == "SNOWFLAKE_JWT"
    assert argv[argv.index("--format") + 1] == "JSON"
    assert "app" not in argv and "--force" not in argv
    assert not any("snowflake.yml" in a for a in argv)
    for args, kw in run.calls:
        assert KEY_LINE not in repr(args) and KEY_LINE not in repr(kw["env"])
        assert set(kw["env"]) <= ALLOWED_ENV
        assert "SNOWFLAKE_PRIVATE_KEY" not in kw["env"] and "DATABRICKS_TOKEN" not in kw["env"]
    assert kwargs["env"]["HOME"] == kwargs["cwd"] == str(run.key_file.parent)  # type: ignore[union-attr]
    assert run.key_text == SF_PEM
    if os.name == "posix":
        assert run.key_mode == 0o600
    assert run.key_file is not None and not run.key_file.parent.exists()


def test_a_failed_copy_reports_only_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failing = _Run(code=3)
    monkeypatch.setattr(clients.subprocess, "run", failing)
    monkeypatch.setattr(clients.shutil, "which", lambda name: name)
    with pytest.raises(RuntimeError) as failure:
        _copier().copy(tmp_path, "SHIFDE_PKG.SHIFDE.SOURCE", DIGEST)
    assert str(failure.value) == "Snowflake CLI command failed with exit code 3"
    assert failing.key_file is not None and not failing.key_file.parent.exists()


def test_a_wrong_cli_version_is_refused_before_copying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wrong = _Run(version="Snowflake CLI version: 3.34.0")
    monkeypatch.setattr(clients.subprocess, "run", wrong)
    monkeypatch.setattr(clients.shutil, "which", lambda name: name)
    with pytest.raises(ValueError, match="3.33.0"):
        _copier().copy(tmp_path, "SHIFDE_PKG.SHIFDE.SOURCE", DIGEST)
    assert len(wrong.calls) == 1
    with pytest.raises(ValueError, match="3.33.0"):
        _copier().check_version()


def test_check_version_accepts_the_pin(run: _Run) -> None:
    assert _copier().check_version() == "3.33.0"


@pytest.mark.parametrize(
    ("stage", "digest"),
    [("PKG;DROP.SHIFDE.SOURCE", DIGEST), ("PKG.SHIFDE.SOURCE", "../x"), ("PKG", "E" * 64)],
)
def test_the_copier_refuses_odd_stages_and_digests(
    run: _Run, tmp_path: Path, stage: str, digest: str
) -> None:
    with pytest.raises(ValueError):
        _copier().copy(tmp_path, stage, digest)
    assert run.calls == []


@pytest.mark.parametrize(
    "credential_host",
    [
        "https://acct2.snowflakecomputing.com",
        "https://evil.example.com",
        "https://acct1.snowflakecomputing.com.evil.com",
        "not a url",
    ],
)
def test_the_copier_refuses_a_credential_of_another_host_before_running(
    run: _Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_host: str
) -> None:
    made: list[str] = []
    real = clients.tempfile.TemporaryDirectory

    def recording(*args: Any, **kwargs: Any) -> Any:  # Any: TemporaryDirectory arguments
        directory = real(*args, **kwargs)
        made.append(directory.name)
        return directory

    monkeypatch.setattr(clients.tempfile, "TemporaryDirectory", recording)
    with pytest.raises(PermissionError, match="another account host"):
        _copier(credential_host).copy(tmp_path, "SHIFDE_PKG.SHIFDE.SOURCE", DIGEST)
    assert run.calls == [] and run.key_file is None and made == []


def test_the_copier_compares_the_credential_host_normalized(run: _Run, tmp_path: Path) -> None:
    _copier("https://ACCT1.snowflakecomputing.com:443/").copy(
        tmp_path, "SHIFDE_PKG.SHIFDE.SOURCE", DIGEST
    )
    argv = run.calls[-1][0]
    assert argv[argv.index("--host") + 1] == "acct1.snowflakecomputing.com"


@pytest.mark.parametrize(
    "host", ["https://evil.example.com", "http://acct1.snowflakecomputing.com", "https://10.0.0.1"]
)
def test_the_copier_is_bound_to_a_snowflakecomputing_com_target(host: str) -> None:
    target = _target().model_copy(update={"host": host})
    credential = sf_credential(host)
    with pytest.raises(ValueError):
        SnowflakeStageCopier(target, credential_provider=lambda: credential)


def test_a_missing_cli_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        _copier().check_version()
