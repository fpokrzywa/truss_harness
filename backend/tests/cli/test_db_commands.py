"""`shifde-admin db upgrade` and `db check` with their defaults (two-org-proof unit, B-2470).

The migrations are one Alembic branch per module, so the code has several heads; the default
target must be every head (`heads`), not `head`, which Alembic refuses when there is more than one.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from shifde.cli import main as cli_main
from shifde.core.migrations import code_heads
from shifde.core.settings import Settings


def test_db_upgrade_with_no_revision_reaches_every_head(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(code_heads()) > 1  # the case the default has to handle
    monkeypatch.setattr(cli_main, "_settings", lambda: settings)
    runner = CliRunner()
    upgraded = runner.invoke(cli_main.app, ["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    assert "database upgraded to heads" in upgraded.output
    checked = runner.invoke(cli_main.app, ["db", "check"])
    assert checked.exit_code == 0, checked.output
    assert "status: ok" in checked.output
