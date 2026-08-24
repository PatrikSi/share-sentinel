from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator
from urllib.parse import urljoin, urlparse

import requests

from .auth import GraphAuthProvider, GraphTokenContext

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0/"
RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
SAFE_ERROR_CODE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class GraphAPIError(RuntimeError):
    """A sanitized Microsoft Graph failure that never exposes a request URL or token."""

    def __init__(
        self,
        *,
        status_code: int | None,
        code: str,
        request_id: str | None = None,
        retryable: bool = False,
        reset_url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.retryable = retryable
        self.reset_url = reset_url
        status = f"HTTP {status_code}" if status_code is not None else "network failure"
        request_suffix = f"; request ID {request_id}" if request_id else ""
        super().__init__(f"Microsoft Graph request failed ({status}, {code}{request_suffix})")


class GraphProtocolError(GraphAPIError):
    pass


def _safe_graph_code(value: object, fallback: str) -> str:
    normalized = str(value or "").strip()[:128]
    if normalized and all(character in SAFE_ERROR_CODE for character in normalized):
        return normalized
    return fallback


def _retry_after_seconds(value: str | None, *, now: Callable[[], datetime]) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - now()).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _assessment_context_signature(context: GraphTokenContext) -> tuple[object, ...]:
    """Describe the durable identity/permission boundary, excluding token material."""

    def folded(value: str | None) -> str | None:
        return value.casefold() if value else None

    return (
        context.auth_mode,
        context.auth_type,
        folded(context.tenant_id),
        folded(context.client_id),
        folded(context.user_id) if context.auth_type == "delegated" else None,
        folded(context.user_principal_name) if context.auth_type == "delegated" else None,
        tuple(sorted(set(context.scopes))),
        tuple(sorted(set(context.roles))),
    )


