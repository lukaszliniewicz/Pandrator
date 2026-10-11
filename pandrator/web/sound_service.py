"""Facade for the global, session-independent sound library domain."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from pandrator.logic.audio_cpp_sfx import sound_catalogue
from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .jobs import JobQueue
from .sound_generation import (
    PreparedSoundGeneration,
    SettingsProvider,
    admit_generation_in_session,
    run_generate,
    runtime_settings,
)
from .sound_generation import (
    prepare_generation as prepare_sound_generation,
)
from .sound_imports import (
    PreparedSoundImport,
    cleanup_prepared_import,
)
from .sound_imports import (
    import_audio as import_sound_audio,
)
from .sound_imports import (
    import_in_session as import_sound_in_session,
)
from .sound_imports import (
    prepare_import as prepare_sound_import,
)
from .sound_library_commands import (
    discard_in_session as discard_sound_in_session,
)
from .sound_library_commands import (
    save_in_session as save_sound_in_session,
)
from .sound_library_commands import (
    update_in_session as update_sound_in_session,
)
from .sound_library_queries import (
    get_candidate as query_candidate,
)
from .sound_library_queries import (
    get_sound as query_sound,
)
from .sound_library_queries import (
    list_candidates as query_candidates,
)
from .sound_library_queries import (
    list_sounds as query_sounds,
)


class SoundService:
    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        artifacts: ArtifactService,
        jobs: JobQueue,
        *,
        settings_provider: SettingsProvider | None = None,
    ) -> None:
        self.database = database
        self.paths = paths
        self.artifacts = artifacts
        self.jobs = jobs
        self.settings_provider = settings_provider

    def _runtime_settings(self) -> dict[str, Any]:
        return runtime_settings(self.settings_provider)

    def catalogue(self) -> dict[str, Any]:
        return sound_catalogue(self._runtime_settings())

    def prepare_generation(self, payload: dict[str, Any]) -> PreparedSoundGeneration:
        return prepare_sound_generation(payload, self._runtime_settings())

    def generate_in_session(
        self,
        session: Session,
        payload: dict[str, Any],
        *,
        prepared: PreparedSoundGeneration | None = None,
    ) -> dict[str, Any]:
        prepared_generation = prepared or self.prepare_generation(payload)
        return admit_generation_in_session(
            session,
            self.jobs,
            self.paths,
            payload,
            prepared_generation,
        )

    def list_sounds(
        self,
        query: str = "",
        tag: str | None = None,
        cursor: str | None = None,
        limit: int = 40,
        archived: bool = False,
    ) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            return query_sounds(
                session,
                query=query,
                tag=tag,
                cursor=cursor,
                limit=limit,
                archived=archived,
            )

    def get_sound(
        self,
        item_id: str,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            return query_sound(session, item_id, cursor=cursor, limit=limit)

    def list_candidates(
        self,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            return query_candidates(session, cursor=cursor, limit=limit)

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self.database.snapshot_session() as session:
            return query_candidate(session, candidate_id)

    def save_in_session(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
        return save_sound_in_session(session, self.paths, payload)

    def update_in_session(
        self,
        session: Session,
        item_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return update_sound_in_session(session, item_id, payload)

    def discard_in_session(self, session: Session, candidate_id: str) -> dict[str, Any]:
        return discard_sound_in_session(session, candidate_id)

    def prepare_import(
        self,
        staged_path: Path,
        display_name: str,
    ) -> PreparedSoundImport:
        return prepare_sound_import(
            self.paths,
            self.artifacts,
            staged_path,
            display_name,
        )

    def cleanup_prepared_import(self, prepared: PreparedSoundImport) -> None:
        cleanup_prepared_import(prepared)

    def import_in_session(
        self,
        session: Session,
        prepared: PreparedSoundImport,
    ) -> dict[str, Any]:
        return import_sound_in_session(session, self.paths, self.artifacts, prepared)

    def import_audio(
        self,
        path: Path,
        display_name: str,
        *,
        db_session: Session | None = None,
    ) -> dict[str, Any]:
        return import_sound_audio(
            self.database,
            self.paths,
            self.artifacts,
            path,
            display_name,
            db_session=db_session,
        )

    def run_generate(
        self,
        payload: dict[str, Any],
        progress,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        return run_generate(
            self.database,
            self.jobs,
            self.paths,
            self.artifacts,
            payload,
            progress,
            cancel_event,
            settings_provider=self.settings_provider,
        )


__all__ = ["PreparedSoundGeneration", "PreparedSoundImport", "SoundService"]
