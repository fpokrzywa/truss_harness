"""The opt-in live validation of the generated Snowflake Native App (SP6 design section 9, issue
#275; runbook "Native App" in `docs/runbooks/snowflake-live-validation.md`).

`test_a_native_app_against_a_development_account` drives the real adapter (as
`RealPlatform.snowflake_adapter` builds it) against the owner's development account: the #358 gate,
capabilities, the frozen Snowpark column-name table on the account's own Snowpark (DECISIONS B-4000,
S-043), a first deploy of the `SNOWFLAKE_SAMPLE_DRAFT` release to `verified` checks, the Snowpark
version the deep health procedure resolved to, and a second release as patch 1. It runs only when
`<checkout root>/.devdata/snowflake.env` exists, holds every deploy key and `-m live` selects it;
else it is skipped, so it never runs in CI, `factory/checks.sh` or a default pytest run. `apply` is
called once per release and never retried. The integration stays `experimental`: a passing run does
not change that.

The env file is read here, never from the process environment, and the read connection's user, key
and role are not among its keys. No value of it appears in a message, skip reason, exception or
`repr`: every refusal names only the key or rule. The default-suite tests below the live test need
no network and no database; they run the live test's own functions on the native fake.
"""

from __future__ import annotations

import ast
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any  # Any: JSON documents and SQL API values
from uuid import UUID, uuid4

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from dotenv import dotenv_values
from pydantic import SecretStr

from shifde.adapters import snowflake_jwt
from shifde.adapters.native.addresses import SnowflakeAddressRefused, snowflake_claim_identity
from shifde.adapters.native.base import (
    BuildContext,
    DeployContext,
    InMemoryArtifactStore,
    InMemoryJournal,
    ReadContext,
    create_archive,
)
from shifde.adapters.native.contracts import (
    ApplyReceipt,
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    Check,
    ObserveRequest,
    PlanRequest,
    ResourceBinding,
    SnowflakeApplicationSpec,
    Target,
    ValidateRequest,
    VerifyRequest,
)
from shifde.adapters.native.snowflake.adapter import SnowflakeNativeAdapter
from shifde.adapters.native.snowflake.clients import ACCOUNT, SnowflakeKeyPair, SnowflakeSqlApi
from shifde.adapters.native.snowflake.fake import FakeSnowflakeNativePlatform
from shifde.adapters.native.snowflake.source import identifier, literal, quoted
from shifde.appgen.render import render
from shifde.core.sqlrules import statement_rules
from shifde.delivery.schemas import SnowflakeBindings
from shifde.executor.credentials import snowflake_account
from shifde.executor.runner import FakePlatform, RealPlatform, toolchain_digest
from shifde.packaging.canonical import digest as manifest_digest
from tests._support.appgen_fixtures import SNOWFLAKE_SAMPLE_DRAFT
from tests._support.snowpark_types import COLUMN_NAMES, SNOWPARK_VERSION
from tests.adapters.dataplatforms.helpers import pkcs8, rsa_key
from tests.adapters.native.test_databricks_live import live_selected
from tests.appgen.rendered import (
    CORTEX_MODEL,
    load_module,
    resolved_of,
    snowflake_manifest_of,
    write_files,
)

LIVE_ENV = Path(__file__).resolve().parents[4] / ".devdata" / "snowflake.env"
NO_ACCOUNT = "live validation waiting on owner input: no Snowflake account"
OPT_IN = "live validation is opt-in: run with -m live"
RUNBOOK = "docs/runbooks/snowflake-live-validation.md"
KEYS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ACCOUNT_PURPOSE",
    "SNOWFLAKE_DEPLOY_USER",
    "SNOWFLAKE_DEPLOY_PRIVATE_KEY_FILE",
    "SNOWFLAKE_DEPLOY_ROLE",
    "SNOWFLAKE_LIVE_DATABASE",
    "SNOWFLAKE_CORTEX_MODEL",
)
KEY_FILE_KEY = "SNOWFLAKE_DEPLOY_PRIVATE_KEY_FILE"
UNREADABLE_KEY_FILE = f"{KEY_FILE_KEY} does not name a readable file"
BAD_KEY = f"{KEY_FILE_KEY} is not an unencrypted PKCS#8 RSA key of at least 2048 bits"
BAD_ACCOUNT = "SNOWFLAKE_ACCOUNT is not an organization-account name (<org>-<account>)"
GATE_PROBES = ("select 1 ->> select 2", "select system$wait(5)")
GATE_REFUSED = (
    "issue #358 is not fixed on this commit: the Snowflake SQL rules still accept a probe it "
    "refuses (S-043, S-047). No live Native App is deployed before it"
)
PREFIX = "SHIFDE_LIVE_"
SNOWPARK = "snowflake-snowpark-python"
NOT_REPORTED = "not reported"
CHECKS = ("native-version", "runtime-health", "app-deep-health")
SAMPLE_DATABASE = "DEMO_DB"
LIVE_TABLES = ("PUBLIC.CUSTOMERS", "PUBLIC.ORDERS", "SALES.INVOICES")
POLL_SECONDS = 15
DEADLINE_SECONDS = 20 * 60
sleep: Callable[[float], None] = time.sleep


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """These tests use no database: override the root conftest's per-test wipe."""


