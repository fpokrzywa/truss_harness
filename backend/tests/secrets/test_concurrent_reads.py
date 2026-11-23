"""Concurrent reads of one secret, as `shifde_app` (issue #315, B-3952).

A read writes the access-log row (whose foreign key takes `FOR KEY SHARE` on the secret row),
appends the `secret.resolved` audit event (the client's audit-chain lock, held to commit) and
touches `last_used_at`. Before B-3952 the touch was a plain UPDATE before the audit event: a read
held the secret row while it waited for the chain lock, and a second read that already held the
chain lock waited for the row, a deadlock (40P01). Every UPDATE of a secret is a key update (the
stored generated `installation_secret_id` is in a unique constraint), so it also conflicts with
the other reads' `FOR KEY SHARE`. The touch now runs after the audit event, is throttled to once
per 60 seconds and skips a locked row (`FOR UPDATE SKIP LOCKED`).

Every session sets a `lock_timeout` of a few seconds, so a regression fails fast (40P01 or
55P03) instead of hanging."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.audit import service as audit
from shifde.core.context import RequestContext
from shifde.core.crypto import KeyRing
from shifde.core.db import Database
from shifde.secrets import service as secrets_service
from shifde.secrets.service import LAST_USED_INTERVAL_SECONDS, SecretProvider
from tests.secrets.conftest import Clients, app_session

VALUE = "Concurrent-S3cret-Value-0123456789"
READERS = 4
WAIT_SECONDS = 10


@pytest.fixture
def pool_db(app_url: str) -> Iterator[Database]:
    """A `shifde_app` pool with room for one connection per thread (`app_db` holds one)."""
    db = Database(app_url, pool_size=READERS + 1, max_overflow=0)
    yield db
    db.dispose()


@pytest.fixture
def secret_id(pool_db: Database, clients: Clients, keyring: KeyRing) -> UUID:
    with app_session(pool_db, clients.a.tenant) as s:
        sec = secrets_service.create_secret(
            s, keyring, tenant_id=clients.a.tenant, name="shared-conn", value=VALUE, created_by=None
        )
        s.commit()
    return sec.id


def _reader(db: Database, tenant: UUID) -> Session:
    s = app_session(db, tenant)
    s.execute(text("SET LOCAL lock_timeout = '4s'"))
    return s


def _provider(s: Session, keyring: KeyRing) -> SecretProvider:
    return SecretProvider(s, keyring, RequestContext(request_id="concurrent-reads"))


def _run(target: Callable[[], None], errors: list[BaseException]) -> threading.Thread:
    def body() -> None:
        try:
            target()
        except BaseException as exc:  # reported by the test thread
            errors.append(exc)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


def _access_rows(owner: Session, secret_id: UUID) -> int:
    count = owner.execute(
        text("SELECT count(*) FROM shifde.secret_access_log WHERE secret_id = :i"),
        {"i": secret_id},
    ).scalar_one()
    owner.rollback()
    return int(count)


def _last_used(owner: Session, secret_id: UUID) -> object:
    found = owner.execute(
        text("SELECT last_used_at FROM shifde.secrets WHERE id = :i"), {"i": secret_id}
    ).scalar_one()
    owner.rollback()
    return found


def test_a_read_waiting_on_the_chain_lock_never_blocks_a_read_holding_it(
    pool_db: Database,
    owner_session: Session,
    keyring: KeyRing,
    clients: Clients,
    secret_id: UUID,
) -> None:
    """Session 2 audits first (holds client A's chain lock). Session 1 resolves: it writes its
    access-log row and then waits in `audit.record` for the chain lock. Session 2 then resolves
    the same secret: it must not wait on session 1's lock on the secret row, and commits; session
    1 then gets the chain lock, audits and commits. (Session 1's resolve audits itself, so its
    audit event is the one it waits on: the chain lock is held from session 2's first event.)"""
    tenant = clients.a.tenant
    s2_holds_chain = threading.Event()
    s1_waits = threading.Event()
    s1_pid: list[int] = []
    errors: list[BaseException] = []

    def session_2() -> None:
        with _reader(pool_db, tenant) as s:
            audit.record(
                s,
                RequestContext(request_id="concurrent-reads"),
                action="test.chain_lock",
                tenant_id=tenant,
                actor_label="system",
            )
            s2_holds_chain.set()
            assert s1_waits.wait(WAIT_SECONDS), "session 1 never waited on the chain lock"
            value = _provider(s, keyring).resolve_system(secret_id, "case.two", tenant_id=tenant)
            assert value.reveal() == VALUE
            s.commit()

    def session_1() -> None:
        assert s2_holds_chain.wait(WAIT_SECONDS), "session 2 never took the chain lock"
        with _reader(pool_db, tenant) as s:
            s1_pid.append(s.execute(text("SELECT pg_backend_pid()")).scalar_one())
            value = _provider(s, keyring).resolve_system(secret_id, "case.one", tenant_id=tenant)
            assert value.reveal() == VALUE
            s.commit()

    second = _run(session_2, errors)
    first = _run(session_1, errors)
    deadline = time.monotonic() + WAIT_SECONDS
    while not errors and time.monotonic() < deadline:
        if s1_pid:
            waiting = owner_session.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :p AND NOT granted)"),
                {"p": s1_pid[0]},
            ).scalar_one()
            owner_session.rollback()
            if waiting:
                s1_waits.set()
                break
        time.sleep(0.05)
    for thread in (second, first):
        thread.join(WAIT_SECONDS * 2)
        assert not thread.is_alive(), "a read is still blocked"
    assert not errors, errors
    assert s1_waits.is_set()
    assert _access_rows(owner_session, secret_id) == 2
    assert _last_used(owner_session, secret_id) is not None


