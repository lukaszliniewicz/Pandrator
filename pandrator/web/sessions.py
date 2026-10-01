"""Session repository with revision-safe updates."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .database import Database
from .models import (
    AppSetting,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    TranslationProject,
    TranslationProjectBranch,
    utcnow,
)
from .multilingual_setup import (
    MultilingualSetup,
    deferred_source_outcome,
    read_setup,
    validate_source,
    write_setup,
)
from .outcome_plans import OutcomePlanService, derive_legacy_outcome


class RevisionConflict(RuntimeError):
    pass


class SessionService:
    def __init__(self, database: Database):
        self.database = database

    def list(
        self,
        *,
        include_trashed: bool = False,
        query: str | None = None,
    ) -> list[SessionRecord]:
        with self.database.session() as session:
            statement = select(SessionRecord).order_by(SessionRecord.updated_at.desc())
            if not include_trashed:
                statement = statement.where(SessionRecord.trashed_at.is_(None))
            if query and query.strip():
                term = f"%{query.strip()}%"
                statement = statement.where(
                    or_(
                        SessionRecord.name.ilike(term),
                        SessionRecord.id.ilike(term),
                        SessionRecord.source_language.ilike(term),
                        SessionRecord.target_language.ilike(term),
                    )
                )
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
        with self.database.session() as session:
            record = self.find_active_by_name_in_session(session, name)
            if record is not None:
                session.expunge(record)
            return record

    @staticmethod
    def find_active_by_name_in_session(
        session: Session,
        name: str,
    ) -> SessionRecord | None:
        normalized = str(name or "").strip().casefold()
        if not normalized:
            return None
        records = list(
            session.scalars(
                select(SessionRecord)
                .where(SessionRecord.trashed_at.is_(None))
                .order_by(SessionRecord.updated_at.desc())
            ).all()
        )
        return next(
            (
                item
                for item in records
                if str(item.name or "").strip().casefold()
                == normalized
            ),
            None,
        )

    def create(
        self,
        name: str,
        *,
        workflow_kind: str = "audiobook",
        source_language: str = "auto",
        target_language: str | None = None,
        workflow_preset: str = "custom",
        included_stages: list[str] | None = None,
        record_id: str | None = None,
        storage_key: str | None = None,
        db_session: Session | None = None,
        seed_voice_mode: bool = True,
        multilingual_setup: MultilingualSetup | dict | None = None,
    ) -> SessionRecord:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Session name is required.")
        setup = MultilingualSetup.model_validate(multilingual_setup) if multilingual_setup is not None else None
        stages = list(included_stages or [])
        if setup is not None:
            stages = validate_source(
                setup, workflow_kind=workflow_kind, source_language=source_language,
                target_language=target_language, included_stages=stages,
            )
        record = SessionRecord(
            **({"id": record_id} if record_id else {}),
            **({"storage_key": storage_key} if storage_key else {}),
            name=normalized_name,
            workflow_kind=workflow_kind,
            source_language=str(source_language or "auto").strip().lower(),
            target_language=str(target_language).strip().lower() if target_language else None,
            workflow_preset=workflow_preset,
            included_stages_json=stages,
        )
        if db_session is not None:
            db_session.add(record)
            db_session.flush()
            if setup is not None:
                write_setup(db_session, record.id, setup)
                db_session.add(OutcomePlan(
                    session_id=record.id,
                    value_json=deferred_source_outcome(
                        derive_legacy_outcome(record), workflow_kind=record.workflow_kind,
                    ),
                ))
            if seed_voice_mode and workflow_kind in {"audiobook", "voiceover"}:
                db_session.add(SessionSetting(session_id=record.id, section="tts", value_json={"voice_mode_version": 1}, revision=0))
            return record
        with self.database.session() as session:
            session.add(record)
            session.flush()
            if setup is not None:
                write_setup(session, record.id, setup)
                session.add(OutcomePlan(
                    session_id=record.id,
                    value_json=deferred_source_outcome(
                        derive_legacy_outcome(record), workflow_kind=record.workflow_kind,
                    ),
                ))
            if seed_voice_mode and workflow_kind in {"audiobook", "voiceover"}:
                session.add(SessionSetting(session_id=record.id, section="tts", value_json={"voice_mode_version": 1}, revision=0))
            session.expunge(record)
        return record

    def update(
        self,
        session_id: str,
        revision: int,
        changes: dict,
        *,
        db_session: Session | None = None,
    ) -> SessionRecord:
        if db_session is not None:
            return self.update_in_session(
                db_session,
                session_id,
                revision,
                changes,
            )
        with self.database.immediate_session() as session:
            record = self.update_in_session(
                session,
                session_id,
                revision,
                changes,
            )
            session.expunge(record)
            return record

    @staticmethod
    def update_in_session(
        session: Session,
        session_id: str,
        revision: int,
        changes: dict,
    ) -> SessionRecord:
        allowed = {"name", "workflow_kind", "source_language", "target_language", "workflow_preset", "included_stages_json", "status"}
        record = SessionService._locked_current(session, session_id)
        if record.revision != revision:
            raise RevisionConflict(
                f"Expected revision {revision}, found {record.revision}."
            )
        if record.status == "purging":
            raise ValueError("Session purge has already started.")
        if changes.get("status") == "purging":
            raise ValueError("Purging is a reserved session lifecycle state.")
        current_setup = read_setup(session, record.id)
        setup_changed = "multilingual_setup" in changes
        workflow_change_requested = (
            "workflow_kind" in changes or "included_stages_json" in changes
        )
        if setup_changed and (
            session.scalar(select(TranslationProject.id).where(TranslationProject.source_session_id == record.id))
            or session.scalar(select(TranslationProjectBranch.id).where(TranslationProjectBranch.session_id == record.id))
        ):
            raise ValueError("A project source or branch cannot change multilingual setup.")
        setup = (
            MultilingualSetup.model_validate(changes["multilingual_setup"])
            if setup_changed and changes["multilingual_setup"] is not None
            else None if setup_changed else current_setup
        )
        effective_kind = changes.get("workflow_kind", record.workflow_kind)
        effective_source = changes.get("source_language", record.source_language)
        effective_target = changes.get("target_language", record.target_language)
        effective_stages = list(changes.get("included_stages_json", record.included_stages_json) or [])
        if setup is not None:
            changes = dict(changes)
            changes["included_stages_json"] = validate_source(
                setup, workflow_kind=effective_kind, source_language=effective_source,
                target_language=effective_target, included_stages=effective_stages,
            )
        for key, value in changes.items():
            if key in allowed:
                setattr(record, key, value)
        if not str(record.name or "").strip():
            raise ValueError("Session name is required.")
        if setup is not None and (
            current_setup is None
            or workflow_change_requested
        ):
            plan = session.get(OutcomePlan, record.id)
            current_value = dict(plan.value_json or {}) if plan else derive_legacy_outcome(record)
            deferred = deferred_source_outcome(current_value, workflow_kind=record.workflow_kind)
            if plan is None or deferred != current_value:
                OutcomePlanService.update_in_session(
                    session, record.id, plan.revision if plan else 0,
                    deferred, sync_session=False,
                )
        record.revision += 1
        record.updated_at = utcnow()
        if setup_changed:
            write_setup(session, record.id, setup)
        session.flush()
        return record

    def trash(
        self,
        session_id: str,
        revision: int,
        *,
        db_session: Session | None = None,
    ) -> SessionRecord:
        if db_session is not None:
            return self.trash_in_session(
                db_session,
                session_id,
                revision,
            )
        with self.database.immediate_session() as session:
            record = self.trash_in_session(
                session,
                session_id,
                revision,
            )
            session.expunge(record)
            return record

    @staticmethod
    def trash_in_session(
        session: Session,
        session_id: str,
        revision: int,
    ) -> SessionRecord:
        record = SessionService._locked_current(session, session_id)
        if record.revision != revision:
            raise RevisionConflict(
                f"Expected revision {revision}, found {record.revision}."
            )
        if record.status == "purging":
            raise ValueError("Session purge has already started.")
        trashed_at = utcnow()
        record.trashed_at = trashed_at
        policy = session.get(AppSetting, "session.trash_policy")
        days = (policy.value_json or {}).get("days") if policy else None
        record.purge_after = trashed_at + timedelta(days=days) if days is not None else None
        record.status = "trashed"
        record.revision += 1
        record.updated_at = utcnow()
        session.flush()
        return record

    @staticmethod
    def _locked_current(session: Session, session_id: str) -> SessionRecord:
        """Reserve the SQLite writer and discard any cached pre-claim state."""
        with session.no_autoflush:
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.id == session_id)
                .values(id=SessionRecord.id)
                .execution_options(synchronize_session=False)
            )
            record = session.get(SessionRecord, session_id, populate_existing=True)
            if record is None:
                raise KeyError(session_id)
            return record

    def restore(self, session_id: str, revision: int) -> SessionRecord:
        with self.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            if record.revision != revision:
                raise RevisionConflict(f"Expected revision {revision}, found {record.revision}.")
            if record.status == "purging":
                raise ValueError("Session purge has already started.")
            record.trashed_at = None
            record.purge_after = None
            record.status = "idle"
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            session.expunge(record)
            return record
