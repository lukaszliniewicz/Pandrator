"""Out-of-band native-app enrollment for a configured remote target."""

from __future__ import annotations

import getpass
import hmac
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from authlib.common.security import generate_token
from authlib.integrations.requests_client import OAuth2Session
from authlib.oauth2.rfc6749.errors import OAuth2Error
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from .clients import ApplicationClient
from .credentials import (
    CredentialReference,
    CredentialResolver,
    SecretValue,
)
from .errors import PandratorMcpError
from .network_policy import TargetMode, normalize_origin
from .targets import (
    MANAGER_RECOVERY_SCOPES,
    TargetBinding,
    TargetIdentityExpectation,
    TargetProfile,
    TargetRegistry,
    TargetStore,
)
from .transport import PinnedAddressAdapter

TTY_REDIRECT_URI = "urn:pandrator:oauth:2.0:oob"
MANAGER_TTY_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


@dataclass(frozen=True, slots=True)
class EnrollmentSummary:
    target: str
    client_id: str
    subject: str
    scopes: tuple[str, ...]
    target_instance_id: str
    canonical_origin: str
    expires_at: str | None
    credential_backend: str
    browser_flow: bool
    credential_rotated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "client_id": self.client_id,
            "subject": self.subject,
            "scopes": list(self.scopes),
            "target_instance_id": self.target_instance_id,
            "canonical_origin": self.canonical_origin,
            "expires_at": self.expires_at,
            "credential_backend": self.credential_backend,
            "browser_flow": self.browser_flow,
            "credential_rotated": self.credential_rotated,
        }


@dataclass(frozen=True, slots=True)
class ManagerEnrollmentSummary:
    target: str
    client_id: str
    subject: str
    scopes: tuple[str, ...]
    manager_instance_id: str
    recovery_origin: str
    expires_at: str
    credential_backend: str
    browser_flow: bool
    credential_rotated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "client_id": self.client_id,
            "subject": self.subject,
            "scopes": list(self.scopes),
            "manager_instance_id": self.manager_instance_id,
            "recovery_origin": self.recovery_origin,
            "expires_at": self.expires_at,
            "credential_backend": self.credential_backend,
            "browser_flow": self.browser_flow,
            "credential_rotated": self.credential_rotated,
        }


class _CallbackHandler(BaseHTTPRequestHandler):
    server: "_CallbackServer"

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        self.server.authorization_response = (
            f"http://127.0.0.1:{self.server.server_port}{self.path}"
        )
        body = (
            b"<!doctype html><title>Pandrator enrollment</title>"
            b"<h1>Enrollment received</h1>"
            b"<p>Return to the terminal. You may close this page.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.callback_event.set()

    def log_message(self, _format: str, *args: object) -> None:
        _ = args


class _CallbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.authorization_response: str | None = None
        self.callback_event = threading.Event()


def _validate_token_response(
    payload: dict[str, Any],
    *,
    client_id: str,
    requested_scopes: tuple[str, ...],
    configured_origin: str,
) -> tuple[str, str, str, str | None]:
    token_type = str(payload.get("token_type") or "")
    access_token = str(payload.get("access_token") or "")
    returned_client = str(payload.get("client_id") or "")
    subject = str(payload.get("subject") or "")
    target_instance_id = str(
        payload.get("target_instance_id") or ""
    )
    try:
        canonical_origin = normalize_origin(
            str(payload.get("canonical_origin") or "")
        )
    except PandratorMcpError as error:
        raise PandratorMcpError(
            "target_identity_mismatch",
            "The enrollment response contains an invalid target origin.",
        ) from error
    returned_scopes = tuple(
        sorted(
            value
            for value in str(payload.get("scope") or "").split()
            if value
        )
    )
    if (
        token_type.lower() != "bearer"
        or not access_token.startswith("pan_")
        or not hmac.compare_digest(returned_client, client_id)
        or not subject
        or not target_instance_id
        or canonical_origin != configured_origin
        or returned_scopes != tuple(sorted(requested_scopes))
    ):
        raise PandratorMcpError(
            "target_identity_mismatch",
            "The enrollment response did not match the requested target, "
            "client, or scopes.",
        )
    expires_in = payload.get("expires_in")
    expires_at = None
    if expires_in is not None:
        try:
            seconds = max(0, int(expires_in))
        except (TypeError, ValueError) as error:
            raise PandratorMcpError(
                "downstream_unavailable",
                "The enrollment response contains an invalid expiry.",
            ) from error
        expires_at = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + seconds,
            tz=UTC,
        ).isoformat()
    return access_token, subject, target_instance_id, expires_at


