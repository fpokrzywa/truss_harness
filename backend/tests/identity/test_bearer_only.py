"""`identity.dependencies.authenticate_bearer_only` (SP8 design 2026-11-23 section 10 items 2, 3
and 19): `Authorization: Bearer shf_...` only, cookies never read, every refusal one generic 401
`invalid_credentials`, failed bearer attempts counted in `api_key_failure_limiter`."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any  # Any: JSON bodies, TestClient options

from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.core.db import Database
from shifde.core.deps import RequestCtxDep, SessionDep, SettingsDep
from shifde.core.settings import Settings
from shifde.identity.dependencies import authenticate_bearer_only
from tests.identity import harness
from tests.identity.conftest import World
from tests.identity.test_api_keys import bearer, mint

PROBE = "/probe/bearer-only"
probe = APIRouter()


@probe.get(PROBE)
def bearer_only(
    request: Request, session: SessionDep, settings: SettingsDep, ctx: RequestCtxDep
) -> dict[str, Any]:
    auth = authenticate_bearer_only(request, session, settings, ctx)
    return {
        "principal_id": str(auth.principal.principal_id),
        "credential": auth.principal.credential,
    }


@contextmanager
def _client(settings: Settings, **kwargs: Any) -> Iterator[TestClient]:
    app = harness.make_app(settings)
    app.include_router(probe)
    try:
        with TestClient(app, base_url="https://testserver", **kwargs) as c:
            yield c
    finally:
        app.state.db.dispose()


def test_a_valid_key_authenticates_and_everything_else_is_invalid_credentials(
    settings: Settings, app_db: Database, world: World, owner_session: Session
) -> None:
    _, good = mint(app_db, settings, world.a.tenant, "service_account", world.a.service_account)
    revoked_id, revoked = mint(
        app_db, settings, world.a.tenant, "service_account", world.a.service_account
    )
    _, expired = mint(
        app_db,
        settings,
        world.a.tenant,
        "service_account",
        world.a.service_account,
        expires_at=utcnow() + timedelta(seconds=1),
    )
    owner_session.execute(
        text("UPDATE shifde.api_keys SET revoked_at = now() WHERE id = :i"), {"i": revoked_id}
    )
    owner_session.execute(
        text(
            "UPDATE shifde.api_keys SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE expires_at < now() + interval '1 minute'"
        )
    )
    owner_session.commit()
    with _client(settings) as c:
        r = c.get(PROBE, headers=bearer(good))
        assert r.status_code == 200 and r.json()["credential"] == "api_key"
        refusals = {
            "no header": c.get(PROBE),
            "basic": c.get(PROBE, headers={"Authorization": "Basic YTpi"}),
            "token scheme": c.get(PROBE, headers={"Authorization": f"Token {good}"}),
            "executor": c.get(PROBE, headers=bearer("shx_test_abcdefgh_" + "a" * 43)),
            "unknown": c.get(PROBE, headers=bearer("shf_test_ZZZZZZZZ_" + "a" * 43)),
            "revoked": c.get(PROBE, headers=bearer(revoked)),
            "expired": c.get(PROBE, headers=bearer(expired)),
            "cookie only": c.get(PROBE, cookies={harness.COOKIE: "any-session-token"}),
        }
    for name, r in refusals.items():
        assert r.status_code == 401, name
        assert r.json()["code"] == "invalid_credentials", name
        assert r.json()["detail"] == "invalid credentials", name


def test_failed_attempts_count_in_the_api_key_failure_limiter(
    settings: Settings, app_db: Database, world: World
) -> None:
    limited = settings.model_copy(update={"api_key_failures_per_minute": 2})
    _, good = mint(app_db, settings, world.a.tenant, "service_account", world.a.service_account)
    garbage = bearer("shf_test_ZZZZZZZZ_" + "c" * 43)
    with _client(limited, client=("203.0.113.8", 5000)) as c:
        assert [c.get(PROBE, headers=garbage).status_code for _ in range(2)] == [401, 401]
        r = c.get(PROBE, headers=bearer(good))  # a full window refuses even a correct key
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"
