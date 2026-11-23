"""`shifde-api` entrypoint."""

from __future__ import annotations

import contextlib
import signal
import sys
from typing import Any

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from shifde.app import create_app
from shifde.core.dbrole import UnsafeDatabaseRole, check_database_url
from shifde.core.settings import Settings, settings_or_exit

EXIT_UNSAFE_ROLE = 3
EXIT_STOPPED = 0


def refuse_unsafe_role(settings: Settings) -> None:
    """Exit with code 3 and one line on stderr unless `SHIFDE_DATABASE_URL` connects as a safe
    application role (see `shifde.core.dbrole`). Fails closed: a role that cannot be checked is
    refused too. Runs before anything binds a port."""
    try:
        check_database_url(settings.database_url)
    except UnsafeDatabaseRole as exc:
        print(f"shifde: refusing to start: {exc.reason}", file=sys.stderr, flush=True)
        raise SystemExit(EXIT_UNSAFE_ROLE) from None
    except SQLAlchemyError as exc:
        # The driver message can echo connection details; report the error class only.
        print(
            "shifde: refusing to start: cannot verify the database role "
            f"({exc.__class__.__name__})",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(EXIT_UNSAFE_ROLE) from None


def _stop(_signum: int, _frame: object) -> None:
    raise SystemExit(EXIT_STOPPED)


def _install_stop_handlers() -> dict[int, Any]:
    """Make a stop signal exit 0 ("stopped on request"), both before uvicorn serves and when it
    re-raises the signal after its graceful shutdown: the default action would end the process
    with 3 on Windows (SIGBREAK, SIGINT), the unsafe-role code. Returns the handlers replaced."""
    previous: dict[int, Any] = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            # not the main thread / unsupported here
            with contextlib.suppress(ValueError, OSError):
                previous[sig] = signal.signal(sig, _stop)
    return previous


def _restore_handlers(previous: dict[int, Any]) -> None:
    for sig, handler in previous.items():
        if handler is not None:  # None: installed outside Python, cannot be put back
            signal.signal(sig, handler)


def run() -> None:
    previous = _install_stop_handlers()
    try:
        settings = settings_or_exit()
        refuse_unsafe_role(settings)
        uvicorn.run(
            create_app(settings),
            host=settings.api_host,
            port=settings.api_port,
            # X-Forwarded-For is handled in-app, only from SHIFDE_TRUSTED_PROXIES
            proxy_headers=False,
            log_config=None,  # logging is configured by the app (observability port, as SETH)
            access_log=False,  # the request middleware emits the access log (observability port)
            # open requests get this long after a stop signal (SP4 design 6.2)
            timeout_graceful_shutdown=settings.api_shutdown_grace_seconds,
        )
    finally:
        _restore_handlers(previous)


if __name__ == "__main__":
    run()
