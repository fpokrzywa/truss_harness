"""The opt-in live check of an Entra registration (SP3-6, issue #83; design SP3 section 8;
DECISIONS B-2711; runbook `docs/runbooks/entra-live-validation.md`).

`test_real_entra_discovery_and_signing_keys` runs the real `OidcClient.run_checks` against
`https://login.microsoftonline.com/<tenant>/v2.0`: the discovery document's issuer must equal the
tenant's issuer and its JWKS must publish signing keys. It runs only when
`<checkout root>/.devdata/entra.env` exists and `-m live` selects it; else it is skipped, so it
never runs in CI, `factory/checks.sh` or a default pytest run. A full sign-in needs a person at
a browser and is a manual runbook step. Entra stays `experimental`: a passing run does not change
that.

The env file is read here, never from the process environment. No value of it appears in a
message, skip reason, exception or log line: every refusal names only the key or rule. The
parsing tests at the end need no network and no database.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from dotenv import dotenv_values

from shifde.core.egress import default_resolver
from shifde.core.settings import Settings
from shifde.core.ssrf import OutboundHttp
from shifde.identity.oidc import OidcClient
from tests.adapters.native.test_databricks_live import live_selected

LIVE_ENV = Path(__file__).resolve().parents[3] / ".devdata" / "entra.env"
NO_REGISTRATION = "live validation waiting on owner input: no Entra registration"
OPT_IN = "live validation is opt-in: run with -m live"
KEYS = ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID")


class LiveConfigRefused(ValueError):
    """The live env file is unusable (fixed text naming only the key or rule)."""


@dataclass(frozen=True, repr=False)
class LiveConfig:
    tenant_id: str  # canonical lowercase GUID
    client_id: str

    def __repr__(self) -> str:
        return "LiveConfig(<values hidden>)"

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"


def read_live_config(path: Path) -> LiveConfig:
    """The Entra registration of the env file at `path`, else `LiveConfigRefused`."""
    # no interpolation: a `${VAR}` in a value is never read from the process environment
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    for key in KEYS:
        if not values.get(key):
            raise LiveConfigRefused(f"{key} is missing or empty in the live env file")
    try:
        tenant = str(UUID(str(values["ENTRA_TENANT_ID"])))
    except ValueError:
        raise LiveConfigRefused("ENTRA_TENANT_ID must be a GUID") from None
    return LiveConfig(tenant_id=tenant, client_id=str(values["ENTRA_CLIENT_ID"]))


@pytest.mark.live
def test_real_entra_discovery_and_signing_keys(request: pytest.FixtureRequest) -> None:
    if not LIVE_ENV.is_file():
        pytest.skip(NO_REGISTRATION)
    if not live_selected(str(request.config.getoption("markexpr") or "")):
        pytest.skip(OPT_IN)
    try:
        config = read_live_config(LIVE_ENV)
    except LiveConfigRefused as exc:
        pytest.fail(str(exc), pytrace=False)
    settings: Settings = request.getfixturevalue("settings")
    client = OidcClient(
        settings,
        OutboundHttp(
            allow_dev_loopback=False,
            resolver=default_resolver,
            timeout=settings.oidc_http_timeout_seconds,
        ),
    )
    checks = client.run_checks(f"{config.issuer}/.well-known/openid-configuration", config.issuer)
    # names and pass/fail only: a detail could carry the tenant's URLs
    assert [(name, ok) for name, ok, _ in checks] == [("discovery", True), ("jwks", True)], [
        (name, ok) for name, ok, _ in checks
    ]


# ------------------------------------------------------------------ the env file parser
GUID = "3F2A1B4C-5D6E-4F70-8192-A3B4C5D6E7F8"
CLIENT = "entra-live-test-client-never-shown"


def _env_file(directory: Path, **overrides: str | None) -> Path:
    values: dict[str, str | None] = {"ENTRA_TENANT_ID": GUID, "ENTRA_CLIENT_ID": CLIENT}
    values.update(overrides)
    path = directory / "entra.env"
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
    )
    return path


def test_a_complete_file_parses(tmp_path: Path) -> None:
    config = read_live_config(_env_file(tmp_path))
    assert config.tenant_id == GUID.lower() and config.client_id == CLIENT
    assert config.issuer == f"https://login.microsoftonline.com/{GUID.lower()}/v2.0"
    assert GUID.lower() not in repr(config) and CLIENT not in repr(config)


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_key_is_refused_by_name(tmp_path: Path, key: str, value: str | None) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, **{key: value}))
    assert str(refused.value) == f"{key} is missing or empty in the live env file"


def test_a_tenant_that_is_not_a_guid_is_refused_without_its_value(tmp_path: Path) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, ENTRA_TENANT_ID="contoso.onmicrosoft.com"))
    assert str(refused.value) == "ENTRA_TENANT_ID must be a GUID"
    error = refused.value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


def test_values_are_not_read_from_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIFDE_LIVE_TEST_TENANT", GUID)
    path = _env_file(tmp_path, ENTRA_TENANT_ID="${SHIFDE_LIVE_TEST_TENANT}")
    with pytest.raises(LiveConfigRefused, match="ENTRA_TENANT_ID must be a GUID"):
        read_live_config(path)
