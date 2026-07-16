import logging
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app import metrics

logger = logging.getLogger("share_sentinel.api")
_REQUEST_ID_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = _normalize_request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_seconds = max(0.0, time.perf_counter() - started)
            elapsed_ms = int(elapsed_seconds * 1000)
            path_label = _normalize_metric_path(request)
            metrics.record_http_request(request.method, path_label, 500, elapsed_seconds)
            metrics.record_http_error(request.method, path_label, type(exc).__name__)
            logger.exception(
                "request_failed method=%s path=%s latency_ms=%s request_id=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                request_id,
            )
            raise

        elapsed_seconds = max(0.0, time.perf_counter() - started)
        elapsed_ms = int(elapsed_seconds * 1000)
        path_label = _normalize_metric_path(request)
        metrics.record_http_request(request.method, path_label, response.status_code, elapsed_seconds)
        if response.status_code >= 500:
            metrics.record_http_error(request.method, path_label, "http_5xx")
        elif response.status_code >= 400:
            metrics.record_http_error(request.method, path_label, "http_4xx")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        if request.url.path.startswith("/auth/") or "set-cookie" in response.headers:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        logger.info(
            "request method=%s path=%s status=%s latency_ms=%s request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response


def _normalize_request_id(raw_request_id: str | None) -> str:
    generated = str(uuid.uuid4())
    if not raw_request_id:
        return generated

    candidate = raw_request_id.strip()
    if not candidate:
        return generated

    sanitized = "".join(char for char in candidate if char in _REQUEST_ID_ALLOWED_CHARS)
    if not sanitized:
        return generated

    return sanitized[:128]


def _normalize_metric_path(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path.strip():
        return route_path
    return "__unmatched__"
