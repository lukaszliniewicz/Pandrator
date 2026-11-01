"""Pinned, sparse guest corrections and ordinary project-editor dispositions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..access.contracts import AccessContext, AccessDenied, ProjectAction
from ..access.project_policy import require_project_access
from ..accounts.actor_authorization import require_actor_in_session
from ..accounts.execution_actor import current_execution_actor
from ..artifact_selection import selected_artifacts
from ..audit import AuditService
from ..database import Database
from ..model_base import utcnow
from ..models import Artifact, Document, DocumentRevision, SessionRecord
from ..projects.models import Project
from ..subtitle_review import SubtitleReviewService, cleanup_review_publications
from ..workspace_settings import WorkspaceSettingsService
from .authorization import aware, require_link_in_session, require_project_link_actor_in_session
from .contracts import PublicLinkError
from .models import ProjectLink, ProjectLinkSession, ProjectLinkSubmission


def _page(offset: int, limit: int) -> tuple[int, int]:
    if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
        raise PublicLinkError("invalid_pagination")
    return offset, min(limit, 100)


def _key(value: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > 100:
        raise PublicLinkError("invalid_request_key")


def _conflict() -> PublicLinkError:
    return PublicLinkError("submission_source_conflict", 409)


class PublicTranslationReviewService:
    def __init__(
        self, database: Database, subtitle_review: SubtitleReviewService, audit: AuditService
    ):
        self.database = database
        self.subtitle_review = subtitle_review
        self.audit = audit

    @staticmethod
    def _ordinary(context: AccessContext) -> None:
        if type(context) is not AccessContext or context.actor.principal_kind == "project_link":
            raise AccessDenied()

    def _guest(self, session: Session, context: AccessContext, link_id: str) -> ProjectLink:
        if type(context) is not AccessContext or context.actor.principal_kind != "project_link":
            raise AccessDenied()
        require_actor_in_session(session, context.actor, required_scopes={"app.read"})
        require_project_link_actor_in_session(session, context.actor)
        bound = session.scalar(
            select(ProjectLinkSession.link_id).where(
                ProjectLinkSession.browser_session_id == context.actor.browser_session_id,
            )
        )
        if bound != link_id:
            raise AccessDenied()
        link = session.get(ProjectLink, link_id, populate_existing=True)
        if link is None or link.mode != "review_translation":
            raise AccessDenied()
        require_link_in_session(session, link)
        self._source(session, link, require_current=False)
        return link

    def _source(
        self, session: Session, link: ProjectLink, *, require_current: bool = True
    ) -> str | None:
        """Check pointers and immutable witnesses without loading subtitle bodies."""
        selected_source = (
            selected_artifacts(session, str(link.target_session_id)).get("translate")
            if require_current
            else None
        )
        selected = selected_source.id if selected_source is not None else None
        source = session.get(Artifact, link.target_artifact_id)
        revision = session.get(DocumentRevision, link.target_revision_id)
        document = session.get(Document, revision.document_id) if revision else None
        target = session.get(SessionRecord, link.target_session_id)
        if (
            source is None
            or revision is None
            or document is None
            or target is None
            or target.trashed_at is not None
            or target.project_id != link.project_id
            or (require_current and selected != link.target_artifact_id)
            or source.state == "deleted"
            or source.role != "translation"
            or source.project_id != link.project_id
            or source.workspace_id != link.workspace_id
            or source.session_id != target.id
            or source.content_hash != link.target_hash
            or revision.revision_number != link.target_revision_number
            or (require_current and document.active_revision_id != revision.id)
            or document.session_id != target.id
            or document.stage != "translation"
            or (source.metadata_json or {}).get("revision_id") != revision.id
            or (source.metadata_json or {}).get("document_id") != document.id
        ):
            raise _conflict()
        if require_current:
            _, composition_hash = WorkspaceSettingsService(self.database).resolve_in_session(
                session,
                target.id,
                sections=["subtitles"],
            )
            if composition_hash != link.target_composition_hash:
                raise _conflict()
        return selected

    def _column(self, context: AccessContext, link: ProjectLink) -> dict[str, Any]:
        token = current_execution_actor.set(context.actor)
        try:
            column = self.subtitle_review.review(
                str(link.target_session_id),
                [str(link.target_artifact_id)],
            )["columns"][0]
        finally:
            current_execution_actor.reset(token)
        if (
            column["artifact_id"] != link.target_artifact_id
            or column["source_content_hash"] != link.target_hash
            or column["revision_id"] != link.target_revision_id
            or column["revision"] != link.target_revision_number
        ):
            raise _conflict()
        return column

    def preview(
        self, context: AccessContext, link_id: str, *, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        offset, limit = _page(offset, limit)
        with self.database.snapshot_session() as session:
            link = self._guest(session, context, link_id)
        column = self._column(context, link)
        rows = column["logical_passages"]
        with self.database.snapshot_session() as session:
            self._guest(session, context, link_id)
        return {
            "link_id": link.id,
            "project_id": link.project_id,
            "label": link.label,
            "mode": link.mode,
            "target_artifact_id": link.target_artifact_id,
            "target_hash": link.target_hash,
            "document_revision": link.target_revision_number,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "items": [
                {
                    "ordinal": offset + index,
                    **{key: row[key] for key in ("id", "start_ms", "end_ms", "text")},
                }
                for index, row in enumerate(rows[offset : offset + limit])
            ],
        }

    @staticmethod
    def _normalize(
        changes: list[dict[str, Any]], reviewer_label: str | None, expected_source_hash: str
    ) -> tuple[list[dict[str, str]], str | None, str]:
        if (
            type(changes) is not list
            or not changes
            or len(changes) > 5000
            or type(expected_source_hash) is not str
        ):
            raise PublicLinkError("invalid_changes")
        if reviewer_label is not None and (
            type(reviewer_label) is not str or len(reviewer_label) > 80
        ):
            raise PublicLinkError("invalid_reviewer_label")
        label = reviewer_label.strip() or None if reviewer_label is not None else None
        normalized: list[dict[str, str]] = []
        encoded_change_bytes = 0
        seen = set()
        for change in changes:
            if (
                type(change) is not dict
                or set(change) != {"segment_id", "text"}
                or type(change["segment_id"]) is not str
                or not change["segment_id"]
                or change["segment_id"] in seen
                or type(change["text"]) is not str
                or len(change["text"]) > 20000
                or not change["text"].strip()
            ):
                raise PublicLinkError("invalid_changes")
            seen.add(change["segment_id"])
            candidate = {"segment_id": change["segment_id"], "text": change["text"]}
            try:
                encoded_change_bytes += len(
                    json.dumps(
                        candidate,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except UnicodeError:
                raise PublicLinkError("invalid_changes") from None
            if encoded_change_bytes > 200 * 1024:
                raise PublicLinkError("submission_too_large", 413)
            normalized.append(candidate)
        normalized.sort(key=lambda item: item["segment_id"])
        try:
            encoded = json.dumps(
                {
                    "changes": normalized,
                    "reviewer_label": label,
                    "expected_source_hash": expected_source_hash,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except UnicodeError:
            raise PublicLinkError("invalid_changes") from None
        if len(encoded) > 200 * 1024:
            raise PublicLinkError("submission_too_large", 413)
        return normalized, label, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _saved(row: ProjectLinkSubmission) -> dict[str, Any]:
        return {
            "submission_id": row.id,
            "status": row.status,
            "revision": row.revision,
            "change_count": len(row.changes_json),
            "created_at": aware(row.created_at).isoformat(),
        }

    def _audit(self, session: Session, context: AccessContext, row: ProjectLinkSubmission) -> None:
        project_id = session.scalar(
            select(ProjectLink.project_id).where(ProjectLink.id == row.link_id)
        )
        if project_id is None:
            raise RuntimeError("Submission project is unavailable.")
        self.audit.record_command_in_session(
            session,
            context,
            action=f"public.translation.{row.status}",
            resource_kind="public_link_submission",
            workspace_id=row.id,
            metadata={
                "revision": row.revision,
                "state": row.status,
                "source_project_id": project_id,
                "mode": "review_translation",
            },
        )

    def submit(
        self,
        context: AccessContext,
        link_id: str,
        *,
        expected_source_hash: str,
        changes: list[dict[str, Any]],
        reviewer_label: str | None = None,
        request_key: str,
    ) -> dict[str, Any]:
        _key(request_key)
        normalized, label, payload_hash = self._normalize(
            changes, reviewer_label, expected_source_hash
        )
        with self.database.snapshot_session() as session:
            link = self._guest(session, context, link_id)
            if expected_source_hash != link.target_hash:
                raise _conflict()
        column = self._column(context, link)
        by_id = {row["id"]: row for row in column["logical_passages"]}
        for change in normalized:
            if (
                change["segment_id"] not in by_id
                or by_id[change["segment_id"]]["text"] == change["text"]
            ):
                raise PublicLinkError("invalid_changes")
        with self.database.immediate_session() as session:
            self._guest(session, context, link_id)
            existing = session.scalar(
                select(ProjectLinkSubmission).where(
                    ProjectLinkSubmission.link_id == link_id,
                    ProjectLinkSubmission.browser_session_id == context.actor.browser_session_id,
                    ProjectLinkSubmission.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise PublicLinkError("submission_request_conflict", 409)
                return self._saved(existing)
            pending = (
                session.scalar(
                    select(func.count(ProjectLinkSubmission.id)).where(
                        ProjectLinkSubmission.link_id == link_id,
                        ProjectLinkSubmission.status == "pending",
                    )
                )
                or 0
            )
            if pending >= 100:
                raise PublicLinkError("submission_queue_full", 429)
            row = ProjectLinkSubmission(
                link_id=link_id,
                browser_session_id=context.actor.browser_session_id,
                reviewer_label=label,
                changes_json=normalized,
                request_key=request_key,
                payload_hash=payload_hash,
            )
            session.add(row)
            session.flush()
            self._audit(session, context, row)
            return self._saved(row)

    def _operator(
        self, session: Session, context: AccessContext, link: ProjectLink, action: ProjectAction
    ) -> None:
        self._ordinary(context)
        require_project_access(session, context, link.project_id, action)
        project = session.get(Project, link.project_id)
        if (
            project is None
            or project.source_session_id != link.root_session_id
            or project.workspace_id != link.workspace_id
        ):
            raise AccessDenied()
        root = session.scalar(
            select(SessionRecord.id).where(
                SessionRecord.id == link.root_session_id,
                SessionRecord.project_id == link.project_id,
                SessionRecord.trashed_at.is_(None),
            )
        )
        target_project = session.scalar(
            select(SessionRecord.project_id).where(
                SessionRecord.id == link.target_session_id,
            )
        )
        if root is None or (target_project is not None and target_project != link.project_id):
            raise AccessDenied()

    def _submission(
        self, session: Session, context: AccessContext, submission_id: str, action: ProjectAction
    ) -> tuple[ProjectLinkSubmission, ProjectLink]:
        self._ordinary(context)
        # Resolve only the ownership pointer before loading protected submission bodies.
        link_id = session.scalar(
            select(ProjectLinkSubmission.link_id).where(
                ProjectLinkSubmission.id == submission_id,
            )
        )
        link = session.get(ProjectLink, link_id) if link_id else None
        if link is None:
            raise PublicLinkError("submission_not_found", 404)
        self._operator(session, context, link, action)
        row = session.get(ProjectLinkSubmission, submission_id, populate_existing=True)
        if row is None:
            raise PublicLinkError("submission_not_found", 404)
        return row, link

    @classmethod
    def _summary(cls, row: ProjectLinkSubmission, link: ProjectLink) -> dict[str, Any]:
        return {
            **cls._saved(row),
            "link_id": link.id,
            "link_label": link.label,
            "reviewer_label": row.reviewer_label,
            "reviewer_label_verified": False,
            "reviewed_at": aware(row.reviewed_at).isoformat() if row.reviewed_at else None,
            "reviewed_by_user_id": row.reviewed_by_user_id,
            "accepted_artifact_id": row.accepted_artifact_id,
        }

    def list_submissions(
        self,
        context: AccessContext,
        project_id: str,
        *,
        status: str = "pending",
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        self._ordinary(context)
        offset, limit = _page(offset, limit)
        if status not in {"pending", "accepted", "rejected"}:
            raise PublicLinkError("invalid_submission_status")
        with self.database.snapshot_session() as session:
            require_project_access(session, context, project_id, ProjectAction.REVIEW)
            pairs = session.execute(
                select(ProjectLinkSubmission, ProjectLink)
                .join(
                    ProjectLink,
                    ProjectLink.id == ProjectLinkSubmission.link_id,
                )
                .where(ProjectLink.project_id == project_id, ProjectLinkSubmission.status == status)
                .order_by(ProjectLinkSubmission.created_at, ProjectLinkSubmission.id)
                .offset(offset)
                .limit(limit)
            ).all()
            for _, link in pairs:
                self._operator(session, context, link, ProjectAction.REVIEW)
            return {
                "items": [self._summary(row, link) for row, link in pairs],
                "offset": offset,
                "limit": limit,
            }

    def get_submission(
        self, context: AccessContext, submission_id: str, *, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        offset, limit = _page(offset, limit)
        with self.database.snapshot_session() as session:
            row, link = self._submission(session, context, submission_id, ProjectAction.REVIEW)
            selected_source = selected_artifacts(session, str(link.target_session_id)).get(
                "translate"
            )
            selected = selected_source.id if selected_source is not None else None
            summary = self._summary(row, link)
        try:
            column = self._column(context, link)
            by_id = {
                item["id"]: (index, item) for index, item in enumerate(column["logical_passages"])
            }
        except (KeyError, ValueError, PublicLinkError):
            # Retain submitted text when the immutable source can no longer
            # support a trustworthy before/after comparison.
            by_id = {}
        with self.database.snapshot_session() as session:
            self._submission(session, context, submission_id, ProjectAction.REVIEW)
        source_unavailable = any(change["segment_id"] not in by_id for change in row.changes_json)
        changes = []
        for change in row.changes_json[offset : offset + limit]:
            ordinal, original = (
                by_id[change["segment_id"]] if not source_unavailable else (None, None)
            )
            changes.append(
                {
                    "segment_id": change["segment_id"],
                    "ordinal": ordinal,
                    "start_ms": original["start_ms"] if original is not None else None,
                    "end_ms": original["end_ms"] if original is not None else None,
                    "before": original["text"] if original is not None else None,
                    "after": change["text"],
                }
            )
        return {
            "submission": summary,
            "project_id": link.project_id,
            "target_artifact_id": link.target_artifact_id,
            "target_hash": link.target_hash,
            "document_revision": link.target_revision_number,
            "target_composition_hash": link.target_composition_hash,
            "selected_artifact_id": selected,
            "source_unavailable": source_unavailable,
            "changes": changes,
            "total": len(row.changes_json),
            "offset": offset,
            "limit": limit,
        }

    @staticmethod
    def _decision(row: ProjectLinkSubmission, status: str, expected_revision: int) -> bool:
        if type(expected_revision) is not int or expected_revision < 1:
            raise PublicLinkError("invalid_submission_revision")
        if row.status == status and row.revision == expected_revision + 1:
            return True
        if row.status != "pending" or row.revision != expected_revision:
            raise PublicLinkError("submission_revision_conflict", 409)
        return False

    @staticmethod
    def _accepted_guards(
        link: ProjectLink,
        expected_source_hash: str,
        expected_document_revision: int,
        expected_selected_artifact_id: str,
    ) -> None:
        if (
            expected_source_hash != link.target_hash
            or type(expected_document_revision) is not int
            or expected_document_revision != link.target_revision_number
            or expected_selected_artifact_id != link.target_artifact_id
        ):
            raise _conflict()

    def accept(
        self,
        context: AccessContext,
        submission_id: str,
        *,
        expected_revision: int,
        expected_source_hash: str,
        expected_document_revision: int,
        expected_selected_artifact_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _key(idempotency_key)
        with self.database.snapshot_session() as session:
            row, link = self._submission(session, context, submission_id, ProjectAction.EDIT)
            self._accepted_guards(
                link,
                expected_source_hash,
                expected_document_revision,
                expected_selected_artifact_id,
            )
            if self._decision(row, "accepted", expected_revision):
                return self._summary(row, link)
            self._source(session, link)
        column = self._column(context, link)
        values = deepcopy(column["logical_passages"])
        changes = {item["segment_id"]: item["text"] for item in row.changes_json}
        if set(changes) - {item["id"] for item in values}:
            raise _conflict()
        for item in values:
            if item["id"] in changes:
                item["text"] = changes[item["id"]]
        token = current_execution_actor.set(context.actor)
        try:
            prepared = self.subtitle_review.prepare_passage_review(
                str(link.target_session_id),
                "translation",
                expected_document_revision,
                values,
                source_artifact_id=str(link.target_artifact_id),
                expected_source_hash=str(link.target_hash),
                expected_composition_hash=str(link.target_composition_hash),
            )
        finally:
            current_execution_actor.reset(token)
        if isinstance(prepared, dict):
            raise RuntimeError("Subtitle preparation did not produce a publication.")
        paths: list[Path] = []
        try:
            with self.database.immediate_session() as session:
                row, link = self._submission(session, context, submission_id, ProjectAction.EDIT)
                self._accepted_guards(
                    link,
                    expected_source_hash,
                    expected_document_revision,
                    expected_selected_artifact_id,
                )
                if self._decision(row, "accepted", expected_revision):
                    return self._summary(row, link)
                self._source(session, link)
                token = current_execution_actor.set(context.actor)
                try:
                    result = self.subtitle_review.publish_prepared_review_in_session(
                        session, prepared, paths
                    )
                finally:
                    current_execution_actor.reset(token)
                row.status = "accepted"
                row.accepted_artifact_id = str(result["artifact_id"])
                row.reviewed_at = utcnow()
                row.reviewed_by_user_id = context.actor.user_id
                row.revision += 1
                self._audit(session, context, row)
                response = self._summary(row, link)
            return response
        except BaseException:
            # A context manager may raise after a successful commit. Never unlink
            # a registered immutable artifact when the transaction already won.
            with self.database.snapshot_session() as session:
                registered = session.scalar(
                    select(Artifact.id).where(
                        Artifact.relative_path == prepared.registration.relative_path,
                    )
                )
            if registered is None:
                cleanup_review_publications(paths)
            raise
        finally:
            prepared.cleanup()

    def reject(
        self,
        context: AccessContext,
        submission_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _key(idempotency_key)
        with self.database.immediate_session() as session:
            row, link = self._submission(session, context, submission_id, ProjectAction.EDIT)
            if self._decision(row, "rejected", expected_revision):
                return self._summary(row, link)
            row.status = "rejected"
            row.reviewed_at = utcnow()
            row.reviewed_by_user_id = context.actor.user_id
            row.revision += 1
            self._audit(session, context, row)
            return self._summary(row, link)