def test_concurrent_reads_of_one_secret_all_succeed_and_are_logged(
    pool_db: Database,
    owner_session: Session,
    keyring: KeyRing,
    clients: Clients,
    secret_id: UUID,
) -> None:
    tenant = clients.a.tenant
    start = threading.Barrier(READERS)
    errors: list[BaseException] = []

    def read(n: int) -> None:
        with _reader(pool_db, tenant) as s:
            start.wait(WAIT_SECONDS)
            value = _provider(s, keyring).resolve_system(secret_id, f"case.{n}", tenant_id=tenant)
            assert value.reveal() == VALUE
            s.commit()

    threads = [_run(lambda n=n: read(n), errors) for n in range(READERS)]
    for thread in threads:
        thread.join(WAIT_SECONDS * 2)
        assert not thread.is_alive(), "a read is still blocked"
    assert not errors, errors
    assert _access_rows(owner_session, secret_id) == READERS
    assert _last_used(owner_session, secret_id) is not None


def test_last_used_at_is_throttled_but_every_read_is_logged(
    pool_db: Database,
    owner_session: Session,
    keyring: KeyRing,
    clients: Clients,
    secret_id: UUID,
) -> None:
    tenant = clients.a.tenant

    def read() -> None:
        with _reader(pool_db, tenant) as s:
            _provider(s, keyring).resolve_system(secret_id, "case", tenant_id=tenant)
            s.commit()

    read()
    first = _last_used(owner_session, secret_id)
    assert first is not None
    read()
    assert _last_used(owner_session, secret_id) == first
    assert _access_rows(owner_session, secret_id) == 2

    owner_session.execute(
        text(
            "UPDATE shifde.secrets SET last_used_at = last_used_at - make_interval(secs => :s) "
            "WHERE id = :i"
        ),
        {"s": LAST_USED_INTERVAL_SECONDS + 1, "i": secret_id},
    )
    owner_session.commit()
    stale = _last_used(owner_session, secret_id)
    read()
    moved = _last_used(owner_session, secret_id)
    assert isinstance(stale, datetime) and isinstance(moved, datetime) and moved > stale
    assert _access_rows(owner_session, secret_id) == 3
