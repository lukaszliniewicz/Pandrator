"""Transactional, transport-independent public project link lifecycle."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..access.contracts import AccessContext, AccessDenied, ProjectAction
from ..access.project_policy import require_project_access
from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.auth_models import BrowserAuthSession
from ..accounts.credential_resources import CredentialResources, require_delegation_resources
from ..accounts.execution_actor import UserActor, execution_actor_scope
from ..artifact_selection import selected_artifacts
from ..database import Database
from ..model_base import utcnow
from ..models import AppSetting, Artifact, Document, DocumentRevision
from ..workspace_settings import WorkspaceSettingsService
from .authorization import aware, require_link_in_session, require_project_link_governance
from .contracts import (
    LINK_SCOPES,
    SETTING_KEY,
    ProjectLinkContext,
    PublicLinkError,
    RedeemedProjectLink,
)
from .models import ProjectLink, ProjectLinkSession

if TYPE_CHECKING:
    from ..audit import AuditService
    from ..auth import AuthService


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _origin(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or value != value.strip():
        raise PublicLinkError("invalid_public_origin")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or host is None
            or not parsed.netloc
            or value != f"{parsed.scheme}://{parsed.netloc}"
            or port == 0
        ):
            raise ValueError
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
        if any(character.isspace() or ord(character) < 33 for character in value):
            raise ValueError
        # Reject parser normalization, encoded host tricks, and non-ASCII authority.
        if any(character in parsed.netloc for character in "\\%") or not parsed.netloc.isascii():
            raise ValueError
    except ValueError:
        raise PublicLinkError("invalid_public_origin") from None
    return value


def _label(value: str) -> str:
    if type(value) is not str or not value.strip() or len(value.strip()) > 80:
        raise PublicLinkError("invalid_label")
    return value.strip()


class PublicLinkService:
    def __init__(self, database: Database, auth: AuthService, audit: AuditService):
        self.database = database
        self.auth = auth
        self.audit = audit

    @staticmethod
    def _settings(session: Session) -> dict[str, Any]:
        row = session.get(AppSetting, SETTING_KEY, populate_existing=True)
        if row is None:
            return {"enabled": False, "public_origin": None, "revision": 0}
        value = row.value_json if isinstance(row.value_json, dict) else {}
        return {
            "enabled": value.get("enabled") is True,
            "public_origin": value.get("public_origin"),
            "revision": row.revision,
        }

    def status(self) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            settings = self._settings(session)
            return {
                "enabled": settings["enabled"],
                "configured": session.scalar(
                    select(AppSetting.key).where(AppSetting.key == SETTING_KEY)
                )
                is not None,
                "revision": settings["revision"],
            }

    @staticmethod
    def _admin(session: Session, context: AccessContext) -> None:
        if type(context) is not AccessContext or context.actor.principal_kind == "project_link":
            raise AccessDenied()
        require_actor_in_session(
            session, context.actor, required_scopes={"app.admin"}, require_admin=True
        )

    def settings(self, context: AccessContext) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            self._admin(session, context)
            return self._settings(session)

    def _audit(
        self,
        session: Session,
        context: AccessContext,
        action: str,
        resource_id: str,
        revision: int,
        mode: str | None = None,
    ) -> None:
        self.audit.record_command_in_session(
            session,
            context,
            action=action,
            workspace_id=resource_id,
            resource_kind="public_link",
            metadata={"revision": revision, "mode": mode},
        )

    @staticmethod
    def _revoke_sessions(session: Session, link_ids: list[str]) -> None:
        if not link_ids:
            return
        session.execute(
            update(BrowserAuthSession)
            .where(
                BrowserAuthSession.id.in_(
                    select(ProjectLinkSession.browser_session_id).where(
                        ProjectLinkSession.link_id.in_(link_ids)
                    )
                ),
                BrowserAuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
            .execution_options(synchronize_session=False)
        )

    def configure(
        self,
        context: AccessContext,
        *,
        expected_revision: int,
        enabled: bool,
        public_origin: str | None,
    ) -> dict[str, Any]:
        if type(enabled) is not bool or type(expected_revision) is not int or expected_revision < 0:
            raise PublicLinkError("invalid_settings")
        origin = _origin(public_origin)
        if enabled and origin is None:
            raise PublicLinkError("public_origin_required")
        with self.database.immediate_session() as session:
            self._admin(session, context)
            current = self._settings(session)
            if current["revision"] != expected_revision:
                raise PublicLinkError("settings_revision_conflict", 409)
            row = session.get(AppSetting, SETTING_KEY)
            if row is None:
                row = AppSetting(key=SETTING_KEY, value_json={}, revision=0)
                session.add(row)
            row.value_json = {"enabled": enabled, "public_origin": origin}
            row.revision += 1
            row.updated_at = utcnow()
            if not enabled:
                ids = list(session.scalars(select(ProjectLink.id)).all())
                session.execute(
                    update(ProjectLink)
                    .where(ProjectLink.revoked_at.is_(None))
                    .values(
                        revoked_at=utcnow(),
                        revision=ProjectLink.revision + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                self._revoke_sessions(session, ids)
            self._audit(session, context, "public.links.configure", SETTING_KEY, row.revision)
            session.flush()
            return self._settings(session)

    @staticmethod
    def _manager(
        session: Session, context: AccessContext, project_id: str, *, write: bool
    ) -> tuple[str, str]:
        if type(context) is not AccessContext or context.actor.principal_kind == "project_link":
            raise AccessDenied()
        access = require_project_access(session, context, project_id, ProjectAction.MANAGE_MEMBERS)
        if write and not {"app.read", "app.write"} <= context.actor.scopes:
            raise AccessDenied()
        if access.workspace_role not in {"owner", "admin"}:
            raise AccessDenied()
        return require_project_link_governance(
            session, context.actor.user_id, context.actor.auth_revision, project_id
        )

    @staticmethod
    def _descriptor(link: ProjectLink) -> dict[str, Any]:
        return {
            key: getattr(link, key)
            for key in (
                "id",
                "project_id",
                "root_session_id",
                "label",
                "mode",
                "revision",
                "target_session_id",
                "target_artifact_id",
                "target_revision_id",
                "target_hash",
                "target_revision_number",
                "target_composition_hash",
            )
        } | {
            "expires_at": aware(link.expires_at).isoformat(),
            "revoked_at": aware(link.revoked_at).isoformat() if link.revoked_at else None,
        }

    def list_links(self, context: AccessContext, project_id: str) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            self._manager(session, context, project_id, write=False)
            return {
                "items": [
                    self._descriptor(link)
                    for link in session.scalars(
                        select(ProjectLink)
                        .where(ProjectLink.project_id == project_id)
                        .order_by(ProjectLink.created_at.desc(), ProjectLink.id)
                    )
                ]
            }

    @staticmethod
    def _target(
        session: Session, project_id: str, workspace_id: str, artifact_id: str | None
    ) -> dict[str, Any]:
        artifact = session.get(Artifact, artifact_id) if artifact_id else None
        if (
            artifact is None
            or artifact.project_id != project_id
            or artifact.workspace_id != workspace_id
            or artifact.session_id is None
            or artifact.role != "translation"
            or artifact.state == "deleted"
            or not artifact.content_hash
        ):
            raise PublicLinkError("invalid_translation_target")
        selected = selected_artifacts(session, artifact.session_id).get("translate")
        metadata = artifact.metadata_json or {}
        revision = session.get(DocumentRevision, str(metadata.get("revision_id") or ""))
        document = session.get(Document, str(metadata.get("document_id") or ""))
        if (
            selected is None
            or selected.id != artifact.id
            or revision is None
            or document is None
            or document.session_id != artifact.session_id
            or document.stage != "translation"
            or revision.document_id != document.id
            or document.active_revision_id != revision.id
            or metadata.get("revision_number", revision.revision_number) != revision.revision_number
        ):
            raise PublicLinkError("invalid_translation_target")
        return {
            "target_session_id": artifact.session_id,
            "target_artifact_id": artifact.id,
            "target_revision_id": revision.id,
            "target_hash": artifact.content_hash,
            "target_revision_number": revision.revision_number,
        }

    def create_link(
        self,
        context: AccessContext,
        project_id: str,
        *,
        label: str,
        mode: str,
        expires_in_days: int = 7,
        target_artifact_id: str | None = None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        label = _label(label)
        if (
            mode not in LINK_SCOPES
            or type(expires_in_days) is not int
            or not 1 <= expires_in_days <= 30
        ):
            raise PublicLinkError("invalid_link_options")
        if request_key is not None and (
            type(request_key) is not str
            or not request_key
            or request_key != request_key.strip()
            or len(request_key) > 100
        ):
            raise PublicLinkError("invalid_request_key")
        if mode == "manage_project" and target_artifact_id is not None:
            raise PublicLinkError("invalid_translation_target")
        payload_hash = digest(
            json.dumps([label, mode, expires_in_days, target_artifact_id], separators=(",", ":"))
        )
        with self.database.immediate_session() as session:
            workspace_id, root_id = self._manager(session, context, project_id, write=True)
            settings = self._settings(session)
            if not settings["enabled"] or not settings["public_origin"]:
                raise PublicLinkError("public_links_disabled", 403)
            if not LINK_SCOPES[mode] <= context.actor.scopes:
                raise AccessDenied()
            require_delegation_resources(
                session, context.actor, CredentialResources(project_ids=frozenset({project_id}))
            )
            if request_key is not None:
                existing = session.scalar(
                    select(ProjectLink).where(
                        ProjectLink.project_id == project_id,
                        ProjectLink.issuer_user_id == context.actor.user_id,
                        ProjectLink.request_key == request_key,
                    )
                )
                if existing is not None:
                    if existing.payload_hash != payload_hash:
                        raise PublicLinkError("request_key_conflict", 409)
                    return {
                        "link": self._descriptor(existing),
                        "url": None,
                        "already_created": True,
                    }
            target = (
                self._target(session, project_id, workspace_id, target_artifact_id)
                if mode == "review_translation"
                else {}
            )
            if mode == "review_translation":
                with execution_actor_scope(context.actor):
                    _, composition_hash = WorkspaceSettingsService(
                        self.database
                    ).resolve_in_session(
                        session,
                        target["target_session_id"],
                        sections=["subtitles"],
                    )
                target["target_composition_hash"] = composition_hash
            secret = secrets.token_urlsafe(32)
            link = ProjectLink(
                project_id=project_id,
                workspace_id=workspace_id,
                root_session_id=root_id,
                issuer_user_id=context.actor.user_id,
                issuer_auth_revision=context.actor.auth_revision,
                label=label,
                mode=mode,
                token_hash=digest(secret),
                expires_at=utcnow() + timedelta(days=expires_in_days),
                request_key=request_key,
                payload_hash=payload_hash,
                **target,
            )
            session.add(link)
            session.flush()
            require_link_in_session(session, link)
            self._audit(session, context, "public.link.create", link.id, link.revision, mode)
            return {
                "link": self._descriptor(link),
                "url": f"{settings['public_origin']}/share/{link.id}#token={secret}",
                "already_created": False,
            }

    def revoke_link(
        self, context: AccessContext, link_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            link = session.get(ProjectLink, link_id, populate_existing=True)
            if link is None:
                raise PublicLinkError("link_not_found", 404)
            self._manager(session, context, link.project_id, write=True)
            if type(expected_revision) is not int or link.revision != expected_revision:
                raise PublicLinkError("link_revision_conflict", 409)
            if link.revoked_at is None:
                link.revoked_at = utcnow()
                link.revision += 1
                self._revoke_sessions(session, [link.id])
                self._audit(
                    session, context, "public.link.revoke", link.id, link.revision, link.mode
                )
            return {"link": self._descriptor(link)}

    def redeem(self, link_id: str, secret: str) -> RedeemedProjectLink:
        if type(secret) is not str or not secret or len(secret) > 256:
            raise PublicLinkError("link_unavailable", 403)
        with self.database.snapshot_session() as session:
            link = session.get(ProjectLink, link_id, populate_existing=True)
            if link is None or not secrets.compare_digest(link.token_hash, digest(secret)):
                raise PublicLinkError("link_unavailable", 403)
            try:
                require_link_in_session(session, link)
            except PermissionError:
                raise PublicLinkError("link_unavailable", 403) from None
            seconds = min(86400, int((aware(link.expires_at) - utcnow()).total_seconds()))
            if seconds < 1:
                raise PublicLinkError("link_unavailable", 403)
            user_id, revision, project_id, mode = (
                link.issuer_user_id,
                link.issuer_auth_revision,
                link.project_id,
                link.mode,
            )
        try:
            issued = self.auth.browser_sessions.issue(
                user_id,
                revision,
                subject=f"project-link:{link_id}",
                principal_kind="project_link",
                scopes=LINK_SCOPES[mode],
                idle_seconds=min(3600, seconds),
                absolute_seconds=seconds,
                resource_grants=CredentialResources(project_ids=frozenset({project_id})),
            )
        except (PermissionError, ValueError):
            raise PublicLinkError("link_unavailable", 403) from None
        csrf = secrets.token_urlsafe(32)
        try:
            with self.database.immediate_session() as session:
                link = session.get(ProjectLink, link_id, populate_existing=True)
                if link is None:
                    raise PermissionError
                require_link_in_session(session, link)
                browser = session.get(BrowserAuthSession, issued.record_id, populate_existing=True)
                if browser is None or browser.revoked_at is not None:
                    raise PermissionError
                browser.expires_at = min(aware(browser.expires_at), aware(link.expires_at))
                browser.idle_expires_at = min(
                    aware(browser.idle_expires_at), aware(browser.expires_at)
                )
                session.add(
                    ProjectLinkSession(
                        browser_session_id=issued.record_id, link_id=link_id, csrf_hash=digest(csrf)
                    )
                )
                descriptor = self._descriptor(link)
        except BaseException as error:
            self.auth.browser_sessions.revoke(issued.raw_token)
            if isinstance(error, PermissionError):
                raise PublicLinkError("link_unavailable", 403) from None
            raise
        return RedeemedProjectLink(issued.raw_token, csrf, descriptor)

    def session_context(self, link_id: str, raw_browser_session: str) -> ProjectLinkContext:
        principal = self.auth.browser_sessions.resolve(raw_browser_session)
        if principal is None or principal.kind != "project_link":
            raise PublicLinkError("link_unavailable", 403)
        actor = UserActor(
            principal.user_id,
            principal.auth_revision,
            principal.subject,
            principal.kind,
            principal.scopes,
            browser_session_id=principal.session_id,
            client_id=principal.client_id,
        )
        with self.database.snapshot_session() as session:
            binding = session.get(ProjectLinkSession, principal.session_id, populate_existing=True)
            if binding is None or binding.link_id != link_id:
                raise PublicLinkError("link_unavailable", 403)
            try:
                require_actor_in_session(session, actor, required_scopes={"app.read"})
            except PermissionError:
                raise PublicLinkError("link_unavailable", 403) from None
            link = session.get(ProjectLink, link_id, populate_existing=True)
            if link is None:
                raise PublicLinkError("link_unavailable", 403)
            return ProjectLinkContext(actor, self._descriptor(link), binding.csrf_hash)
