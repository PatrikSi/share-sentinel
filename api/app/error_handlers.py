import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app import metrics

logger = logging.getLogger("share_sentinel.errors")

HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
}


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(SQLAlchemyTimeoutError, database_pool_timeout_handler)
    app.add_exception_handler(OperationalError, database_operational_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = HTTP_ERROR_CODES.get(exc.status_code, f"http_{exc.status_code}")
    return _error_response(
        request,
        status_code=exc.status_code,
        code=code,
        detail=exc.detail,
        headers=exc.headers,
    )


async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(
        request,
        status_code=422,
        code="validation_error",
        detail=exc.errors(),
        message="Request validation failed",
    )


async def database_pool_timeout_handler(request: Request, exc: SQLAlchemyTimeoutError) -> JSONResponse:
    metrics.record_database_error("pool_timeout")
    logger.exception("database_pool_exhausted request_id=%s", _request_id(request), exc_info=exc)
    return _error_response(
        request,
        status_code=503,
        code="database_capacity_exceeded",
        detail="Database capacity is temporarily exhausted; retry shortly",
        headers={"Retry-After": "1"},
    )


async def database_operational_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    metrics.record_database_error("operational_error")
    logger.exception("database_operation_failed request_id=%s", _request_id(request), exc_info=exc)
    return _error_response(
        request,
        status_code=503,
        code="database_unavailable",
        detail="Database operation could not be completed; retry shortly",
        headers={"Retry-After": "1"},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # RequestContextMiddleware records the stack and latency. Keep the client
    # response diagnostic but free of database, filesystem, or credential data.
    return _error_response(
        request,
        status_code=500,
        code="internal_error",
        detail="An unexpected server error occurred",
    )


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: Any,
    message: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    response_message = message or _detail_message(detail, status_code)
    response_headers = {
        "X-Request-ID": request_id,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
        **(headers or {}),
    }
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            {
                # Preserve FastAPI's established `detail` contract for clients.
                "detail": detail,
                "error": {
                    "code": code,
                    "message": response_message,
                    "request_id": request_id,
                },
            }
        ),
        headers=response_headers,
    )


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def _detail_message(detail: Any, status_code: int) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail
    if isinstance(detail, dict):
        candidate = detail.get("message") or detail.get("detail")
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request failed"
