"""`secrets_service.check_all` and `shifde-admin secrets check` (go-live design 6.1, SP4-5).

Behaviour: with the right ring every `db_encrypted` secret (a disabled one too) decrypts and none
fails; a tampered ciphertext is reported by id; nothing is written (`secret_access_log` and
`audit_events` keep their counts); with a ring lacking the key the command prints `ok 0, failed
<m>` and each failed id, exits 1, and shows no plaintext and no key material.
"""

from __future__ import annotations

import base64
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from shifde.cli import main as cli_main
from shifde.core.crypto import KeyRing
from shifde.core.db import Database
from shifde.core.settings import Settings
from shifde.secrets import service as secrets_service
from tests.secrets.conftest import Clients, app_session

PLAIN = ("check-all-plain-A-0123456789", "check-all-plain-B-0123456789")


def _seed(app_db: Database, keyring: KeyRing, clients: Clients) -> list[UUID]:
    metas = []
    for cl, value in zip((clients.a, clients.b), PLAIN, strict=True):
        with app_session(app_db, cl.tenant) as s:
            meta = secrets_service.create_secret(
                s, keyring, tenant_id=cl.tenant, name="check-all", value=value, created_by=None
            )
            s.commit()
            metas.append(meta)
    with app_session(app_db, clients.a.tenant) as s:  # a disabled secret is checked too
        secrets_service.disable_secret(s, metas[0], expected_version=None)
        s.commit()
    return [m.id for m in metas]


def _counts(owner_db: Database) -> tuple[int, int]:
    with owner_db.session() as o:
        log = o.execute(text("SELECT count(*) FROM shifde.secret_access_log")).scalar_one()
        events = o.execute(text("SELECT count(*) FROM shifde.audit_events")).scalar_one()
    return int(log), int(events)


def test_check_all_opens_every_envelope_reports_a_tampered_one_and_writes_nothing(
    owner_db: Database, app_db: Database, keyring: KeyRing, clients: Clients, seed_engine: Engine
) -> None:
    ids = _seed(app_db, keyring, clients)
    before = _counts(owner_db)
    with owner_db.session() as o:
        assert secrets_service.check_all(o, keyring) == (2, [])
    with seed_engine.begin() as c:  # superuser: flip the ciphertext's first byte
        c.execute(
            text(
                "UPDATE shifde.secrets SET ciphertext = "
                "set_byte(ciphertext, 0, get_byte(ciphertext, 0) # 255) WHERE id = :i"
            ),
            {"i": ids[1]},
        )
    with owner_db.session() as o:
        assert secrets_service.check_all(o, keyring) == (1, [ids[1]])
    assert _counts(owner_db) == before


def test_secrets_check_with_a_ring_lacking_the_key_fails_every_secret_without_plaintext(
    settings: Settings,
    app_db: Database,
    keyring: KeyRing,
    clients: Clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed(app_db, keyring, clients)
    runner = CliRunner()
    good = settings.model_copy(update={"secret_keys": SecretStr(_entry(keyring, "k1"))})
    monkeypatch.setattr(cli_main, "_settings", lambda: good)
    ok = runner.invoke(cli_main.app, ["secrets", "check"])
    assert ok.exit_code == 0, ok.output
    assert ok.output.strip() == "ok 2, failed 0"
    other = KeyRing.generate_entry("k9")
    wrong = settings.model_copy(update={"secret_keys": SecretStr(other)})
    monkeypatch.setattr(cli_main, "_settings", lambda: wrong)
    result = runner.invoke(cli_main.app, ["secrets", "check"])
    assert result.exit_code == 1
    lines = result.output.strip().splitlines()
    assert lines[0] == "ok 0, failed 2"
    assert sorted(lines[1:]) == sorted(f"failed {i}" for i in ids)
    for secret in (*PLAIN, other.split(":", 1)[1], _entry(keyring, "k1").split(":", 1)[1]):
        assert secret not in result.output
        assert secret not in ok.output


def _entry(ring: KeyRing, kid: str) -> str:
    return f"{kid}:{base64.b64encode(ring.kek(kid)).decode()}"