def enroll_target(
    *,
    profile: TargetProfile,
    binding: TargetBinding,
    store: TargetStore,
    credentials: CredentialResolver,
    scopes: tuple[str, ...],
    expires_in_days: int = 30,
    headless: bool = False,
    open_browser: bool = True,
    timeout_seconds: float = 180.0,
    credential_backend: str = "keyring",
    credential_reference: str | None = None,
) -> EnrollmentSummary:
    """Enroll one remote target without returning or printing its credential."""

    if profile.mode == TargetMode.LOCAL_MANAGED:
        raise ValueError(
            "Local managed targets use Manager bootstrap and do not require "
            "remote enrollment."
        )
    client_id = str(profile.automation_client_id or "")
    if not client_id:
        raise ValueError(
            "This profile has no automation client ID; re-add or update it "
            "before enrollment."
        )
    reference = CredentialReference(
        backend=credential_backend,
        reference=(
            credential_reference
            or f"target:{client_id}:application"
        ),
        audience="application",
    )
    if (
        profile.application_credential is not None
        and profile.application_credential != reference
    ):
        raise ValueError(
            "Changing an enrolled credential reference can orphan the old "
            "secret. Run target logout first or reuse the existing reference."
        )
    selected_scopes = tuple(dict.fromkeys(scopes))
    if not selected_scopes:
        raise ValueError("At least one application scope is required.")
    target = binding.resolve()
    health_client = ApplicationClient(binding, credentials)
    health = health_client.health()
    if health.get("service") != "pandrator":
        raise PandratorMcpError(
            "incompatible_downstream",
            "The configured endpoint is not a compatible Pandrator service.",
        )

    callback_server: _CallbackServer | None = None
    callback_thread: threading.Thread | None = None
    if headless:
        redirect_uri = TTY_REDIRECT_URI
    else:
        callback_server = _CallbackServer()
        redirect_uri = (
            f"http://127.0.0.1:{callback_server.server_port}/callback"
        )
        callback_thread = threading.Thread(
            target=callback_server.serve_forever,
            name="pandrator-mcp-oauth-callback",
            daemon=True,
        )
        callback_thread.start()

    state = generate_token(48)
    code_verifier = generate_token(64)
    oauth = OAuth2Session(
        client_id=client_id,
        token_endpoint_auth_method="none",
        scope=list(selected_scopes),
        state=state,
        redirect_uri=redirect_uri,
        code_challenge_method="S256",
    )
    oauth.trust_env = False
    adapter = PinnedAddressAdapter(
        target.application.origin,
        target.application.addresses,
    )
    oauth.mount(f"{target.application.origin}/", adapter)
    verify: bool | str = target.application.ca_bundle or True
    proxies = (
        {
            "http": target.application.proxy_origin,
            "https": target.application.proxy_origin,
        }
        if target.application.proxy_origin
        else {}
    )
    authorization_endpoint = (
        f"{target.application.origin}"
        "/api/v1/auth/automation/authorize"
    )
    token_endpoint = (
        f"{target.application.origin}/api/v1/auth/automation/token"
    )
    authorization_url, returned_state = oauth.create_authorization_url(
        authorization_endpoint,
        state=state,
        code_verifier=code_verifier,
        client_name=profile.automation_client_name,
        expires_in_days=max(1, min(int(expires_in_days), 90)),
    )
    if not hmac.compare_digest(returned_state, state):
        raise PandratorMcpError(
            "authentication_required",
            "The OAuth library returned an inconsistent state value.",
        )

    try:
        if open_browser:
            webbrowser.open(authorization_url)
        else:
            print(
                "Open this trusted Pandrator authorization URL in a browser:\n"
                f"{authorization_url}"
            )
        if headless:
            authorization_response = getpass.getpass(
                "Paste the complete one-use enrollment response: "
            ).strip()
        else:
            assert callback_server is not None
            if not callback_server.callback_event.wait(
                max(10.0, min(float(timeout_seconds), 600.0))
            ):
                raise PandratorMcpError(
                    "authentication_required",
                    "Timed out waiting for the trusted browser callback.",
                    retryable=True,
                )
            authorization_response = str(
                callback_server.authorization_response or ""
            )
        try:
            token = oauth.fetch_token(
                token_endpoint,
                authorization_response=authorization_response,
                code_verifier=code_verifier,
                grant_type="authorization_code",
                include_client_id=True,
                allow_redirects=False,
                timeout=(3, 20),
                verify=verify,
                proxies=proxies,
            )
        except (OAuth2Error, requests.RequestException) as error:
            raise PandratorMcpError(
                "authentication_required",
                "Pandrator rejected the one-use enrollment response.",
            ) from error
        raw, subject, instance_id, expires_at = _validate_token_response(
            dict(token),
            client_id=client_id,
            requested_scopes=selected_scopes,
            configured_origin=target.application.origin,
        )
        credentials.store(
            reference,
            SecretValue(raw),
            audience="application",
        )
        identity = TargetIdentityExpectation(
            application_instance_id=instance_id,
            canonical_application_origin=target.application.origin,
            manager_instance_id=None,
        )
        store.update_enrollment(
            profile.name,
            identity=identity,
            application_credential=reference,
            automation_client_id=client_id,
            requested_scopes=selected_scopes,
            enrolled_subject=subject,
            credential_expires_at=expires_at,
        )
        return EnrollmentSummary(
            target=profile.name,
            client_id=client_id,
            subject=subject,
            scopes=tuple(sorted(selected_scopes)),
            target_instance_id=instance_id,
            canonical_origin=target.application.origin,
            expires_at=expires_at,
            credential_backend=reference.backend,
            browser_flow=not headless,
            credential_rotated=(
                profile.application_credential is not None
            ),
        )
    finally:
        if callback_server is not None:
            callback_server.shutdown()
            callback_server.server_close()
        if callback_thread is not None:
            callback_thread.join(timeout=2)
        oauth.close()
        adapter.close()


