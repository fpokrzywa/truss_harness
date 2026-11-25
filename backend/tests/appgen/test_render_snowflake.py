"""SP6-5: `appgen.render` renders a `snowflake.native-app/v1` release as a Native App package
(`appgen.render_snowflake`, SP6 design 3 and 6.3): the exact file set and deterministic bytes,
`ui/agent_loop.py` equal to the Databricks app's loop and its template, the same `manifest.yml` for
every release, the `setup.sql` rules (no INSERT grant, no `EXECUTE IMMEDIATE`, no external access,
no references, `IMPORTS` exactly the deep-health handler and its two config files, a stdlib-only
handler), `agent.json` and `tools.json` (positional SQL and `param_order`), `RUNTIME_VERSION` and
the Streamlit pin equal in `environment.yml` and the dev group, Snowpark pinned in
`environment.yml` only (Call S6-G, DECISIONS B-3998), no SETH origin, account address, key material
or secret-scan finding, the artifact cap, the generator version of the other platform refused both
ways, and a data-read table, tool SQL the app may not run or a tool that reads a table outside the
data reads (#358) refused before any byte (`release_not_renderable`)."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
import sys
import tomllib
from importlib.resources import files
from typing import Any  # Any: JSON documents

import pytest
import yaml

from shifde.adapters.native.base import create_archive, read_archive
from shifde.appgen.render import (
    GENERATOR_VERSION,
    GENERATOR_VERSIONS,
    GeneratorVersionUnsupported,
    render,
)
from shifde.appgen.render_snowflake import ReleaseNotRenderable
from shifde.delivery.plans import MAX_ARTIFACT_BYTES
from shifde.packaging.canonical import canonical, digest
from tests._support.appgen_fixtures import SNOWFLAKE_SAMPLE_DRAFT
from tests.appgen.rendered import (
    BAD_TABLES,
    CORTEX_MODEL,
    SAMPLE_DIGEST,
    SAMPLE_MANIFEST,
    SAMPLE_RESOLVED,
    SNOWFLAKE_DIGEST,
    SNOWFLAKE_KIND,
    SNOWFLAKE_MANIFEST,
    SNOWFLAKE_RESOLVED,
    input_schema,
)
from tests.appgen.test_render import BACKEND, FORBIDDEN, ROOT

TEMPLATES = files("shifde.appgen").joinpath("templates")
KEYS = {
    "manifest.yml",
    "setup.sql",
    "release.json",
    "ui/environment.yml",
    "ui/streamlit_app.py",
    "ui/agent_loop.py",
    "ui/backend_snowflake.py",
    "ui/deep_health.py",
    "ui/config/agent.json",
    "ui/config/tools.json",
    "ui/config/screens.json",
    "ui/config/limits.json",
}
# the archive scan of `test_render.py` plus key material and a Snowflake account address
SNOWFLAKE_FORBIDDEN = [
    *FORBIDDEN,
    re.compile("-----BEGIN"),
    re.compile(r"\.snowflakecomputing\.com"),
]


def _render(resolved: dict[str, Any] = SNOWFLAKE_RESOLVED) -> dict[str, bytes]:  # Any: JSON
    return render(SNOWFLAKE_MANIFEST, SNOWFLAKE_DIGEST, resolved)


def _setup() -> str:
    return _render()["setup.sql"].decode("utf-8")


def test_the_files_are_exactly_the_package_and_two_renders_are_equal() -> None:
    first, second = _render(), _render()
    assert set(first) == KEYS
    assert first == second
    a, b = create_archive(first), create_archive(second)
    assert a == b and hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
    assert read_archive(a) == first


def test_the_loop_is_the_databricks_apps_loop_and_its_template() -> None:
    loop = _render()["ui/agent_loop.py"]
    assert loop == render(SAMPLE_MANIFEST, SAMPLE_DIGEST, SAMPLE_RESOLVED)["app/agent_loop.py"]
    assert loop == TEMPLATES.joinpath("agent_loop.py.txt").read_bytes()


def test_the_fixed_files_are_the_templates() -> None:
    rendered, snowflake = _render(), TEMPLATES.joinpath("snowflake")
    for path, name in [
        ("manifest.yml", "manifest.yml.txt"),
        ("setup.sql", "setup.sql.txt"),
        ("ui/environment.yml", "environment.yml"),
        ("ui/streamlit_app.py", "streamlit_app.py.txt"),
        ("ui/backend_snowflake.py", "backend_snowflake.py.txt"),
        ("ui/deep_health.py", "deep_health.py.txt"),
    ]:
        assert rendered[path] == snowflake.joinpath(name).read_bytes(), path
    assert rendered["release.json"] == canonical(
        {"manifest": SNOWFLAKE_MANIFEST, "digest": SNOWFLAKE_DIGEST}
    )


def test_the_manifest_is_the_same_text_for_every_release() -> None:
    other = copy.deepcopy(SNOWFLAKE_MANIFEST)
    other["data_reads"][0]["tables"] = ["DEMO_DB.PUBLIC.ORDERS"]
    other_resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    other_resolved["tools"] = [
        t for t in other_resolved["tools"] if t["name"] == "orders_by_customer"
    ]
    other_resolved["agent"]["instructions"] = "Something else."
    other_resolved["model"]["serving_endpoint"] = "llama3.1-70b"
    mine, theirs = _render()["manifest.yml"], render(other, digest(other), other_resolved)
    assert theirs["release.json"] != _render()["release.json"]
    assert mine == theirs["manifest.yml"]
    assert yaml.safe_load(mine) == {
        "manifest_version": 1,
        "version": {"name": "V1", "label": "SHIFDE V1"},
        "artifacts": {"setup_script": "setup.sql", "default_streamlit": "APP_PUBLIC.UI"},
    }
    text = mine.decode("utf-8").lower()
    assert "privileges" not in text and "references" not in text and "external" not in text


def _statements(sql: str) -> list[str]:
    return [" ".join(s.split()) for s in sql.split(";\n") if s.strip()]


def test_setup_sql_creates_the_app_objects_and_grants_app_user_no_insert() -> None:
    sql = _setup()
    statements = _statements(sql)
    assert statements[:3] == [
        "CREATE APPLICATION ROLE IF NOT EXISTS APP_USER",
        "CREATE OR ALTER VERSIONED SCHEMA APP_PUBLIC",
        "CREATE SCHEMA IF NOT EXISTS APP_DATA",
    ]
    assert (
        "CREATE TABLE IF NOT EXISTS APP_DATA.USAGE (at TIMESTAMP_LTZ, release_digest STRING, "
        "model STRING, prompt_tokens NUMBER, completion_tokens NUMBER)" in statements
    )
    assert (
        "CREATE OR REPLACE SECURE FUNCTION APP_PUBLIC.HEALTH() RETURNS VARCHAR LANGUAGE SQL AS "
        "$$ SELECT 'OK' $$" in statements
    )
    assert (
        "CREATE OR REPLACE STREAMLIT APP_PUBLIC.UI FROM '/ui' MAIN_FILE = '/streamlit_app.py'"
        in statements
    )
    [procedure] = [
        s for s in statements if "PROCEDURE APP_PUBLIC.DEEP_HEALTH()" in s and "CREATE" in s
    ]
    assert "LANGUAGE PYTHON" in procedure and "RETURNS VARIANT" in procedure
    assert "PACKAGES = ('snowflake-snowpark-python')" in procedure
    assert "HANDLER = 'deep_health.run'" in procedure
    grants = [s for s in statements if s.startswith("GRANT")]
    assert grants == [
        "GRANT USAGE ON SCHEMA APP_PUBLIC TO APPLICATION ROLE APP_USER",
        "GRANT USAGE ON FUNCTION APP_PUBLIC.HEALTH() TO APPLICATION ROLE APP_USER",
        "GRANT USAGE ON PROCEDURE APP_PUBLIC.DEEP_HEALTH() TO APPLICATION ROLE APP_USER",
        "GRANT USAGE ON STREAMLIT APP_PUBLIC.UI TO APPLICATION ROLE APP_USER",
        "GRANT USAGE ON SCHEMA APP_DATA TO APPLICATION ROLE APP_USER",
        "GRANT SELECT ON TABLE APP_DATA.USAGE TO APPLICATION ROLE APP_USER",
    ]
    upper = sql.upper()
    for forbidden in ("INSERT", "EXECUTE IMMEDIATE", "EXTERNAL_ACCESS", "INTEGRATION", "REFERENCE"):
        assert forbidden not in upper, forbidden
    assert len(statements) == 13


def test_imports_list_the_handler_and_its_config_and_the_handler_is_stdlib_only() -> None:
    found = re.search(r"IMPORTS = \(([^)]*)\)", _setup())
    assert found is not None
    imports = [p.strip().strip("'") for p in found.group(1).split(",")]
    assert imports == ["/ui/deep_health.py", "/ui/config/tools.json", "/ui/config/agent.json"]
    assert all(p.removeprefix("/") in _render() for p in imports)
    tree = ast.parse(_render()["ui/deep_health.py"])
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules and modules <= set(sys.stdlib_module_names) | {"__future__"}, modules


def test_agent_json_names_the_cortex_model_and_the_tables_uppercased() -> None:
    rendered = _render()
    for name in ("agent", "tools", "screens", "limits"):
        content = rendered[f"ui/config/{name}.json"]
        encoded = json.dumps(json.loads(content), sort_keys=True, separators=(",", ":"))
        assert content == encoded.encode("utf-8")
    assert json.loads(rendered["ui/config/agent.json"]) == {
        "name": SNOWFLAKE_RESOLVED["agent"]["name"],
        "instructions": SNOWFLAKE_RESOLVED["agent"]["instructions"],
        "cortex_model": CORTEX_MODEL,
        "generator_version": "shifde.appgen.snowflake/1.0",
        "release_digest": SNOWFLAKE_DIGEST,
        # uppercased, de-duplicated and sorted
        "tables": ["DEMO_DB.PUBLIC.CUSTOMERS", "DEMO_DB.PUBLIC.ORDERS", "DEMO_DB.SALES.INVOICES"],
    }
    assert json.loads(rendered["ui/config/screens.json"]) == SNOWFLAKE_SAMPLE_DRAFT["screens"]
    assert json.loads(rendered["ui/config/limits.json"]) == {
        "max_iterations": 8,
        "max_tool_calls": 8,
        "deadline_s": 60,
    }


def test_tools_json_has_positional_sql_and_the_parameter_order() -> None:
    tools = json.loads(_render()["ui/config/tools.json"])
    assert [t["name"] for t in tools] == ["invoiced_by_region", "orders_by_customer"]
    for tool in tools:
        assert set(tool) == {
            "name",
            "description",
            "sql",
            "param_order",
            "parameters",
            "input_schema",
            "max_rows",
        }
    by_name = {t["name"]: t for t in tools}
    orders = by_name["orders_by_customer"]
    assert orders["param_order"] == ["customer_id", "since"]
    assert orders["sql"] == (
        "select o.id, o.amount, o.ordered_on from DEMO_DB.PUBLIC.ORDERS o "
        "where o.customer_id = ? and o.ordered_on >= ? order by o.id"
    )
    assert by_name["invoiced_by_region"]["param_order"] == ["region"]
    # a name used twice is listed twice; `:name` inside a string literal stays as written
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    resolved["tools"][0]["parameters"].append({"name": "n", "type": "integer", "required": True})
    resolved["tools"][0]["sql"] = (
        "select ':region' as label, ':x''s' as q from DEMO_DB.PUBLIC.CUSTOMERS c "
        "where c.region = :region or c.name = :region or c.id = :n"
    )
    [tool, _] = json.loads(_render(resolved)["ui/config/tools.json"])
    assert tool["sql"] == (
        "select ':region' as label, ':x''s' as q from DEMO_DB.PUBLIC.CUSTOMERS c "
        "where c.region = ? or c.name = ? or c.id = ?"
    )
    assert tool["param_order"] == ["region", "region", "n"]


def _refused(manifest: dict[str, Any], resolved: dict[str, Any]) -> str:  # Any: JSON
    """The detail of the 409 the render raises."""
    with pytest.raises(ReleaseNotRenderable) as caught:
        render(manifest, digest(manifest), resolved)
    assert caught.value.status == 409 and caught.value.code == "release_not_renderable"
    return caught.value.detail


@pytest.mark.parametrize("table", BAD_TABLES)
def test_a_data_read_that_is_not_a_plain_table_name_is_refused(table: str) -> None:
    """The sealed-release API takes any text as a table name; deep health would put it into SQL."""
    manifest = copy.deepcopy(SNOWFLAKE_MANIFEST)
    manifest["data_reads"][0]["tables"].append(table)
    assert "is not database.schema.table of plain names" in _refused(manifest, SNOWFLAKE_RESOLVED)


@pytest.mark.parametrize("table", ["demo_db.public.orders", "_D$1.S_2.T$", "DEMO_DB.PUBLIC.TABLE"])
def test_a_plain_table_name_renders_uppercased(table: str) -> None:
    manifest = copy.deepcopy(SNOWFLAKE_MANIFEST)
    manifest["data_reads"][0]["tables"] = [table]
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    resolved["tools"] = [
        {
            **resolved["tools"][0],
            "sql": f"select id from {table}",
            "parameters": [],
            "input_schema": input_schema([]),
        }
    ]
    agent = json.loads(render(manifest, digest(manifest), resolved)["ui/config/agent.json"])
    assert agent["tables"] == [table.upper()]


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (
            "select c.doc:field from DEMO_DB.PUBLIC.CUSTOMERS c",
            ":field is not a declared parameter",
        ),
        (
            "select c.id from DEMO_DB.PUBLIC.CUSTOMERS c where c.region = :region$x",
            ":region$x is not a declared parameter",
        ),
        ("delete from DEMO_DB.PUBLIC.CUSTOMERS", "must start with SELECT or WITH"),
        ("select 1; select 2", "exactly one statement"),
        ("select c.id from DEMO_DB.PUBLIC.CUSTOMERS c -- x", "comments"),
        ("select c.id from DEMO_DB.PUBLIC.CUSTOMERS c where c.region = ?", "positional ?"),
        ('select c."id" from DEMO_DB.PUBLIC.CUSTOMERS c', '"quoted" identifier'),
        ("select 'x from DEMO_DB.PUBLIC.CUSTOMERS c", "is not closed"),
        # the rules shared with the runtime check (#358)
        ("select system$wait(5)", "system functions are not allowed"),
        ('select "SYSTEM$WAIT"(5)', "system functions are not allowed"),
        ("select * from identifier(:region||'x')", "must hold one plain string literal or name"),
        ("select * from identifier(:region)", "must hold one plain string literal or name"),
        (
            "select 1 ->> execute immediate 'drop table x'",
            "the ->> flow operator is not allowed",
        ),
        # a table outside the data reads (#358)
        (
            "select s.id from DEMO_DB.PUBLIC.SECRETS s",
            "DEMO_DB.PUBLIC.SECRETS is not allowed",
        ),
        ("select c.id from customers c", "CUSTOMERS must be named as database.schema.table"),
        (
            "select c.id from DEMO_DB.PUBLIC.CUSTOMERS c where c.id in "
            "(select s.id from DEMO_DB.PUBLIC.SECRETS s)",
            "DEMO_DB.PUBLIC.SECRETS is not allowed",
        ),
    ],
)
def test_tool_sql_the_app_may_not_run_is_refused(sql: str, reason: str) -> None:
    """A tool published before the statement rules, or with a `:name` that SETH (which binds
    `%(name)s`) reads as SQL text."""
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    resolved["tools"][0]["sql"] = sql
    detail = _refused(SNOWFLAKE_MANIFEST, resolved)
    assert "invoiced_by_region" in detail and reason in detail


def test_a_declared_table_in_another_letter_case_renders() -> None:
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    sql = "select c.id from Demo_Db.public.CUSTOMERS c where c.region = :region"
    resolved["tools"][0]["sql"] = sql
    [tool, _] = json.loads(_render(resolved)["ui/config/tools.json"])
    assert tool["sql"] == sql.replace(":region", "?")


def test_a_table_of_any_data_read_is_declared() -> None:
    """A resolved tool names no connection: its tables are compared with every data read's."""
    manifest = copy.deepcopy(SNOWFLAKE_MANIFEST)
    manifest["data_reads"].append(
        {
            "connection_id": "00000000-0000-0000-0000-00000000c003",
            "tables": ["demo_db.public.more"],
        }
    )
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    resolved["tools"][0]["sql"] = "select m.id from DEMO_DB.PUBLIC.MORE m where m.r = :region"
    assert "ui/config/tools.json" in render(manifest, digest(manifest), resolved)
    assert "DEMO_DB.PUBLIC.MORE is not allowed" in _refused(SNOWFLAKE_MANIFEST, resolved)


