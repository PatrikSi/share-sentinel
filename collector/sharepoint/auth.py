from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

try:
    import msal
except ImportError:  # Keep --help and token-import mode diagnosable.
    msal = None


GRAPH_APPLICATION_ID = "00000003-0000-0000-c000-000000000000"
MAX_TOKEN_BYTES = 1024 * 1024
MAX_JWT_SEGMENT_BYTES = 128 * 1024
MAX_CERTIFICATE_BYTES = 1024 * 1024
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$")
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----",
    re.DOTALL,
)
PUBLIC_CERTIFICATE_PATTERN = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)
THUMBPRINT_PATTERN = re.compile(r"^[A-Fa-f0-9]{40}$")


@dataclass(frozen=True)
class GraphCloudProfile:
    name: str
    authority_host: str
    graph_resource: str
    sharepoint_hostname_suffixes: tuple[str, ...]

    @property
    def graph_base_url(self) -> str:
        return f"{self.graph_resource}/v1.0/"

    @property
    def app_scope(self) -> tuple[str, ...]:
        return (f"{self.graph_resource}/.default",)

    @property
    def delegated_scopes(self) -> tuple[str, ...]:
        return (
            f"{self.graph_resource}/Sites.Read.All",
            f"{self.graph_resource}/Files.Read.All",
        )

    @property
    def audiences(self) -> frozenset[str]:
        return frozenset({GRAPH_APPLICATION_ID, self.graph_resource, f"{self.graph_resource}/"})

    def allows_sharepoint_hostname(self, hostname: str) -> bool:
        normalized = str(hostname or "").strip().rstrip(".").casefold()
        return any(
            normalized.endswith(f".{suffix}") and normalized != suffix for suffix in self.sharepoint_hostname_suffixes
        )


GRAPH_CLOUD_PROFILES = {
    "global": GraphCloudProfile(
        name="global",
        authority_host="https://login.microsoftonline.com",
        graph_resource="https://graph.microsoft.com",
        sharepoint_hostname_suffixes=("sharepoint.com",),
    ),
    "gcc-high": GraphCloudProfile(
        name="gcc-high",
        authority_host="https://login.microsoftonline.us",
        graph_resource="https://graph.microsoft.us",
        sharepoint_hostname_suffixes=("sharepoint.us",),
    ),
    "dod": GraphCloudProfile(
        name="dod",
        authority_host="https://login.microsoftonline.us",
        graph_resource="https://dod-graph.microsoft.us",
        sharepoint_hostname_suffixes=("sharepoint-mil.us",),
    ),
    "china": GraphCloudProfile(
        name="china",
        authority_host="https://login.chinacloudapi.cn",
        graph_resource="https://microsoftgraph.chinacloudapi.cn",
        sharepoint_hostname_suffixes=("sharepoint.cn",),
    ),
}
GRAPH_APP_SCOPE = GRAPH_CLOUD_PROFILES["global"].app_scope
GRAPH_DELEGATED_SCOPES = GRAPH_CLOUD_PROFILES["global"].delegated_scopes
GRAPH_AUDIENCES = GRAPH_CLOUD_PROFILES["global"].audiences


def resolve_graph_cloud(value: str | GraphCloudProfile | None) -> GraphCloudProfile:
    if isinstance(value, GraphCloudProfile):
        return value
    normalized = str(value or "global").strip().casefold()
    try:
        return GRAPH_CLOUD_PROFILES[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(GRAPH_CLOUD_PROFILES))
        raise TokenAcquisitionError(
            f"unsupported Microsoft Graph cloud {normalized!r}; choose one of: {choices}"
        ) from exc


class TokenAcquisitionError(RuntimeError):
    """A safe, operator-facing authentication failure."""


