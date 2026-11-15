"""`build_0002` (DECISIONS B-1940, B-1941): `prompt_test_runs.model_id` and
`pipeline_version_refs.model_id` may name the client's own model or a shared one (tenant NULL), and
never another client's. `trg_build_model_ref_guard` gives another client's model and an unknown id
the same 23503, before any key is checked (no lock wait on another client's uncommitted `models`
row); the client's own models keep the composite key through `own_model_id`. Run as `shifde_app`
and as the schema owner (constraints and triggers are checked without row-level security).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from shifde.core.db import Database
from shifde.core.ids import uuid7
from shifde.models_admin.repository import SHI_PROVIDER_ID
from tests.build.conftest import World
from tests.build.test_cross_client import _artifact, _version
from tests.ledger.util import add_model
from tests.tenancy.conftest import app_session

Table = Literal["prompt_test_runs", "pipeline_version_refs"]
TABLES: tuple[Table, ...] = ("prompt_test_runs", "pipeline_version_refs")


@dataclass(frozen=True)
class Fix:
    a: UUID
    b: UUID
    version: dict[Table, UUID]  # A's version for each table
    artifact: UUID
    a_model: UUID
    b_model: UUID
    shared: UUID


@pytest.fixture
def fix(owner_db: Database, world: World) -> Iterator[Fix]:
    shared = uuid7()
    with owner_db.session() as s:
        art = _artifact(s, world.a.tenant, "prompts", "p")
        pv = _version(s, world.a.tenant, art, "prompts", 1)
        pipe = _artifact(s, world.a.tenant, "pipelines", "pl")
        plv = _version(s, world.a.tenant, pipe, "pipelines", 1)
        a_model = add_model(s, world.a.tenant, model_key="m-a")
        b_model = add_model(s, world.b.tenant, model_key="m-b")
        made_provider = (
            s.execute(
                text(
                    "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key) "
                    "VALUES (:p, NULL, 'shi', 'openai_compatible') ON CONFLICT DO NOTHING "
                    "RETURNING id"
                ),
                {"p": SHI_PROVIDER_ID},
            ).first()
            is not None
        )
        s.execute(
            text(
                "INSERT INTO shifde.models (id, tenant_id, provider_id, model_key, display_name) "
                "VALUES (:m, NULL, :p, :k, :k)"
            ),
            {"m": shared, "p": SHI_PROVIDER_ID, "k": f"shared-{shared.hex}"},
        )
        s.commit()
    try:
        yield Fix(
            world.a.tenant,
            world.b.tenant,
            {"prompt_test_runs": pv, "pipeline_version_refs": plv},
            art,
            a_model,
            b_model,
            shared,
        )
    finally:
        with owner_db.session() as s:
            s.execute(text("DELETE FROM shifde.models WHERE id = :m"), {"m": shared})
            if made_provider:
                s.execute(
                    text("DELETE FROM shifde.model_providers WHERE id = :p"),
                    {"p": SHI_PROVIDER_ID},
                )
            s.commit()


def _insert(s: Session, f: Fix, table: Table, model: UUID | None) -> None:
    if table == "prompt_test_runs":
        s.execute(
            text(
                "INSERT INTO shifde.prompt_test_runs (id, tenant_id, artifact_id, version_id, "
                "model_id, output, latency_ms, verdict) "
                "VALUES (:i, :t, :a, :v, :m, 'o', 1, 'pass')"
            ),
            {"i": uuid7(), "t": f.a, "a": f.artifact, "v": f.version[table], "m": model},
        )
    else:
        s.execute(
            text(
                "INSERT INTO shifde.pipeline_version_refs (tenant_id, pipeline_version_id, "
                "node_id, ref_type, ref_id, model_id) VALUES (:t, :v, 'n1', 'model', :m, :m)"
            ),
            {"t": f.a, "v": f.version[table], "m": model},
        )
    s.flush()


def _state(s: Session, f: Fix, table: Table, model: UUID | None) -> str:
    try:
        _insert(s, f, table, model)
        return "ok"
    except DBAPIError as err:
        return str(getattr(err.orig, "sqlstate", None))
    finally:
        s.rollback()


def _sessions(app_db: Database, owner_db: Database, f: Fix, who: str) -> Iterator[Session]:
    if who == "app":
        with app_session(app_db, f.a) as s:
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            yield s
    else:
        with owner_db.session() as s:
            s.execute(text("SET LOCAL lock_timeout = '500ms'"))
            yield s


@pytest.mark.parametrize("who", ["app", "owner"])
@pytest.mark.parametrize("table", TABLES)
def test_bs_model_and_an_unknown_id_give_a_the_same_refusal(
    app_db: Database, owner_db: Database, fix: Fix, table: Table, who: str
) -> None:
    errors: list[str] = []
    for model in (fix.b_model, uuid7()):
        for s in _sessions(app_db, owner_db, fix, who):
            try:
                _insert(s, fix, table, model)
            except DBAPIError as err:
                errors.append(f"{getattr(err.orig, 'sqlstate', None)}|{err.orig}")
            finally:
                s.rollback()
    assert len(errors) == 2 and errors[0].startswith("23503|model not found")
    assert errors[0].split("|")[1].split("\n")[0] == errors[1].split("|")[1].split("\n")[0]


@pytest.mark.parametrize("who", ["app", "owner"])
@pytest.mark.parametrize("table", TABLES)
def test_a_own_and_the_shared_model_are_accepted(
    app_db: Database, owner_db: Database, fix: Fix, table: Table, who: str
) -> None:
    for model in (fix.a_model, fix.shared):
        for s in _sessions(app_db, owner_db, fix, who):
            assert _state(s, fix, table, model) == "ok"


@pytest.mark.parametrize("table", TABLES)
def test_own_model_id_follows_the_model(owner_db: Database, fix: Fix, table: Table) -> None:
    got: dict[str, object] = {}
    with owner_db.session() as s:
        for label, model in (("own", fix.a_model), ("shared", fix.shared)):
            _insert(s, fix, table, model)
            got[label] = s.execute(
                text(f"SELECT own_model_id FROM shifde.{table} WHERE model_id = :m"),
                {"m": model},
            ).scalar_one()
        s.rollback()
    assert got == {"own": fix.a_model, "shared": None}


def test_an_own_model_cannot_be_deleted_while_a_run_names_it(owner_db: Database, fix: Fix) -> None:
    with owner_db.session() as s:
        _insert(s, fix, "prompt_test_runs", fix.a_model)
        with pytest.raises(DBAPIError) as err:
            s.execute(text("DELETE FROM shifde.models WHERE id = :m"), {"m": fix.a_model})
            s.flush()
        assert getattr(err.value.orig, "sqlstate", None) == "23503"
        s.rollback()


@pytest.mark.parametrize("who", ["app", "owner"])
@pytest.mark.parametrize("table", TABLES)
def test_a_does_not_wait_on_bs_uncommitted_model_insert(
    app_db: Database, owner_db: Database, fix: Fix, table: Table, who: str
) -> None:
    pending = uuid7()
    with owner_db.session() as o:
        provider = o.execute(
            text("SELECT provider_id FROM shifde.models WHERE id = :m"), {"m": fix.b_model}
        ).scalar_one()
        o.execute(
            text(
                "INSERT INTO shifde.models (id, tenant_id, provider_id, model_key, display_name) "
                "VALUES (:m, :t, :p, 'pending', 'pending')"
            ),
            {"m": pending, "t": fix.b, "p": provider},
        )  # not committed: holds the key
        for s in _sessions(app_db, owner_db, fix, who):
            assert _state(s, fix, table, pending) == "23503"
        o.rollback()