# ------------------------------------------------------------------ the env file
class LiveConfigRefused(ValueError):
    """The live env file is unusable (fixed text naming only the key or rule)."""


class LiveConfigMissing(LiveConfigRefused):
    """A required key is missing or empty: the live test skips instead of failing."""


@dataclass(frozen=True, repr=False)
class LiveConfig:
    host: str  # `https://<org>-<account>.snowflakecomputing.com`
    user: str
    role: str
    warehouse: str
    database: str
    cortex_model: str
    private_key: SecretStr  # the unencrypted PKCS#8 PEM

    def __repr__(self) -> str:
        return "LiveConfig(<values hidden>)"


def _fails(check: Callable[[str], object], value: str) -> bool:
    """Whether `check` refuses `value` (a pydantic `ValidationError` is a `ValueError`): a refusal
    raised after it has no context to show the value."""
    try:
        check(value)
    except ValueError:
        return True
    return False


def _key_text(path: Path) -> str | None:
    """The key file's text (empty when it is not text); None when it cannot be read."""
    try:
        return path.read_bytes().decode("utf-8")
    except OSError:
        return None
    except UnicodeDecodeError:
        return ""


def _host_of(account: str) -> str | None:
    """The host URL of an organization-account name, else None."""
    url = f"https://{account.lower().replace('_', '-')}.snowflakecomputing.com"
    try:
        # equal only for a bare `<org>-<account>`: a `privatelink` name reduces to another host
        return url if snowflake_claim_identity(url) == url else None
    except SnowflakeAddressRefused:
        return None


