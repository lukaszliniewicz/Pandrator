"""Reusable source assets, session attachments and source lifecycle operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .artifact_selection import select_source_path
from .database import Database
from .models import (
    Artifact,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    SourceRecord,
    utcnow,
)
from .settings_policy import RevisionConflict
from .source_resolution import classify_source, resolve_media_source


class SourceLibraryService:
    def __init__(self, database: Database, artifacts=None):
        self.database = database
        self.artifacts = artifacts

    def _managed_source_path(self, relative_path: str | None) -> str | None:
        if not relative_path or self.artifacts is None:
            return None
        try:
            return str(self.artifacts.paths.managed_path(relative_path))
        except (OSError, ValueError):
            # Invalid legacy paths must not break the entire Sources tab.
            return None

    @staticmethod
    def _asset_payload(
        asset: SourceAsset,
        *,
        managed_path: str | None = None,
        reference_count: int = 0,
        current_reference_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "id": asset.id,
            "artifact_id": asset.artifact_id,
            "display_name": asset.display_name,
            "kind": asset.kind,
            "mime_type": asset.mime_type,
            "external_path": asset.external_path,
            "path": managed_path,
            "size_bytes": asset.size_bytes,
            "content_hash": asset.content_hash,
            "state": asset.state,
            "metadata": asset.metadata_json,
            "revision": asset.revision,
            "reference_count": reference_count,
            "current_reference_count": current_reference_count,
            "created_at": asset.created_at.isoformat(),
            "updated_at": asset.updated_at.isoformat(),
        }

    def ensure_for_artifact(
        self,
        artifact_id: str,
        *,
        display_name: str | None = None,
        kind: str | None = None,
    ) -> SourceAsset:
        with self.database.immediate_session() as session:
            asset = self.ensure_for_artifact_in_session(
                session,
                artifact_id,
                display_name=display_name,
                kind=kind,
            )
            session.expunge(asset)
            return asset

    @staticmethod
    def ensure_for_artifact_in_session(
        session: Session,
        artifact_id: str,
        *,
        display_name: str | None = None,
        kind: str | None = None,
    ) -> SourceAsset:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        asset = session.scalar(
            select(SourceAsset).where(SourceAsset.artifact_id == artifact_id)
        )
        if asset is None:
            name = display_name or str(
                (artifact.metadata_json or {}).get("original_filename")
                or Path(artifact.relative_path).name
            )
            asset = SourceAsset(
                artifact_id=artifact.id,
                display_name=name,
                kind=kind or Path(name).suffix.lower().lstrip(".") or artifact.kind,
                mime_type=artifact.mime_type,
                size_bytes=artifact.size_bytes,
                content_hash=artifact.content_hash,
                metadata_json={"legacy_session_id": artifact.session_id}
                if artifact.session_id
                else {},
            )
            session.add(asset)
            session.flush()
        return asset

    def attach(
        self,
        session_id: str,
        source_asset_id: str,
        *,
        role: str = "primary",
        expected_session_revision: int | None = None,
        db_session: Session | None = None,
    ) -> dict[str, Any]:
        if db_session is not None:
            return self.attach_in_session(
                db_session,
                session_id,
                source_asset_id,
                role=role,
                expected_session_revision=(expected_session_revision),
            )
        with self.database.immediate_session() as session:
            return self.attach_in_session(
                session,
                session_id,
                source_asset_id,
                role=role,
                expected_session_revision=(expected_session_revision),
            )

    def attach_in_session(
        self,
        session: Session,
        session_id: str,
        source_asset_id: str,
        *,
        role: str = "primary",
        expected_session_revision: int | None = None,
    ) -> dict[str, Any]:
        session_record = session.get(SessionRecord, session_id)
        asset = session.get(SourceAsset, source_asset_id)
        if session_record is None or asset is None:
            raise KeyError(session_id)
        if asset.state == "trashed":
            raise ValueError("Restore this source before attaching it to a session.")
        if (
            expected_session_revision is not None
            and session_record.revision != expected_session_revision
        ):
            raise RevisionConflict(
                "The session changed before its source was attached."
            )
        if role == "media" and classify_source(
            name=asset.display_name, kind=asset.kind, mime_type=asset.mime_type or ""
        ) not in {"audio", "video"}:
            raise ValueError("A media target must be an audio or video source, not subtitle text.")
        if role == "media":
            from .source_management import (
                _invalidate_recording_outputs,
                assert_session_idle,
            )

            assert_session_idle(session, session_id)
            previous_media = resolve_media_source(session, session_id)
            previous_id = previous_media.artifact.id if previous_media.artifact and previous_media.has_audio else None
            if previous_id != asset.artifact_id:
                _invalidate_recording_outputs(session, session_id, previous_id)
                timing = session.get(SessionSetting, (session_id, "_recording_timing_review"))
                if timing is None:
                    timing = SessionSetting(session_id=session_id, section="_recording_timing_review", value_json={})
                    session.add(timing)
                timing.value_json = {
                    "required": bool(previous_id or (timing.value_json or {}).get("required")),
                    "media_artifact_id": asset.artifact_id,
                }
                timing.revision = (timing.revision or 0) + 1
        for current in session.scalars(
            select(SessionSource).where(
                SessionSource.session_id == session_id,
                SessionSource.role == role,
                SessionSource.is_current.is_(True),
            )
        ).all():
            current.is_current = False
            current.revision += 1
            current.updated_at = utcnow()
        attachment = session.scalar(
            select(SessionSource).where(
                SessionSource.session_id == session_id,
                SessionSource.source_asset_id == source_asset_id,
                SessionSource.role == role,
            )
        )
        if attachment is None:
            attachment = SessionSource(
                session_id=session_id,
                source_asset_id=source_asset_id,
                role=role,
            )
            session.add(attachment)
        else:
            attachment.is_current = True
            attachment.revision += 1
            attachment.updated_at = utcnow()
        subtitle_revision = None
        if role == "primary":
            if self.artifacts is not None and str(asset.kind).lower() in {"srt", "vtt"}:
                from .subtitle_sources import adopt_subtitle_source_in_session

                if asset.artifact_id is None:
                    raise KeyError(asset.artifact_id)
                subtitle_revision = adopt_subtitle_source_in_session(
                    session, self.artifacts, session_id, asset.artifact_id
                )
            select_source_path(session, session_id, asset.artifact_id)
        if expected_session_revision is not None:
            session_record.revision += 1
            session_record.updated_at = utcnow()
        session.flush()
        return {
            "id": attachment.id,
            "session_id": session_id,
            "source_asset_id": source_asset_id,
            "subtitle_revision": subtitle_revision,
            "role": role,
            "is_current": True,
            "revision": attachment.revision,
            "session_revision": session_record.revision,
        }

    def adopt_subtitles(
        self, session_id: str, source_asset_id: str, *, expected_session_revision: int | None = None
    ) -> dict[str, Any]:
        """Recover an attached subtitle source without another upload."""
        from .subtitle_sources import adopt_subtitle_source_in_session

        if self.artifacts is None:
            raise ValueError("Subtitle adoption requires the managed artifact service.")
        with self.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            asset = session.get(SourceAsset, source_asset_id)
            attachment = session.scalar(select(SessionSource).where(
                SessionSource.session_id == session_id,
                SessionSource.source_asset_id == source_asset_id,
                SessionSource.role == "primary",
                SessionSource.is_current.is_(True),
            ))
            if record is None or asset is None or attachment is None:
                raise KeyError(source_asset_id)
            if expected_session_revision is not None and record.revision != expected_session_revision:
                raise RevisionConflict("The session changed before its subtitle source was adopted.")
            if asset.artifact_id is None:
                raise KeyError(asset.artifact_id)
            result = adopt_subtitle_source_in_session(session, self.artifacts, session_id, asset.artifact_id)
            select_source_path(session, session_id, asset.artifact_id)
            if not result["reused"]:
                record.revision += 1
                record.updated_at = utcnow()
            result["session_revision"] = record.revision
            result["source_asset_id"] = source_asset_id
            return result

    def detach(
        self, session_id: str, attachment_id: str, expected_revision: int
    ) -> None:
        with self.database.immediate_session() as session:
            attachment = session.get(SessionSource, attachment_id)
            if attachment is None or attachment.session_id != session_id:
                raise KeyError(attachment_id)
            if attachment.revision != expected_revision:
                raise RevisionConflict(
                    "The source attachment changed in another client."
                )
            if attachment.role == "primary" and attachment.is_current:
                select_source_path(session, session_id, None)
            session.delete(attachment)

    def rename(
        self, source_asset_id: str, expected_revision: int, display_name: str
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            asset = session.get(SourceAsset, source_asset_id)
            if asset is None:
                raise KeyError(source_asset_id)
            if asset.revision != expected_revision:
                raise RevisionConflict("The source asset changed in another client.")
            asset.display_name = display_name.strip()
            asset.revision += 1
            asset.updated_at = utcnow()
            session.flush()
            references = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(SessionSource.source_asset_id == asset.id)
                )
                or 0
            )
            current = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(
                        SessionSource.source_asset_id == asset.id,
                        SessionSource.is_current.is_(True),
                    )
                )
                or 0
            )
            return self._asset_payload(
                asset, reference_count=references, current_reference_count=current
            )

    def set_state(
        self, source_asset_id: str, expected_revision: int, state: str
    ) -> dict[str, Any]:
        if state not in {"current", "trashed"}:
            raise ValueError("Unsupported source lifecycle state.")
        with self.database.immediate_session() as session:
            asset = session.get(SourceAsset, source_asset_id)
            if asset is None:
                raise KeyError(source_asset_id)
            if asset.revision != expected_revision:
                raise RevisionConflict("The source asset changed in another client.")
            references = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(SessionSource.source_asset_id == asset.id)
                )
                or 0
            )
            current = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(
                        SessionSource.source_asset_id == asset.id,
                        SessionSource.is_current.is_(True),
                    )
                )
                or 0
            )
            if state == "trashed" and references:
                raise ValueError(
                    f"Detach this source from {references} session attachment(s) before moving it to trash."
                )
            asset.state = state
            asset.revision += 1
            asset.updated_at = utcnow()
            session.flush()
            return self._asset_payload(
                asset, reference_count=references, current_reference_count=current
            )

    def list(
        self,
        *,
        session_id: str | None = None,
        include_trashed: bool = False,
        view: str = "full",
    ) -> list[dict[str, Any]]:
        if view not in {"full", "compact"}:
            raise ValueError("Source view must be full or compact.")
        with self.database.session() as session:
            if view == "compact":
                return self._compact_list(session, session_id, include_trashed)
            if session_id:
                rows = session.execute(
                    select(SessionSource, SourceAsset, Artifact.relative_path)
                    .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
                    .outerjoin(Artifact, Artifact.id == SourceAsset.artifact_id)
                    .where(SessionSource.session_id == session_id)
                    .order_by(SessionSource.updated_at.desc())
                ).all()
                asset_ids = {asset.id for _link, asset, _path in rows}
                counts = (
                    {
                        asset_id: count
                        for asset_id, count in session.execute(
                            select(SessionSource.source_asset_id, func.count())
                            .where(SessionSource.source_asset_id.in_(asset_ids))
                            .group_by(SessionSource.source_asset_id)
                        ).all()
                    }
                    if asset_ids
                    else {}
                )
                current_counts = (
                    {
                        asset_id: count
                        for asset_id, count in session.execute(
                            select(SessionSource.source_asset_id, func.count())
                            .where(
                                SessionSource.source_asset_id.in_(asset_ids),
                                SessionSource.is_current.is_(True),
                            )
                            .group_by(SessionSource.source_asset_id)
                        ).all()
                    }
                    if asset_ids
                    else {}
                )
                return [
                    self._asset_payload(
                        asset,
                        managed_path=self._managed_source_path(relative_path),
                        reference_count=int(counts.get(asset.id, 0)),
                        current_reference_count=int(current_counts.get(asset.id, 0)),
                    )
                    | {
                        "attachment": {
                            "id": link.id,
                            "role": link.role,
                            "is_current": link.is_current,
                            "revision": link.revision,
                        }
                    }
                    for link, asset, relative_path in rows
                ]
            statement = select(SourceAsset).order_by(SourceAsset.updated_at.desc())
            if not include_trashed:
                statement = statement.where(SourceAsset.state != "trashed")
            assets = list(session.scalars(statement).all())
            asset_ids = [asset.id for asset in assets]
            counts = {
                asset_id: count
                for asset_id, count in session.execute(
                    select(SessionSource.source_asset_id, func.count())
                    .where(SessionSource.source_asset_id.in_(asset_ids))
                    .group_by(SessionSource.source_asset_id)
                ).all()
            }
            current_counts = {
                asset_id: count
                for asset_id, count in session.execute(
                    select(SessionSource.source_asset_id, func.count())
                    .where(
                        SessionSource.is_current.is_(True),
                        SessionSource.source_asset_id.in_(asset_ids),
                    )
                    .group_by(SessionSource.source_asset_id)
                ).all()
            }
            return [
                self._asset_payload(
                    asset,
                    reference_count=int(counts.get(asset.id, 0)),
                    current_reference_count=int(current_counts.get(asset.id, 0)),
                )
                for asset in assets
            ]

    @staticmethod
    def _compact_list(
        session: Session, session_id: str | None, include_trashed: bool
    ) -> list[dict[str, Any]]:
        fields = (
            "id",
            "artifact_id",
            "display_name",
            "kind",
            "mime_type",
            "size_bytes",
            "content_hash",
            "state",
            "revision",
            "created_at",
            "updated_at",
        )
        statement = select(*(getattr(SourceAsset, key) for key in fields))
        if session_id:
            statement = (
                statement.add_columns(
                    SessionSource.id.label("attachment_id"),
                    SessionSource.role,
                    SessionSource.is_current,
                    SessionSource.revision.label("attachment_revision"),
                )
                .join(SessionSource, SessionSource.source_asset_id == SourceAsset.id)
                .where(SessionSource.session_id == session_id)
                .order_by(SessionSource.updated_at.desc(), SessionSource.id)
            )
        else:
            statement = statement.order_by(SourceAsset.updated_at.desc(), SourceAsset.id)
            if not include_trashed:
                statement = statement.where(SourceAsset.state != "trashed")
        rows = session.execute(statement).mappings().all()
        ids = {row["id"] for row in rows}
        counts = (
            {
                row[0]: (int(row[1]), int(row[2]))
                for row in session.execute(
                    select(
                        SessionSource.source_asset_id,
                        func.count(),
                        func.sum(case((SessionSource.is_current.is_(True), 1), else_=0)),
                    )
                    .where(SessionSource.source_asset_id.in_(ids))
                    .group_by(SessionSource.source_asset_id)
                )
            }
            if ids
            else {}
        )
        items = []
        for row in rows:
            item = {key: row[key] for key in fields}
            for key in ("created_at", "updated_at"):
                item[key] = item[key].isoformat()
            item["reference_count"], item["current_reference_count"] = counts.get(row["id"], (0, 0))
            if session_id:
                item["attachment"] = {
                    "id": row["attachment_id"],
                    "role": row["role"],
                    "is_current": row["is_current"],
                    "revision": row["attachment_revision"],
                }
            items.append(item)
        return items

    def references(
        self, source_asset_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Reference limit must be 1–100 and offset must not be negative.")
        with self.database.session() as session:
            if (
                session.scalar(select(SourceAsset.id).where(SourceAsset.id == source_asset_id))
                is None
            ):
                raise KeyError(source_asset_id)
            predicate = SessionSource.source_asset_id == source_asset_id
            total = int(
                session.scalar(select(func.count()).select_from(SessionSource).where(predicate))
                or 0
            )
            rows = session.execute(
                select(
                    SessionSource.id.label("attachment_id"),
                    SessionRecord.id.label("session_id"),
                    SessionRecord.name.label("session_name"),
                    SessionRecord.workflow_kind,
                    SessionRecord.source_language,
                    SessionRecord.target_language,
                    SessionRecord.status,
                    SessionSource.role,
                    SessionSource.is_current,
                    SessionSource.updated_at,
                    SessionRecord.trashed_at,
                )
                .join(SessionRecord, SessionRecord.id == SessionSource.session_id)
                .where(predicate)
                .order_by(
                    SessionSource.is_current.desc(),
                    SessionSource.updated_at.desc(),
                    SessionRecord.name,
                    SessionRecord.id,
                    SessionSource.id,
                )
                .limit(limit)
                .offset(offset)
            ).mappings()
            items = []
            for row in rows:
                item = dict(row)
                item["updated_at"] = row["updated_at"].isoformat()
                if item.pop("trashed_at") is not None:
                    item["status"] = "trashed"
                items.append(item)
            return {
                "items": items,
                "total": total,
                "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
            }

    def backfill_legacy(self) -> int:
        """Deprecated compatibility hook.

        Migration ``0022_source_asset_backfill`` owns this promotion for normal
        installations.  The method remains temporarily available to older
        integrations, but application startup must not call it.
        """
        created = 0
        with self.database.session() as session:
            records = list(
                session.scalars(
                    select(SourceRecord).where(SourceRecord.artifact_id.is_not(None))
                ).all()
            )
            for legacy in records:
                artifact = session.get(Artifact, legacy.artifact_id)
                if artifact is None:
                    continue
                asset = session.scalar(
                    select(SourceAsset).where(SourceAsset.artifact_id == artifact.id)
                )
                if asset is None:
                    asset = SourceAsset(
                        artifact_id=artifact.id,
                        display_name=legacy.display_name,
                        kind=legacy.kind,
                        mime_type=artifact.mime_type,
                        size_bytes=artifact.size_bytes,
                        content_hash=artifact.content_hash,
                        metadata_json={"legacy_source_id": legacy.id},
                    )
                    session.add(asset)
                    session.flush()
                    created += 1
                link = session.scalar(
                    select(SessionSource).where(
                        SessionSource.session_id == legacy.session_id,
                        SessionSource.source_asset_id == asset.id,
                        SessionSource.role == "primary",
                    )
                )
                if link is None:
                    session.add(
                        SessionSource(
                            session_id=legacy.session_id,
                            source_asset_id=asset.id,
                            role="primary",
                        )
                    )
        return created
