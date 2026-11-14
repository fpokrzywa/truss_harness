"""Request-end hooks (no web-framework imports so services can register them)."""

from __future__ import annotations

from collections.abc import Callable

import structlog
from sqlalchemy.orm import Session

from shifde.core.db import forget_platform_admin

CLOSE_HOOKS_KEY = "shifde_close_hooks"
log = structlog.get_logger("shifde.hooks")


def add_close_hook(session: Session, hook: Callable[[Session], None]) -> None:
    """Run `hook(session)` when the request ends (after the endpoint committed or failed), in a
    fresh transaction. Used to persist authorization-denial records that must survive the
    request rollback. Hooks must commit their own work."""
    session.info.setdefault(CLOSE_HOOKS_KEY, []).append(hook)


def run_close_hooks(session: Session) -> None:
    hooks = session.info.pop(CLOSE_HOOKS_KEY, [])
    if not hooks:
        return
    session.rollback()  # discard request leftovers; hooks use a fresh transaction
    # Hooks record denials; they never act as platform admin, whatever the request had set.
    forget_platform_admin(session)
    for hook in hooks:
        try:
            hook(session)
        except Exception:  # noqa: BLE001 - decision logging must never break the response
            session.rollback()
            log.error("request.close_hook_failed", exc_info=True)