def read_live_config(path: Path) -> LiveConfig:
    """The development account of the env file at `path`, else `LiveConfigRefused`."""
    # no interpolation: a `${VAR}` in a value is never read from the process environment
    read = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    values = {key: str(read.get(key) or "") for key in KEYS}
    for key in KEYS:
        if not values[key]:
            raise LiveConfigMissing(f"{NO_ACCOUNT}: {key}")
    if values["SNOWFLAKE_ACCOUNT_PURPOSE"] != "development":
        raise LiveConfigRefused("SNOWFLAKE_ACCOUNT_PURPOSE must be development")
    host = _host_of(values["SNOWFLAKE_ACCOUNT"])
    if host is None:
        raise LiveConfigRefused(BAD_ACCOUNT)
    if not ACCOUNT.fullmatch(values["SNOWFLAKE_DEPLOY_USER"]):
        raise LiveConfigRefused("SNOWFLAKE_DEPLOY_USER is not valid")
    for key in ("SNOWFLAKE_DEPLOY_ROLE", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_LIVE_DATABASE"):
        if _fails(identifier, values[key]):
            raise LiveConfigRefused(f"{key} is not valid")
    model = values["SNOWFLAKE_CORTEX_MODEL"]
    if _fails(lambda value: _bindings("P", "A", "W", value), model):
        raise LiveConfigRefused("SNOWFLAKE_CORTEX_MODEL is not valid")
    key_file = Path(values[KEY_FILE_KEY])
    pem = _key_text(key_file if key_file.is_absolute() else path.parent / key_file)
    if pem is None:
        raise LiveConfigRefused(UNREADABLE_KEY_FILE)
    if isinstance(snowflake_jwt.private_key(pem), str):
        raise LiveConfigRefused(BAD_KEY)
    return LiveConfig(
        host=host,
        user=values["SNOWFLAKE_DEPLOY_USER"],
        role=identifier(values["SNOWFLAKE_DEPLOY_ROLE"]),
        warehouse=identifier(values["SNOWFLAKE_WAREHOUSE"]),
        database=identifier(values["SNOWFLAKE_LIVE_DATABASE"]),
        cortex_model=model,
        private_key=SecretStr(pem),
    )


def _bindings(package: str, application: str, warehouse: str, model: str) -> dict[str, str]:
    """The target bindings, checked as the target route checks them."""
    made = SnowflakeBindings(
        package=package, application=application, warehouse=warehouse, cortex_model=model
    )
    return {str(k): str(v) for k, v in made.model_dump().items()}


# ------------------------------------------------------------------ the #358 gate
class GateRefused(ValueError):
    """#358 is not fixed on this commit."""


def check_358(
    rules: Callable[[str], Sequence[object]] = lambda sql: statement_rules(
        sql, dialect="snowflake"
    ),
) -> None:
    """`GateRefused` while `rules` accepts a probe (an empty list of issues)."""
    if any(not rules(probe) for probe in GATE_PROBES):
        raise GateRefused(GATE_REFUSED)


# ------------------------------------------------------------------ the rendered release
@dataclass(frozen=True, repr=False)
class LiveRelease:
    digest: str
    files: dict[str, bytes]

    def __repr__(self) -> str:
        return "LiveRelease(<files hidden>)"


def live_release(database: str, model: str, *, title: str = "") -> LiveRelease:
    """The rendered sample release reading `database`'s tables with Cortex model `model`; `title`
    is added to the first screen's title, for a release of another digest."""

    def on_database(value: Any) -> Any:  # Any: JSON
        text = re.sub(
            re.escape(SAMPLE_DATABASE), lambda _: database, json.dumps(value), flags=re.IGNORECASE
        )
        return json.loads(text)

    draft = on_database(SNOWFLAKE_SAMPLE_DRAFT)
    draft["screens"][0]["title"] += title
    manifest = snowflake_manifest_of(draft)
    manifest["data_reads"] = on_database(manifest["data_reads"])
    digest = manifest_digest(manifest)
    return LiveRelease(digest, render(manifest, digest, resolved_of(draft, endpoint=model)))


def snowpark_pin(files: Mapping[str, bytes]) -> str:
    """The Snowpark version `ui/environment.yml` pins."""
    [pin] = [
        d.split("=", 1)[1]
        for d in yaml.safe_load(files["ui/environment.yml"])["dependencies"]
        if d.split("=", 1)[0] == SNOWPARK
    ]
    return str(pin)


def runtime_version(files: Mapping[str, bytes]) -> str:
    """The Python runtime of `setup.sql`'s deep health procedure."""
    [version] = re.findall(rb"RUNTIME_VERSION = '([0-9.]+)'", files["setup.sql"])
    return bytes(version).decode()


# ------------------------------------------------------------------ the column-name table
def column_check_sql(files: Mapping[str, bytes]) -> str:
    """One anonymous procedure (nothing is created in the account) on the rendered runtime and the
    pinned Snowpark: its version and the `schema` field name of one column per table entry."""
    aliases = ", ".join('1 AS "' + name.replace('"', '""') + '"' for name, _ in COLUMN_NAMES)
    body = (
        "import snowflake.snowpark\n"
        "def run(session):\n"
        f"    fields = session.sql({json.dumps('SELECT ' + aliases)}).schema.fields\n"
        "    version = snowflake.snowpark.__version__\n"
        "    return {'version': version, 'names': [f.name for f in fields]}\n"
    )
    return (
        "WITH COLUMN_CHECK AS PROCEDURE () RETURNS VARIANT LANGUAGE PYTHON "
        f"RUNTIME_VERSION = {literal(runtime_version(files))} "
        f"PACKAGES = ({literal(f'{SNOWPARK}=={snowpark_pin(files)}')}) HANDLER = 'run' "
        f"AS $$\n{body}$$ CALL COLUMN_CHECK()"
    )


def column_problems(
    answer: Any,  # Any: the procedure's VARIANT
    pin: str,
    column_name: Callable[[str], str],
) -> list[str]:
    """What differs from the frozen table, in fixed text and the table's own names."""
    doc = json.loads(answer) if isinstance(answer, str) else answer
    if not isinstance(doc, dict) or not isinstance(doc.get("names"), list):
        return ["the procedure's answer is not the expected object"]
    problems = []
    if doc.get("version") != pin:
        problems.append(f"the account's Snowpark is not the pinned {pin}")
    if len(doc["names"]) != len(COLUMN_NAMES):
        return [*problems, "the procedure did not answer one name per column"]
    for (name, expected), answered in zip(COLUMN_NAMES, doc["names"], strict=True):
        if answered != expected:
            problems.append(f"column {name!r}: Snowpark's name is not {expected!r}")
        elif column_name(answered) != name:
            problems.append(f"column {name!r}: column_name does not give it back")
    return problems


# ------------------------------------------------------------------ deep health's Snowpark
_INSTALLED = re.compile(
    rf"(?<![\w.-]){re.escape(SNOWPARK)}['\"]?\s*==\s*['\"]?(\d+(?:\.\d+)+)(?![\w.])"
)


def installed_snowpark(rows: Sequence[Mapping[str, Any]]) -> str | None:  # Any: SQL API values
    """The Snowpark version in the `installed_packages` property of `DESCRIBE PROCEDURE` rows;
    None when the property, the package or a plain version of it is not there."""
    found = {
        version
        for row in rows
        if str(row.get("property")).lower() == "installed_packages"
        for version in _INSTALLED.findall(str(row.get("value") or ""))
    }
    return found.pop() if len(found) == 1 else None


# ------------------------------------------------------------------ deploy, observe, verify
class LiveFailed(AssertionError):
    """A step of the run failed (fixed text, no value of the env file)."""


@dataclass
class LiveRun:
    """One package and application of one target, deployed release by release."""

    adapter: SnowflakeNativeAdapter
    target: Target
    application_id: UUID = field(default_factory=uuid4)
    receipt: ApplyReceipt | None = None
    artifacts: InMemoryArtifactStore = field(default_factory=InMemoryArtifactStore)

    @property
    def toolchain(self) -> str:
        return toolchain_digest(RealPlatform.kind, "snowflake")

    def read(self) -> ReadContext:
        """A fresh read context (its one-hour deadline starts now) with the owned resources."""
        owned: tuple[ResourceBinding, ...] = tuple(self.receipt.resources) if self.receipt else ()
        return ReadContext(
            tenant_id=self.target.tenant_id, target=self.target, owned_resources=owned
        )

    @property
    def names(self) -> str:
        return (
            f"package {self.target.bindings['package']}, "
            f"application {self.target.bindings['application']}"
        )

    def build(self, release: LiveRelease) -> BuildArtifact:
        tenant, spec = self.target.tenant_id, SnowflakeApplicationSpec()
        context = BuildContext(
            tenant_id=tenant, artifacts=self.artifacts, toolchain_digest=self.toolchain
        )
        source = self.artifacts.put(tenant, create_archive(release.files))
        validation = _step(
            "validate",
            lambda: self.adapter.validate(ValidateRequest(spec=spec, source=source), context),
        )
        if not validation.passed:
            raise LiveFailed("the rendered app failed validation")
        return _step(
            "build",
            lambda: self.adapter.build(
                BuildRequest(spec=spec, source=source, application_id=self.application_id), context
            ),
        )

    def deploy(self, release: LiveRelease) -> ApplyReceipt:
        """Plan, then apply once (never retried); the receipt."""
        artifact = self.build(release)
        read = self.read()
        plan = _step("plan", lambda: self.adapter.plan(PlanRequest(artifact=artifact), read))
        operation = uuid4()
        context = DeployContext(
            tenant_id=self.target.tenant_id,
            target=self.target,
            owned_resources=read.owned_resources,
            operation_id=operation,
            artifacts=self.artifacts,
            journal=InMemoryJournal(self.target.tenant_id, operation),
            toolchain_digest=self.toolchain,
        )
        receipt, failure = None, ""
        try:
            receipt = self.adapter.apply(ApplyRequest(artifact=artifact, plan=plan), context)
        except Exception as exc:
            failure = type(exc).__name__
        if receipt is None:
            raise LiveFailed(
                f"apply raised {failure}: its outcome is unknown ({self.names}). Do not run apply "
                f'again; follow "Unknown outcomes" in {RUNBOOK}'
            )
        self.receipt = receipt
        print(f"applied {self.names}: patch {receipt.operation_handles['patch']}")
        return receipt

    def observe(self, receipt: ApplyReceipt) -> None:
        """Every 15 s until succeeded; `LiveFailed` on failed or after 20 minutes."""
        read, deadline = self.read(), time.monotonic() + DEADLINE_SECONDS
        while True:
            observed = _step(
                "observe", lambda: self.adapter.observe(ObserveRequest(receipt=receipt), read)
            )
            if observed.state == "succeeded":
                return
            if observed.state == "failed":
                raise LiveFailed("observe: failed")
            if time.monotonic() >= deadline:
                raise LiveFailed("observe: not succeeded within 20 minutes")
            sleep(POLL_SECONDS)

    def verify(self, receipt: ApplyReceipt, release: LiveRelease, database: str) -> None:
        """Every 15 s (read only) until the three checks pass; `LiveFailed` after 20 minutes."""
        read, deadline = self.read(), time.monotonic() + DEADLINE_SECONDS
        request = VerifyRequest(receipt=receipt, release_digest=release.digest)
        while True:
            report = _step("verify", lambda: self.adapter.verify(request, read))
            statuses = {c.name: c.status for c in report.checks}
            if statuses == dict.fromkeys(CHECKS, "passed"):
                return
            if time.monotonic() >= deadline:
                raise LiveFailed(
                    f"verify: not passed within 20 minutes: {failed_checks(report.checks, database)}"
                )
            sleep(POLL_SECONDS)


def failed_checks(checks: Sequence[Check], database: str) -> list[str]:
    """The names of the failed checks, deep health's own ones included: no reason, and the
    database as its env key."""
    out = []
    for check in checks:
        if check.status == "passed":
            continue
        out.append(check.name)
        if check.name != "app-deep-health":
            continue
        evidence = check.evidence or ""
        inner = {name: name for name in ("warehouse", "serving_endpoint")}
        inner |= {
            f"table:{database}.{table}": f"table:<SNOWFLAKE_LIVE_DATABASE>.{table}"
            for table in LIVE_TABLES
        }
        out += [shown for name, shown in inner.items() if f" {name}: " in evidence]
        if "release digest differs" in evidence:
            out.append("release_digest")
    return out


def _step[T](name: str, call: Callable[[], T]) -> T:
    """Run one adapter or SQL call; an exception is reported by its type only (its text may carry
    the account host)."""
    try:
        return call()
    except Exception as exc:
        failure = type(exc).__name__
    raise LiveFailed(f"{name} raised {failure}")


def live_target(config: LiveConfig) -> Target:
    """A target of new package and application names on the configured account."""
    run = PREFIX + secrets.token_hex(4).upper()
    return Target(
        tenant_id=uuid4(),
        host=config.host,
        bindings=_bindings(f"{run}_PKG", f"{run}_APP", config.warehouse, config.cortex_model),
    )


def credential_provider(config: LiveConfig, tenant: UUID) -> Callable[[], SnowflakeKeyPair]:
    return lambda: SnowflakeKeyPair(
        tenant_id=tenant,
        host=config.host,
        account=snowflake_account(config.host),
        user=config.user,
        role=config.role,
        private_key_pem=config.private_key.get_secret_value(),
    )


# ------------------------------------------------------------------ the live test
@pytest.mark.live
def test_a_native_app_against_a_development_account(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if not LIVE_ENV.is_file():
        pytest.skip(NO_ACCOUNT)
    if not live_selected(str(request.config.getoption("markexpr") or "")):
        pytest.skip(OPT_IN)
    try:
        config = read_live_config(LIVE_ENV)
    except LiveConfigMissing as exc:
        pytest.skip(str(exc))
    except LiveConfigRefused as exc:
        pytest.fail(str(exc), pytrace=False)
    try:
        _live_run(config, tmp_path)
    except (GateRefused, LiveFailed) as exc:
        pytest.fail(str(exc), pytrace=False)


def _live_run(config: LiveConfig, tmp_path: Path) -> None:
    # 1. the #358 gate, before any host is contacted
    check_358()
    release = live_release(config.database, config.cortex_model)
    target = live_target(config)
    provider = credential_provider(config, target.tenant_id)
    run = LiveRun(RealPlatform().snowflake_adapter(target, provider), target)
    api = SnowflakeSqlApi(target, credential_provider=provider)

    # 2. capabilities: the pinned CLI and the metadata read
    report = _step("capabilities", lambda: run.adapter.capabilities(run.read()))
    statuses = {c.operation: c.status for c in report.capabilities}
    if statuses != {"cli": "supported", "metadata": "supported"}:
        raise LiveFailed(f"capabilities: {statuses}")

    # 3. the column-name table on the account's Snowpark, before any deploy
    files = release.files
    write_files(tmp_path / "app", files)
    backend = load_module(tmp_path / "app" / "ui" / "backend_snowflake.py", "live_sf_backend")
    pin = snowpark_pin(files)
    rows = _step(
        "column-name check",
        lambda: api.execute(column_check_sql(files), warehouse=config.warehouse).rows,
    )
    answer = next(iter(rows[0].values())) if rows else None
    problems = _step("column-name check", lambda: column_problems(answer, pin, backend.column_name))
    if problems:
        raise LiveFailed("column-name check: " + "; ".join(problems))
    print(f"column-name table equal on Snowpark {pin}")

    # 4. deploy and observe: one plan, one apply
    receipt = run.deploy(release)
    run.observe(receipt)

    # 5. verify, while the owner runs the hand grants
    print('run the "Hand grants" of the runbook for the application above now')
    run.verify(receipt, release, config.database)

    # 6. the Snowpark version the deep health procedure resolved to
    resolved = deep_health_snowpark(api, target)
    print(f"deep health's Snowpark: {resolved}; pinned: {pin}")

    # 7. a second release is patch 1
    second = live_release(config.database, config.cortex_model, title=" 2")
    deploy_patch_1(run, second, config.database)
    if resolved != pin:
        raise LiveFailed(f"deep health's Snowpark is {resolved}, the pin is {pin}")


def deploy_patch_1(run: LiveRun, release: LiveRelease, database: str) -> None:
    """Deploy `release` on the run's package and application; it must be patch 1 of V1."""
    receipt = run.deploy(release)
    handles = receipt.operation_handles
    if (handles.get("version"), handles.get("patch")) != ("V1", "1"):
        raise LiveFailed("the second release is not patch 1 of V1")
    run.observe(receipt)
    run.verify(receipt, release, database)


def deep_health_snowpark(api: SnowflakeSqlApi, target: Target) -> str:
    """The version, or "not reported" (also when the describe itself is refused)."""
    sql = (
        f'DESCRIBE PROCEDURE {quoted(target.bindings["application"])}."APP_PUBLIC"."DEEP_HEALTH"()'
    )
    try:
        rows = _step("describe", lambda: api.execute(sql).rows)
    except LiveFailed as exc:
        print(exc)
        return NOT_REPORTED
    return installed_snowpark(rows) or NOT_REPORTED


# ------------------------------------------------------------------ the env file parser
ACCOUNT_NAME = "myorg-liveaccount"
USER = "live_deployer_never_shown"
KEY_FILE = "live_deploy_key_never_shown.p8"
DATABASE = "LIVE_DB_NEVER_SHOWN"
PEM = pkcs8(rsa_key())
READ_USER, READ_ROLE, READ_KEY = "live_reader", "LIVE_READER", "live_read_key.p8"
BAD_IDENTIFIER = "not-an identifier"


def _env_file(directory: Path, pem: str = PEM, **overrides: str | None) -> Path:
    (directory / KEY_FILE).write_bytes(pem.encode())
    values: dict[str, str | None] = {
        "SNOWFLAKE_ACCOUNT": ACCOUNT_NAME,
        "SNOWFLAKE_WAREHOUSE": "LIVE_WH",
        "SNOWFLAKE_ACCOUNT_PURPOSE": "development",
        "SNOWFLAKE_DEPLOY_USER": USER,
        KEY_FILE_KEY: KEY_FILE,
        "SNOWFLAKE_DEPLOY_ROLE": "LIVE_DEPLOYER",
        "SNOWFLAKE_LIVE_DATABASE": DATABASE,
        "SNOWFLAKE_CORTEX_MODEL": CORTEX_MODEL,
        # the read test's keys: in the same file, never used here
        "SNOWFLAKE_USER": READ_USER,
        "SNOWFLAKE_PRIVATE_KEY_FILE": READ_KEY,
        "SNOWFLAKE_ROLE": READ_ROLE,
    }
    values.update(overrides)
    path = directory / "snowflake.env"
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
    )
    return path


def _pem_body(pem: str) -> str:
    return "".join(line for line in pem.splitlines() if not line.startswith("-----"))


def _encrypted() -> str:
    return (
        rsa_key()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"live-passphrase"),
        )
        .decode()
    )


