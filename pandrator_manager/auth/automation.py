"""Scoped, expiring automation enrollment for direct Manager recovery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..state import ManagerStore

MANAGER_AUTOMATION_SCOPES = frozenset(
    {
        "manager.read",
        "manager.runtime",
        "manager.mutate",
    }
)
TTY_CALLBACK = "urn:ietf:wg:oauth:2.0:oob"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _origin(value: object, *, https: bool = False) -> str:
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (https and parsed.scheme != "https")
    ):
        raise ValueError("An exact HTTP(S) origin is required.")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("The origin contains an invalid port.") from error
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (
        parsed.scheme == "https" and port == 443
    ) or (parsed.scheme == "http" and port == 80)
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _callback(value: object) -> str:
    candidate = str(value or "").strip()
    if candidate == TTY_CALLBACK:
        return candidate
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.path != "/callback"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "The callback must be an exact loopback /callback URI or the TTY callback."
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("The callback contains an invalid port.") from error
    if port is None or not 1 <= port <= 65535:
        raise ValueError("The loopback callback requires an explicit port.")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}/callback"


def _client_id(value: object) -> str:
    try:
        selected = uuid.UUID(str(value))
    except ValueError as error:
        raise ValueError("The automation client ID must be a UUID.") from error
    if selected.version != 4:
        raise ValueError("The automation client ID must be a random UUID.")
    return str(selected)


def _scopes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        supplied = value.split()
    else:
        supplied = list(value or ())
    selected = tuple(dict.fromkeys(str(item).strip() for item in supplied))
    if not selected:
        raise ValueError("At least one Manager automation scope is required.")
    unknown = set(selected) - MANAGER_AUTOMATION_SCOPES
    if unknown:
        raise ValueError(
            "Unknown Manager automation scope(s): "
            + ", ".join(sorted(unknown))
        )
    return selected


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    nonce: str
    client_id: str
    client_name: str
    subject: str
    application_instance_id: str
    canonical_application_origin: str
    canonical_recovery_origin: str
    scopes: tuple[str, ...]
    expires_in_seconds: int
    code_challenge: str
    redirect_uri: str
    state: str
    expires_at: float


class ManagerAutomationRateLimiter:
    """Bound authenticated recovery traffic by automation client."""

    def __init__(
        self,
        *,
        maximum_requests: int = 240,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_requests < 1:
            raise ValueError("maximum_requests must be positive.")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive.")
        self.maximum_requests = int(maximum_requests)
        self.window_seconds = float(window_seconds)
        self.clock = clock
        self._requests: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def consume(self, client_id: str) -> int | None:
        """Record one request or return the Retry-After value in seconds."""

        key = str(client_id or "").strip()
        if not key:
            return max(1, math.ceil(self.window_seconds))
        now = self.clock()
        cutoff = now - self.window_seconds
        with self._lock:
            timestamps = self._requests.setdefault(key, deque())
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self.maximum_requests:
                return max(
                    1,
                    math.ceil(
                        timestamps[0] + self.window_seconds - now
                    ),
                )
            timestamps.append(now)
        return None


class ManagerAutomationService:
    """Issue one-use PKCE grants and resolve audience-bound bearer tokens."""

    def __init__(
        self,
        store: ManagerStore,
        *,
        manager_instance_id: str,
        canonical_recovery_origin: str,
        enabled: bool,
        clock: Callable[[], float] = time.time,
        grant_ttl_seconds: int = 5 * 60,
        maximum_token_ttl_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        self.store = store
        self.manager_instance_id = str(manager_instance_id or "")
        self.canonical_recovery_origin = (
            _origin(canonical_recovery_origin, https=True)
            if enabled
            else ""
        )
        self.enabled = bool(enabled and self.manager_instance_id)
        self.clock = clock
        self.grant_ttl_seconds = max(
            60,
            min(int(grant_ttl_seconds), 10 * 60),
        )
        self.maximum_token_ttl_seconds = max(
            300,
            min(
                int(maximum_token_ttl_seconds),
                30 * 24 * 60 * 60,
            ),
        )
        self._pending: dict[str, PendingAuthorization] = {}
        self._lock = threading.RLock()

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "service": "pandrator-manager",
            "manager_instance_id": self.manager_instance_id,
            "manager_version": __import__(
                "pandrator_manager",
                fromlist=["__version__"],
            ).__version__,
            "canonical_recovery_origin": (
                self.canonical_recovery_origin or None
            ),
            "automation_enabled": self.enabled,
            "maximum_scopes": sorted(MANAGER_AUTOMATION_SCOPES),
            "maximum_credential_lifetime_seconds": (
                self.maximum_token_ttl_seconds
            ),
        }

    def _validated_request(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise PermissionError(
                "Manager automation requires an HTTPS recovery origin."
            )
        client_name = " ".join(
            str(payload.get("client_name") or "").split()
        )[:120]
        subject = " ".join(
            str(payload.get("subject") or "").split()
        )[:200]
        application_instance_id = str(
            payload.get("application_instance_id") or ""
        ).strip()[:120]
        code_challenge = str(
            payload.get("code_challenge") or ""
        ).strip()
        if not client_name or not subject or not application_instance_id:
            raise ValueError(
                "Client, subject, and application identities are required."
            )
        if (
            len(code_challenge) < 43
            or len(code_challenge) > 128
            or not all(
                character.isalnum() or character in "_-"
                for character in code_challenge
            )
            or payload.get("code_challenge_method") != "S256"
        ):
            raise ValueError("Manager automation requires S256 PKCE.")
        recovery_origin = _origin(
            payload.get("canonical_recovery_origin"),
            https=True,
        )
        if not hmac.compare_digest(
            recovery_origin,
            self.canonical_recovery_origin,
        ):
            raise ValueError(
                "The requested recovery origin does not match this Manager."
            )
        expires_in_seconds = max(
            300,
            min(
                int(payload.get("expires_in_seconds") or 0),
                self.maximum_token_ttl_seconds,
            ),
        )
        return {
            "client_id": _client_id(payload.get("client_id")),
            "client_name": client_name,
            "subject": subject,
            "application_instance_id": application_instance_id,
            "canonical_application_origin": _origin(
                payload.get("canonical_application_origin")
            ),
            "canonical_recovery_origin": recovery_origin,
            "scopes": _scopes(payload.get("requested_scopes")),
            "expires_in_seconds": expires_in_seconds,
            "code_challenge": code_challenge,
        }

    def begin_authorization(
        self,
        payload: Mapping[str, Any],
    ) -> PendingAuthorization:
        selected = self._validated_request(payload)
        redirect_uri = _callback(payload.get("redirect_uri"))
        state = str(payload.get("state") or "").strip()
        if len(state) < 16 or len(state) > 256:
            raise ValueError("A bounded OAuth state value is required.")
        now = self.clock()
        nonce = secrets.token_urlsafe(32)
        pending = PendingAuthorization(
            nonce=nonce,
            redirect_uri=redirect_uri,
            state=state,
            expires_at=now + self.grant_ttl_seconds,
            **selected,
        )
        with self._lock:
            self._pending = {
                key: item
                for key, item in self._pending.items()
                if item.expires_at > now
            }
            self._pending[nonce] = pending
        return pending

    def approve(self, nonce: str) -> tuple[PendingAuthorization, str]:
        now = self.clock()
        with self._lock:
            pending = self._pending.pop(str(nonce or ""), None)
        if pending is None or pending.expires_at <= now:
            raise ValueError(
                "The Manager automation authorization expired."
            )
        grant_code = self.create_grant(
            {
                "client_id": pending.client_id,
                "client_name": pending.client_name,
                "subject": pending.subject,
                "application_instance_id": (
                    pending.application_instance_id
                ),
                "canonical_application_origin": (
                    pending.canonical_application_origin
                ),
                "canonical_recovery_origin": (
                    pending.canonical_recovery_origin
                ),
                "requested_scopes": pending.scopes,
                "expires_in_seconds": pending.expires_in_seconds,
                "code_challenge": pending.code_challenge,
                "code_challenge_method": "S256",
            }
        )["grant_code"]
        return pending, grant_code

    def deny(self, nonce: str) -> PendingAuthorization | None:
        with self._lock:
            return self._pending.pop(str(nonce or ""), None)

    def create_grant(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        selected = self._validated_request(payload)
        now = self.clock()
        grant_code = f"mag_{secrets.token_urlsafe(32)}"
        expires_at = now + self.grant_ttl_seconds
        token_expires_at = now + selected["expires_in_seconds"]
        self.store.save_automation_grant(
            {
                "grant_digest": _digest(grant_code),
                "manager_instance_id": self.manager_instance_id,
                "created_at": now,
                "expires_at": expires_at,
                "token_expires_at": token_expires_at,
                **selected,
            }
        )
        return {
            "grant_code": grant_code,
            "expires_at": _iso(expires_at),
            "manager_instance_id": self.manager_instance_id,
            "approved_scopes": list(selected["scopes"]),
        }

    def exchange(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise PermissionError(
                "Manager automation requires an HTTPS recovery origin."
            )
        client_id = _client_id(payload.get("client_id"))
        manager_instance_id = str(
            payload.get("manager_instance_id") or ""
        )
        if not hmac.compare_digest(
            manager_instance_id,
            self.manager_instance_id,
        ):
            raise ValueError("The Manager instance identity changed.")
        grant_code = str(payload.get("grant_code") or "")
        verifier = str(payload.get("code_verifier") or "")
        if not 43 <= len(verifier) <= 128:
            raise ValueError("The PKCE verifier is invalid.")
        now = self.clock()
        grant = self.store.consume_automation_grant(
            _digest(grant_code),
            now=now,
        )
        if grant is None:
            raise ValueError(
                "The Manager automation grant is invalid or expired."
            )
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        if (
            not hmac.compare_digest(client_id, grant["client_id"])
            or not hmac.compare_digest(
                challenge,
                grant["code_challenge"],
            )
        ):
            raise ValueError(
                "The Manager automation grant could not be verified."
            )
        access_token = f"mrt_{secrets.token_urlsafe(32)}"
        token_id = str(uuid.uuid4())
        principal = {
            key: grant[key]
            for key in (
                "client_id",
                "client_name",
                "subject",
                "manager_instance_id",
                "application_instance_id",
                "canonical_application_origin",
                "canonical_recovery_origin",
                "scopes",
            )
        }
        self.store.save_automation_token(
            principal=principal,
            token_id=token_id,
            token_digest=_digest(access_token),
            now=now,
            expires_at=float(grant["token_expires_at"]),
        )
        projected = {
            "schema_version": "1",
            **principal,
            "scopes": list(principal["scopes"]),
            "created_at": _iso(now),
            "expires_at": _iso(float(grant["token_expires_at"])),
            "last_used_at": None,
            "revoked_at": None,
        }
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "audience": "pandrator-manager-recovery",
            "expires_at": projected["expires_at"],
            "principal": projected,
        }

    @staticmethod
    def project_principal(
        principal: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            **{
                key: principal.get(key)
                for key in (
                    "subject",
                    "client_id",
                    "client_name",
                    "manager_instance_id",
                    "application_instance_id",
                    "canonical_application_origin",
                    "canonical_recovery_origin",
                )
            },
            "scopes": list(principal.get("scopes") or ()),
            "expires_at": _iso(float(principal["expires_at"])),
            "created_at": _iso(float(principal["created_at"])),
            "last_used_at": (
                _iso(float(principal["last_used_at"]))
                if principal.get("last_used_at") is not None
                else None
            ),
            "revoked_at": (
                _iso(float(principal["revoked_at"]))
                if principal.get("revoked_at") is not None
                else None
            ),
        }

    def authenticate(
        self,
        token: str,
    ) -> dict[str, Any] | None:
        if not self.enabled or not str(token).startswith("mrt_"):
            return None
        principal = self.store.automation_principal(
            _digest(str(token)),
            now=self.clock(),
        )
        if principal is None:
            return None
        if (
            principal["manager_instance_id"]
            != self.manager_instance_id
            or principal["canonical_recovery_origin"]
            != self.canonical_recovery_origin
        ):
            return None
        return principal

    def clients(self) -> list[dict[str, Any]]:
        return [
            self.project_principal(item)
            for item in self.store.automation_clients()
        ]

    def revoke(self, client_id: str) -> bool:
        return self.store.revoke_automation_client(
            _client_id(client_id),
            now=self.clock(),
        )

    @staticmethod
    def callback_url(
        pending: PendingAuthorization,
        *,
        code: str | None = None,
        error: str | None = None,
    ) -> str:
        parameters = {"state": pending.state}
        if code is not None:
            parameters["code"] = code
        if error is not None:
            parameters["error"] = error
        if pending.redirect_uri == TTY_CALLBACK:
            return f"{TTY_CALLBACK}?{urlencode(parameters)}"
        return f"{pending.redirect_uri}?{urlencode(parameters)}"