def test_refusals_keep_their_order() -> None:
    """A data-read name first; then, per tool, the statement, the parameters, the tables."""
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    resolved["tools"][0]["sql"] = "select s.doc:field, system$x() from DEMO_DB.PUBLIC.SECRETS s"
    manifest = copy.deepcopy(SNOWFLAKE_MANIFEST)
    manifest["data_reads"][0]["tables"].append("ORDERS")
    assert "is not database.schema.table" in _refused(manifest, resolved)
    assert "system functions are not allowed" in _refused(SNOWFLAKE_MANIFEST, resolved)
    resolved["tools"][0]["sql"] = "select s.doc:field from DEMO_DB.PUBLIC.SECRETS s"
    assert ":field is not a declared parameter" in _refused(SNOWFLAKE_MANIFEST, resolved)
    resolved["tools"][0]["sql"] = "select s.doc from DEMO_DB.PUBLIC.SECRETS s"
    assert "DEMO_DB.PUBLIC.SECRETS is not allowed" in _refused(SNOWFLAKE_MANIFEST, resolved)


def _environment() -> dict[str, str]:
    doc = yaml.safe_load(_render()["ui/environment.yml"])
    assert doc["channels"] == ["snowflake"]
    return dict(d.split("=", 1) for d in doc["dependencies"])