def test_a_complete_file_parses(tmp_path: Path) -> None:
    config = read_live_config(_env_file(tmp_path, SNOWFLAKE_ACCOUNT="MyOrg-Live_Account"))
    assert config.host == "https://myorg-live-account.snowflakecomputing.com"
    assert (config.user, config.role, config.warehouse) == (USER, "LIVE_DEPLOYER", "LIVE_WH")
    assert (config.database, config.cortex_model) == (DATABASE, CORTEX_MODEL)
    assert config.private_key.get_secret_value() == PEM
    assert repr(config) == str(config) == "LiveConfig(<values hidden>)"


def test_an_absolute_key_path_parses(tmp_path: Path) -> None:
    absolute = tmp_path / "absolute.p8"
    absolute.write_bytes(PEM.encode())
    config = read_live_config(_env_file(tmp_path, **{KEY_FILE_KEY: str(absolute)}))
    assert config.private_key.get_secret_value() == PEM


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_key_is_the_no_account_text_and_its_name(
    tmp_path: Path, key: str, value: str | None
) -> None:
    with pytest.raises(LiveConfigMissing) as refused:
        read_live_config(_env_file(tmp_path, **{key: value}))
    assert str(refused.value) == (
        f"live validation waiting on owner input: no Snowflake account: {key}"
    )


