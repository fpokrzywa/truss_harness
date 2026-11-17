"""`shifde.cli.sampledata` and `shifde-admin dev-sample-data` (app-2 B-2420)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from typer.testing import CliRunner

from shifde.cli import main as cli_main
from shifde.core.settings import Settings


def test_sample_database_is_seeded_and_the_reader_is_read_only(sample_db_url: str) -> None:
    with psycopg.connect(sample_db_url) as conn:
        counts = {
            table: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
            for table in ("employees", "customers", "accounts", "service_requests")
        }
        assert counts == {
            "employees": (20,),
            "customers": (50,),
            "accounts": (80,),
            "service_requests": (200,),
        }
        role = conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles"
            " WHERE rolname = current_user"
        ).fetchone()
        assert role == (False, False, False, False)
        comment = conn.execute(
            "SELECT col_description('customers'::regclass, attnum) FROM pg_attribute"
            " WHERE attrelid = 'customers'::regclass AND attname = 'tax_id'"
        ).fetchone()
        assert comment == ("classification: restricted",)
    with (
        psycopg.connect(sample_db_url) as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute("UPDATE customers SET name = 'x' WHERE id = 1")
    with psycopg.connect(sample_db_url, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE customers SET name = 'x' WHERE id = 1")


def test_dev_sample_data_refuses_outside_development(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = settings.model_copy(update={"env": "production"})
    assert not production.is_development
    monkeypatch.setattr(cli_main, "_settings", lambda: production)
    built: list[str] = []
    monkeypatch.setattr(cli_main, "build_sample_database", lambda *a, **k: built.append("x"))
    result = CliRunner().invoke(cli_main.app, ["dev-sample-data"])
    assert result.exit_code == 1
    assert "refuses to run unless SHIFDE_ENV=development" in result.output
    assert built == []