def test_the_runtime_version_is_the_environments_python() -> None:
    found = re.search(r"RUNTIME_VERSION = '([0-9.]+)'", _setup())
    assert found is not None
    assert found.group(1) == _environment()["python"] == "3.11"


def test_the_streamlit_pin_equals_the_dev_group_and_snowpark_is_the_apps_only() -> None:
    """Call S6-G and DECISIONS B-3998: `ui/environment.yml` pins Streamlit at the version the dev
    group pins for the generated app's tests, and Snowpark, which the backend never installs;
    neither is a runtime dependency."""
    environment = _environment()
    assert set(environment) == {"python", "streamlit", "snowflake-snowpark-python"}
    assert environment["snowflake-snowpark-python"] == "1.55.0"
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    dev = [d.lower() for d in project["dependency-groups"]["dev"]]
    pins = [d for d in dev if d.startswith(("streamlit", "snowflake"))]
    assert pins == [f"streamlit=={environment['streamlit']}"]
    runtime = [d.lower() for d in project["project"]["dependencies"]]
    assert not any(d.startswith(("streamlit", "snowflake")) for d in runtime)


def test_the_archive_has_no_seth_origin_account_address_or_key() -> None:
    for name, content in read_archive(create_archive(_render())).items():
        text = name + "\n" + content.decode("utf-8")
        hits = [rx.pattern for rx in SNOWFLAKE_FORBIDDEN if rx.search(text)]
        assert hits == [], (name, hits)