def test_a_file_of_only_the_read_keys_misses_the_deploy_keys(tmp_path: Path) -> None:
    deploy: dict[str, str | None] = {k: None for k in KEYS if "DEPLOY" in k}
    (tmp_path / READ_KEY).write_bytes(PEM.encode())
    with pytest.raises(LiveConfigMissing, match="SNOWFLAKE_DEPLOY_USER$"):
        read_live_config(_env_file(tmp_path, **deploy))
    read_keys = {"SNOWFLAKE_USER", "SNOWFLAKE_PRIVATE_KEY_FILE", "SNOWFLAKE_ROLE"}
    assert not read_keys & set(KEYS)


def test_the_parsed_credential_is_the_deploy_one(tmp_path: Path) -> None:
    (tmp_path / READ_KEY).write_bytes(pkcs8(rsa_key("read")).encode())
    config = read_live_config(_env_file(tmp_path))
    credential = credential_provider(config, uuid4())()
    assert (credential.user, credential.role) == (USER, "LIVE_DEPLOYER")
    assert credential.private_key_pem == PEM and credential.account == ACCOUNT_NAME
    assert credential.host == config.host


REFUSALS: list[tuple[str, dict[str, str | None], str]] = [
    (
        PEM,
        {"SNOWFLAKE_ACCOUNT_PURPOSE": "production"},
        "SNOWFLAKE_ACCOUNT_PURPOSE must be development",
    ),
    *(
        (PEM, {"SNOWFLAKE_ACCOUNT": account}, BAD_ACCOUNT)
        for account in (
            "xy12345",
            "xy12345.west-europe.azure",
            "myorg-acct.privatelink",
            "myorg-acct.eu",
            "myorg-acct.snowflakecomputing.com",
            "evil.example.com/",
            "myorg-acct@evil",
            "not an account!",
        )
    ),
    (PEM, {KEY_FILE_KEY: f"missing-{KEY_FILE}"}, UNREADABLE_KEY_FILE),
    (_encrypted(), {}, BAD_KEY),
    (pkcs8(rsa_key("short", 1024)), {}, BAD_KEY),
    ("not a key", {}, BAD_KEY),
    *(
        (PEM, {key: BAD_IDENTIFIER}, f"{key} is not valid")
        for key in (
            "SNOWFLAKE_DEPLOY_USER",
            "SNOWFLAKE_DEPLOY_ROLE",
            "SNOWFLAKE_WAREHOUSE",
            "SNOWFLAKE_LIVE_DATABASE",
            "SNOWFLAKE_CORTEX_MODEL",
        )
    ),
    (PEM, {"SNOWFLAKE_CORTEX_MODEL": "Upper-Case"}, "SNOWFLAKE_CORTEX_MODEL is not valid"),
]


