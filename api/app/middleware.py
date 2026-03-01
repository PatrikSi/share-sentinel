import logging
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("share_sentinel.api")
_REQUEST_ID_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = _normalize_request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "request_failed method=%s path=%s latency_ms=%s request_id=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                request_id,
            )
            raise

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
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
