import base64
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sharepoint import auth


def _jwt(claims: dict[str, object]) -> str:
    def encode(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


def _base_claims() -> dict[str, object]:
    return {
        "aud": "https://graph.microsoft.com",
        "tid": "tenant-1",
        "exp": int((datetime.now(tz=UTC) + timedelta(hours=1)).timestamp()),
        "azp": "client-1",
    }


def test_inspect_delegated_token_exposes_only_safe_metadata() -> None:
    claims = {
        **_base_claims(),
        "scp": "Sites.Read.All Files.Read.All",
        "oid": "user-1",
        "preferred_username": "alice@example.com",
    }
    raw_token = _jwt(claims)

    context = auth.inspect_access_token(raw_token, auth_mode="token")

    assert context.auth_type == "delegated"
    assert context.scopes == ("Files.Read.All", "Sites.Read.All")
    assert context.assessed_identity == "alice@example.com"
    assert raw_token not in repr(context)
    assert "access_token" not in context.public_metadata()


def test_inspect_application_token_uses_roles() -> None:
    context = auth.inspect_access_token(
        _jwt({**_base_claims(), "roles": ["Sites.Read.All"]}),
        auth_mode="app",
        expected_auth_type="application",
    )

    assert context.auth_type == "application"
    assert context.roles == ("Sites.Read.All",)
    assert context.user_id is None


@pytest.mark.parametrize(
    "claim_update,message",
    [
        ({"aud": "https://management.azure.com"}, "audience"),
        ({"exp": 1}, "expired"),
        ({"scp": None}, "neither delegated scopes"),
    ],
)
def test_token_metadata_validation_rejects_wrong_or_unusable_tokens(
    claim_update: dict[str, object],
    message: str,
) -> None:
    claims = {**_base_claims(), "scp": "Sites.Read.All", **claim_update}

    with pytest.raises(auth.TokenAcquisitionError, match=message):
        auth.inspect_access_token(_jwt(claims), auth_mode="token")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_token_file_requires_user_only_permissions(tmp_path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret-token", encoding="utf-8")
    token_file.chmod(0o644)

    with pytest.raises(auth.TokenAcquisitionError, match="permissions are too broad"):
        auth.token_reader_from_file(str(token_file))()

    token_file.chmod(0o600)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert auth.token_reader_from_file(str(token_file))() == "secret-token"


def test_token_environment_name_is_validated(monkeypatch) -> None:
    with pytest.raises(auth.TokenAcquisitionError, match="name is invalid"):
        auth.token_reader_from_env("BAD-NAME")

    monkeypatch.setenv("SAFE_GRAPH_TOKEN", "token-value")
    assert auth.token_reader_from_env("SAFE_GRAPH_TOKEN")() == "token-value"


def test_stdin_token_reader_is_one_shot_and_bounded() -> None:
    class OneShot:
        calls = 0

        def read(self, _size):
            self.calls += 1
            return "token-value"

    stream = OneShot()
    reader = auth.token_reader_from_stdin(stream)
    assert reader() == "token-value"
    assert reader() == "token-value"
    assert stream.calls == 1


def test_wam_and_iwa_fail_cleanly_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(auth.TokenAcquisitionError, match="WAM is only supported on Windows"):
        auth.PublicClientAuthProvider(
            auth_mode="wam",
            tenant_id="organizations",
            client_id="client-id",
        )
    with pytest.raises(auth.TokenAcquisitionError, match="only supported on Windows"):
        auth.PublicClientAuthProvider(
            auth_mode="iwa",
            tenant_id="organizations",
            client_id="client-id",
        )


@pytest.mark.skipif(sys.platform != "win32", reason="WAM is Windows-only")
def test_wam_builds_broker_enabled_public_client_without_authenticating(monkeypatch) -> None:
    captured: dict[str, object] = {}
    application = object()

    def build(client_id: str, **kwargs):
        captured["client_id"] = client_id
        captured["kwargs"] = kwargs
        return application

    monkeypatch.setattr(auth.msal, "PublicClientApplication", build)
    provider = auth.PublicClientAuthProvider(
        auth_mode="wam",
        tenant_id="organizations",
        client_id="client-id",
    )

    assert provider._build_application() is application
    assert captured["client_id"] == "client-id"
    assert captured["kwargs"] == {
        "authority": "https://login.microsoftonline.com/organizations",
        "enable_pii_log": False,
        "enable_broker_on_windows": True,
    }


def test_app_secret_is_required_without_echoing_value() -> None:
    with pytest.raises(auth.TokenAcquisitionError, match="SHARE_SENTINEL_GRAPH_CLIENT_SECRET") as exc:
        auth.AppCredentialAuthProvider(
            tenant_id="tenant.example",
            client_id="client-id",
            client_secret="",
        )
    assert "client_secret" not in str(exc.value)


def test_no_ropc_or_password_provider_is_exposed() -> None:
    source = open(auth.__file__, encoding="utf-8").read()
    assert "acquire_token_by_username_password" not in source
    assert "--password" not in source


def test_opaque_delegated_token_requires_explicit_safe_attribution() -> None:
    with pytest.raises(auth.TokenAcquisitionError, match="require --token-type"):
        auth.ExistingTokenAuthProvider(lambda: "opaque-token").acquire_token()

    with pytest.raises(auth.TokenAcquisitionError, match="--assessed-identity"):
        auth.ExistingTokenAuthProvider(
            lambda: "opaque-token",
            opaque_auth_type="delegated",
            tenant_id="tenant.example",
        ).acquire_token()

    context = auth.ExistingTokenAuthProvider(
        lambda: "opaque-token",
        opaque_auth_type="delegated",
        tenant_id="tenant.example",
        assessed_identity="alice@example.com",
    ).acquire_token()
    assert context.auth_type == "delegated"
    assert context.assessed_identity == "alice@example.com"
    assert context.expires_at is None
    assert context.jwt_inspection == "opaque_token_context_supplied_by_operator"
    assert "opaque-token" not in str(context.public_metadata())


def test_opaque_application_token_requires_stable_client_and_tenant() -> None:
    with pytest.raises(auth.TokenAcquisitionError, match="client ID is required"):
        auth.ExistingTokenAuthProvider(
            lambda: "opaque-token",
            opaque_auth_type="application",
            tenant_id="tenant.example",
        ).acquire_token()

    context = auth.ExistingTokenAuthProvider(
        lambda: "opaque-token",
        opaque_auth_type="application",
        tenant_id="tenant.example",
        client_id="client-id",
    ).acquire_token()
    assert context.auth_type == "application"
    assert context.client_id == "client-id"


def test_parseable_jwt_still_validates_explicit_token_type() -> None:
    token = _jwt({**_base_claims(), "scp": "Sites.Read.All", "oid": "user-1"})
    provider = auth.ExistingTokenAuthProvider(
        lambda: token,
        opaque_auth_type="application",
    )

    with pytest.raises(auth.TokenAcquisitionError, match="where application was required"):
        provider.acquire_token()