@pytest.mark.parametrize(
    ("pem", "overrides", "refusal"), REFUSALS, ids=[str(i) for i in range(len(REFUSALS))]
)
def test_a_refusal_names_only_the_key_or_rule(
    tmp_path: Path, pem: str, overrides: dict[str, str | None], refusal: str
) -> None:
    with pytest.raises(LiveConfigRefused) as refused:
        read_live_config(_env_file(tmp_path, pem=pem, **overrides))
    error = refused.value
    assert not isinstance(error, LiveConfigMissing)
    assert str(error) == refusal
    assert error.__cause__ is None and error.__context__ is None
    shown = [str(error), repr(error), *map(str, error.args)]
    hidden = [ACCOUNT_NAME, USER, KEY_FILE, DATABASE, _pem_body(PEM)[:40]]
    hidden += [str(value) for value in overrides.values()]
    assert not any(value in text for text in shown for value in hidden)


def test_values_are_not_read_from_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIFDE_LIVE_TEST_PURPOSE", "development")
    path = _env_file(tmp_path, SNOWFLAKE_ACCOUNT_PURPOSE="${SHIFDE_LIVE_TEST_PURPOSE}")
    with pytest.raises(LiveConfigRefused, match="SNOWFLAKE_ACCOUNT_PURPOSE must be development"):
        read_live_config(path)
    monkeypatch.setenv("SNOWFLAKE_DEPLOY_USER", USER)
    with pytest.raises(LiveConfigMissing, match="SNOWFLAKE_DEPLOY_USER$"):
        read_live_config(_env_file(tmp_path, SNOWFLAKE_DEPLOY_USER=None))


