"""`shifde-api` entrypoint."""

from __future__ import annotations

import sys

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from shifde.app import create_app
from shifde.core.dbrole import UnsafeDatabaseRole, check_database_url
from shifde.core.settings import Settings, settings_or_exit

EXIT_UNSAFE_ROLE = 3


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


def run() -> None:
    settings = settings_or_exit()
    refuse_unsafe_role(settings)
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        proxy_headers=False,  # X-Forwarded-For is handled in-app, only from SHIFDE_TRUSTED_PROXIES
        log_config=None,  # logging is configured by the app (observability port, as SETH)
        access_log=False,  # the request middleware emits the access log (observability port)
        # open requests get this long after a stop signal (SP4 design 6.2)
        timeout_graceful_shutdown=settings.api_shutdown_grace_seconds,
    )


if __name__ == "__main__":
    run()
