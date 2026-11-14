"""The platform installation overview (SETH 3ba0e06 `installation/service.py`, minimal port).

Not ported (B-1101..B-1106): installation profiles (`stored_profile`, `diff_profile`,
`apply_profile`), public branding and logo, and the overview fields that only a profile filled
(`profile`, `retention`, `governance_defaults`, `section_status`).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any  # Any: operator-supplied installation settings are arbitrary JSON
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde import __version__
from shifde.authz import service as authz
from shifde.core import db as dbm
from shifde.core.context import Principal
from shifde.core.health import migration_state
from shifde.core.settings import Settings
from shifde.installation import schemas as s

DEFAULT_MAX_TOOL_CALLS = 500  # build.agents DEFAULT_MAXIMA["max_tool_calls"]
DEFAULT_INLINE_OUTPUT_MAX_BYTES = 65536


def _installation_settings(session: Session) -> dict[str, dict[str, Any]]:
    row = session.execute(
        text("SELECT feature_flags, default_limits FROM shifde.installation_settings WHERE id = 1")
    ).one_or_none()
    if row is None:
        return {"feature_flags": {}, "default_limits": {}}
    return {"feature_flags": dict(row[0] or {}), "default_limits": dict(row[1] or {})}


def _table_exists(session: Session, name: str) -> bool:
    """Later modules own `workers` (runtime) and `egress_policies` (connections): until their
    migrations exist the overview reports those parts absent (B-1105)."""
    return bool(
        session.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar_one()
    )


def _workers(session: Session, settings: Settings) -> list[s.WorkerOut] | None:
    if not _table_exists(session, "shifde.workers"):
        return None
    rows = session.execute(
        text(
            "SELECT id, heartbeat_at, heartbeat_at > now() - make_interval(secs => :win) "
            # A killed worker never marks itself stopped: show the most recent, not all of history.
            "FROM shifde.workers WHERE status = 'running' "
            "ORDER BY heartbeat_at DESC NULLS LAST, id LIMIT 20"
        ),
        {"win": max(settings.worker_lease_seconds * 3, 15)},
    ).all()
    if not rows:
        return None
    return [
        s.WorkerOut(name=r[0], last_seen_at=r[1], status="alive" if r[2] else "stale") for r in rows
    ]


def _oidc_providers(session: Session) -> list[s.OidcProviderOut]:
    # Installation IdPs only (B-1104): the platform administrator context sees every client's
    # IdP rows, and a client's IdP is that client's content.
    rows = session.execute(
        text(
            "SELECT display_name, issuer FROM shifde.idp_configs "
            "WHERE tenant_id IS NULL AND status = 'active' "
            "ORDER BY display_name, issuer LIMIT 200"
        )
    ).all()
    seen: set[tuple[str, str | None]] = set()
    out: list[s.OidcProviderOut] = []
    for name, issuer in rows:
        host = urlsplit(issuer).hostname
        if (name, host) not in seen:
            seen.add((name, host))
            out.append(s.OidcProviderOut(name=name, issuer_host=host))
    return out


def _egress_default_allow_private(session: Session) -> bool:
    if not _table_exists(session, "shifde.egress_policies"):
        return False
    value = session.execute(
        text(
            "SELECT allow_private FROM shifde.egress_policies "
            "WHERE tenant_id IS NULL AND is_default LIMIT 1"
        )
    ).scalar_one_or_none()
    return bool(value)


def installation_overview(
    session: Session, settings: Settings, principal: Principal, feature_keys: Collection[str]
) -> s.InstallationOut:
    """Effective installation configuration (platform administrators only). Environment-owned
    values (auth, session timeouts, API key lifetime) come from settings; flags and limits come
    from `installation_settings` over built-in defaults."""
    authz.new_pdp(session, principal, None).require("platform:admin")
    state = migration_state(session)
    installed = _installation_settings(session)
    flags: dict[str, bool] = {key: True for key in sorted(feature_keys)}
    flags.update({k: bool(v) for k, v in installed["feature_flags"].items()})
    limits = installed["default_limits"]
    with dbm.platform_admin_context(session):
        providers = _oidc_providers(session)
    return s.InstallationOut(
        version=__version__,
        env=settings.env,
        deployment_mode=settings.deployment_mode,
        migration=s.MigrationOut(
            current=state.revision, expected_head=state.head, ok=state.status == "ok"
        ),
        feature_flags=flags,
        auth=s.InstallationAuthOut(
            local_enabled=settings.auth_local_enabled, oidc_providers=providers
        ),
        security=s.InstallationSecurityOut(
            session_idle_minutes=settings.session_idle_minutes,
            session_absolute_hours=settings.session_absolute_hours,
            api_key_max_days=settings.api_key_max_days,
            egress_default_allow_private=_egress_default_allow_private(session),
        ),
        limits=s.InstallationLimitsOut(
            max_run_seconds=limits.get("max_run_seconds", settings.run_default_deadline_seconds),
            max_tool_calls=limits.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS),
            inline_output_max_bytes=limits.get(
                "inline_output_max_bytes", DEFAULT_INLINE_OUTPUT_MAX_BYTES
            ),
        ),
        workers=_workers(session, settings),
    )