@dataclass(frozen=True, repr=False)
class GraphTokenContext:
    access_token: str
    auth_mode: str
    auth_type: str
    tenant_id: str
    client_id: str | None
    user_id: str | None
    user_principal_name: str | None
    scopes: tuple[str, ...]
    roles: tuple[str, ...]
    expires_at: datetime | None
    jwt_inspection: str = "unverified_metadata_only"
    cloud: str = "global"

    def __repr__(self) -> str:
        return (
            "GraphTokenContext(access_token=<redacted>, "
            f"auth_mode={self.auth_mode!r}, auth_type={self.auth_type!r}, "
            f"tenant_id={self.tenant_id!r}, cloud={self.cloud!r})"
        )

    @property
    def assessed_identity(self) -> str | None:
        return self.user_principal_name or self.user_id

    def is_expiring(self, *, leeway_seconds: int = 120) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at.timestamp() <= datetime.now(tz=UTC).timestamp() + leeway_seconds

    def public_metadata(self) -> dict[str, object]:
        return {
            "auth_mode": self.auth_mode,
            "auth_type": self.auth_type,
            "cloud": self.cloud,
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "user_id": self.user_id,
            "user_principal_name": self.user_principal_name,
            "scopes": list(self.scopes),
            "roles": list(self.roles),
            "token_expiration": self.expires_at.isoformat() if self.expires_at else None,
            "jwt_inspection": self.jwt_inspection,
        }


class GraphAuthProvider(Protocol):
    supports_refresh: bool

    def acquire_token(self) -> GraphTokenContext: ...


