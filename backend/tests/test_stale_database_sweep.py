"""The session-start sweep of stale `shifdet_*` databases (issue #115, B-2771): which names
`_stale_databases` picks, and that the cluster gives `_database_ages` a readable age."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from tests.conftest import _database_ages, _stale_databases

RUN = "abc123def456"
LIMIT = 12 * 60 * 60


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """No tenant data here: the selection tests need no database (they pass with the cluster
    stopped), and the cluster test only reads `pg_database`."""


def test_an_old_database_is_selected_and_a_young_one_is_not() -> None:
    rows = [("shifdet_otherrun0001_gw0", LIMIT + 1.0), ("shifdet_otherrun0002_gw0", 60.0)]
    assert _stale_databases(rows, RUN, LIMIT) == ["shifdet_otherrun0001_gw0"]


def test_an_age_exactly_at_the_limit_is_not_selected() -> None:
    assert _stale_databases([("shifdet_otherrun0001_gw0", float(LIMIT))], RUN, LIMIT) == []


def test_a_name_outside_the_prefix_is_never_selected() -> None:
    rows = [(name, 10.0 * LIMIT) for name in ("shifde", "shifde_wt1", "postgres", "xshifdet_a")]
    assert _stale_databases(rows, RUN, LIMIT) == []


def test_an_old_template_is_selected() -> None:
    rows = [("shifdet_otherrun0001_tmpl", LIMIT + 1.0)]
    assert _stale_databases(rows, RUN, LIMIT) == ["shifdet_otherrun0001_tmpl"]


def test_the_current_runs_databases_are_never_selected() -> None:
    names = ("tmpl", "gw0", "main", "mig", "a0f3c2", "sample123_abcdef")
    rows = [(f"shifdet_{RUN}_{suffix}", 10.0 * LIMIT) for suffix in names]
    assert _stale_databases(rows, RUN, LIMIT) == []


def test_an_unknown_age_keeps_the_database() -> None:
    assert _stale_databases([("shifdet_otherrun0001_gw0", None)], RUN, LIMIT) == []


def test_cluster_gives_this_workers_database_a_fresh_age(
    admin_url: str, test_database: tuple[str, str]
) -> None:
    name = make_url(test_database[0]).database
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as c:
            ages = dict(_database_ages(c))
    finally:
        admin.dispose()
    assert name in ages
    age = ages[name]
    assert age is not None
    assert 0 <= age <= 3600
