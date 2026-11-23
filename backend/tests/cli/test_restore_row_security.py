"""The restore's audit-chain check under the app role (issue #218).

Behaviour: under a role subject to row-level security the check lists no client and would pass
vacuously, so `_check_audit` returns the fixed `ROW_SECURITY_HIDES_AUDIT` detail before listing
any client, and `_report_restore_checks` then exits 1 without `RESTORE VERIFIED`. A full restore
as the app role is refused before any check runs, so the helpers are called directly.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from shifde.cli import backup
from shifde.cli import main as cli_main
from shifde.core.db import Database
from tests._support.tenants import add_tenant


def test_audit_check_under_the_app_role_returns_the_fixed_message(
    owner_db: Database, app_db: Database
) -> None:
    with owner_db.session() as o:
        add_tenant(o)
        o.commit()
    with app_db.session() as s:
        assert backup._check_audit(s) == backup.ROW_SECURITY_HIDES_AUDIT


def test_a_refused_audit_check_fails_the_restore_report() -> None:
    results = [backup.CheckResult("audit chains", False, backup.ROW_SECURITY_HIDES_AUDIT)]
    probe = typer.Typer()
    probe.command()(lambda: cli_main._report_restore_checks(results))
    result = CliRunner().invoke(probe, [])
    assert result.exit_code == 1
    assert f"FAILED audit chains: {backup.ROW_SECURITY_HIDES_AUDIT}" in result.output
    assert "RESTORE VERIFIED" not in result.output
