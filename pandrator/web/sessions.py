"""Session repository with revision-safe updates."""

from __future__ import annotations

from sqlalchemy import select

from .database import Database
from .models import SessionRecord, utcnow


class RevisionConflict(RuntimeError):
    pass


class SessionService:
    def __init__(self, database: Database):
        self.database = database

    def list(self, *, include_trashed: bool = False) -> list[SessionRecord]:
        with self.database.session() as session:
            statement = select(SessionRecord).order_by(SessionRecord.updated_at.desc())
            if not include_trashed:
                statement = statement.where(SessionRecord.trashed_at.is_(None))
            records = list(session.scalars(statement).all())
            for record in records:
                session.expunge(record)
            return records

    def get(self, session_id: str) -> SessionRecord:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            session.expunge(record)
            return record

    def find_active_by_name(self, name: str) -> SessionRecord | None:
        """Return the newest active session whose display name matches exactly."""
        normalized = str(name or "").strip().casefold()
        if not normalized:
            return None
        with self.database.session() as session:
            records = list(
                session.scalars(
                    select(SessionRecord)
                    .where(SessionRecord.trashed_at.is_(None))
                    .order_by(SessionRecord.updated_at.desc())
                ).all()
            )
            record = next((item for item in records if str(item.name or "").strip().casefold() == normalized), None)
            if record is not None:
                session.expunge(record)
            return record

    def create(
        self,
        name: str,
        *,
        workflow_kind: str = "audiobook",
        source_language: str = "auto",
        target_language: str | None = None,
        workflow_preset: str = "custom",
        included_stages: list[str] | None = None,
    ) -> SessionRecord:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Session name is required.")
        record = SessionRecord(
            name=normalized_name,
            workflow_kind=workflow_kind,
            source_language=str(source_language or "auto").strip().lower(),
            target_language=str(target_language).strip().lower() if target_language else None,
            workflow_preset=workflow_preset,
            included_stages_json=list(included_stages or []),
        )
        with self.database.session() as session:
            session.add(record)
            session.flush()
            session.expunge(record)
        return record

    def update(self, session_id: str, revision: int, changes: dict) -> SessionRecord:
        allowed = {"name", "workflow_kind", "source_language", "target_language", "workflow_preset", "included_stages_json", "status"}
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            if record.revision != revision:
                raise RevisionConflict(f"Expected revision {revision}, found {record.revision}.")
            for key, value in changes.items():
                if key in allowed:
                    setattr(record, key, value)
            if not str(record.name or "").strip():
                raise ValueError("Session name is required.")
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            session.expunge(record)
            return record

    def trash(self, session_id: str, revision: int) -> SessionRecord:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            if record.revision != revision:
                raise RevisionConflict(f"Expected revision {revision}, found {record.revision}.")
            record.trashed_at = utcnow()
            record.status = "trashed"
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            session.expunge(record)
            return record

    def restore(self, session_id: str, revision: int) -> SessionRecord:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            if record.revision != revision:
                raise RevisionConflict(f"Expected revision {revision}, found {record.revision}.")
            record.trashed_at = None
            record.status = "idle"
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            session.expunge(record)
            return record

