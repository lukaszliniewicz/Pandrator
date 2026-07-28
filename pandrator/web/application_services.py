"""Construction and ownership of backend application services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .audit import AuditService
from .auth import AuthService, BootstrapTokenStore, LoginThrottle
from .automation_enrollment import AutomationEnrollmentService
from .capabilities import CapabilityService, crispasr_install_preferences
from .credentials import SecretRedactor
from .database import Database
from .idempotency import IdempotencyService
from .identity import ApplicationIdentityService
from .jobs import JobQueue
from .legacy_migration import import_legacy_data
from .manager_proxy import LocalManagerProxy
from .models import AppSetting, SessionRecord
from .pronunciations import PronunciationLibrary
from .sessions import SessionService
from .startup import StartupMaintenance
from .subtitle_review import SubtitleReviewService
from .tts_providers import TtsCatalogueService, TtsProviderRegistry
from .uploads import ChunkUploadService
from .work import WorkService
from .workflow_handlers import WorkflowHandlers
from .workflow_plans import WorkflowExecutionPlanService
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
    automation_enrollment: AutomationEnrollmentService
    login_throttle: LoginThrottle
    capabilities: CapabilityService
    redactor: SecretRedactor
    audit: AuditService
    idempotency: IdempotencyService
    jobs: JobQueue
    work: WorkService
    identity: ApplicationIdentityService
    sessions: SessionService
    artifacts: ArtifactService
    workflows: WorkflowService
    workflow_plans: WorkflowExecutionPlanService
    workflow_handlers: WorkflowHandlers
    manager_bridge: LocalManagerProxy
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
        public_origin: str | None = None,
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
        automation_enrollment = AutomationEnrollmentService(database, auth)
        login_throttle = LoginThrottle()
        capabilities = CapabilityService(
            database,
            paths,
            ttl_seconds=capability_ttl_seconds,
        )
        redactor = SecretRedactor(database, paths)
        audit = AuditService(database, redactor)
        idempotency = IdempotencyService(database, redactor)
        jobs = JobQueue(database, secret_redactor=redactor)
        work = WorkService(jobs, redactor)
        identity = ApplicationIdentityService(
            database,
            public_origin=public_origin,
        )
        sessions = SessionService(database)
        artifacts = ArtifactService(database, paths)
        workflows = WorkflowService(database, jobs)
        workflow_plans = WorkflowExecutionPlanService(
            database,
            workflows,
            jobs,
            work,
            idempotency,
        )
        manager_bridge = LocalManagerProxy()
        tts_providers = TtsProviderRegistry()
        workflow_handlers = WorkflowHandlers(
            database,
            paths,
            tts_providers=tts_providers,
            manager_bridge=manager_bridge,
        )
        tts_catalogue = TtsCatalogueService(
            database,
            paths,
            tts_providers,
            manager_bridge=manager_bridge,
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
            automation_enrollment=automation_enrollment,
            login_throttle=login_throttle,
            capabilities=capabilities,
            redactor=redactor,
            audit=audit,
            idempotency=idempotency,
            jobs=jobs,
            work=work,
            identity=identity,
            sessions=sessions,
            artifacts=artifacts,
            workflows=workflows,
            workflow_plans=workflow_plans,
            workflow_handlers=workflow_handlers,
            manager_bridge=manager_bridge,
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
            "automation_enrollment": self.automation_enrollment,
            "login_throttle": self.login_throttle,
            "capabilities": self.capabilities,
            "redactor": self.redactor,
            "audit": self.audit,
            "idempotency": self.idempotency,
            "jobs": self.jobs,
            "work": self.work,
            "identity": self.identity,
            "sessions": self.sessions,
            "artifacts": self.artifacts,
            "workflows": self.workflows,
            "workflow_plans": self.workflow_plans,
            "workflow_handlers": self.workflow_handlers,
            "manager_bridge": self.manager_bridge,
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