class GraphClient:
    def __init__(
        self,
        auth_provider: GraphAuthProvider,
        *,
        base_url: str = GRAPH_BASE_URL,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_attempts: int = 5,
        backoff_base: float = 0.5,
        max_retry_delay: float = 120.0,
        max_response_bytes: int = 32 * 1024 * 1024,
        max_pages: int = 100_000,
        session: requests.Session | None = None,
        initial_token_context: GraphTokenContext | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        parsed_base = urlparse(base_url)
        if parsed_base.scheme != "https" or not parsed_base.hostname:
            raise ValueError("Microsoft Graph base URL must be HTTPS")
        self.base_url = base_url.rstrip("/") + "/"
        self._allowed_host = parsed_base.hostname.casefold()
        self._allowed_port = parsed_base.port or 443
        self._allowed_path_prefix = parsed_base.path.rstrip("/") + "/"
        self.auth_provider = auth_provider
        self.connect_timeout = max(0.1, float(connect_timeout))
        self.read_timeout = max(0.1, float(read_timeout))
        self.max_attempts = max(1, min(int(max_attempts), 20))
        self.backoff_base = max(0.0, float(backoff_base))
        self.max_retry_delay = max(0.0, float(max_retry_delay))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.max_pages = max(1, int(max_pages))
        self._injected_session = session
        self._thread_local = threading.local()
        self._sleep = sleep
        self._random = random_source
        self._now = now
        self._token_lock = threading.Lock()
        self._token_context = initial_token_context
        self._assessment_context = (
            _assessment_context_signature(initial_token_context) if initial_token_context is not None else None
        )
        self._stats_lock = threading.Lock()
        self._retry_count = 0

    @property
    def token_context(self) -> GraphTokenContext:
        return self._get_token_context()

    @property
    def retry_count(self) -> int:
        with self._stats_lock:
            return self._retry_count

    def _get_session(self):
        if self._injected_session is not None:
            return self._injected_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
        return session

    def _get_token_context(self, *, force: bool = False) -> GraphTokenContext:
        with self._token_lock:
            refreshable = bool(getattr(self.auth_provider, "supports_refresh", False))
            if self._token_context is None:
                self._accept_token_context(self.auth_provider.acquire_token())

            context = self._token_context
            assert context is not None
            if not force and not context.is_expiring():
                return context
            if not refreshable:
                if context.is_expiring():
                    raise GraphAPIError(
                        status_code=401,
                        code="token_expiring",
                        retryable=False,
                    )
                return context

            self._accept_token_context(self.auth_provider.acquire_token())
            context = self._token_context
            assert context is not None
            if context.is_expiring():
                raise GraphAPIError(
                    status_code=401,
                    code="token_expiring",
                    retryable=False,
                )
            return context

    def _accept_token_context(self, candidate: GraphTokenContext) -> None:
        signature = _assessment_context_signature(candidate)
        if self._assessment_context is None:
            self._assessment_context = signature
        elif signature != self._assessment_context:
            raise GraphAPIError(
                status_code=401,
                code="auth_context_changed",
                retryable=False,
            )
        self._token_context = candidate

    def _validated_url(self, url: str) -> str:
        absolute = urljoin(self.base_url, str(url or "").lstrip("/"))
        if str(url or "").startswith(("http://", "https://")):
            absolute = str(url)
        parsed = urlparse(absolute)
        try:
            parsed_port = parsed.port or 443
        except ValueError as exc:
            raise GraphProtocolError(status_code=None, code="invalid_continuation_url") from exc
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() != self._allowed_host
            or parsed_port != self._allowed_port
        ):
            raise GraphProtocolError(
                status_code=None,
                code="unsafe_continuation_url",
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise GraphProtocolError(status_code=None, code="invalid_continuation_url")
        normalized_path = parsed.path.rstrip("/") + "/"
        if not normalized_path.startswith(self._allowed_path_prefix):
            raise GraphProtocolError(status_code=None, code="unsafe_continuation_path")
        return absolute

    def validate_continuation_url(self, url: str) -> str:
        """Validate an opaque next/delta link without logging or modifying it."""

        return self._validated_url(url)

    def get(self, url: str) -> dict[str, object]:
        return self.request("GET", url)

    def post(self, url: str, *, json_body: dict[str, object]) -> dict[str, object]:
        return self.request("POST", url, json_body=json_body)

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        absolute_url = self._validated_url(url)
        last_transport_error = False
        refreshed_after_401 = False
        for attempt in range(1, self.max_attempts + 1):
            context = self._get_token_context()
            headers = {
                "Authorization": f"Bearer {context.access_token}",
                "Accept": "application/json",
                "User-Agent": "share-sentinel-sharepoint/1.0.0",
            }
            try:
                response = self._get_session().request(
                    method.upper(),
                    absolute_url,
                    headers=headers,
                    json=json_body,
                    timeout=(self.connect_timeout, self.read_timeout),
                    stream=True,
                )
                last_transport_error = False
            except requests.RequestException as exc:
                last_transport_error = True
                if attempt >= self.max_attempts:
                    raise GraphAPIError(
                        status_code=None,
                        code="transport_failure",
                        retryable=True,
                    ) from exc
                self._wait_before_retry(attempt, retry_after=None)
                continue

            request_id = (
                str(response.headers.get("request-id") or response.headers.get("client-request-id") or "").strip()[:128]
                or None
            )
            if response.status_code in RETRIABLE_STATUSES:
                if attempt < self.max_attempts:
                    retry_after = _retry_after_seconds(
                        response.headers.get("Retry-After"),
                        now=self._now,
                    )
                    if retry_after is not None and retry_after > self.max_retry_delay:
                        self._close_response(response)
                        raise GraphAPIError(
                            status_code=response.status_code,
                            code="retry_after_exceeds_budget",
                            request_id=request_id,
                            retryable=True,
                        )
                    self._close_response(response)
                    self._wait_before_retry(
                        attempt,
                        retry_after=retry_after,
                    )
                    continue
                code = "throttled" if response.status_code == 429 else "transient_failure"
                self._close_response(response)
                raise GraphAPIError(
                    status_code=response.status_code,
                    code=code,
                    request_id=request_id,
                    retryable=True,
                )

            try:
                payload = self._read_json(response, request_id=request_id)
            except requests.RequestException as exc:
                if attempt >= self.max_attempts:
                    raise GraphAPIError(
                        status_code=None,
                        code="transport_failure",
                        retryable=True,
                    ) from exc
                self._wait_before_retry(attempt, retry_after=None)
                continue
            if 200 <= response.status_code < 300:
                return payload

            raw_error = payload.get("error") if isinstance(payload, dict) else None
            graph_code = raw_error.get("code") if isinstance(raw_error, dict) else None
            code = _safe_graph_code(graph_code, f"http_{response.status_code}")
            reset_url = response.headers.get("Location") if response.status_code == 410 else None
            if (
                response.status_code == 401
                and attempt < self.max_attempts
                and not refreshed_after_401
                and bool(getattr(self.auth_provider, "supports_refresh", False))
            ):
                # A cached MSAL token may have been revoked. Refresh once, but
                # imported-token providers are deliberately terminal: rereading
                # stdin/file/env cannot produce a new credential safely.
                self._get_token_context(force=True)
                refreshed_after_401 = True
                continue
            raise GraphAPIError(
                status_code=response.status_code,
                code=code,
                request_id=request_id,
                retryable=False,
                reset_url=str(reset_url) if reset_url else None,
            )

        code = "transport_failure" if last_transport_error else "request_failed"
        raise GraphAPIError(status_code=None, code=code, retryable=True)

    def _read_json(self, response, *, request_id: str | None) -> dict[str, object]:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self.max_response_bytes:
                    self._close_response(response)
                    raise GraphProtocolError(
                        status_code=response.status_code,
                        code="response_too_large",
                        request_id=request_id,
                    )
            except ValueError:
                pass

        chunks: list[bytes] = []
        size = 0
        try:
            iterator = response.iter_content(chunk_size=64 * 1024)
            for chunk in iterator:
                if not chunk:
                    continue
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise GraphProtocolError(
                        status_code=response.status_code,
                        code="response_too_large",
                        request_id=request_id,
                    )
                chunks.append(chunk)
        finally:
            self._close_response(response)
        raw = b"".join(chunks)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraphProtocolError(
                status_code=response.status_code,
                code="malformed_json",
                request_id=request_id,
            ) from exc
        if not isinstance(payload, dict):
            raise GraphProtocolError(
                status_code=response.status_code,
                code="malformed_response",
                request_id=request_id,
            )
        return payload

    @staticmethod
    def _close_response(response) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    def _wait_before_retry(self, attempt: int, *, retry_after: float | None) -> None:
        exponential_cap = min(
            self.max_retry_delay,
            self.backoff_base * (2 ** min(max(0, attempt - 1), 16)),
        )
        jittered = exponential_cap * max(0.0, min(1.0, float(self._random())))
        delay = max(jittered, retry_after or 0.0)
        delay = min(delay, self.max_retry_delay)
        with self._stats_lock:
            self._retry_count += 1
        self._sleep(delay)

    def iter_pages(self, url: str) -> Iterator[dict[str, object]]:
        next_url: str | None = url
        seen: set[str] = set()
        page_count = 0
        while next_url:
            page_count += 1
            if page_count > self.max_pages:
                raise GraphProtocolError(status_code=None, code="page_limit_reached")
            validated = self._validated_url(next_url)
            fingerprint = hashlib.sha256(validated.encode("utf-8")).hexdigest()
            if fingerprint in seen:
                raise GraphProtocolError(status_code=None, code="pagination_cycle")
            seen.add(fingerprint)
            payload = self.get(validated)
            yield payload
            raw_next = payload.get("@odata.nextLink")
            if raw_next is None:
                next_url = None
            elif not isinstance(raw_next, str) or not raw_next.strip():
                raise GraphProtocolError(status_code=None, code="invalid_next_link")
            else:
                next_url = raw_next


def iter_values(pages: Iterator[dict[str, object]]) -> Iterator[dict[str, object]]:
    for page in pages:
        values = page.get("value")
        if not isinstance(values, list):
            raise GraphProtocolError(status_code=None, code="missing_page_values")
        for value in values:
            if not isinstance(value, dict):
                raise GraphProtocolError(status_code=None, code="malformed_page_item")
            yield value
