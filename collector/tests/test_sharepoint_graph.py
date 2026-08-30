import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import requests
from sharepoint.auth import ExistingTokenAuthProvider, GraphTokenContext
from sharepoint.graph import GraphAPIError, GraphClient, GraphProtocolError, GraphRunAttemptBudget, iter_values


class StaticProvider:
    supports_refresh = False

    def __init__(self, token: str = "sensitive-token") -> None:
        self.calls = 0
        self.context = GraphTokenContext(
            access_token=token,
            auth_mode="token",
            auth_type="delegated",
            tenant_id="tenant-1",
            client_id="client-1",
            user_id="user-1",
            user_principal_name="alice@example.com",
            scopes=("Sites.Read.All",),
            roles=(),
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )

    def acquire_token(self) -> GraphTokenContext:
        self.calls += 1
        return self.context


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, object] | None = None,
        *,
        headers: dict[str, str] | None = None,
        stream_error: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._raw = json.dumps(payload or {}).encode()
        self.stream_error = stream_error
        self.closed = False

    def iter_content(self, chunk_size: int):  # noqa: ARG002
        if self.stream_error:
            raise self.stream_error
        yield self._raw

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class AttemptBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0

    def reserve_attempt(self) -> bool:
        if self.used >= self.maximum:
            return False
        self.used += 1
        return True


def _client(session: FakeSession, **kwargs) -> GraphClient:
    return GraphClient(StaticProvider(), session=session, sleep=lambda _delay: None, **kwargs)


def test_graph_pagination_follows_absolute_next_links() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [{"id": "1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites?page=2",
                },
            ),
            FakeResponse(
                200,
                {
                    "value": [{"id": "2"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites?page=3",
                },
            ),
            FakeResponse(200, {"value": [{"id": "3"}]}),
        ]
    )

    values = list(iter_values(_client(session).iter_pages("sites")))

    assert [value["id"] for value in values] == ["1", "2", "3"]
    assert len(session.calls) == 3


def test_national_cloud_client_uses_only_its_selected_graph_boundary() -> None:
    provider = StaticProvider()
    provider.context = replace(provider.context, cloud="gcc-high")
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [{"id": "1"}],
                    "@odata.nextLink": "https://graph.microsoft.us/v1.0/sites?page=2",
                },
            ),
            FakeResponse(200, {"value": [{"id": "2"}]}),
        ]
    )
    client = GraphClient(provider, cloud="gcc-high", session=session, sleep=lambda _delay: None)

    assert [row["id"] for row in iter_values(client.iter_pages("sites"))] == ["1", "2"]
    assert session.calls[0]["url"] == "https://graph.microsoft.us/v1.0/sites"
    assert client.sharepoint_hostname_allowed("tenant.sharepoint.us") is True
    assert client.sharepoint_hostname_allowed("tenant.sharepoint.com") is False


def test_national_cloud_client_rejects_cross_cloud_continuation_and_token_context() -> None:
    provider = StaticProvider()
    provider.context = replace(provider.context, cloud="gcc-high")
    client = GraphClient(
        provider,
        cloud="gcc-high",
        session=FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "value": [],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites?page=2",
                    },
                )
            ]
        ),
        sleep=lambda _delay: None,
    )

    with pytest.raises(GraphProtocolError, match="unsafe_continuation_url"):
        list(client.iter_pages("sites"))

    with pytest.raises(ValueError, match="does not match the selected cloud"):
        GraphClient(StaticProvider(), cloud="gcc-high", initial_token_context=StaticProvider().context)


@pytest.mark.parametrize(
    "next_link,code",
    [
        ("https://attacker.example/collect", "unsafe_continuation_url"),
        ("http://graph.microsoft.com/v1.0/sites", "unsafe_continuation_url"),
        ("https://graph.microsoft.com/beta/sites", "unsafe_continuation_path"),
        ("https://graph.microsoft.com/v1.0/../beta/sites", "unsafe_continuation_path"),
        ("https://graph.microsoft.com/v1.0/%2e%2e/beta/sites", "unsafe_continuation_path"),
        ("https://graph.microsoft.com/v1.0/%252e%252e/beta/sites", "unsafe_continuation_path"),
        (
            "https://graph.microsoft.com/v1.0/%252525252e%252525252e/beta/sites",
            "unsafe_continuation_path",
        ),
        ("https://graph.microsoft.com:444/v1.0/sites", "unsafe_continuation_url"),
    ],
)
def test_continuation_links_cannot_exfiltrate_authorization(next_link: str, code: str) -> None:
    session = FakeSession([FakeResponse(200, {"value": [], "@odata.nextLink": next_link})])
    client = _client(session)

    with pytest.raises(GraphProtocolError) as exc:
        list(client.iter_pages("sites"))

    assert exc.value.code == code
    assert len(session.calls) == 1


def test_continuation_path_with_ambiguous_decode_depth_fails_closed() -> None:
    client = _client(FakeSession([]))
    deeply_encoded = "%" + ("25" * 16) + "41"

    with pytest.raises(GraphProtocolError) as exc:
        client.validate_continuation_url(f"https://graph.microsoft.com/v1.0/sites/{deeply_encoded}")

    assert exc.value.code == "unsafe_continuation_path"


