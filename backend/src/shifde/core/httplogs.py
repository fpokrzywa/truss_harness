"""The HTTP client libraries' own loggers (B-3901): at INFO, httpx logs every request's URL,
query values included, so they are raised to WARNING by each process's logging setup (API,
worker, executor) and no outbound request URL is ever logged."""

from __future__ import annotations

import logging

HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def quiet_http_client_logs() -> None:
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
