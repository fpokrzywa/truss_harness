"""RFC 9457 problem+json error rendering for every error the API can emit."""

from __future__ import annotations

from typing import Any  # Any: problem documents are JSON objects

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from shifde.core.errors import AppError
from shifde.core.redaction import scrub, scrub_text
from shifde.core.securityheaders import security_headers

PROBLEM_MEDIA_TYPE = "application/problem+json"
log = structlog.get_logger("shifde.api")

_STATUS_CODES = {
    400: ("bad_request", "Bad Request"),
    401: ("unauthenticated", "Unauthorized"),
    403: ("forbidden", "Forbidden"),
    404: ("not_found", "Not Found"),
    405: ("method_not_allowed", "Method Not Allowed"),
    409: ("conflict", "Conflict"),
    413: ("payload_too_large", "Payload Too Large"),
    415: ("unsupported_media_type", "Unsupported Media Type"),
    422: ("validation_error", "Unprocessable Entity"),
    429: ("rate_limited", "Too Many Requests"),
}


class ProblemFieldError(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class Problem(BaseModel):
    """OpenAPI schema of every error response."""

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str
    request_id: str | None = None
    errors: list[ProblemFieldError] | None = None


def _request_id(request: Request) -> str | None:
    value = request.scope.get("state", {}).get("request_id")
    return value if isinstance(value, str) else None


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
        "detail": scrub_text(detail),
        "request_id": _request_id(request),
    }
    if errors:
        body["errors"] = scrub(errors)
    headers = {}
    request_id = _request_id(request)
    if request_id:
        headers["X-Request-ID"] = request_id
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return problem_response(
        request,
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        errors=exc.errors,
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Only loc/msg/type are echoed: the offending input value is never returned or logged
    # (it may be a password).
    errors = [
        {
            "loc": list(e.get("loc", ())),
            "msg": str(e.get("msg", "")),
            "type": str(e.get("type", "")),
        }
        for e in exc.errors()
    ]
    return problem_response(
        request,
        status=422,
        code="validation_error",
        title="Unprocessable Entity",
        detail="request validation failed",
        errors=errors,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code, title = _STATUS_CODES.get(exc.status_code, ("http_error", "Error"))
    response = problem_response(
        request, status=exc.status_code, code=code, title=title, detail=str(exc.detail)
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.error("request.unhandled_exception", exc_info=exc)
    response = problem_response(
        request,
        status=500,
        code="internal_error",
        title="Internal Server Error",
        detail="an unexpected error occurred",
    )
    # Starlette's ServerErrorMiddleware runs this outside every user middleware (B-2772)
    response.headers.update(security_headers(request.app.state.settings))
    return response


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)


PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": Problem, "description": desc, "content": {PROBLEM_MEDIA_TYPE: {}}}
    for code, desc in {
        401: "unauthenticated / invalid_credentials",
        403: "forbidden / csrf_failed / auth_disabled",
        404: "not_found (also for resources in other tenants)",
        409: "conflict / scope_archived",
        422: "validation_error",
        429: "rate_limited",
    }.items()
}
