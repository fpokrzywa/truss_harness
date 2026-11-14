"""Login hardening of identity part 2b (identity review 2 items 2-4, carry_forward victim-lockout
row; DECISIONS B-918, B-919), as `shifde_app` on the owner-seeded identity `World`."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any  # Any: SQLAlchemy event hook arguments
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from shifde.core.context import RequestContext
from shifde.core.db import Database, get_context
from shifde.core.errors import InvalidCredentials, RateLimited
from shifde.core.ratelimit import SlidingWindowLimiter, source_key
from shifde.core.settings import Settings
from shifde.identity import service
from shifde.identity.dependencies import enforce_login_limits
from shifde.identity.passwords import PasswordService
from tests.identity.conftest import World, app_session, owner_membership
from tests.identity.harness import PASSWORD, person

EMAIL = "hardened@a.test"


@pytest.fixture(scope="module")
def passwords() -> PasswordService:
    return PasswordService(fast=True)


@pytest.fixture
def svc_settings(app_url: str, owner_url: str) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=app_url,
        migration_database_url=owner_url,
        auth_local_enabled=True,
    )


@pytest.fixture
def s(app_db: Database) -> Iterator[Session]:
    session = app_session(app_db, None)
    yield session
    session.close()


@pytest.fixture
def member(owner_session: Session, world: World) -> UUID:
    uid = person(owner_session, EMAIL)
    owner_membership(owner_session, world.a.tenant, uid)
    owner_session.commit()
    return uid


def _login(
    s: Session,
    settings: Settings,
    passwords: PasswordService,
    *,
    email: str = EMAIL,
    password: str = PASSWORD,
    ip: str = "10.0.0.1",
) -> service.LoginResult:
    ctx = RequestContext(request_id="req", client_ip=ip, user_agent="pytest")
    return service.login_local(
        s, settings, passwords, ctx, email=email, password=password, previous_token=None
    )


def _db_context(s: Session) -> tuple[Any, Any]:  # Any: uuid or NULL
    row = s.execute(text("SELECT shifde.current_principal_id(), shifde.current_tenant_id()")).one()
    return row[0], row[1]


# ------------------------------------------------------------------ review 2 item 2
@pytest.mark.parametrize("email", [EMAIL, "ghost@a.test"])
def test_a_failed_login_leaves_no_principal_or_client_on_the_session(
    s: Session, svc_settings: Settings, passwords: PasswordService, member: UUID, email: str
) -> None:
    with pytest.raises(InvalidCredentials):
        _login(s, svc_settings, passwords, email=email, password="wrong")
    ctx = get_context(s)
    assert ctx.principal_id is None and ctx.tenant_id is None
    assert _db_context(s) == (None, None)


def test_a_login_whose_audit_write_fails_still_clears_the_context(
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
    member: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fan-out sets the member's client before `audit.record`; if that raises, neither the
    person nor the client stays on the Session."""

    def boom(*args: Any, **kwargs: Any) -> None:  # Any: audit.record's signature
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service.audit, "record", boom)
    with pytest.raises(RuntimeError):
        _login(s, svc_settings, passwords, password="wrong")
    ctx = get_context(s)
    assert ctx.principal_id is None and ctx.tenant_id is None
    s.rollback()
    assert _db_context(s) == (None, None)


