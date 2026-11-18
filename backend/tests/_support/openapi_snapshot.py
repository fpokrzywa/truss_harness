"""The committed OpenAPI snapshot `backend/openapi.json` (F-17).

The schema comes from `create_app()` with fixed test settings and a dummy database URL. The
startup role check (`Database` -> `check_app_role`, the only connection `create_app` opens) is
replaced by a no-op while the app is built, so it never connects to a database, and it reads no
environment file: it does not depend on the machine.

Refresh it after any route or schema change, or a version bump (`info.version`):
    cd backend; uv run python -m tests._support.openapi_snapshot
Check it (writes nothing; exit 1 and a diff when it is stale):
    cd backend; uv run python -m tests._support.openapi_snapshot --check
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any  # Any: JSON document
from unittest import mock

from shifde.app import create_app
from shifde.core.settings import Settings

SNAPSHOT = Path(__file__).resolve().parents[2] / "openapi.json"
REFRESH = "cd backend; uv run python -m tests._support.openapi_snapshot"
# never connected to: the role check is patched out and the engine connects lazily
DUMMY_URL = "postgresql+psycopg://openapi:openapi@127.0.0.1:1/openapi"


def build_schema() -> dict[str, Any]:
    settings = Settings(_env_file=None, env="test", database_url=DUMMY_URL)
    with mock.patch("shifde.core.db.check_app_role"):
        app = create_app(settings)
    return app.openapi()


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def diff(schema: dict[str, Any], path: Path = SNAPSHOT) -> str:
    """A unified text diff from the snapshot at `path` to `schema`; empty when they are equal as
    parsed JSON (line endings never count). A missing file differs from every schema."""
    committed: Any = json.loads(path.read_text("utf-8")) if path.exists() else None
    if committed == schema:
        return ""
    old = render(committed).splitlines(keepends=True) if committed is not None else []
    return "".join(
        difflib.unified_diff(old, render(schema).splitlines(keepends=True), str(path), "generated")
    )


def check(path: Path = SNAPSHOT) -> int:
    text = diff(build_schema(), path)
    if not text:
        return 0
    sys.stdout.write(text)
    print(f"\nThe OpenAPI snapshot {path} is stale. Refresh it with:\n    {REFRESH}")
    return 1


def write(path: Path = SNAPSHOT) -> None:
    path.write_bytes(render(build_schema()).encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        return check()
    if args:
        print("usage: python -m tests._support.openapi_snapshot [--check]", file=sys.stderr)
        return 2
    write()
    print(f"wrote {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
