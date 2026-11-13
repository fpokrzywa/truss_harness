"""Write `backend/tests/schema/seth_3ba0e06.json`: the catalog snapshot of SETH's final schema.

Run from the repository (or worktree) root, inside the backend environment, with the local
cluster running (`python scripts/dev_db.py start`):

    cd backend; uv run python ../scripts/make_seth_schema_snapshot.py

Steps (SETH's repository is only read, with `git archive`):
  1. extract SETH 3ba0e06 `backend/` into a scratch directory;
  2. `uv sync --frozen` there (SETH's own locked environment);
  3. create a scratch database on the local cluster (port 55450) owned by `shifde_owner`;
  4. run SETH's own migrations on it (`alembic upgrade head`, as `shifde_owner`);
  5. snapshot it with `backend/tests/schema/snapshot.py` and write the JSON;
  6. drop the scratch database and delete the scratch directory.

Connection details come from `SHIFDE_TEST_ADMIN_DATABASE_URL` and
`SHIFDE_TEST_MIGRATION_DATABASE_URL` (environment first, then `.devdata/dev.env`).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SETH_REPO = Path("S:/Development_2026/shi_work/shi_enterprise_truss_harness")
SETH_COMMIT = "3ba0e06"
OUTPUT = BACKEND / "tests" / "schema" / f"seth_{SETH_COMMIT}.json"

_MIGRATE = (
    "import os\n"
    "from alembic import command\n"
    "from shifde.core.migrations import alembic_config\n"
    "command.upgrade(alembic_config(os.environ['SETH_SNAPSHOT_URL']), 'head')\n"
)


def _env(key: str) -> str:
    from dotenv import dotenv_values

    value = os.environ.get(key)
    dev_env = ROOT / ".devdata" / "dev.env"
    if not value and dev_env.exists():
        value = dotenv_values(dev_env, encoding="utf-8-sig").get(key)
    if not value:
        raise SystemExit(f"{key} is not set (run: python scripts/dev_db.py start)")
    return value


def extract_seth(seth_repo: Path, commit: str, dest: Path) -> Path:
    archive = subprocess.run(
        ["git", "-C", str(seth_repo), "archive", "--format=tar", commit, "backend"],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return dest / "backend"


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHIFDE_")}
    env.pop("VIRTUAL_ENV", None)  # let uv use SETH's own project environment
    env["UV_LINK_MODE"] = "copy"
    env.update(extra)
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the SETH 3ba0e06 schema snapshot.")
    parser.add_argument("--seth", type=Path, default=SETH_REPO, help="SETH repository path")
    parser.add_argument("--out", type=Path, default=OUTPUT, help="output JSON path")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args(argv)

    # Third-party and backend imports need the backend environment (`uv run`, see the docstring).
    sys.path.insert(0, str(BACKEND))
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from tests.schema.snapshot import snapshot

    admin_url = _env("SHIFDE_TEST_ADMIN_DATABASE_URL")
    owner_url = _env("SHIFDE_TEST_MIGRATION_DATABASE_URL")
    app_role = make_url(_env("SHIFDE_TEST_DATABASE_URL")).username or "shifde_app"
    owner_role = make_url(owner_url).username or "shifde_owner"
    db_name = f"shifde_sethsnap_{secrets.token_hex(4)}"
    scratch = Path(tempfile.mkdtemp(prefix="seth-snapshot-"))
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    created = False
    try:
        print(f"extracting SETH {SETH_COMMIT} backend into {scratch}")
        seth_backend = extract_seth(args.seth, SETH_COMMIT, scratch)
        print("syncing SETH's environment (uv sync --frozen)")
        subprocess.run(
            ["uv", "sync", "--frozen", "--no-dev"],
            cwd=seth_backend,
            env=_child_env(),
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{db_name}" OWNER "{owner_role}"'))
            created = True
            c.execute(text(f'REVOKE ALL ON DATABASE "{db_name}" FROM PUBLIC'))
            c.execute(text(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{app_role}"'))
        snap_url = make_url(owner_url).set(database=db_name).render_as_string(hide_password=False)
        print(f"running SETH's migrations on scratch database {db_name}")
        subprocess.run(
            ["uv", "run", "--frozen", "--no-dev", "python", "-c", _MIGRATE],
            cwd=seth_backend,
            env=_child_env(SETH_SNAPSHOT_URL=snap_url),
            check=True,
        )
        engine = create_engine(snap_url, poolclass=NullPool)
        try:
            with engine.connect() as c:
                data = snapshot(c)
        finally:
            engine.dispose()
        args.out.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"wrote {args.out} ({len(data['tables'])} tables)")
    finally:
        if created:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin.dispose()
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