def registry_for_store(store: TargetStore) -> TargetRegistry:
    """Build the standard policy registry used by the enrollment CLI."""

    from .clients import discover_local_application

    return TargetRegistry(
        store.load(missing_ok=False),
        local_discovery=discover_local_application,
    )


def enroll_manager_recovery(
    *,
    profile: TargetProfile,
    binding: TargetBinding,
    store: TargetStore,
    credentials: CredentialResolver,
    scopes: tuple[str, ...],
    expires_in_days: int = 7,
    headless: bool = False,
    open_browser: bool = True,
    timeout_seconds: float = 180.0,
    credential_backend: str = "keyring",
    credential_reference: str | None = None,
) -> ManagerEnrollmentSummary:
    """Enroll the separate HTTPS Manager-recovery audience."""

    client_id = str(
        profile.manager_automation_client_id or uuid.uuid4()
    )
    reference = CredentialReference(
        backend=credential_backend,
        reference=(
            credential_reference
            or f"target:{client_id}:manager-recovery"
        ),
        audience="manager_recovery",
    )
    if (
        profile.manager_recovery_credential is not None
        and profile.manager_recovery_credential != reference
    ):
        raise ValueError(
            "Changing an enrolled recovery credential reference can orphan "
            "the old secret. Run target logout --manager-recovery first or "
            "reuse the existing reference."
        )
    target = binding.resolve()
    endpoint = target.manager_recovery
    if endpoint is None:
        raise ValueError(
            "This target has no Manager recovery origin."
        )
    if endpoint.scheme != "https":
        raise PandratorMcpError(
            "network_policy_denied",
            "Manager recovery enrollment requires authenticated HTTPS.",
        )
    selected_scopes = tuple(dict.fromkeys(scopes))
    if (
        not selected_scopes
        or set(selected_scopes) - MANAGER_RECOVERY_SCOPES
    ):
        raise ValueError(
            "Manager recovery scopes must be selected from manager.read, "
            "manager.runtime, and manager.mutate."
        )
    application = ApplicationClient(binding, credentials)
    application_identity = application.identity()
    application_instance_id = str(
        application_identity.get("instance_id") or ""
    )
    if not application_instance_id:
        raise PandratorMcpError(
            "target_identity_mismatch",
            "The application identity is unavailable for recovery binding.",
        )
    verify: bool | str = endpoint.ca_bundle or True
    proxies = (
        {
            "http": endpoint.proxy_origin,
            "https": endpoint.proxy_origin,
        }
        if endpoint.proxy_origin
        else {}
    )
    session = requests.Session()
    session.trust_env = False
    adapter = PinnedAddressAdapter(
        endpoint.origin,
        endpoint.addresses,
    )
    session.mount(f"{endpoint.origin}/", adapter)
    callback_server: _CallbackServer | None = None
    callback_thread: threading.Thread | None = None
    if headless:
        redirect_uri = MANAGER_TTY_REDIRECT_URI
    else:
        callback_server = _CallbackServer()
        redirect_uri = (
            f"http://127.0.0.1:{callback_server.server_port}/callback"
        )
        callback_thread = threading.Thread(
            target=callback_server.serve_forever,
            name="pandrator-mcp-manager-callback",
            daemon=True,
        )
        callback_thread.start()
    state = generate_token(48)
    code_verifier = generate_token(64)
    challenge = create_s256_code_challenge(code_verifier)
    identity_url = (
        f"{endpoint.origin}/v1/automation/identity"
    )
    token_url = f"{endpoint.origin}/v1/automation/token"
    try:
        identity_status = 0
        try:
            identity_response = session.get(
                identity_url,
                headers={"Accept": "application/json"},
                timeout=(3, 20),
                allow_redirects=False,
                verify=verify,
                proxies=proxies,
            )
            identity_status = identity_response.status_code
            identity_payload = identity_response.json()
        except (requests.RequestException, ValueError) as error:
            raise PandratorMcpError(
                "manager_unavailable",
                "The Manager recovery identity endpoint is unavailable.",
                retryable=True,
            ) from error
        finally:
            if "identity_response" in locals():
                identity_response.close()
        if (
            identity_status != 200
            or not isinstance(identity_payload, dict)
            or identity_payload.get("service")
            != "pandrator-manager"
            or not identity_payload.get("automation_enabled")
            or identity_payload.get("canonical_recovery_origin")
            != endpoint.origin
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The configured recovery endpoint does not expose compatible Manager automation.",
            )
        manager_instance_id = str(
            identity_payload.get("manager_instance_id") or ""
        )
        if not manager_instance_id:
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Manager recovery identity omitted its stable instance ID.",
            )
        expected_manager = (
            profile.expected_identity.manager_instance_id
        )
        if expected_manager and not hmac.compare_digest(
            expected_manager,
            manager_instance_id,
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Manager recovery instance identity changed.",
            )
        query = urlencode(
            {
                "client_id": client_id,
                "client_name": (
                    profile.manager_automation_client_name
                ),
                "subject": profile.enrolled_subject or "owner",
                "application_instance_id": application_instance_id,
                "canonical_application_origin": (
                    target.application.origin
                ),
                "canonical_recovery_origin": endpoint.origin,
                "scope": " ".join(selected_scopes),
                "expires_in_seconds": (
                    max(1, min(int(expires_in_days), 30))
                    * 24
                    * 60
                    * 60
                ),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        authorization_url = (
            f"{endpoint.origin}/v1/automation/authorize?{query}"
        )
        if open_browser:
            webbrowser.open(authorization_url)
        else:
            print(
                "Open this trusted Manager recovery authorization URL "
                f"in an already authorized recovery browser:\n{authorization_url}"
            )
        if headless:
            authorization_response = getpass.getpass(
                "Paste the complete one-use Manager authorization response: "
            ).strip()
        else:
            assert callback_server is not None
            if not callback_server.callback_event.wait(
                max(10.0, min(float(timeout_seconds), 600.0))
            ):
                raise PandratorMcpError(
                    "authentication_required",
                    "Timed out waiting for the Manager recovery browser callback.",
                    retryable=True,
                )
            authorization_response = str(
                callback_server.authorization_response or ""
            )
        parsed = urlsplit(authorization_response)
        parameters = parse_qs(parsed.query)
        returned_state = str(
            (parameters.get("state") or [""])[0]
        )
        grant_code = str(
            (parameters.get("code") or [""])[0]
        )
        if (
            not hmac.compare_digest(returned_state, state)
            or not grant_code
            or parameters.get("error")
        ):
            raise PandratorMcpError(
                "authentication_required",
                "Manager recovery authorization was denied or invalid.",
            )
        token_status = 0
        try:
            token_response = session.post(
                token_url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "client_id": client_id,
                    "grant_code": grant_code,
                    "code_verifier": code_verifier,
                    "manager_instance_id": manager_instance_id,
                },
                timeout=(3, 20),
                allow_redirects=False,
                verify=verify,
                proxies=proxies,
            )
            token_status = token_response.status_code
            token_payload = token_response.json()
        except (requests.RequestException, ValueError) as error:
            raise PandratorMcpError(
                "authentication_required",
                "Manager rejected the one-use recovery enrollment response.",
            ) from error
        finally:
            if "token_response" in locals():
                token_response.close()
        if not isinstance(token_payload, dict):
            raise PandratorMcpError(
                "authentication_required",
                "Manager returned an invalid recovery enrollment response.",
            )
        principal = (
            token_payload.get("principal")
            if isinstance(token_payload.get("principal"), dict)
            else {}
        )
        returned_scopes = tuple(
            sorted(
                str(item)
                for item in principal.get("scopes", []) or []
            )
        )
        access_token = str(
            token_payload.get("access_token") or ""
        )
        if (
            token_status != 200
            or token_payload.get("audience")
            != "pandrator-manager-recovery"
            or str(token_payload.get("token_type") or "").lower()
            != "bearer"
            or not access_token.startswith("mrt_")
            or principal.get("client_id") != client_id
            or principal.get("manager_instance_id")
            != manager_instance_id
            or principal.get("application_instance_id")
            != application_instance_id
            or principal.get("canonical_recovery_origin")
            != endpoint.origin
            or principal.get("canonical_application_origin")
            != target.application.origin
            or returned_scopes
            != tuple(sorted(selected_scopes))
        ):
            raise PandratorMcpError(
                "target_identity_mismatch",
                "The Manager recovery enrollment response did not match the reviewed identities and scopes.",
            )
        credentials.store(
            reference,
            SecretValue(access_token),
            audience="manager_recovery",
        )
        identity = TargetIdentityExpectation(
            application_instance_id=application_instance_id,
            canonical_application_origin=target.application.origin,
            manager_instance_id=manager_instance_id,
        )
        expires_at = str(token_payload.get("expires_at") or "")
        store.update_manager_enrollment(
            profile.name,
            identity=identity,
            manager_recovery_credential=reference,
            automation_client_id=client_id,
            requested_scopes=selected_scopes,
            enrolled_subject=str(
                principal.get("subject") or "owner"
            ),
            credential_expires_at=expires_at or None,
        )
        return ManagerEnrollmentSummary(
            target=profile.name,
            client_id=client_id,
            subject=str(principal.get("subject") or "owner"),
            scopes=tuple(sorted(selected_scopes)),
            manager_instance_id=manager_instance_id,
            recovery_origin=endpoint.origin,
            expires_at=expires_at,
            credential_backend=reference.backend,
            browser_flow=not headless,
            credential_rotated=(
                profile.manager_recovery_credential is not None
            ),
        )
    finally:
        if callback_server is not None:
            callback_server.shutdown()
            callback_server.server_close()
        if callback_thread is not None:
            callback_thread.join(timeout=2)
        session.close()
        adapter.close()
