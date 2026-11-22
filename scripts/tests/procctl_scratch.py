"""Scratch database for the procctl integration tests (`test_procctl.py`); run with the backend
venv's interpreter from the repository root.

    procctl_scratch.py create ENV_FILE   create and migrate `shifde_procctl_<hex>`, append its
                                         SHIFDE_DATABASE_URL and SHIFDE_MIGRATION_DATABASE_URL
                                         to ENV_FILE, print the database name
    procctl_scratch.py drop NAME         drop it

Credentials come from `SHIFDE_TEST_*_DATABASE_URL` (environment, then `.devdata/dev.env`), as in
`scripts/delivery_demo.py` `scratch_database`. It never prints a URL. Exit 1 on any failure.
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))  # the `tests` helpers

URL_KEYS = (
    "SHIFDE_TEST_ADMIN_DATABASE_URL",
    "SHIFDE_TEST_DATABASE_URL",
    "SHIFDE_TEST_MIGRATION_DATABASE_URL",
)


def _urls() -> tuple[str, str, str]:
    from tests.conftest import _env_or_none

    urls = [_env_or_none(key) for key in URL_KEYS]
    if not all(urls):
        raise SystemExit("procctl_scratch: .devdata/dev.env is missing or incomplete")
    return (str(urls[0]), str(urls[1]), str(urls[2]))


def create(env_file: Path) -> int:
    from alembic import command
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from shifde.core.migrations import alembic_config
    from tests.conftest import _create_database, _drop_database, _with_db

    admin_url, app_url, owner_url = _urls()
    name = f"shifde_procctl_{secrets.token_hex(6)}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as c:
            _create_database(
                c,
                name,
                make_url(owner_url).username or "shifde_owner",
                make_url(app_url).username or "shifde_app",
                "template0",
            )
        try:
            command.upgrade(alembic_config(_with_db(owner_url, name)), "heads")
        except Exception:
            with admin.connect() as c:
                _drop_database(c, name)
            raise SystemExit("procctl_scratch: the scratch database could not be migrated") from None
    finally:
        admin.dispose()
    with env_file.open("a", encoding="utf-8") as f:
        f.write(f"SHIFDE_DATABASE_URL={_with_db(app_url, name)}\n")
        f.write(f"SHIFDE_MIGRATION_DATABASE_URL={_with_db(owner_url, name)}\n")
    print(name)
    return 0


def drop(name: str) -> int:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from tests.conftest import _drop_database

    if not re.fullmatch(r"shifde_procctl_[0-9a-f]{12}", name):
        raise SystemExit("procctl_scratch: not a procctl scratch database name")
    admin = create_engine(_urls()[0], isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as c:
            _drop_database(c, name)
    finally:
        admin.dispose()
    return 0


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "create":
            return create(Path(argv[1]))
        if len(argv) == 2 and argv[0] == "drop":
            return drop(argv[1])
    except Exception as exc:  # noqa: BLE001 - a driver message can echo connection details
        raise SystemExit(f"procctl_scratch: failed ({exc.__class__.__name__})") from None
    raise SystemExit("usage: procctl_scratch.py create ENV_FILE | drop NAME")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