def _decode_jwt_segment(segment: str) -> dict[str, object]:
    if not segment or len(segment) > MAX_JWT_SEGMENT_BYTES * 2:
        raise TokenAcquisitionError("access token JWT payload is empty or exceeds the safety limit")
    try:
        padding = "=" * (-len(segment) % 4)
        payload = base64.urlsafe_b64decode((segment + padding).encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise TokenAcquisitionError("access token does not contain valid base64url JWT metadata") from exc
    if len(payload) > MAX_JWT_SEGMENT_BYTES:
        raise TokenAcquisitionError("access token JWT metadata exceeds the safety limit")
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenAcquisitionError("access token does not contain valid JSON JWT metadata") from exc
    if not isinstance(claims, dict):
        raise TokenAcquisitionError("access token JWT metadata must be a JSON object")
    return claims


def inspect_access_token(
    token: str,
    *,
    auth_mode: str,
    expected_auth_type: str | None = None,
    cloud: str | GraphCloudProfile | None = None,
) -> GraphTokenContext:
    """Inspect useful JWT metadata locally; this is not signature validation."""

    cloud_profile = resolve_graph_cloud(cloud)
    normalized = str(token or "").strip()
    if not normalized:
        raise TokenAcquisitionError("no Microsoft Graph access token was provided")
    try:
        encoded_token = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TokenAcquisitionError("Microsoft Graph access token contains invalid Unicode") from exc
    if len(encoded_token) > MAX_TOKEN_BYTES:
        raise TokenAcquisitionError("Microsoft Graph access token exceeds the safety limit")
    if any(character.isspace() for character in normalized):
        raise TokenAcquisitionError("Microsoft Graph access token contains unexpected whitespace")
    segments = normalized.split(".")
    if len(segments) != 3:
        raise TokenAcquisitionError("Microsoft Graph access token is not a three-part JWT")

    claims = _decode_jwt_segment(segments[1])
    audience = str(claims.get("aud") or "").strip()
    if audience not in cloud_profile.audiences:
        raise TokenAcquisitionError(
            f"access token audience is not Microsoft Graph for the selected {cloud_profile.name} cloud; "
            "acquire a token from that cloud and retry"
        )

    try:
        expires_at = datetime.fromtimestamp(float(claims["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise TokenAcquisitionError("access token is missing a valid expiration timestamp") from exc
    if expires_at <= datetime.now(tz=UTC):
        raise TokenAcquisitionError("Microsoft Graph access token has expired")

    raw_scopes = claims.get("scp")
    scopes = tuple(sorted({value for value in str(raw_scopes or "").split() if value}))
    raw_roles = claims.get("roles")
    roles = (
        tuple(sorted({str(value) for value in raw_roles if str(value).strip()})) if isinstance(raw_roles, list) else ()
    )
    if scopes:
        auth_type = "delegated"
    elif roles:
        auth_type = "application"
    else:
        raise TokenAcquisitionError("access token has neither delegated scopes (`scp`) nor application roles (`roles`)")
    if expected_auth_type and auth_type != expected_auth_type:
        raise TokenAcquisitionError(
            f"authentication returned a {auth_type} token where {expected_auth_type} was required"
        )

    tenant_id = str(claims.get("tid") or "").strip()
    if not tenant_id:
        raise TokenAcquisitionError("access token is missing tenant ID metadata (`tid`)")
    client_id = str(claims.get("azp") or claims.get("appid") or "").strip() or None
    user_id = str(claims.get("oid") or claims.get("sub") or "").strip() or None
    upn = str(claims.get("preferred_username") or claims.get("upn") or "").strip() or None

    return GraphTokenContext(
        access_token=normalized,
        auth_mode=auth_mode,
        auth_type=auth_type,
        tenant_id=tenant_id,
        client_id=client_id,
        user_id=user_id if auth_type == "delegated" else None,
        user_principal_name=upn if auth_type == "delegated" else None,
        scopes=scopes,
        roles=roles,
        expires_at=expires_at,
        cloud=cloud_profile.name,
    )


def _validated_tenant(tenant_id: str | None, *, delegated: bool) -> str:
    normalized = str(tenant_id or ("organizations" if delegated else "")).strip()
    if not normalized:
        raise TokenAcquisitionError("tenant ID is required (use --tenant-id or SHARE_SENTINEL_GRAPH_TENANT_ID)")
    if not TENANT_PATTERN.fullmatch(normalized):
        raise TokenAcquisitionError("tenant ID contains unsupported characters")
    if not delegated and normalized in {"common", "organizations", "consumers"}:
        raise TokenAcquisitionError("app authentication requires a specific Entra tenant ID")
    return normalized


def _validated_client_id(client_id: str | None) -> str:
    normalized = str(client_id or "").strip()
    if not normalized:
        raise TokenAcquisitionError("client ID is required (use --client-id or SHARE_SENTINEL_GRAPH_CLIENT_ID)")
    if len(normalized) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
        raise TokenAcquisitionError("client ID contains unsupported characters")
    return normalized


def _require_msal() -> None:
    if msal is None:
        raise TokenAcquisitionError("MSAL is required for this authentication mode; install collector requirements")


def _safe_msal_failure(result: object) -> TokenAcquisitionError:
    if not isinstance(result, dict):
        return TokenAcquisitionError("Microsoft identity authentication returned an invalid response")
    error = str(result.get("error") or "authentication_failed")[:128]
    correlation = str(result.get("correlation_id") or "").strip()[:128]
    suffix = f" (correlation ID: {correlation})" if correlation else ""
    return TokenAcquisitionError(f"Microsoft identity authentication failed: {error}{suffix}")


class AppCredentialAuthProvider:
    supports_refresh = True

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        cloud: str | GraphCloudProfile | None = None,
    ) -> None:
        self.tenant_id = _validated_tenant(tenant_id, delegated=False)
        self.client_id = _validated_client_id(client_id)
        self.cloud_profile = resolve_graph_cloud(cloud)
        self._client_secret = str(client_secret or "")
        if not self._client_secret:
            raise TokenAcquisitionError("client secret is required in SHARE_SENTINEL_GRAPH_CLIENT_SECRET")
        self._application = None
        self._lock = threading.Lock()

    def acquire_token(self) -> GraphTokenContext:
        _require_msal()
        try:
            with self._lock:
                if self._application is None:
                    self._application = msal.ConfidentialClientApplication(
                        self.client_id,
                        authority=f"{self.cloud_profile.authority_host}/{self.tenant_id}",
                        client_credential=self._client_secret,
                        enable_pii_log=False,
                    )
                result = self._application.acquire_token_for_client(scopes=list(self.cloud_profile.app_scope))
        except Exception as exc:
            raise TokenAcquisitionError("Microsoft app authentication could not be completed") from exc
        if not isinstance(result, dict) or "access_token" not in result:
            raise _safe_msal_failure(result)
        return inspect_access_token(
            str(result["access_token"]),
            auth_mode="app",
            expected_auth_type="application",
            cloud=self.cloud_profile,
        )


def certificate_credential_from_file(
    path: str,
    *,
    thumbprint: str | None = None,
    passphrase: str | None = None,
    send_certificate_chain: bool = False,
) -> dict[str, object]:
    """Read a protected PEM key/certificate bundle into an MSAL credential."""

    certificate_path = Path(path).expanduser()
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(certificate_path, flags)
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise TokenAcquisitionError("certificate credential file must be a regular file")
            if file_stat.st_size > MAX_CERTIFICATE_BYTES:
                raise TokenAcquisitionError("certificate credential file exceeds the safety limit")
            if os.name != "nt":
                if file_stat.st_uid != os.geteuid():
                    raise TokenAcquisitionError("certificate credential file must be owned by the current user")
                if stat.S_IMODE(file_stat.st_mode) & 0o077:
                    raise TokenAcquisitionError(
                        "certificate credential file permissions are too broad; restrict it to the current user (chmod 600)"
                    )
            with os.fdopen(descriptor, "rb", closefd=False) as certificate_fp:
                raw = certificate_fp.read(MAX_CERTIFICATE_BYTES + 1)
            if len(raw) > MAX_CERTIFICATE_BYTES:
                raise TokenAcquisitionError("certificate credential file exceeds the safety limit")
        finally:
            os.close(descriptor)
    except TokenAcquisitionError:
        raise
    except OSError as exc:
        raise TokenAcquisitionError("unable to read the configured certificate credential file") from exc

    try:
        pem = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TokenAcquisitionError("certificate credential file must be ASCII PEM") from exc
    key_match = PRIVATE_KEY_PATTERN.search(pem)
    certificates = PUBLIC_CERTIFICATE_PATTERN.findall(pem)
    if key_match is None:
        raise TokenAcquisitionError("certificate credential file does not contain a PEM private key")

    normalized_thumbprint = str(thumbprint or "").replace(":", "").strip()
    if normalized_thumbprint and not THUMBPRINT_PATTERN.fullmatch(normalized_thumbprint):
        raise TokenAcquisitionError("certificate thumbprint must be a 40-character SHA-1 hexadecimal value")
    if not normalized_thumbprint and not certificates:
        raise TokenAcquisitionError(
            "certificate credential requires a public certificate in the PEM bundle when no thumbprint is supplied"
        )
    if send_certificate_chain and not certificates:
        raise TokenAcquisitionError("--certificate-send-x5c requires a public certificate in the PEM bundle")

    credential: dict[str, object] = {"private_key": key_match.group(0)}
    if normalized_thumbprint:
        credential["thumbprint"] = normalized_thumbprint.upper()
    if passphrase:
        credential["passphrase"] = passphrase
    # MSAL >= 1.35 calculates a SHA-256 thumbprint when a public certificate
    # is present without a legacy SHA-1 thumbprint. Only send x5c when the
    # operator explicitly opts into Subject Name/Issuer authentication.
    if certificates and (not normalized_thumbprint or send_certificate_chain):
        credential["public_certificate"] = "\n".join(certificates)
    return credential


class CertificateCredentialAuthProvider:
    supports_refresh = True

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_credential: dict[str, object],
        cloud: str | GraphCloudProfile | None = None,
    ) -> None:
        self.tenant_id = _validated_tenant(tenant_id, delegated=False)
        self.client_id = _validated_client_id(client_id)
        self.cloud_profile = resolve_graph_cloud(cloud)
        if not isinstance(client_credential, dict) or "private_key" not in client_credential:
            raise TokenAcquisitionError("a valid certificate client credential is required")
        self._client_credential = dict(client_credential)
        self._application = None
        self._lock = threading.Lock()

    def acquire_token(self) -> GraphTokenContext:
        _require_msal()
        try:
            with self._lock:
                if self._application is None:
                    self._application = msal.ConfidentialClientApplication(
                        self.client_id,
                        authority=f"{self.cloud_profile.authority_host}/{self.tenant_id}",
                        client_credential=self._client_credential,
                        enable_pii_log=False,
                    )
                result = self._application.acquire_token_for_client(scopes=list(self.cloud_profile.app_scope))
        except Exception as exc:
            raise TokenAcquisitionError("Microsoft certificate authentication could not be completed") from exc
        if not isinstance(result, dict) or "access_token" not in result:
            raise _safe_msal_failure(result)
        return inspect_access_token(
            str(result["access_token"]),
            auth_mode="app_certificate",
            expected_auth_type="application",
            cloud=self.cloud_profile,
        )


class PublicClientAuthProvider:
    supports_refresh = True

    def __init__(
        self,
        *,
        auth_mode: str,
        tenant_id: str | None,
        client_id: str,
        login_hint: str | None = None,
        scopes: tuple[str, ...] | None = None,
        cloud: str | GraphCloudProfile | None = None,
    ) -> None:
        if auth_mode not in {"interactive", "wam", "iwa"}:
            raise TokenAcquisitionError(f"unsupported public-client authentication mode: {auth_mode}")
        if auth_mode in {"wam", "iwa"} and sys.platform != "win32":
            label = "WAM" if auth_mode == "wam" else "Integrated Windows Authentication"
            raise TokenAcquisitionError(f"{label} is only supported on Windows")
        self.auth_mode = auth_mode
        self.tenant_id = _validated_tenant(tenant_id, delegated=True)
        self.client_id = _validated_client_id(client_id)
        self.cloud_profile = resolve_graph_cloud(cloud)
        self.login_hint = str(login_hint or "").strip() or None
        self.scopes = tuple(dict.fromkeys(scopes or self.cloud_profile.delegated_scopes))
        self._application = None
        self._lock = threading.Lock()

    def _build_application(self):
        _require_msal()
        kwargs: dict[str, object] = {
            "authority": f"{self.cloud_profile.authority_host}/{self.tenant_id}",
            "enable_pii_log": False,
        }
        if self.auth_mode == "wam":
            kwargs["enable_broker_on_windows"] = True
        try:
            return msal.PublicClientApplication(self.client_id, **kwargs)
        except (ImportError, ValueError, RuntimeError) as exc:
            if self.auth_mode == "wam":
                raise TokenAcquisitionError(
                    "WAM initialization failed; install the MSAL broker extra and ensure the "
                    "public-client redirect URI is registered"
                ) from exc
            raise TokenAcquisitionError("MSAL public-client initialization failed") from exc

    def acquire_token(self) -> GraphTokenContext:
        try:
            with self._lock:
                if self._application is None:
                    self._application = self._build_application()
                app = self._application
                result = None
                accounts = app.get_accounts(username=self.login_hint)
                if accounts:
                    result = app.acquire_token_silent(list(self.scopes), account=accounts[0])
                if not result:
                    if self.auth_mode == "iwa":
                        if not self.login_hint:
                            raise TokenAcquisitionError("IWA requires --login-hint with the target user's UPN")
                        result = app.acquire_token_by_integrated_windows_authentication(
                            username=self.login_hint,
                            scopes=list(self.scopes),
                        )
                    else:
                        interactive_kwargs: dict[str, object] = {
                            "login_hint": self.login_hint,
                            "prompt": "select_account",
                        }
                        if self.auth_mode == "wam":
                            interactive_kwargs["parent_window_handle"] = app.CONSOLE_WINDOW_HANDLE
                        result = app.acquire_token_interactive(
                            scopes=list(self.scopes),
                            **interactive_kwargs,
                        )
        except TokenAcquisitionError:
            raise
        except Exception as exc:
            raise TokenAcquisitionError(f"Microsoft {self.auth_mode} authentication could not be completed") from exc
        if not isinstance(result, dict) or "access_token" not in result:
            raise _safe_msal_failure(result)
        return inspect_access_token(
            str(result["access_token"]),
            auth_mode=self.auth_mode,
            expected_auth_type="delegated",
            cloud=self.cloud_profile,
        )


class ExistingTokenAuthProvider:
    supports_refresh = False

    def __init__(
        self,
        reader: Callable[[], str],
        *,
        opaque_auth_type: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        assessed_identity: str | None = None,
        cloud: str | GraphCloudProfile | None = None,
    ) -> None:
        self._reader = reader
        self.opaque_auth_type = opaque_auth_type
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.assessed_identity = str(assessed_identity or "").strip() or None
        self.cloud_profile = resolve_graph_cloud(cloud)

    def acquire_token(self) -> GraphTokenContext:
        raw_token = self._reader()
        normalized = str(raw_token or "").strip()
        if normalized.count(".") == 2:
            return inspect_access_token(
                normalized,
                auth_mode="token",
                expected_auth_type=self.opaque_auth_type,
                cloud=self.cloud_profile,
            )
        if self.opaque_auth_type not in {"delegated", "application"}:
            raise TokenAcquisitionError("opaque access tokens require --token-type delegated|application")
        try:
            encoded = normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TokenAcquisitionError("Microsoft Graph access token contains invalid Unicode") from exc
        if not normalized:
            raise TokenAcquisitionError("no Microsoft Graph access token was provided")
        if len(encoded) > MAX_TOKEN_BYTES:
            raise TokenAcquisitionError("Microsoft Graph access token exceeds the safety limit")
        if any(character.isspace() for character in normalized):
            raise TokenAcquisitionError("Microsoft Graph access token contains unexpected whitespace")

        tenant_id = _validated_tenant(self.tenant_id, delegated=False)
        if self.opaque_auth_type == "application":
            client_id = _validated_client_id(self.client_id)
            user_principal_name = None
        else:
            client_id = _validated_client_id(self.client_id) if str(self.client_id or "").strip() else None
            if not self.assessed_identity:
                raise TokenAcquisitionError(
                    "opaque delegated tokens require --assessed-identity for safe scan attribution"
                )
            user_principal_name = self.assessed_identity
        return GraphTokenContext(
            access_token=normalized,
            auth_mode="token",
            auth_type=self.opaque_auth_type,
            tenant_id=tenant_id,
            client_id=client_id,
            user_id=None,
            user_principal_name=user_principal_name,
            scopes=(),
            roles=(),
            expires_at=None,
            jwt_inspection="opaque_token_context_supplied_by_operator",
            cloud=self.cloud_profile.name,
        )


def token_reader_from_env(name: str) -> Callable[[], str]:
    if not ENV_NAME_PATTERN.fullmatch(name):
        raise TokenAcquisitionError("token environment variable name is invalid")

    def _read() -> str:
        token = os.environ.get(name, "")
        if not token:
            raise TokenAcquisitionError(f"token environment variable {name} is empty or unset")
        return token

    return _read


def token_reader_from_file(path: str) -> Callable[[], str]:
    token_path = Path(path).expanduser()

    def _read() -> str:
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(token_path, flags)
            try:
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise TokenAcquisitionError("token file must be a regular file")
                if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
                    raise TokenAcquisitionError(
                        "token file permissions are too broad; restrict the file to the current user (chmod 600)"
                    )
                if file_stat.st_size > MAX_TOKEN_BYTES:
                    raise TokenAcquisitionError("token file exceeds the safety limit")
                with os.fdopen(descriptor, "rb", closefd=False) as token_fp:
                    raw = token_fp.read(MAX_TOKEN_BYTES + 1)
                if len(raw) > MAX_TOKEN_BYTES:
                    raise TokenAcquisitionError("token file exceeds the safety limit")
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise TokenAcquisitionError("token file is not valid UTF-8") from exc
            finally:
                os.close(descriptor)
        except TokenAcquisitionError:
            raise
        except OSError as exc:
            raise TokenAcquisitionError("unable to read the configured token file") from exc

    return _read


def token_reader_from_stdin(stream=None) -> Callable[[], str]:
    input_stream = stream if stream is not None else sys.stdin
    cached: list[str] = []

    def _read() -> str:
        if cached:
            return cached[0]
        value = input_stream.read(MAX_TOKEN_BYTES + 1)
        try:
            encoded_value = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TokenAcquisitionError("token read from stdin contains invalid Unicode") from exc
        if len(encoded_value) > MAX_TOKEN_BYTES:
            raise TokenAcquisitionError("token read from stdin exceeds the safety limit")
        cached.append(value)
        return value

    return _read