def test_no_rendered_file_has_a_secret_scan_finding() -> None:
    spec = importlib.util.spec_from_file_location(
        "scan_secrets", ROOT / "scripts" / "validate" / "scan-secrets.py"
    )
    assert spec is not None and spec.loader is not None
    scan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scan)
    _globs, allow_values = scan.load_allow(ROOT / "scripts" / "validate" / "scan-secrets.allow")
    for name, content in _render().items():
        assert scan.scan_file(name, content.decode("utf-8"), allow_values) == [], name


def _maximal() -> dict[str, Any]:  # Any: a Resolved document
    resolved = copy.deepcopy(SNOWFLAKE_RESOLVED)
    resolved["agent"]["instructions"] = "Answer with figures. " * 380 + "x" * 20  # 8000 chars
    parameters = [{"name": f"p{i}", "type": "string", "required": True} for i in range(10)]
    sql = "select id from DEMO_DB.PUBLIC.ORDERS where " + " and ".join(
        f"label = :p{i % 10}" for i in range(400)
    )
    sql = (sql + " " * 8000)[:8000]
    resolved["tools"] = [
        {
            **resolved["tools"][0],
            "name": f"tool_{i}",
            "description": "d" * 500,
            "sql": sql,
            "parameters": parameters,
            "input_schema": input_schema(parameters),
        }
        for i in range(8)
    ]
    resolved["screens"] = [
        {"kind": "chat", "title": f"Screen {i}", "placeholder": "p" * 200} for i in range(6)
    ]
    return resolved


