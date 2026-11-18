"""The OpenAPI schema matches the committed `backend/openapi.json` (F-17), and the comparison
catches a difference (negative control)."""

from __future__ import annotations

import copy
from pathlib import Path

from tests._support import openapi_snapshot as snap


def test_schema_matches_committed_snapshot() -> None:
    text = snap.diff(snap.build_schema())
    assert not text, f"backend/openapi.json is stale; refresh it with: {snap.REFRESH}\n{text}"


def test_comparison_reports_a_changed_schema(tmp_path: Path) -> None:
    schema = snap.build_schema()
    changed = copy.deepcopy(schema)
    changed["paths"].pop(sorted(changed["paths"])[0])
    assert snap.diff(changed)  # the committed snapshot no longer matches

    stale = tmp_path / "openapi.json"
    stale.write_text(snap.render(changed), "utf-8")
    assert snap.check(stale) == 1
    assert stale.read_text("utf-8") == snap.render(changed)  # --check writes nothing

    fresh = tmp_path / "fresh.json"
    fresh.write_bytes(snap.render(schema).replace("\n", "\r\n").encode("utf-8"))
    assert snap.check(fresh) == 0  # parsed JSON is compared: CRLF line endings do not count
