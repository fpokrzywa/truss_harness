"""The DB-backed limiter (SETH 3ba0e06 `test_p3_models.py`, carry_forward row 20; DECISIONS B-1706).

Moved with its assertions; SETH's seeded `demo-a` is the tenancy world's client A, and
`release_lease` names the client (B-1704).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core import db as dbmod
from shifde.core.db import Database
from shifde.core.errors import ModelRateLimited
from shifde.models_admin import limiter
from tests.models_admin.conftest import start_of_window
from tests.tenancy.conftest import World


def test_db_backed_limiter_counts_atomically_and_leases_expire(
    world: World, owner_session: Session, app_url: str
) -> None:
    tid = world.a.tenant
    pool = Database(app_url, pool_size=3)
    start_of_window(owner_session)  # the three take_request calls must share one minute window
    with pool.session() as s1, pool.session() as s2:
        dbmod.set_tenant(s1, tid)
        dbmod.set_tenant(s2, tid)
        limiter.take_request(s1, tid, "b", 2)
        limiter.take_request(s1, tid, "b", 2)
        s1.commit()
        with pytest.raises(ModelRateLimited):
            limiter.take_request(s2, tid, "b", 2)
        s2.rollback()
        first = limiter.acquire_lease(s1, tid, "c", 1)
        assert first is not None
        with pytest.raises(ModelRateLimited):
            limiter.acquire_lease(s2, tid, "c", 1)
        limiter.release_lease(s1, tid, first)
        second = limiter.acquire_lease(s2, tid, "c", 1)
        assert second is not None and limiter.acquire_lease(s1, tid, "x", None) is None
        limiter.release_lease(s2, tid, second)
        # an expired lease (crashed worker) does not hold the slot
        owner_session.execute(
            text(
                "INSERT INTO shifde.concurrency_leases (id, tenant_id, bucket_key, expires_at) "
                "VALUES (gen_random_uuid(), :t, 'c', now() - interval '1 minute')"
            ),
            {"t": tid},
        )
        owner_session.commit()
        third = limiter.acquire_lease(s1, tid, "c", 1)
        assert third is not None
        limiter.release_lease(s1, tid, third)
    pool.dispose()


def test_one_clients_limits_do_not_count_against_another(world: World, app_db: Database) -> None:
    a, b = world.a.tenant, world.b.tenant
    with app_db.session() as s:
        dbmod.set_tenant(s, a)
        limiter.take_request(s, a, "shared-bucket", 1)
        limiter.add_tokens(s, a, "shared-bucket", 100)
        s.commit()
        assert limiter.acquire_lease(s, a, "shared-bucket", 1) is not None
        with pytest.raises(ModelRateLimited):
            limiter.take_request(s, a, "shared-bucket", 1)
        s.rollback()
    with app_db.session() as s:
        dbmod.set_tenant(s, b)
        # the same bucket name in client B: A's requests, tokens and lease are not seen
        limiter.take_request(s, b, "shared-bucket", 1)
        limiter.check_tokens(s, b, "shared-bucket", 50, 50)
        s.commit()
        lease = limiter.acquire_lease(s, b, "shared-bucket", 1)
        assert lease is not None
        limiter.release_lease(s, b, lease)