def test_graph_429_respects_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            FakeResponse(429, {"error": {"code": "tooManyRequests"}}, headers={"Retry-After": "7"}),
            FakeResponse(200, {"value": []}),
        ]
    )
    client = GraphClient(
        StaticProvider(),
        session=session,
        sleep=sleeps.append,
        random_source=lambda: 0.0,
    )

    assert client.get("sites") == {"value": []}
    assert sleeps == [7.0]
    assert client.retry_count == 1


def test_attempt_budget_counts_retries_and_pages_before_sending_http() -> None:
    retry_session = FakeSession(
        [
            FakeResponse(429, {"error": {"code": "tooManyRequests"}}, headers={"Retry-After": "0"}),
            FakeResponse(200, {"value": []}),
        ]
    )
    retry_budget = AttemptBudget(1)
    retry_client = GraphClient(
        StaticProvider(),
        session=retry_session,
        sleep=lambda _delay: None,
        random_source=lambda: 0.0,
    )

    with pytest.raises(GraphAPIError) as retry_error:
        retry_client.get("sites", attempt_budget=retry_budget)

    assert retry_error.value.code == "request_budget_exhausted"
    assert retry_budget.used == 1
    assert len(retry_session.calls) == 1

    page_session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "value": [{"id": "1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites?page=2",
                },
            ),
            FakeResponse(200, {"value": [{"id": "2"}]}),
        ]
    )
    page_budget = AttemptBudget(1)
    pages = _client(page_session).iter_pages("sites", attempt_budget=page_budget)

    assert next(pages)["value"] == [{"id": "1"}]
    with pytest.raises(GraphAPIError) as page_error:
        next(pages)
    assert page_error.value.code == "request_budget_exhausted"
    assert page_budget.used == 1
    assert len(page_session.calls) == 1


def test_run_attempt_budget_is_thread_safe_and_enforces_atomic_surface_limits() -> None:
    budget = GraphRunAttemptBudget(20)
    permission_budget = budget.scoped("permissions", 7)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def reserve() -> None:
        outcome = permission_budget.reserve_attempt()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=reserve) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = budget.snapshot()
    assert outcomes.count(True) == 7
    assert outcomes.count(False) == 23
    assert snapshot.used == 7
    assert snapshot.attempts_by_surface == {"permissions": 7}
    assert snapshot.exhausted is True
    assert snapshot.exhausted_surfaces == ("permissions",)


def test_default_run_budget_counts_retries_and_stops_before_the_next_http_attempt() -> None:
    session = FakeSession(
        [
            FakeResponse(429, {"error": {"code": "tooManyRequests"}}, headers={"Retry-After": "0"}),
            FakeResponse(200, {"value": []}),
        ]
    )
    budget = GraphRunAttemptBudget(1)
    client = _client(session, attempt_budget=budget)

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites")

    assert exc.value.code == "request_budget_exhausted"
    assert len(session.calls) == 1
    assert budget.snapshot().public_metadata()["exhausted"] is True


def test_retry_after_beyond_operator_budget_fails_without_early_retry() -> None:
    session = FakeSession([FakeResponse(429, {}, headers={"Retry-After": "600", "request-id": "req-1"})])
    client = _client(session, max_retry_delay=30)

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites")

    assert exc.value.code == "retry_after_exceeds_budget"
    assert exc.value.retryable is True
    assert len(session.calls) == 1


def test_stream_failure_is_retried_and_sanitized() -> None:
    secret_url = "https://graph.microsoft.com/v1.0/sites?$skiptoken=sensitive"
    stream_error = requests.exceptions.ChunkedEncodingError(f"failed reading {secret_url}")
    session = FakeSession(
        [
            FakeResponse(200, {}, stream_error=stream_error),
            FakeResponse(200, {"value": []}),
        ]
    )
    client = _client(session)

    assert client.get("sites") == {"value": []}
    assert len(session.calls) == 2

    failing = _client(FakeSession([FakeResponse(200, {}, stream_error=stream_error)]), max_attempts=1)
    with pytest.raises(GraphAPIError) as exc:
        failing.get("sites")
    assert "skiptoken" not in str(exc.value)
    assert "sensitive-token" not in str(exc.value)


@pytest.mark.parametrize(
    "raw_error",
    [
        {"code": "Request_UnsupportedQuery", "message": "The requested query is not supported."},
        {"code": "BadRequest", "message": "Could not find a property named 'owner' on this type."},
        {"code": "BadRequest", "message": "The $select property 'createdBy' is not supported."},
    ],
)
def test_explicit_optional_select_rejections_receive_a_safe_specific_code(raw_error) -> None:
    client = _client(FakeSession([FakeResponse(400, {"error": raw_error})]))

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites?$select=id,root")

    assert exc.value.status_code == 400
    assert exc.value.code == "unsupported_select"
    assert "owner" not in str(exc.value)
    assert "createdBy" not in str(exc.value)