# ------------------------------------------------------------------ review 2 item 3
def test_source_key_groups_ipv6_by_64() -> None:
    assert source_key("2001:db8:1:2::1") == source_key("2001:db8:1:2:ffff:ffff:ffff:ffff")
    assert source_key("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert source_key("2001:db8:1:3::1") != source_key("2001:db8:1:2::1")
    assert source_key("::ffff:203.0.113.9") == "203.0.113.9"
    assert source_key("203.0.113.9") == "203.0.113.9"
    assert source_key(None) == "" and source_key("testclient") == "testclient"


def test_failures_within_one_ipv6_64_count_against_one_source(
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
    member: UUID,
    owner_session: Session,
) -> None:
    for host in range(1, 11):  # rotating within the /64 does not reset the counter
        with pytest.raises(InvalidCredentials):
            _login(s, svc_settings, passwords, password="wrong", ip=f"2001:db8:5:6::{host:x}")
    rows = owner_session.execute(
        text("SELECT failed_attempts, locked_until FROM shifde.login_lockouts WHERE user_id = :u"),
        {"u": member},
    ).all()
    assert len(rows) == 1 and rows[0][0] == 10 and rows[0][1] is not None
    with pytest.raises(InvalidCredentials):  # locked for the whole /64, right password or not
        _login(s, svc_settings, passwords, ip="2001:db8:5:6:abcd::1")
    _login(s, svc_settings, passwords, ip="2001:db8:5:7::1")  # another /64 is not locked


@pytest.mark.parametrize(("age", "refused"), [("1 minute", True), ("2 hours", False)])
def test_an_account_wide_ceiling_bounds_failures_over_every_source(
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
    member: UUID,
    owner_session: Session,
    age: str,
    refused: bool,
) -> None:
    """Ten sources of ten failures each (rotating addresses) reach the ceiling within the window:
    a further source is refused even with the right password; older counters do not count."""
    for n in range(service.ACCOUNT_CEILING // 10):
        owner_session.execute(
            text(
                "INSERT INTO shifde.login_lockouts (user_id, source_hash, failed_attempts, "
                "updated_at) VALUES (:u, :h, 10, now() - CAST(:age AS interval))"
            ),
            {"u": member, "h": f"source-{n}".encode(), "age": age},
        )
    owner_session.commit()
    if refused:
        with pytest.raises(InvalidCredentials):
            _login(s, svc_settings, passwords, ip="198.51.100.1")
    else:
        _login(s, svc_settings, passwords, ip="198.51.100.1")


def test_hammering_from_one_source_does_not_reach_the_account_wide_ceiling(
    s: Session,
    svc_settings: Settings,
    passwords: PasswordService,
    member: UUID,
    owner_session: Session,
) -> None:
    """Isolation review 3 item 1: one address that keeps failing past its own lock counts at most
    `LOCKOUT_THRESHOLD` towards the ceiling, so it cannot lock the person out from elsewhere."""
    attempts = service.ACCOUNT_CEILING + 50
    for _ in range(attempts):
        with pytest.raises(InvalidCredentials):
            _login(s, svc_settings, passwords, password="wrong", ip="203.0.113.7")
    counted = owner_session.execute(
        text("SELECT failed_attempts FROM shifde.login_lockouts WHERE user_id = :u"),
        {"u": member},
    ).scalar_one()
    assert counted == attempts  # the source's own counter (and its doubling lock) is unchanged
    with pytest.raises(InvalidCredentials):  # that source stays locked, right password or not
        _login(s, svc_settings, passwords, ip="203.0.113.7")
    _login(s, svc_settings, passwords, ip="198.51.100.2")  # the person signs in from elsewhere


# ------------------------------------------------------------------ review 2 item 4
def _statements(app_db: Database, run: Any) -> int:  # Any: a zero-argument callable
    seen: list[str] = []

    def count(*args: Any) -> None:  # Any: before_cursor_execute arguments
        seen.append(args[2])

    event.listen(app_db.engine, "before_cursor_execute", count)
    try:
        run()
    finally:
        event.remove(app_db.engine, "before_cursor_execute", count)
    return len(seen)


def test_known_and_unknown_account_failures_do_the_same_database_work(
    app_db: Database, svc_settings: Settings, passwords: PasswordService, member: UUID
) -> None:
    """Review 2 item 4, measured: the known-account failure used to run more statements (credential
    and source locks, counter write, membership lookup) than an unknown account. The unknown path
    now runs the same lookups against an id no person has; a person with one membership and an
    unknown e-mail issue the same number of statements."""

    def attempt(email: str) -> None:
        session = app_session(app_db, None)
        try:
            with pytest.raises(InvalidCredentials):
                _login(session, svc_settings, passwords, email=email, password="wrong")
        finally:
            session.close()

    attempt(EMAIL)  # warm up (first-use statements of a fresh connection)
    attempt("ghost@a.test")
    known = _statements(app_db, lambda: attempt(EMAIL))
    unknown = _statements(app_db, lambda: attempt("ghost@a.test"))
    assert known == unknown, (known, unknown)


# ------------------------------------------------------------------ carry_forward victim lockout
def test_the_per_account_limit_is_kept_per_source(svc_settings: Settings) -> None:
    """`login_rate_per_account_per_minute` was keyed by e-mail only: failures sent from any
    address throttled the person everywhere. Keyed per (e-mail, source) now; one source's
    attempts do not throttle another's (B-919)."""
    state = SimpleNamespace(
        login_ip_limiter=SlidingWindowLimiter(0),
        login_account_limiter=SlidingWindowLimiter(2),
    )
    request: Any = SimpleNamespace(app=SimpleNamespace(state=state))  # Any: a stand-in Request

    def ctx(ip: str) -> RequestContext:
        return RequestContext(request_id="r", client_ip=ip)

    for _ in range(2):
        enforce_login_limits(request, svc_settings, ctx("203.0.113.1"), EMAIL)
    with pytest.raises(RateLimited):
        enforce_login_limits(request, svc_settings, ctx("203.0.113.1"), f" {EMAIL.upper()} ")
    enforce_login_limits(request, svc_settings, ctx("198.51.100.2"), EMAIL)  # victim unaffected
    for _ in range(2):  # rotating inside one IPv6 /64 is one source
        enforce_login_limits(request, svc_settings, ctx("2001:db8:9:9::1"), EMAIL)
    with pytest.raises(RateLimited):
        enforce_login_limits(request, svc_settings, ctx("2001:db8:9:9::2"), EMAIL)