# ------------------------------------------------------------------ the #358 gate
@pytest.mark.parametrize("accepted", GATE_PROBES)
def test_the_gate_refuses_while_a_probe_is_accepted(accepted: str) -> None:
    with pytest.raises(GateRefused) as refused:
        check_358(lambda sql: [] if sql == accepted else ["refused"])
    assert str(refused.value) == GATE_REFUSED and "#358" in GATE_REFUSED


def test_the_gate_passes_when_every_probe_is_refused() -> None:
    asked: list[str] = []

    def refuse(sql: str) -> list[str]:
        asked.append(sql)
        return ["refused"]

    check_358(refuse)
    assert tuple(asked) == GATE_PROBES


# ------------------------------------------------------------------ the column-name check
RELEASE = live_release(SAMPLE_DATABASE, CORTEX_MODEL)


def test_the_frozen_table_is_of_the_pinned_snowpark() -> None:
    """Another pin in `ui/environment.yml` needs the table compared again on that version."""
    assert snowpark_pin(RELEASE.files) == SNOWPARK_VERSION


def test_the_anonymous_procedure_is_pinned_and_one_statement() -> None:
    files = dict(RELEASE.files)
    sql = column_check_sql(files)
    head, body, tail = sql.split("$$")
    assert f"PACKAGES = ('{SNOWPARK}=={SNOWPARK_VERSION}')" in head
    assert f"RUNTIME_VERSION = '{runtime_version(files)}'" in head
    assert head.startswith("WITH COLUMN_CHECK AS PROCEDURE ()") and tail == " CALL COLUMN_CHECK()"
    assert ";" not in head + tail and "CREATE" not in sql.upper()
    compile(body, "column_check", "exec")
    [select] = [
        node.value
        for node in ast.walk(ast.parse(body))
        if isinstance(node, ast.Constant) and str(node.value).startswith("SELECT ")
    ]
    aliases = ['1 AS "' + name.replace('"', '""') + '"' for name, _ in COLUMN_NAMES]
    assert select == "SELECT " + ", ".join(aliases)
    assert '1 AS "a ""b"" c"' in str(select)
    # both versions come from the rendered files
    files["ui/environment.yml"] = files["ui/environment.yml"].replace(b"1.55.0", b"9.9.9")
    files["setup.sql"] = files["setup.sql"].replace(b"'3.11'", b"'3.99'")
    changed = column_check_sql(files)
    assert f"'{SNOWPARK}==9.9.9'" in changed and "RUNTIME_VERSION = '3.99'" in changed


def test_the_column_names_of_the_answer_are_compared(tmp_path: Path) -> None:
    write_files(tmp_path, RELEASE.files)
    backend = load_module(tmp_path / "ui" / "backend_snowflake.py", "live_test_sf_backend")
    names = [expected for _, expected in COLUMN_NAMES]
    good = {"version": SNOWPARK_VERSION, "names": names}

    def problems(answer: Any) -> list[str]:  # Any: JSON
        return column_problems(answer, SNOWPARK_VERSION, backend.column_name)

    assert problems(good) == problems(json.dumps(good)) == []
    assert problems({**good, "version": "1.54.0"}) == [
        f"the account's Snowpark is not the pinned {SNOWPARK_VERSION}"
    ]
    assert problems({**good, "names": ['"ID"', *names[1:]]}) == [
        "column 'ID': Snowpark's name is not 'ID'"
    ]
    assert problems({**good, "names": names[1:]}) and problems(None) and problems("[]")
    assert column_problems(good, SNOWPARK_VERSION, lambda name: name)


# ------------------------------------------------------------------ installed_packages
def _described(value: object) -> list[dict[str, object]]:
    return [
        {"property": "signature", "value": "()"},
        {"property": "installed_packages", "value": value},
    ]


@pytest.mark.parametrize(
    ("rows", "version"),
    [
        (_described("['cloudpickle==3.0.0','snowflake-snowpark-python==1.55.0']"), "1.55.0"),
        (_described('["snowflake-snowpark-python==1.9.1","python==3.11.9"]'), "1.9.1"),
        ([{"property": "INSTALLED_PACKAGES", "value": f"{SNOWPARK}==1.55.0"}], "1.55.0"),
        ([{"property": "signature", "value": f"{SNOWPARK}==1.55.0"}], None),
        ([], None),
        (_described(None), None),
        (_described("['cloudpickle==3.0.0']"), None),
        (_described("['not-snowflake-snowpark-python==1.0.0']"), None),
        (_described("['snowflake-snowpark-python']"), None),
        (_described("['snowflake-snowpark-python==']"), None),
        (_described("['snowflake-snowpark-python==latest']"), None),
        (_described("['snowflake-snowpark-python==1.55.0rc1']"), None),
        (_described(f"['{SNOWPARK}==1.0.0','{SNOWPARK}==2.0.0']"), None),
    ],
)
def test_the_installed_snowpark_version_is_read(
    rows: list[dict[str, object]], version: str | None
) -> None:
    assert installed_snowpark(rows) == version


