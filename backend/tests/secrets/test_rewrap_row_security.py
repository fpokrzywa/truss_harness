"""`secrets_service.rewrap_all` and `shifde-admin secrets rewrap` under the app role (issue #218).

Behaviour: under a role subject to row-level security nothing is re-wrapped: `rewrap_all` raises
`RowSecurityHidesSecrets` with `ROW_SECURITY_BLOCKS_REWRAP` before reading any row, and the command
exits 1 with that fixed message and no `re-wrapped` line. Every secret keeps its original key id,
and the output carries no secret id, no database URL and no key material.
"""

from __future__ import annotations

import base64
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from typer.testing import CliRunner

from shifde.cli import main as cli_main
from shifde.core.crypto import KeyRing
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.secrets import service as secrets_service
from tests.secrets.conftest import Clients, app_session

PLAIN = ("rewrap-plain-A-0123456789", "rewrap-plain-B-0123456789")


def _seed(app_db: Database, keyring: KeyRing, clients: Clients) -> list[UUID]:
    ids = []
    for cl, value in zip((clients.a, clients.b), PLAIN, strict=True):
        with app_session(app_db, cl.tenant) as s:
            meta = secrets_service.create_secret(
                s, keyring, tenant_id=cl.tenant, name="rewrap", value=value, created_by=None
            )
            s.commit()
            ids.append(meta.id)
    return ids


def _kek_ids(owner_db: Database, ids: list[UUID]) -> set[str]:
    with owner_db.session() as o:
        rows = o.execute(
            text("SELECT kek_id FROM shifde.secrets WHERE id = ANY(:ids)"), {"ids": ids}
        )
        kids = rows.scalars().all()
    assert len(kids) == len(ids)
    return set(kids)


def test_rewrap_all_under_the_app_role_refuses_and_rewraps_nothing(
    owner_db: Database, app_db: Database, keyring: KeyRing, clients: Clients
) -> None:
    ids = _seed(app_db, keyring, clients)
    both = KeyRing.parse(f"{KeyRing.generate_entry('k2')},{_entry(keyring, 'k1')}")
    with app_db.session() as s, pytest.raises(secrets_service.RowSecurityHidesSecrets) as err:
        secrets_service.rewrap_all(s, both)
    assert str(err.value) == secrets_service.ROW_SECURITY_BLOCKS_REWRAP
    assert _kek_ids(owner_db, ids) == {"k1"}


def test_secrets_rewrap_under_the_app_role_exits_1_with_the_fixed_message(
    settings: Settings,
    owner_db: Database,
    app_db: Database,
    keyring: KeyRing,
    clients: Clients,
    app_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed(app_db, keyring, clients)
    new = KeyRing.generate_entry("k2")
    as_app = settings.model_copy(
        update={
            "secret_keys": SecretStr(f"{new},{_entry(keyring, 'k1')}"),
            "migration_database_url": app_url,
        }
    )
    monkeypatch.setattr(cli_main, "_settings", lambda: as_app)
    result = CliRunner().invoke(cli_main.app, ["secrets", "rewrap"])
    assert result.exit_code == 1
    assert f"error: {secrets_service.ROW_SECURITY_BLOCKS_REWRAP}" in result.output
    assert not any(line.startswith("re-wrapped") for line in result.output.splitlines())
    assert _kek_ids(owner_db, ids) == {"k1"}
    material = (new.split(":", 1)[1], _entry(keyring, "k1").split(":", 1)[1])
    for value in (*PLAIN, *(str(i) for i in ids), app_url, *material):
        assert value not in result.output


def _entry(ring: KeyRing, kid: str) -> str:
    return f"{kid}:{base64.b64encode(ring.kek(kid)).decode()}"
