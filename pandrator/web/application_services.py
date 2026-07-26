"""Construction and ownership of backend application services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .auth import AuthService, BootstrapTokenStore, LoginThrottle
from .capabilities import CapabilityService, crispasr_install_preferences
from .database import Database
from .jobs import JobQueue
from .legacy_migration import import_legacy_data
from .models import AppSetting, SessionRecord
from .pronunciations import PronunciationLibrary
from .sessions import SessionService
from .startup import StartupMaintenance
from .subtitle_review import SubtitleReviewService
from .tts_providers import TtsCatalogueService, TtsProviderRegistry
from .uploads import ChunkUploadService
from .workflow_handlers import WorkflowHandlers
from .workflows import WorkflowService
from .workspace import (
    GenerationService,
    OutcomePlanService,
    SourceLibraryService,
    WorkspaceSettingsService,
)


@dataclass(slots=True)
class ApplicationServices:
    """The explicitly constructed dependency graph for one Flask application."""

    paths: DataPaths
    migration: dict[str, Any]
    database: Database
    auth: AuthService
    login_throttle: LoginThrottle
    capabilities: CapabilityService
    jobs: JobQueue
    sessions: SessionService
    artifacts: ArtifactService
    workflows: WorkflowService
    workflow_handlers: WorkflowHandlers
    tts_providers: TtsProviderRegistry
    tts_catalogue: TtsCatalogueService
    workspace_settings: WorkspaceSettingsService
    outcome_plans: OutcomePlanService
    source_library: SourceLibraryService
    generation: GenerationService
    pronunciations: PronunciationLibrary
    chunk_uploads: ChunkUploadService
    startup_maintenance: StartupMaintenance
    subtitle_review: SubtitleReviewService
    bootstrap: BootstrapTokenStore
    session_directory: Callable[[str], Path]

    @classmethod
    def build(
        cls,
        *,
        data_root: str | os.PathLike[str] | None = None,
        bootstrap_tokens: BootstrapTokenStore | None = None,
        capability_ttl_seconds: int | None = None,
    ) -> ApplicationServices:
        """Build services once, in dependency order, without Flask request state."""

        paths = DataPaths.from_value(data_root).ensure()
        migration = import_legacy_data(paths)
        database = Database(paths.database)

        stt_preferences = crispasr_install_preferences(paths)
        if stt_preferences["configured"]:
            with database.session() as settings_session:
                if settings_session.get(AppSetting, "defaults.stt") is None:
                    settings_session.add(
                        AppSetting(
                            key="defaults.stt",
                            value_json={
                                "stt_engine": stt_preferences["engine"],
                                "stt_model_quantization": stt_preferences["quantization"],
                            },
                        )
                    )

        auth = AuthService(database)
        login_throttle = LoginThrottle()
        capabilities = CapabilityService(
            database,
            paths,
            ttl_seconds=capability_ttl_seconds,
        )
        jobs = JobQueue(database)
        sessions = SessionService(database)
        artifacts = ArtifactService(database, paths)
        workflows = WorkflowService(database, jobs)
        tts_providers = TtsProviderRegistry()
        workflow_handlers = WorkflowHandlers(
            database,
            paths,
            tts_providers=tts_providers,
        )
        tts_catalogue = TtsCatalogueService(
            database,
            paths,
            tts_providers,
        )
        workspace_settings = WorkspaceSettingsService(database)
        outcome_plans = OutcomePlanService(database)
        source_library = SourceLibraryService(database)
        generation = GenerationService(
            database,
            jobs,
            workspace_settings,
            artifacts,
            plan_refresher=workflow_handlers.refresh_generation_plan,
        )
        pronunciations = PronunciationLibrary(database)
        chunk_uploads = ChunkUploadService(
            database,
            paths,
            artifacts,
            source_library,
        )
        startup_maintenance = StartupMaintenance(
            database,
            paths,
            chunk_uploads,
        )

        def session_directory(session_id: str) -> Path:
            with database.session() as db_session:
                record = db_session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                destination = paths.sessions / record.storage_key
            destination.mkdir(parents=True, exist_ok=True)
            return destination

        subtitle_review = SubtitleReviewService(
            database,
            artifacts,
            session_directory,
        )
        return cls(
            paths=paths,
            migration=migration,
            database=database,
            auth=auth,
            login_throttle=login_throttle,
            capabilities=capabilities,
            jobs=jobs,
            sessions=sessions,
            artifacts=artifacts,
            workflows=workflows,
            workflow_handlers=workflow_handlers,
            tts_providers=tts_providers,
            tts_catalogue=tts_catalogue,
            workspace_settings=workspace_settings,
            outcome_plans=outcome_plans,
            source_library=source_library,
            generation=generation,
            pronunciations=pronunciations,
            chunk_uploads=chunk_uploads,
            startup_maintenance=startup_maintenance,
            subtitle_review=subtitle_review,
            bootstrap=bootstrap_tokens or BootstrapTokenStore(),
            session_directory=session_directory,
        )

    def extension_mapping(self) -> dict[str, Any]:
        """Return the stable test/plugin surface exposed through Flask."""

        return {
            "paths": self.paths,
            "database": self.database,
            "auth": self.auth,
            "login_throttle": self.login_throttle,
            "capabilities": self.capabilities,
            "jobs": self.jobs,
            "sessions": self.sessions,
            "artifacts": self.artifacts,
            "workflows": self.workflows,
            "workflow_handlers": self.workflow_handlers,
            "tts_providers": self.tts_providers,
            "tts_catalogue": self.tts_catalogue,
            "workspace_settings": self.workspace_settings,
            "outcome_plans": self.outcome_plans,
            "source_library": self.source_library,
            "generation": self.generation,
            "pronunciations": self.pronunciations,
            "chunk_uploads": self.chunk_uploads,
            "startup_maintenance": self.startup_maintenance,
            "subtitle_review": self.subtitle_review,
            "bootstrap": self.bootstrap,
            "migration": self.migration,
            "services": self,
        }