@pytest.mark.parametrize("which", ["sample", "maximal"])
def test_the_archive_is_within_the_artifact_cap(which: str) -> None:
    resolved = SNOWFLAKE_RESOLVED if which == "sample" else _maximal()
    if which == "maximal":
        assert len(resolved["agent"]["instructions"]) == 8000
        assert all(len(t["sql"]) == 8000 for t in resolved["tools"])
    assert len(create_archive(_render(resolved))) <= MAX_ARTIFACT_BYTES


def test_each_platform_refuses_the_others_generator_version() -> None:
    assert GENERATOR_VERSIONS[SNOWFLAKE_KIND] != GENERATOR_VERSION
    snowflake_with_databricks = {
        **SNOWFLAKE_MANIFEST,
        "app": {"generator_version": GENERATOR_VERSION},
    }
    databricks_with_snowflake = {
        **SAMPLE_MANIFEST,
        "app": {"generator_version": GENERATOR_VERSIONS[SNOWFLAKE_KIND]},
    }
    for manifest, resolved in [
        (snowflake_with_databricks, SNOWFLAKE_RESOLVED),
        (databricks_with_snowflake, SAMPLE_RESOLVED),
    ]:
        with pytest.raises(GeneratorVersionUnsupported) as caught:
            render(manifest, digest(manifest), resolved)
        assert caught.value.status == 409 and caught.value.code == "generator_version_unsupported"
