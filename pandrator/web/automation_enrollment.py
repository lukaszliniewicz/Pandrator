"""Native-app enrollment with exact redirects, PKCE, and bound credentials."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult

from .accounts.actor_authorization import require_actor_in_session
from .accounts.auth_state import account_state_in_session, initial_user_id
from .accounts.credential_resources import (
    CredentialOwner,
    CredentialResources,
    CredentialResourcesDenied,
    default_personal_resources,
    record_resources,
    replace_client_resources,
    require_delegation_resources,
    require_resources_within,
    resources_for_owner,
    validate_resources_for_user,
)
from .accounts.execution_actor import UserActor, actor_from_principal
from .accounts.installation_mode import installation_mode_in_session
from .auth import (
    ALL_SCOPES,
    AuthService,
    Principal,
    normalize_scopes,
)
from .database import Database
from .models import (
    ApiToken,
    AutomationClient,
    AutomationEnrollmentGrant,
    utcnow,
)

TTY_REDIRECT_URI = "urn:pandrator:oauth:2.0:oob"
AUTOMATION_SCOPES = ALL_SCOPES
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class EnrollmentError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(slots=True)
class EnrollmentToken:
    record: ApiToken
    raw_token: str
    client: AutomationClient


class AutomationEnrollmentService:
    """Own automation-client consent, one-use codes, and token rotation."""

    def __init__(self, database: Database, auth: AuthService) -> None:
        self.database = database
        self.auth = auth

    @staticmethod
    def validate_client_id(value: object) -> str:
        try:
            parsed = uuid.UUID(str(value))
        except ValueError as error:
            raise EnrollmentError(
                "invalid_client",
                "The automation client ID must be a UUID.",
                400,
            ) from error
        if parsed.version != 4:
            raise EnrollmentError(
                "invalid_client",
                "The automation client ID must be a random UUID.",
                400,
            )
        return str(parsed)

    @staticmethod
    def validate_redirect_uri(value: object) -> str:
        candidate = str(value or "").strip()
        if candidate == TTY_REDIRECT_URI:
            return candidate
        parsed = urlsplit(candidate)
        try:
            port = parsed.port
        except ValueError as error:
            raise EnrollmentError(
                "invalid_redirect_uri",
                "The loopback redirect URI contains an invalid port.",
                400,
            ) from error
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None
            or not 1 <= port <= 65535
            or parsed.username
            or parsed.password
            or parsed.path != "/callback"
            or parsed.query
            or parsed.fragment
        ):
            raise EnrollmentError(
                "invalid_redirect_uri",
                "Automation redirects must be an exact loopback callback URI.",
                400,
            )
        return candidate

    @staticmethod
    def validate_scopes(value: object) -> frozenset[str]:
        try:
            scopes = normalize_scopes(value)
        except ValueError as error:
            raise EnrollmentError(
                "invalid_scope",
                str(error),
                400,
            ) from error
        return scopes

    @staticmethod
    def validate_code_challenge(
        value: object,
        method: object,
    ) -> str:
        challenge = str(value or "").strip()
        if str(method or "") != "S256" or not _CHALLENGE.fullmatch(
            challenge
        ):
            raise EnrollmentError(
                "invalid_request",
                "A valid S256 PKCE code challenge is required.",
                400,
            )
        return challenge

    def _register_in_session(
        self,
        session,
        *,
        client_id: str,
        name: str,
        redirect_uris: list[str],
        scopes: frozenset[str],
        target_instance_id: str,
        canonical_origin: str,
        created_by: str,
        user_id: str,
        auth_revision: int,
        resource_grants: CredentialResources | None = None,
    ) -> AutomationClient:
        selected_resources = (
            default_personal_resources(session, user_id)
            if resource_grants is None
            else resource_grants
        )
        try:
            selected_resources = validate_resources_for_user(session, user_id, selected_resources)
        except CredentialResourcesDenied as error:
            raise EnrollmentError(
                "access_denied", "The requested credential resources are not available.", 403
            ) from error
        client = session.get(AutomationClient, client_id)
        if client is None:
            client = AutomationClient(
                id=client_id,
                name=str(name).strip()[:160],
                subject=f"automation:{client_id}",
                redirect_uris_json=redirect_uris,
                allowed_scopes_json=sorted(scopes),
                target_instance_id=target_instance_id,
                canonical_origin=canonical_origin,
                created_by=created_by[:200],
                user_id=user_id,
                user_auth_revision=auth_revision,
            )
            session.add(client)
            session.flush()
            record_resources(session, CredentialOwner.CLIENT, client.id, selected_resources)
            return client
        if client.revoked_at is not None:
            raise EnrollmentError(
                "invalid_client",
                "This automation client was revoked and must use a new client ID.",
                401,
            )
        if client.user_id != user_id or client.user_auth_revision != auth_revision:
            raise EnrollmentError(
                "invalid_client", "The automation client's account has changed.", 403
            )
        if (
            client.target_instance_id != target_instance_id
            or client.canonical_origin != canonical_origin
        ):
            raise EnrollmentError(
                "target_identity_mismatch",
                "The automation client is bound to a different Pandrator target.",
                409,
            )
        old_resources = resources_for_owner(session, CredentialOwner.CLIENT, client.id)
        resources_changed = old_resources != selected_resources
        if frozenset(client.allowed_scopes_json or []) != scopes or resources_changed:
            now = utcnow()
            session.execute(
                update(ApiToken)
                .where(ApiToken.client_id == client.id, ApiToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            session.execute(
                update(AutomationEnrollmentGrant)
                .where(
                    AutomationEnrollmentGrant.client_id == client.id,
                    AutomationEnrollmentGrant.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
        if resources_changed:
            replace_client_resources(session, client.id, selected_resources)
        client.name = str(name).strip()[:160]
        client.redirect_uris_json = list(
            dict.fromkeys(
                [
                    *list(client.redirect_uris_json or []),
                    *redirect_uris,
                ]
            )
        )[-10:]
        client.allowed_scopes_json = sorted(scopes)
        session.flush()
        return client

    def register(
        self,
        *,
        client_id: object,
        name: str,
        redirect_uris: list[str],
        scopes: object,
        target_instance_id: str,
        canonical_origin: str,
        created_by: str,
        user_id: str | None = None,
        auth_revision: int | None = None,
        resource_grants: CredentialResources | None = None,
        approval_actor: UserActor | None = None,
    ) -> AutomationClient:
        validated_id = self.validate_client_id(client_id)
        validated_redirects = [
            self.validate_redirect_uri(value) for value in redirect_uris
        ]
        selected_scopes = self.validate_scopes(scopes)
        with self.database.immediate_session() as session:
            if approval_actor is not None:
                try:
                    require_actor_in_session(session, approval_actor, required_scopes=selected_scopes | {"app.admin", "app.credentials.write"}, require_admin=True)
                    if approval_actor.user_id != user_id or approval_actor.auth_revision != auth_revision:
                        raise PermissionError("The approving account changed.")
                    resource_grants = require_delegation_resources(session, approval_actor, default_personal_resources(session, approval_actor.user_id) if resource_grants is None else resource_grants)
                except PermissionError:
                    raise EnrollmentError("access_denied", "The approving credential cannot register this client.", 403) from None
            try:
                actor = account_state_in_session(
                    session, user_id if user_id is not None else initial_user_id(session),
                    auth_revision=auth_revision, require_admin=True,
                )
            except PermissionError as error:
                raise EnrollmentError("access_denied", "An active Administrator is required.", 403) from error
            client = self._register_in_session(
                session,
                client_id=validated_id,
                name=name,
                redirect_uris=validated_redirects,
                scopes=selected_scopes,
                target_instance_id=target_instance_id,
                canonical_origin=canonical_origin,
                created_by=created_by,
                user_id=actor.user_id,
                auth_revision=actor.auth_revision,
                resource_grants=resource_grants,
            )
            session.expunge(client)
            return client

    def issue_code(
        self,
        *,
        client_id: object,
        client_name: str,
        redirect_uri: object,
        scopes: object,
        code_challenge: object,
        code_challenge_method: object,
        expires_in_days: int,
        target_instance_id: str,
        canonical_origin: str,
        approved_by: Principal,
        resource_grants: CredentialResources | None = None,
    ) -> str:
        validated_id = self.validate_client_id(client_id)
        validated_redirect = self.validate_redirect_uri(redirect_uri)
        selected_scopes = self.validate_scopes(scopes)
        challenge = self.validate_code_challenge(
            code_challenge,
            code_challenge_method,
        )
        bounded_days = max(1, min(int(expires_in_days), 90))
        raw_code = f"pan_code_{secrets.token_urlsafe(32)}"
        with self.database.immediate_session() as session:
            now = utcnow()
            collaboration = installation_mode_in_session(session).enabled
            if (
                approved_by.user_id is None
                or approved_by.auth_revision is None
                or approved_by.kind != "owner_session"
                or (not collaboration and not approved_by.has_scope("app.admin"))
            ):
                raise EnrollmentError("access_denied", "Sign in with an account allowed to approve enrollment.", 403)
            try:
                actor = actor_from_principal(approved_by)
                if (
                    not isinstance(actor, UserActor)
                    or actor.principal_kind != "owner_session"
                    or actor.browser_session_id is None
                    or actor.token_id is not None
                ):
                    raise ValueError("The approving transport snapshot is invalid.")
                approving_account = require_actor_in_session(
                    session,
                    actor,
                    required_scopes=selected_scopes | ({"app.credentials.write"} if collaboration else {"app.admin"}),
                    require_admin=not collaboration,
                    allow_host=False,
                )
                if approving_account is None:
                    raise ValueError("The approving account snapshot is invalid.")
            except (PermissionError, ValueError):
                raise EnrollmentError(
                    "access_denied",
                    "The approving account credential is no longer valid.",
                    403,
                ) from None
            selected_resources = (
                default_personal_resources(session, approving_account.user_id)
                if resource_grants is None
                else resource_grants
            )
            try:
                selected_resources = require_delegation_resources(session, actor, selected_resources)
            except PermissionError as error:
                raise EnrollmentError(
                    "access_denied", "The approving credential cannot delegate these resources.", 403
                ) from error
            client = self._register_in_session(
                session,
                client_id=validated_id,
                name=client_name,
                redirect_uris=[validated_redirect],
                scopes=selected_scopes,
                target_instance_id=target_instance_id,
                canonical_origin=canonical_origin,
                created_by=approved_by.subject,
                user_id=approving_account.user_id,
                auth_revision=approving_account.auth_revision,
                resource_grants=selected_resources,
            )
            grant = AutomationEnrollmentGrant(
                client_id=client.id,
                code_hash=_digest(raw_code),
                code_prefix=raw_code[:16],
                redirect_uri=validated_redirect,
                scopes_json=sorted(selected_scopes),
                code_challenge=challenge,
                code_challenge_method="S256",
                expires_at=now + timedelta(minutes=5),
                token_expires_at=now + timedelta(days=bounded_days),
            )
            session.add(grant)
            session.flush([grant])
            record_resources(session, CredentialOwner.CODE, grant.id, selected_resources)
        return raw_code

    def exchange_code(
        self,
        *,
        code: object,
        client_id: object,
        redirect_uri: object,
        code_verifier: object,
        target_instance_id: str,
        canonical_origin: str,
    ) -> EnrollmentToken:
        raw_code = str(code or "")
        validated_id = self.validate_client_id(client_id)
        validated_redirect = self.validate_redirect_uri(redirect_uri)
        verifier = str(code_verifier or "")
        try:
            supplied_challenge = create_s256_code_challenge(verifier)
        except (TypeError, ValueError) as error:
            raise EnrollmentError(
                "invalid_grant",
                "The PKCE code verifier is invalid.",
                400,
            ) from error
        with self.database.immediate_session() as session:
            now = utcnow()
            grants = list(
                session.scalars(
                    select(AutomationEnrollmentGrant).where(
                        AutomationEnrollmentGrant.code_prefix
                        == raw_code[:16]
                    )
                ).all()
            )
            grant = next(
                (
                    candidate
                    for candidate in grants
                    if hmac.compare_digest(
                        candidate.code_hash,
                        _digest(raw_code),
                    )
                ),
                None,
            )
            if (
                grant is None
                or grant.consumed_at is not None
                or _aware(grant.expires_at) <= now
                or grant.client_id != validated_id
                or grant.redirect_uri != validated_redirect
                or not hmac.compare_digest(
                    grant.code_challenge,
                    supplied_challenge,
                )
            ):
                raise EnrollmentError(
                    "invalid_grant",
                    "The authorization code is invalid, expired, or already used.",
                    400,
                )
            client = session.get(AutomationClient, grant.client_id)
            if (
                client is None
                or client.revoked_at is not None
                or client.user_id is None
                or client.user_auth_revision is None
                or client.target_instance_id != target_instance_id
                or client.canonical_origin != canonical_origin
            ):
                raise EnrollmentError(
                    "target_identity_mismatch",
                    "The enrolled target identity no longer matches.",
                    409,
                )
            try:
                account_state_in_session(
                    session, client.user_id, auth_revision=client.user_auth_revision
                )
            except PermissionError as error:
                raise EnrollmentError("invalid_grant", "The consenting account has changed.", 400) from error
            try:
                allowed_scopes = self.validate_scopes(client.allowed_scopes_json)
                grant_scopes = self.validate_scopes(grant.scopes_json)
            except EnrollmentError as error:
                raise EnrollmentError("invalid_grant", "The client's approved scopes have changed.", 400) from error
            if not grant_scopes <= allowed_scopes:
                raise EnrollmentError("invalid_grant", "The client's approved scopes have changed.", 400)
            selected_resources = resources_for_owner(session, CredentialOwner.CODE, grant.id)
            client_resources = resources_for_owner(session, CredentialOwner.CLIENT, client.id)
            try:
                selected_resources = validate_resources_for_user(
                    session, client.user_id, selected_resources
                )
                require_resources_within(session, selected_resources, client_resources)
            except CredentialResourcesDenied as error:
                raise EnrollmentError(
                    "invalid_grant", "The client's approved resources have changed.", 400
                ) from error
            grant.consumed_at = now
            session.execute(
                update(ApiToken)
                .where(
                    ApiToken.client_id == client.id,
                    ApiToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            token, raw_token = self.auth.create_api_token_in_session(
                session,
                f"{client.name} automation",
                scopes=grant.scopes_json,
                expires_at=grant.token_expires_at,
                principal_kind="automation_client",
                created_by=client.created_by,
                subject=client.subject,
                client_id=client.id,
                target_instance_id=client.target_instance_id,
                canonical_origin=client.canonical_origin,
                user_id=client.user_id,
                auth_revision=client.user_auth_revision,
                resource_grants=selected_resources,
            )
            session.expunge(token)
            session.expunge(client)
            return EnrollmentToken(token, raw_token, client)

    def list_clients(self, *, actor: UserActor | None = None) -> list[dict[str, object]]:
        with self.database.session() as session:
            statement = select(AutomationClient).order_by(AutomationClient.created_at.desc())
            if actor is not None:
                require_actor_in_session(session, actor, required_scopes={"app.credentials.read"})
                statement = statement.where(AutomationClient.user_id == actor.user_id)
            clients = list(
                session.scalars(
                    statement
                ).all()
            )
            return [
                {
                    "id": client.id,
                    "name": client.name,
                    "subject": client.subject,
                    "redirect_uris": list(
                        client.redirect_uris_json or []
                    ),
                    "allowed_scopes": list(
                        client.allowed_scopes_json or []
                    ),
                    "target_instance_id": client.target_instance_id,
                    "canonical_origin": client.canonical_origin,
                    "created_by": client.created_by,
                    "created_at": client.created_at.isoformat(),
                    "last_used_at": (
                        client.last_used_at.isoformat()
                        if client.last_used_at
                        else None
                    ),
                    "revoked_at": (
                        client.revoked_at.isoformat()
                        if client.revoked_at
                        else None
                    ),
                }
                for client in clients
            ]

    def revoke(self, client_id: object, *, actor: UserActor | None = None) -> None:
        validated_id = self.validate_client_id(client_id)
        with self.database.immediate_session() as session:
            now = utcnow()
            statement = select(AutomationClient).where(AutomationClient.id == validated_id)
            if actor is not None:
                require_actor_in_session(session, actor, required_scopes={"app.credentials.write"})
                statement = statement.where(AutomationClient.user_id == actor.user_id)
            client = session.scalar(statement)
            if client is None:
                raise KeyError(validated_id)
            client.revoked_at = now
            session.execute(
                update(ApiToken)
                .where(
                    ApiToken.client_id == client.id,
                    ApiToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            session.execute(
                delete(AutomationEnrollmentGrant).where(
                    AutomationEnrollmentGrant.client_id == client.id,
                    AutomationEnrollmentGrant.consumed_at.is_(None),
                )
            )

    def cleanup(self) -> int:
        with self.database.session() as session:
            result = session.execute(
                delete(AutomationEnrollmentGrant).where(
                    AutomationEnrollmentGrant.expires_at < utcnow()
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)
