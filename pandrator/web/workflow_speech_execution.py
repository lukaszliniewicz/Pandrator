"""Current-actor speech optimization effects, fenced to one captured operation."""

from __future__ import annotations

import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, load_only

from pandrator.logic.cancellable_process import ProcessCancelled

from .access.contracts import AccessDenied, ProjectAction
from .access.project_visibility import workspace_authority
from .access.source_preparation_policy import (
    SpeechOptimizationInput,
    _source_processing_child,
    _source_processing_lease_values,
    require_speech_optimization_access,
)
from .accounts.credential_resources import credential_workspace_clause
from .accounts.execution_actor import UserActor, current_execution_actor
from .accounts.job_authorization import require_current_job_actor_lease
from .accounts.models import Workspace
from .agentic_runs import AgenticRunStore, operation_settings_snapshot
from .artifacts import ArtifactService
from .database import Database
from .job_execution import current_job_lease
from .models import (
    AgentRun,
    AgentStep,
    Artifact,
    Document,
    DocumentRevision,
    Job,
    PronunciationEntry,
    Segment,
    SessionRecord,
    UsageEvent,
    utcnow,
)
from .projects.models import Project
from .pronunciations import PronunciationLibrary, normalize_backend, normalize_language
from .speech_optimization_llm import build_speech_optimization_completion
from .usage_identity import bind_usage_ownership
from .workflow_srt_documents import store_srt_document_in_session
from .workflow_text_protocols import SaveSpeechPlanProposalsProtocol


class SpeechOptimizationExecutionContext(Protocol):
    @property
    def database(self) -> Database: ...

    @property
    def artifacts(self) -> ArtifactService: ...

    @property
    def _save_speech_plan_proposals(self) -> SaveSpeechPlanProposalsProtocol: ...


class _StopView:
    def __init__(self, operation: SpeechOptimizationExecution):
        self.operation = operation

    def is_set(self) -> bool:
        return self.operation.error is not None or self.operation.cancel_event.is_set()


class _ProposalUnit:
    def __init__(self, library: PronunciationLibrary, unit: Session):
        self.library = library
        self.unit = unit

    def propose(self, **kwargs):
        return self.library.propose_in_session(self.unit, **kwargs)


