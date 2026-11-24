"""SP5-5: `appgen.render.render` gives the Databricks App source of a `/2` release (design 6.3):
the exact file set, deterministic bytes and archive digest, the generator-version refusal,
`release.json` as `packaging.canonical`, `databricks.yml` as SP2's managed bundle, the committed
lock and web bundle byte for byte, no SETH origin or credential pattern and no secret-scan finding,
the 5 MB cap for sample and maximal inputs, and the R32 pins (the dev-group flask and
databricks-sdk equal `templates/requirements.in`, and neither is a runtime dependency)."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any  # Any: JSON documents

import pytest
import yaml

from shifde.adapters.native.base import create_archive, read_archive
from shifde.adapters.native.contracts import DatabricksApplicationSpec
from shifde.adapters.native.databricks.source import source_bundle
from shifde.appgen.render import GENERATOR_VERSION, GeneratorVersionUnsupported, render
from shifde.appgen.schemas import Resolved
from shifde.delivery.plans import MAX_ARTIFACT_BYTES
from shifde.packaging.canonical import canonical
from tests.appgen.rendered import (
    SAMPLE_DIGEST,
    SAMPLE_MANIFEST,
    SAMPLE_RESOLVED,
    input_schema,
)

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
TEMPLATES = files("shifde.appgen").joinpath("templates")
WEB = Path(str(TEMPLATES.joinpath("web")))
FIXED_KEYS = {
    "databricks.yml",
    "app/app.yaml",
    "app/app.py",
    "app/agent_loop.py",
    "app/requirements.txt",
    "app/config/agent.json",
    "app/config/tools.json",
    "app/config/screens.json",
    "app/config/limits.json",
    "release.json",
}
# a SETH origin or credential: the API prefix, SETH's settings, test hosts, key prefixes
FORBIDDEN = [
    re.compile(re.escape(p))
    for p in ("/api/v1/", "SHIFDE_", "testserver", "localhost", "127.0.0.1")
] + [re.compile(r"\bshx_"), re.compile(r"\bsk-")]


def _render() -> dict[str, bytes]:
    return render(SAMPLE_MANIFEST, SAMPLE_DIGEST, SAMPLE_RESOLVED)


def _web_files() -> dict[str, bytes]:
    return {
        "app/static/" + p.relative_to(WEB).as_posix(): p.read_bytes()
        for p in sorted(WEB.rglob("*"))
        if p.is_file()
    }


def test_two_renders_give_equal_files_and_archive_bytes() -> None:
    first, second = _render(), _render()
    assert first == second
    a, b = create_archive(first), create_archive(second)
    assert a == b
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
    # a model instance renders the same as its mapping
    assert render(SAMPLE_MANIFEST, SAMPLE_DIGEST, Resolved.model_validate(SAMPLE_RESOLVED)) == first


def test_the_rendered_keys_are_exactly_the_app_files() -> None:
    rendered = _render()
    assert set(rendered) == FIXED_KEYS | set(_web_files())
    assert "app/static/BUILD_DIGEST" in rendered and "app/static/index.html" in rendered
    assert read_archive(create_archive(rendered)) == rendered  # every key is a valid entry


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema_version": "shifde.release/1"},  # a /1 manifest has no app key
        {**SAMPLE_MANIFEST, "app": {}},
        {**SAMPLE_MANIFEST, "app": {"generator_version": "shifde.appgen/0.9"}},
        {**SAMPLE_MANIFEST, "app": "shifde.appgen/1.0"},
    ],
    ids=["v1-no-app", "no-version", "other-version", "not-an-object"],
)
def test_another_generator_version_is_refused(manifest: dict[str, Any]) -> None:
    with pytest.raises(GeneratorVersionUnsupported) as caught:
        render(manifest, SAMPLE_DIGEST, SAMPLE_RESOLVED)
    assert caught.value.status == 409 and caught.value.code == "generator_version_unsupported"


def test_release_json_is_the_canonical_release_document() -> None:
    expected = canonical({"manifest": SAMPLE_MANIFEST, "digest": SAMPLE_DIGEST})
    assert _render()["release.json"] == expected


def test_databricks_yml_is_sp2s_managed_bundle() -> None:
    rendered = _render()["databricks.yml"]
    bundle = source_bundle(DatabricksApplicationSpec())
    assert yaml.safe_load(rendered) == bundle
    assert rendered == yaml.safe_dump(bundle, sort_keys=False).encode()


def test_the_lock_service_and_web_bundle_are_copied_byte_for_byte() -> None:
    rendered = _render()
    assert rendered["app/requirements.txt"] == TEMPLATES.joinpath("requirements.txt").read_bytes()
    assert rendered["app/app.py"] == TEMPLATES.joinpath("app.py.txt").read_bytes()
    assert rendered["app/agent_loop.py"] == TEMPLATES.joinpath("agent_loop.py.txt").read_bytes()
    assert rendered["app/app.yaml"] == TEMPLATES.joinpath("app.yaml.txt").read_bytes()
    assert {k: v for k, v in rendered.items() if k.startswith("app/static/")} == _web_files()


def test_app_yaml_names_the_warehouse_and_serving_endpoint_resources() -> None:
    doc = yaml.safe_load(_render()["app/app.yaml"])
    assert doc == {
        "command": ["python", "app.py"],
        "env": [
            {"name": "DATABRICKS_WAREHOUSE_ID", "valueFrom": "sql-warehouse"},
            {"name": "SERVING_ENDPOINT", "valueFrom": "serving-endpoint"},
        ],
    }


def test_the_config_files_carry_the_resolved_release() -> None:
    rendered = _render()
    for name in ("agent", "tools", "screens", "limits"):
        content = rendered[f"app/config/{name}.json"]
        doc = json.loads(content)
        encoded = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert content == encoded.encode("utf-8")
    agent = json.loads(rendered["app/config/agent.json"])
    assert agent == {
        "name": SAMPLE_RESOLVED["agent"]["name"],
        "instructions": SAMPLE_RESOLVED["agent"]["instructions"],
        "serving_endpoint": "agent-endpoint",
        "generator_version": GENERATOR_VERSION,
        "release_digest": SAMPLE_DIGEST,
        # lowercased, de-duplicated and sorted
        "tables": ["main.default.customers", "main.default.orders", "main.sales.invoices"],
    }
    tools = json.loads(rendered["app/config/tools.json"])
    assert [t["name"] for t in tools] == ["invoiced_by_region", "orders_by_customer"]
    assert all(
        set(t) == {"name", "description", "sql", "parameters", "input_schema", "max_rows"}
        for t in tools
    )
    assert json.loads(rendered["app/config/screens.json"]) == SAMPLE_RESOLVED["screens"]
    assert json.loads(rendered["app/config/limits.json"]) == {
        "max_iterations": 8,
        "max_tool_calls": 8,
        "deadline_s": 60,
    }


def test_the_archive_has_no_seth_origin_or_credential_pattern() -> None:
    entries = read_archive(create_archive(_render()))
    for name, content in entries.items():
        text = name + "\n" + content.decode("utf-8")
        hits = [rx.pattern for rx in FORBIDDEN if rx.search(text)]
        assert hits == [], (name, hits)


def test_no_rendered_text_file_has_a_secret_scan_finding() -> None:
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
    resolved = copy.deepcopy(SAMPLE_RESOLVED)
    resolved["agent"]["instructions"] = "Answer with figures. " * 380 + "x" * 20  # 8000 chars
    parameters = [{"name": f"p{i}", "type": "string", "required": True} for i in range(10)]
    sql = "select id from main.default.orders where " + " and ".join(
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
    resolved = SAMPLE_RESOLVED if which == "sample" else _maximal()
    if which == "maximal":
        assert len(resolved["agent"]["instructions"]) == 8000
        assert all(len(t["sql"]) == 8000 and len(t["parameters"]) == 10 for t in resolved["tools"])
    archive = create_archive(render(SAMPLE_MANIFEST, SAMPLE_DIGEST, resolved))
    assert len(archive) <= MAX_ARTIFACT_BYTES


def test_the_dev_pins_equal_the_generated_apps_requirements_in() -> None:
    """R32: the generated app's libraries are pinned for the tests in the dev group only, at the
    versions of `templates/requirements.in`."""
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    lines = TEMPLATES.joinpath("requirements.in").read_text(encoding="utf-8").split()
    assert [line.lower() for line in lines] == ["flask==3.1.3", "databricks-sdk==0.163.0"]
    dev = [d.lower() for d in project["dependency-groups"]["dev"]]
    pins = [d for d in dev if d.split("==")[0] in ("flask", "databricks-sdk")]
    assert sorted(pins) == sorted(line.lower() for line in lines)
    runtime = [d.lower() for d in project["project"]["dependencies"]]
    assert not any(d.startswith(("flask", "databricks")) for d in runtime)
