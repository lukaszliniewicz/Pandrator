"""Source document read projections without HTTP dependencies."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from .database import Database
from .http_serialization import model_payload as _model_dict
from .models import Artifact, Document, DocumentRevision, Segment, SessionRecord, TimedWord


def list_session_documents(database: Database, session_id: str) -> dict[str, Any]:
    with database.session() as db_session:
        if db_session.get(SessionRecord, session_id) is None:
            raise KeyError(session_id)
        documents = list(
            db_session.scalars(
                select(Document)
                .where(Document.session_id == session_id)
                .order_by(Document.created_at)
            ).all()
        )
        revision_artifacts = {
            str((item.metadata_json or {}).get("revision_id") or ""): item
            for item in db_session.scalars(
                select(Artifact).where(Artifact.session_id == session_id)
            ).all()
            if (item.metadata_json or {}).get("revision_id")
        }
        revisions_by_document: dict[str, list[tuple[DocumentRevision, int, int]]] = {}
        if documents:
            revision_rows = db_session.execute(
                select(DocumentRevision, func.count(Segment.id), func.max(Segment.end_ms))
                .outerjoin(Segment, Segment.revision_id == DocumentRevision.id)
                .where(
                    DocumentRevision.document_id.in_((document.id for document in documents)),
                    ~DocumentRevision.id.in_(
                        select(Segment.revision_id).where(Segment.node_kind == "logical_passage")
                    ),
                )
                .group_by(*DocumentRevision.__table__.columns)
                .order_by(DocumentRevision.document_id, DocumentRevision.revision_number.desc())
            ).all()
            for revision, segment_count, duration_ms in revision_rows:
                revisions_by_document.setdefault(revision.document_id, []).append(
                    (revision, int(segment_count or 0), int(duration_ms or 0))
                )
        items = []
        for document in documents:
            revision_items = []
            for revision, segment_count, duration_ms in revisions_by_document.get(document.id, []):
                artifact = revision_artifacts.get(revision.id)
                revision_items.append(
                    {
                        "id": revision.id,
                        "revision_number": revision.revision_number,
                        "parent_revision_id": revision.parent_revision_id,
                        "reviewed": revision.reviewed,
                        "content_hash": revision.content_hash,
                        "created_at": revision.created_at.isoformat(),
                        "segment_count": int(segment_count or 0),
                        "duration_ms": int(duration_ms or 0),
                        "artifact": _model_dict(
                            artifact,
                            (
                                "id",
                                "kind",
                                "role",
                                "relative_path",
                                "mime_type",
                                "size_bytes",
                                "state",
                                "metadata_json",
                                "created_at",
                            ),
                        )
                        if artifact
                        else None,
                    }
                )
            items.append(
                {
                    "id": document.id,
                    "stage": document.stage,
                    "language": document.language,
                    "active_revision_id": document.active_revision_id,
                    "created_at": document.created_at.isoformat(),
                    "revisions": revision_items,
                }
            )
        return {"items": items}


def list_revision_words(
    database: Database, revision_id: str, *, cursor: int, limit: int
) -> dict[str, Any]:
    with database.session() as db_session:
        if db_session.get(DocumentRevision, revision_id) is None:
            raise KeyError(revision_id)
        rows = list(
            db_session.scalars(
                select(TimedWord)
                .where(TimedWord.revision_id == revision_id, TimedWord.ordinal >= cursor)
                .order_by(TimedWord.ordinal)
                .limit(limit + 1)
            ).all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "items": [
                _model_dict(
                    word,
                    (
                        "id",
                        "revision_id",
                        "segment_id",
                        "ordinal",
                        "text",
                        "start_ms",
                        "end_ms",
                        "speaker",
                        "confidence",
                        "metadata_json",
                    ),
                )
                for word in rows
            ],
            "next_cursor": rows[-1].ordinal + 1 if rows and has_more else None,
        }
