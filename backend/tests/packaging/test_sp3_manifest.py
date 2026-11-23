"""SP5-3 (design 6.2): a version without screens still seals under `shifde.packaging/1`, its
manifest unchanged. `data/sp3_release_manifest.json` holds a manifest and its digest sealed by
the SP3 compiler (before screens existed); a version sealed now has the same keys, nesting,
order and values once every UUID and every 64-hex hash is replaced by a placeholder."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any  # Any: JSON values

from fastapi.testclient import TestClient

from shifde.packaging.canonical import digest
from tests._support.fullapp import World
from tests._support.p3util import ADMIN
from tests._support.p5util import user_session
from tests.packaging.helpers import (
    bindings,
    databricks_connection,
    new_version,
    seal,
    solution,
    stack,
    version_body,
)

STORED = Path(__file__).parent / "data" / "sp3_release_manifest.json"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HASH = re.compile(r"^[0-9a-f]{64}$")


def _placeholders(value: Any) -> Any:  # Any: a JSON value
    """`value` with every UUID string `<uuid>` and every 64-hex string `<hash>` (keys too)."""
    if isinstance(value, dict):
        return {_placeholders(k): _placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_placeholders(v) for v in value]
    if isinstance(value, str) and UUID.match(value):
        return "<uuid>"
    if isinstance(value, str) and HASH.match(value):
        return "<hash>"
    return value


def _shape(value: Any) -> str:  # Any: a JSON value
    """The placeholder form as JSON text in key order: equal text means equal keys, nesting,
    order and values."""
    return json.dumps(_placeholders(value))


def test_the_stored_manifest_matches_its_digest() -> None:
    stored = json.loads(STORED.read_text(encoding="utf-8"))
    assert digest(stored["manifest"]) == stored["digest"]


def test_a_version_without_screens_seals_the_sp3_manifest(client: TestClient, world: World) -> None:
    stored = json.loads(STORED.read_text(encoding="utf-8"))
    h = user_session(client, world.passwords, ADMIN)
    s = stack(h.client, h)
    lake = databricks_connection(h.client, h)
    sid = solution(h.client, h, "Sales assistant")["id"]
    version = new_version(
        h.client, h, sid, version_body(s.agent_v1, connection=lake, dataset=s.dataset)
    )
    assert version["spec"]["screens"] == []
    r = seal(h.client, h, sid, version["id"], bindings({s.model: s.provider}, {lake: lake}))
    assert r.status_code == 201, r.text
    manifest = r.json()["manifest"]
    assert r.json()["digest"] == digest(manifest)
    assert manifest["compiler_version"] == "shifde.packaging/1"
    assert "screens" not in manifest and "app" not in manifest
    assert list(manifest) == list(stored["manifest"])
    assert _shape(manifest) == _shape(stored["manifest"])