class SpeechOptimizationExecution:
    def __init__(
        self,
        context: SpeechOptimizationExecutionContext,
        payload,
        cancel_event,
        captured: SpeechOptimizationInput,
    ):
        self.context = context
        self.payload = dict(payload)
        self.cancel_event = cancel_event
        self.captured = captured
        self.error: Exception | None = None
        self.lock = threading.RLock()
        self.stop = _StopView(self)
        self.metadata_consumed = False
        self.workspace_pronunciations_consumed = False
        self.store = AgenticRunStore(context.database)
        self.run = None
        self.settings: dict[str, Any] = {}
        self.model_name = ""
        self.backend = "*"
        self.languages: list[str] = []
        self.candidates: dict[tuple[str, str], Any] = {}
        self.known_keys: set[str] = set()
        self.next_ordinal = 0
        self.owned: list[Path] = []
        self.committed = False
        self.plan = None
        self.source = None
        self.completion_port = None

    def reject(self, error: Exception) -> None:
        with self.lock:
            if self.error is None:
                self.error = error

    def prove(self, unit: Session | None = None) -> None:
        if self.error is not None:
            raise self.error
        try:
            if self.cancel_event.is_set():
                raise ProcessCancelled("Speech optimization was canceled.")
            if unit is None:
                with self.context.database.snapshot_session() as current:
                    self.prove(current)
                return
            require_speech_optimization_access(
                unit,
                self.captured.session_id,
                self.captured.source.id,
                payload=self.payload,
                expected=self.captured,
                agent_run_id=self.run.id if self.run else None,
                require_metadata=self.metadata_consumed,
                require_workspace_pronunciations=self.workspace_pronunciations_consumed,
            )
        except Exception as error:
            self.reject(error)
            raise

    def progress(self, callback):
        def guarded(value, message=None):
            self.prove()
            callback(value, message)
            self.prove()

        return guarded

    def load_source(self):
        self.prove()
        headers = self.captured.source
        metadata: dict[str, Any] = {}
        with self.context.database.snapshot_session() as unit:
            self.prove(unit)
            fresh = require_speech_optimization_access(
                unit,
                self.captured.session_id,
                headers.id,
                payload=self.payload,
                expected=self.captured,
            )
            if fresh is not None and fresh.ordinary_visibility:
                row = unit.execute(
                    select(
                        Artifact.metadata_json["language"],
                        Artifact.metadata_json["speech_markup"],
                        Artifact.metadata_json["revision_id"],
                    ).where(Artifact.id == headers.id)
                ).one()
                metadata = deepcopy(
                    dict(zip(("language", "speech_markup", "revision_id"), row, strict=True))
                )
                self.metadata_consumed = True
        source = Artifact(
            id=headers.id,
            workspace_id=headers.workspace_id,
            project_id=headers.project_id,
            session_id=headers.session_id,
            relative_path=headers.relative_path,
            content_hash=headers.content_hash,
            size_bytes=headers.size_bytes,
            kind=headers.kind,
            role=headers.role,
            mime_type=headers.mime_type,
            state=headers.state,
            metadata_json=metadata,
        )
        self.source = source
        self.prove()
        return source, self.context.artifacts.paths.managed_path(headers.relative_path)

    def read_text(self, path: Path, *, encoding: str) -> str:
        self.prove()
        value = path.read_text(encoding=encoding)
        self.prove()
        return value

    def begin(self, requested_settings, instructions, settings):
        self.settings = settings
        self.model_name = str(
            settings.get("tts_optimization_model") or settings["llm_default_model"]
        )
        self.backend = normalize_backend(
            settings.get("service") or settings.get("tts_service") or settings.get("backend") or "*"
        )
        assert self.source is not None
        digest, stored = operation_settings_snapshot(
            kind="tts_optimization",
            requested_settings=requested_settings,
            instructions=instructions,
            usage_settings=settings,
        )
        with self.context.database.immediate_session() as unit:
            self.prove(unit)
            run = self.store.start_in_session(
                unit,
                kind="tts_optimization",
                session_id=self.captured.session_id,
                source_artifact=self.source,
                settings_hash=digest,
                settings=stored,
                job_id=self.captured.lease[0] if self.captured.lease else None,
                requested_run_id=str(self.payload.get("_agent_run_id") or "") or None,
            )
        self.run = run
        self.known_keys = set(run.completed_units)
        self.next_ordinal = len(self.known_keys)
        return self.store, run, self.checkpoint

    def completion(self):
        if self.completion_port is not None:
            return self.completion_port
        self.completion_port = build_speech_optimization_completion(
            self.context.database,
            self.context.artifacts.paths,
            self.captured.session_id,
            self.model_name,
            int(self.settings.get("request_timeout_seconds") or 180),
            self.prove,
            self.record_response,
            self.reject,
            cancel_event=self.cancel_event,
        )
        return self.completion_port

    def prepare_units(self, languages: list[str]) -> None:
        from .speech_planning import SPEECH_PROMPT_REVISION

        self.languages = languages
        if self.run:
            for key, output in self.run.completed_units.items():
                plan = output.get("plan")
                if (
                    isinstance(plan, dict)
                    and plan.get("prompt_revision") == SPEECH_PROMPT_REVISION
                    and "proposals" not in plan
                ):
                    self.checkpoint(key, output)

    def reviewed(self, library: PronunciationLibrary, text: str, language: str, backend: str):
        key = (normalize_language(language), normalize_backend(backend))
        if key not in self.candidates:
            actor = self.captured.actor
            global_authority = Workspace.state == "active"
            if type(actor) is UserActor:
                global_authority = and_(
                    global_authority,
                    workspace_authority(actor.user_id, action=ProjectAction.READ),
                    credential_workspace_clause(actor),
                )
            globals_allowed = exists(
                select(Workspace.id).where(
                    Workspace.id == self.captured.workspace_id,
                    global_authority,
                )
            )
            predicate = or_(
                and_(
                    PronunciationEntry.session_id == self.captured.session_id,
                    PronunciationEntry.project_id == self.captured.project_id,
                ),
                and_(
                    PronunciationEntry.session_id.is_(None),
                    PronunciationEntry.project_id.is_(None),
                    globals_allowed,
                ),
            )
            with self.context.database.snapshot_session() as unit:
                self.prove(unit)
                self.candidates[key] = library.reviewed_candidates_in_session(
                    unit,
                    session_id=self.captured.session_id,
                    language=language,
                    backend=backend,
                    workspace_id=self.captured.workspace_id,
                    predicate=predicate,
                )
        matches = library.match_reviewed_candidates(
            self.candidates[key],
            text,
            session_id=self.captured.session_id,
            language=language,
            backend=backend,
        )
        if any(item.get("scope") == "global" for item in matches):
            self.workspace_pronunciations_consumed = True
        return matches

    def checkpoint(self, unit_key: str, output: dict[str, Any], **_kwargs) -> None:
        assert self.run is not None
        library = PronunciationLibrary(self.context.database)
        with self.lock:
            with self.context.database.immediate_session() as unit:
                self.prove(unit)
                plan = output.get("plan")
                proposals = None
                if isinstance(plan, dict):
                    indices = output.get("original_indices") or [0]
                    index = int(indices[0])
                    language = self.languages[index] if index < len(self.languages) else "en"
                    proposals = (
                        self.context._save_speech_plan_proposals(
                            library=_ProposalUnit(library, unit),
                            session_id=self.captured.session_id,
                            plan=plan,
                            backend=self.backend,
                            model_name=self.model_name,
                            default_language=language,
                        )
                        if bool(self.settings.get("speech_plan_save_proposals", True))
                        else []
                    )
                    output = {**output, "plan": {**plan, "proposals": proposals}}
                ordinal = (
                    unit.scalar(
                        select(AgentStep.ordinal).where(
                            AgentStep.agent_run_id == self.run.id,
                            AgentStep.unit_key == unit_key,
                        )
                    )
                    if unit_key in self.known_keys
                    else None
                )
                self.store.checkpoint_in_session(
                    unit,
                    self.run.id,
                    unit_key=unit_key,
                    ordinal=ordinal if ordinal is not None else self.next_ordinal,
                    input_value={
                        "kind": output.get("kind"),
                        "stage": output.get("stage"),
                        "source_hash": output.get("source_hash"),
                        "original_indices": output.get("original_indices", []),
                    },
                    output=output,
                    phase="tts_optimization",
                    summary=f"Saved tts optimization checkpoint {unit_key}.",
                    cost_usd=float(output.get("cost") or 0.0)
                    if output.get("cost_sources")
                    else None,
                )
            if isinstance(plan, dict) and proposals is not None:
                plan["proposals"] = proposals
            if unit_key not in self.known_keys:
                self.known_keys.add(unit_key)
                self.next_ordinal += 1

    def _structural(self, unit: Session) -> AgentRun:
        captured = self.captured
        lease = current_job_lease.get()
        values = _source_processing_lease_values(lease) if lease is not None else None
        if (
            current_execution_actor.get() != captured.actor
            or values != captured.lease
            or _source_processing_child.get() != captured.child
            or self.run is None
        ):
            raise AccessDenied()
        if lease is not None:
            assert values is not None
            try:
                require_current_job_actor_lease(unit, captured.actor)
            except PermissionError:
                raise AccessDenied() from None
            bound_job = unit.scalar(
                select(Job.id).where(
                    Job.id == values[0],
                    Job.kind == ("workflow.continue" if captured.child else "text.optimize_tts"),
                    Job.session_id == captured.session_id,
                    Job.project_id == captured.project_id,
                    Job.workspace_id == captured.workspace_id,
                )
            )
            if bound_job is None:
                raise AccessDenied()
            run_selector = (
                Job.payload_json["_agent_run_ids"]["tts_optimization"].as_string()
                if captured.child
                else Job.payload_json["_agent_run_id"].as_string()
            )
            selectors = unit.execute(
                select(
                    Job.payload_json["session_id"].as_string().label("session_id"),
                    Job.payload_json["source_artifact_id"].as_string().label("source_artifact_id"),
                    run_selector.label("run_id"),
                ).where(Job.id == bound_job)
            ).one()
            if (
                selectors.session_id != captured.session_id
                or (captured.child is None and selectors.source_artifact_id != captured.source.id)
                or (str(selectors.run_id or "") or None)
                != (str(self.payload.get("_agent_run_id") or "") or None)
            ):
                raise AccessDenied()
        target = unit.scalar(
            select(SessionRecord.id)
            .join(Project, Project.id == SessionRecord.project_id)
            .where(
                SessionRecord.id == captured.session_id,
                SessionRecord.project_id == captured.project_id,
                Project.workspace_id == captured.workspace_id,
            )
        )
        source = unit.scalar(
            select(Artifact.id).where(
                Artifact.id == captured.source.id,
                Artifact.workspace_id == captured.source.workspace_id,
                Artifact.project_id == captured.source.project_id,
                Artifact.session_id == captured.source.session_id,
            )
        )
        if target is None or source is None:
            raise AccessDenied()
        run = unit.scalar(
            select(AgentRun)
            .options(
                load_only(
                    AgentRun.id,
                    AgentRun.status,
                    AgentRun.result_artifact_id,
                    raiseload=True,
                )
            )
            .where(
                AgentRun.id == self.run.id,
                AgentRun.kind == "tts_optimization",
                AgentRun.session_id == captured.session_id,
                AgentRun.source_artifact_id == captured.source.id,
                AgentRun.job_id == values[0] if values else AgentRun.job_id.is_(None),
            )
        )
        if run is None:
            raise AccessDenied()
        return run

    def record_response(self, *, usage, provider_key, model_id, cost, cost_source) -> None:
        try:
            with self.context.database.immediate_session() as unit:
                run = self._structural(unit)
                detail = (
                    usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
                )
                event = UsageEvent(
                    workspace_id=self.captured.workspace_id,
                    project_id=self.captured.project_id,
                    session_id=self.captured.session_id,
                    agent_run_id=run.id,
                    job_id=self.captured.lease[0] if self.captured.lease else None,
                    stage="tts_optimization",
                    provider_key=provider_key,
                    model_id=model_id,
                    input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                    cached_input_tokens=int(
                        usage.get("cached_prompt_tokens") or detail.get("cached_tokens") or 0
                    ),
                    output_tokens=int(
                        usage.get("completion_tokens") or usage.get("output_tokens") or 0
                    ),
                    cost_usd=cost,
                    cost_source=cost_source,
                    raw_usage_json=usage,
                )
                bind_usage_ownership(unit, event)
                unit.add(event)
        except Exception as error:
            self.reject(error)
            raise

    def fail(self, error: Exception) -> None:
        if self.run is None:
            return
        try:
            with self.context.database.immediate_session() as unit:
                run = self._structural(unit)
                if run.status != "completed" and run.result_artifact_id is None:
                    run.status = "interrupted" if self.cancel_event.is_set() else "failed"
                    run.error_message = str(error)[:8000]
                    run.updated_at = utcnow()
        except Exception:
            # Failure bookkeeping cannot replace the operation's original error.
            return

    def session_dir(self) -> Path:
        self.prove()
        path = self.context.artifacts.paths.sessions / self.captured.storage_key
        path.mkdir(parents=True, exist_ok=True)
        self.prove()
        return path

    def write_text(self, path: Path, value: str, *, encoding: str) -> Path:
        self.prove()
        path = self.context.artifacts.next_available_path(path)
        self.prove()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding=encoding) as handle:
            self.owned.append(path)
            handle.write(value)
        self.prove()
        return path

    def cleanup(self) -> None:
        if self.committed:
            return
        for path in self.owned:
            relative = self.context.artifacts.paths.relative_managed_path(path)
            try:
                with self.context.database.session() as unit:
                    registered = unit.scalar(
                        select(Artifact.id).where(Artifact.relative_path == relative).limit(1)
                    )
                if registered is None:
                    path.unlink(missing_ok=True)
            except Exception:
                # An uncertain lookup cannot establish that deleting bytes is safe.
                continue

    def publish(
        self, destination: Path, *, kind: str, metadata, settings, segments=None, source_cues=None
    ):
        assert self.source is not None and self.run is not None
        self.prove()
        prepared = self.context.artifacts.prepare_registration(destination, settings=settings)
        plan_prepared = None
        if self.plan:
            plan_prepared = self.context.artifacts.prepare_registration(
                self.plan[0], settings=settings
            )
        self.prove()
        with self.context.database.immediate_session() as unit:
            self.prove(unit)
            parents = [self.captured.source.id]
            if self.plan:
                plan = self.context.artifacts.register_in_session(
                    unit,
                    self.plan[0],
                    kind="json",
                    role="speech_plan",
                    session_id=self.captured.session_id,
                    parent_ids=parents,
                    settings=settings,
                    metadata=self.plan[1],
                    _prepared=plan_prepared,
                )
                parents = [*parents, plan.id]
                metadata = {**metadata, "speech_plan_artifact_id": plan.id}
            artifact = self.context.artifacts.register_in_session(
                unit,
                destination,
                kind=kind,
                role="tts_optimization_candidate" if segments is not None else "tts_optimized",
                session_id=self.captured.session_id,
                parent_ids=parents,
                settings=settings,
                metadata=metadata,
                _prepared=prepared,
            )
            if segments is not None:
                source_parents = []
                revision_id = (
                    (self.source.metadata_json or {}).get("revision_id")
                    if self.metadata_consumed
                    else None
                )
                if revision_id and self.captured.source.session_id is not None:
                    source_parents = list(
                        unit.scalars(
                            select(Segment)
                            .join(DocumentRevision, Segment.revision_id == DocumentRevision.id)
                            .join(Document, Document.id == DocumentRevision.document_id)
                            .join(Artifact, Artifact.session_id == Document.session_id)
                            .where(
                                DocumentRevision.id == revision_id,
                                Artifact.id == self.captured.source.id,
                                Artifact.session_id == self.captured.source.session_id,
                                Artifact.project_id == self.captured.source.project_id,
                                Artifact.workspace_id == self.captured.source.workspace_id,
                            )
                            .order_by(Segment.ordinal)
                        ).all()
                    )
                store_srt_document_in_session(
                    unit,
                    self.captured.session_id,
                    artifact,
                    artifact,
                    "tts_optimization",
                    segments,
                    source_parents,
                    source_cues or [],
                    language=str((self.source.metadata_json or {}).get("language") or "") or None,
                    parent_artifact=self.source,
                )
                self.prove(unit)
                artifact = self.context.artifacts.register_in_session(
                    unit,
                    destination,
                    kind=kind,
                    role="tts_optimized",
                    session_id=self.captured.session_id,
                    parent_ids=parents,
                    settings=settings,
                    _prepared=prepared,
                )
            self.prove(unit)
            self.store.finish_in_session(unit, self.run.id, artifact_id=artifact.id)
            self.prove(unit)
            unit.flush()
            unit.expunge(artifact)
        self.committed = True
        return artifact


def capture_speech_optimization(context: SpeechOptimizationExecutionContext, payload, cancel_event):
    actor = current_execution_actor.get()
    if actor is None and current_job_lease.get() is None:
        return None
    with context.database.snapshot_session() as unit:
        captured = require_speech_optimization_access(
            unit,
            str(payload.get("session_id") or ""),
            str(payload.get("source_artifact_id") or ""),
            payload=payload,
        )
    if captured is None:
        return None
    return SpeechOptimizationExecution(context, payload, cancel_event, captured)
