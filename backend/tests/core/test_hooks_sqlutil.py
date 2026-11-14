"""Review follow-ups: close hooks never run as platform admin; `update_returning` validates every
identifier it interpolates."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.db import Database, get_context, set_platform_admin
from shifde.core.hooks import add_close_hook, run_close_hooks
from shifde.core.ids import uuid7
from shifde.core.sqlutil import update_returning


def test_close_hooks_run_without_platform_admin(app_url: str) -> None:
    db = Database(app_url)
    seen: dict[str, Any] = {}

    def hook(session: Session) -> None:
        seen["ctx"] = get_context(session).platform_admin
        seen["sql"] = session.execute(text("SELECT shifde.is_platform_admin_ctx()")).scalar_one()

    try:
        with db.session() as s:
            set_platform_admin(s, True)
            add_close_hook(s, hook)
            run_close_hooks(s)
        assert seen == {"ctx": False, "sql": False}
    finally:
        db.dispose()


def _call(**over: Any) -> None:  # Any: update_returning keyword overrides
    args: dict[str, Any] = {
        "session": None,
        "table": "things",
        "columns": "id",
        "updatable": frozenset({"name"}),
        "row_id": uuid7(),
        "expected_version": None,
        "values": {"name": "x"},
    }
    update_returning(**{**args, **over})


@pytest.mark.parametrize(
    "over",
    [
        {"table": "things; DROP TABLE x"},
        {"table": "Things"},
        {"table": "a.b"},
        {"table": ""},
        {"updatable": frozenset({"na me"}), "values": {"na me": "x"}},
        {"updatable": frozenset({"name = 1, tenant_id"}), "values": {"name = 1, tenant_id": "x"}},
        {"updatable": frozenset({"tenant_id"}), "values": {"tenant_id": "x"}},
        {"updatable": frozenset({"id"}), "values": {"id": "x"}},
        {"updatable": frozenset({"name", "tenant_id"})},
        {"json_columns": frozenset({"bad col"})},
    ],
)
def test_update_returning_refuses_unsafe_identifiers(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - any ValueError, raised before SQL is built
        _call(**over)