# ------------------------------------------------------------------ deployments on the native fake
def test_the_release_reads_the_live_database_with_the_live_model() -> None:
    made = live_release("OTHER$DB", "llama3.1-70b")
    text = b"\n".join(made.files.values()).decode()
    assert "demo_db" not in text.lower() and "OTHER$DB.PUBLIC.ORDERS" in text
    agent = json.loads(made.files["ui/config/agent.json"])
    assert agent["cortex_model"] == "llama3.1-70b" and agent["release_digest"] == made.digest
    assert [t.upper() for t in agent["tables"]] == [f"OTHER$DB.{t}" for t in LIVE_TABLES]
    again = live_release("OTHER$DB", "llama3.1-70b")
    other = live_release("OTHER$DB", "llama3.1-70b", title=" 2")
    assert (again.digest, again.files) == (made.digest, made.files) and other.digest != made.digest


def test_the_names_of_a_run_are_new(tmp_path: Path) -> None:
    config = read_live_config(_env_file(tmp_path))
    first, second = live_target(config), live_target(config)
    for key in ("package", "application"):
        assert first.bindings[key].startswith(PREFIX)
        assert first.bindings[key] != second.bindings[key]


def _fake_run(tmp_path: Path) -> tuple[LiveConfig, LiveRun, FakeSnowflakeNativePlatform]:
    path = _env_file(tmp_path, SNOWFLAKE_LIVE_DATABASE=SAMPLE_DATABASE)
    config = read_live_config(path)
    fake = FakeSnowflakeNativePlatform(
        environment="test", public_key=rsa_key().public_key(), account=ACCOUNT_NAME, user=USER
    )
    target = live_target(config)
    platform = FakePlatform(snowflake=fake, environment="test")
    adapter = platform.snowflake_adapter(target, credential_provider(config, target.tenant_id))
    return config, LiveRun(adapter, target), fake


def test_two_releases_deploy_and_verify_on_the_native_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live test's deploy, verify and patch-1 functions; a first verify that fails (a missing
    grant) is polled until it passes."""
    config, run, fake = _fake_run(tmp_path)
    faults = fake.data(config.host).faults
    denied = f"table_denied:{SAMPLE_DATABASE}.PUBLIC.ORDERS"
    faults.add(denied)
    slept: list[float] = []

    def granted(seconds: float) -> None:
        slept.append(seconds)
        faults.discard(denied)

    monkeypatch.setitem(globals(), "sleep", granted)
    report = run.adapter.capabilities(run.read())
    assert {c.operation: c.status for c in report.capabilities} == {
        "cli": "supported",
        "metadata": "supported",
    }
    release = live_release(config.database, config.cortex_model)
    receipt = run.deploy(release)
    run.observe(receipt)
    assert receipt.operation_handles["patch"] == "0" and slept == []
    run.verify(receipt, release, config.database)
    assert slept == [POLL_SECONDS]
    printed = capsys.readouterr().out
    assert run.names in printed and config.host not in printed and USER not in printed

    second = live_release(config.database, config.cortex_model, title=" 2")
    assert second.digest != release.digest
    before = len(fake.writes())
    deploy_patch_1(run, second, config.database)
    writes = "\n".join(fake.writes()[before:])
    assert "ADD PATCH 1 FOR VERSION V1" in writes and "CREATE APPLICATION" not in writes
    app = fake.state(config.host).applications[run.target.bindings["application"]]
    assert app["patch"] == 1 and slept == [POLL_SECONDS]


def test_a_verify_that_never_passes_names_the_checks_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run, fake = _fake_run(tmp_path)
    fake.data(config.host).faults.add(f"table_denied:{SAMPLE_DATABASE}.PUBLIC.ORDERS")
    monkeypatch.setitem(globals(), "DEADLINE_SECONDS", 0)
    release = live_release(config.database, config.cortex_model)
    receipt = run.deploy(release)
    with pytest.raises(LiveFailed) as failed:
        run.verify(receipt, release, config.database)
    assert str(failed.value) == (
        "verify: not passed within 20 minutes: "
        "['app-deep-health', 'table:<SNOWFLAKE_LIVE_DATABASE>.PUBLIC.ORDERS']"
    )


def test_an_apply_that_raises_is_sent_once_and_its_outcome_is_unknown(tmp_path: Path) -> None:
    config, run, fake = _fake_run(tmp_path)
    fake.faults.add("fail_after_accept:version-create")
    with pytest.raises(LiveFailed) as failed:
        run.deploy(live_release(config.database, config.cortex_model))
    assert "its outcome is unknown" in str(failed.value) and run.names in str(failed.value)
    assert RUNBOOK in str(failed.value) and config.host not in str(failed.value)
    assert failed.value.__cause__ is None and failed.value.__context__ is None
    assert sum(" ADD VERSION V1 " in write for write in fake.writes()) == 1


def test_a_refused_describe_is_not_reported(tmp_path: Path) -> None:
    """The native fake answers no `DESCRIBE PROCEDURE`."""
    config, run, fake = _fake_run(tmp_path)
    api = fake.gateway(run.target, credential_provider(config, run.target.tenant_id))
    assert deep_health_snowpark(api, run.target) == NOT_REPORTED