def test_generic_bad_request_is_not_mislabeled_as_an_optional_select_rejection() -> None:
    client = _client(
        FakeSession([FakeResponse(400, {"error": {"code": "BadRequest", "message": "Malformed identifier."}})])
    )

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites/not-valid")

    assert exc.value.code == "BadRequest"


def test_delta_410_preserves_reset_location_without_logging_it() -> None:
    reset_url = "https://graph.microsoft.com/v1.0/drives/d/root/delta?$token=opaque"
    client = _client(
        FakeSession(
            [
                FakeResponse(
                    410,
                    {"error": {"code": "resyncChangesApplyDifferences"}},
                    headers={"Location": reset_url},
                )
            ]
        )
    )

    with pytest.raises(GraphAPIError) as exc:
        client.get("drives/d/root/delta")

    assert exc.value.status_code == 410
    assert exc.value.reset_url == reset_url
    assert "opaque" not in str(exc.value)


def test_single_attempt_401_reports_authentication_failure() -> None:
    client = _client(
        FakeSession([FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}})]),
        max_attempts=1,
    )

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites")

    assert exc.value.status_code == 401
    assert exc.value.code == "InvalidAuthenticationToken"


def test_imported_one_shot_token_401_is_terminal_and_not_reread() -> None:
    calls = 0

    def one_shot_reader() -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("imported token must not be reacquired after a Graph 401")
        return "opaque-token"

    provider = ExistingTokenAuthProvider(
        one_shot_reader,
        opaque_auth_type="delegated",
        tenant_id="tenant.example",
        assessed_identity="alice@example.com",
    )
    session = FakeSession([FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}})])
    client = GraphClient(provider, session=session, sleep=lambda _delay: None)

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites")

    assert exc.value.status_code == 401
    assert calls == 1
    assert len(session.calls) == 1


def test_msal_managed_provider_refreshes_once_after_401() -> None:
    provider = StaticProvider()
    provider.supports_refresh = True
    session = FakeSession(
        [
            FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}}),
            FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}}),
        ]
    )
    client = GraphClient(provider, session=session, sleep=lambda _delay: None)

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites")

    assert exc.value.status_code == 401
    assert provider.calls == 2
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "tenant-2"},
        {"client_id": "client-2"},
        {"user_id": "user-2"},
        {"user_principal_name": "mallory@example.com"},
        {"auth_type": "application"},
        {"scopes": ("Sites.ReadWrite.All",)},
        {"roles": ("Sites.Read.All",)},
    ],
)
def test_refreshed_token_cannot_change_assessment_context(changes: dict[str, object]) -> None:
    initial = StaticProvider().context
    refreshed = replace(initial, access_token="replacement-token", **changes)

    class RefreshingProvider:
        supports_refresh = True

        def __init__(self) -> None:
            self.calls = 0

        def acquire_token(self) -> GraphTokenContext:
            self.calls += 1
            return refreshed

    provider = RefreshingProvider()
    session = FakeSession([FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}})])
    client = GraphClient(
        provider,
        initial_token_context=initial,
        session=session,
        sleep=lambda _delay: None,
    )

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites")

    assert exc.value.code == "auth_context_changed"
    assert exc.value.retryable is False
    assert provider.calls == 1
    assert len(session.calls) == 1
    assert "sensitive-token" not in str(exc.value)
    assert "replacement-token" not in str(exc.value)


def test_near_expiry_imported_token_is_not_reacquired_or_sent() -> None:
    reads = 0

    def token_reader() -> str:
        nonlocal reads
        reads += 1
        raise AssertionError("the imported token source must not be reread")

    provider = ExistingTokenAuthProvider(
        token_reader,
        opaque_auth_type="delegated",
        tenant_id="tenant-1",
        assessed_identity="alice@example.com",
    )
    initial = replace(
        StaticProvider().context,
        access_token="imported-token",
        expires_at=datetime.now(tz=UTC) + timedelta(seconds=30),
    )
    session = FakeSession([])
    client = GraphClient(
        provider,
        initial_token_context=initial,
        session=session,
        sleep=lambda _delay: None,
    )

    with pytest.raises(GraphAPIError) as exc:
        client.get("sites")

    assert exc.value.code == "token_expiring"
    assert exc.value.retryable is False
    assert reads == 0
    assert session.calls == []


def test_pagination_cycle_and_page_limit_are_bounded() -> None:
    repeated = "https://graph.microsoft.com/v1.0/sites?page=1"
    cycle = _client(FakeSession([FakeResponse(200, {"value": [], "@odata.nextLink": repeated})]))
    with pytest.raises(GraphProtocolError, match="pagination_cycle"):
        list(cycle.iter_pages(repeated))

    limited = _client(
        FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "value": [],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites?page=2",
                    },
                )
            ]
        ),
        max_pages=1,
    )
    with pytest.raises(GraphProtocolError, match="page_limit_reached"):
        list(limited.iter_pages("sites"))


def test_response_size_is_bounded_before_json_decode() -> None:
    response = FakeResponse(200, {"value": ["x" * 2048]}, headers={"Content-Length": "999999"})
    client = _client(FakeSession([response]), max_response_bytes=1024)

    with pytest.raises(GraphProtocolError, match="response_too_large"):
        client.get("sites")
    assert response.closed is True
