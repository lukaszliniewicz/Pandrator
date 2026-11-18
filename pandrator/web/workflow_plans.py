"""Immutable workflow previews and atomic execute-once submissions."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, load_only

from .access.contracts import AccessContext, AccessDenied, ProjectAction
from .access.session_policy import require_session_access
from .accounts.execution_actor import UserActor, actor_from_principal, execution_actor_scope
from .artifact_selection import canonical_stage_key
from .auth import Principal
from .credentials import contains_inline_secret, is_sensitive_field
from .database import Database
from .export_contract import export_requires_generation_assembly
from .idempotency import IdempotencyService
from .jobs import JobQueue
from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    OutcomePlan,
    OutputAssembly,
    Provider,
    ProviderModel,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    WorkflowExecutionPlan,
    WorkspaceSetting,
    utcnow,
)
from .outcome_plans import resolve_workflow_inputs
from .provider_identity import provider_namespace
from .work import WorkService
from .workflow_inputs import workflow_transformations
from .workflow_prerequisites import WorkflowPrerequisiteService
from .workflows import (
    GenerationPreflightError,
    ResolvedWorkflowStage,
    WorkflowService,
    _workflow_primary_source,
)

logger = logging.getLogger(__name__)

PLAN_SCHEMA_VERSION = "1"
# Export plans may all be executed side by side: an export resolves its output
# assembly and file names at execution, so the outputs a sibling export added
# never change what this plan produces. Plans stored before this variant
# existed carry no marker and keep comparing against the full snapshot.
FINGERPRINT_FULL = "full"
FINGERPRINT_EXPORT = "export_outputs_ignored"
FINGERPRINT_VARIANTS = frozenset({FINGERPRINT_FULL, FINGERPRINT_EXPORT})
EXPORT_PLAN_TARGET_STAGE = "export"
# Roles an export job registers for its own files; "export" is the generic
# output and soundtrack/subtitle tracks add a suffix.
EXPORT_OUTPUT_ROLE = "export"
EXPORT_OUTPUT_ROLE_PREFIX = "export_"

LOCAL_PROVIDER_IDS = frozenset(
    {
        "audio.cpp",
        "audio_cpp",
        "chatterbox",
        "crispasr",
        "faster-whisper",
        "fishs2",
        "kokoro",
        "kobold_qwen",
        "koboldcpp",
        "local",
        "lm-studio",
        "magpie",
        "moss",
        "ollama",
        "parakeet",
        "silero",
        "stable-ts",
        "voxcpm",
        "voxtral",
        "whisper",
        "xtts",
    }
)


def canonical_json(value: Any) -> str:
    """Serialize a plan deterministically for hashing and comparison."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _safe_endpoint(value: object) -> str | None:
    """Return a disclosure-safe origin/path without credentials or queries."""

    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return None
    return urlunsplit(
        (
            parsed.scheme.lower(),
            f"{host.lower()}{port}",
            parsed.path or "",
            "",
            "",
        )
    )


def _contains_url_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_url_credentials(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_url_credentials(item) for item in value)
    if not isinstance(value, str) or "://" not in value:
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.username
        or parsed.password
        or any(
            is_sensitive_field(key)
            for key, _item in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
        )
    )


def _is_export_execution_output(artifact, assembly_artifact_ids: set[str]) -> bool:
    """Return whether an export execution itself can add this artifact.

    Export files carry the generic "export" role or an "export_" prefix. An
    export that must assemble its generation run also publishes the assembly's
    audio artifact, so the artifact an output assembly points at counts too.
    """

    role = str(getattr(artifact, "role", "") or "")
    if role == EXPORT_OUTPUT_ROLE or role.startswith(EXPORT_OUTPUT_ROLE_PREFIX):
        return True
    return artifact.id in assembly_artifact_ids


_EXPORT_OUTPUT_KEYS = (
    "audio_mode",
    "export_mode",
    "subtitle_mode",
    "subtitle_selection",
    "video_tail_extension_policy",
)
_EXPORT_AUDIO_KEYS = ("synchronization_speed",)
# The keys that decide what the transcription and speech stages will do. A plan
# reports them with their origin so an inherited engine or voice route cannot
# pass unseen.
_STT_DECISION_KEYS = ("stt_engine", "stt_model_quantization", "stt_language")
_TTS_DECISION_KEYS = (
    "service",
    "model",
    "voice",
    "language",
    "tts_concurrent_requests",
)


def _resolved_section(
    snapshot: dict[str, Any], section: str, keys: tuple[str, ...]
) -> dict[str, Any] | None:
    """Return the resolved section, or its flat equivalent when there is none.

    A section that supplies nothing is not authoritative, so it reports nothing
    and the caller falls back to the layered session snapshot.
    """

    value = snapshot.get(section)
    if isinstance(value, dict) and value:
        return value
    selected = {key: snapshot[key] for key in keys if key in snapshot}
    return selected or None


def _effective_output_flag(
    resolved_snapshot: dict[str, Any],
    snapshot: dict[str, Any],
    key: str,
    default: bool,
) -> bool:
    """Read a boolean output setting the plan did not project.

    The plan's own resolution wins over the layered session snapshot, which in
    turn wins over the built-in default the resolver starts from.
    """

    resolved_output = resolved_snapshot.get("output")
    if isinstance(resolved_output, dict) and key in resolved_output:
        return bool(resolved_output[key])
    for candidate in (snapshot.get("effective"), snapshot.get("builtin")):
        if isinstance(candidate, dict) and key in candidate:
            return bool(candidate[key])
    return default


def _synchronization_multiplier(value: Any) -> float | None:
    """Interpret a stored speed the way alignment does: multiplier or percent."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number if number <= 10 else number / 100


def _reviewable_settings(value: Any) -> Any:
    """Project resolved settings without secrets or bulky runtime catalogues."""

    if isinstance(value, dict):
        omitted_runtime_catalogues = {
            "provider_configs",
            "voice_catalogues",
            "voice_metadata",
            "model_catalog",
        }
        return {
            str(key): _reviewable_settings(item)
            for key, item in value.items()
            if not is_sensitive_field(key)
            and str(key) not in omitted_runtime_catalogues
        }
    if isinstance(value, list):
        return [_reviewable_settings(item) for item in value]
    if isinstance(value, str) and "://" in value:
        return _safe_endpoint(value) or "[configured endpoint]"
    return value


class WorkflowPlanError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 409,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details
        self.retryable = retryable


class WorkflowExecutionPlanService:
    """Persist previews and atomically consume an unchanged exact plan."""

    def __init__(
        self,
        database: Database,
        workflows: WorkflowService,
        jobs: JobQueue,
        work: WorkService,
        idempotency: IdempotencyService,
        handlers: Any | None = None,
    ) -> None:
        self.database = database
        self.workflows = workflows
        self.jobs = jobs
        self.work = work
        self.idempotency = idempotency
        self.handlers = handlers

    @staticmethod
    def _context(principal: Principal) -> AccessContext:
        try:
            actor = actor_from_principal(principal)
        except (TypeError, ValueError):
            raise AccessDenied() from None
        if type(actor) is not UserActor:
            raise AccessDenied()
        return AccessContext(actor)

    def _owned(self, db_session: Session, plan_id: str, principal: Principal, action: ProjectAction) -> WorkflowExecutionPlan:
        context = self._context(principal)
        row = db_session.execute(
            select(WorkflowExecutionPlan.session_id, WorkflowExecutionPlan.principal_subject)
            .where(WorkflowExecutionPlan.id == plan_id)
        ).one_or_none()
        if row is None or row.principal_subject != principal.subject:
            raise WorkflowPlanError("not_found", "Workflow execution plan not found.", 404)
        require_session_access(db_session, context, row.session_id, action)
        record = db_session.get(WorkflowExecutionPlan, plan_id)
        if record is None:
            raise WorkflowPlanError("not_found", "Workflow execution plan not found.", 404)
        return record

    @staticmethod
    def _state_snapshot(
        db_session,
        session_id: str,
        *,
        fingerprint_variant: str = FINGERPRINT_FULL,
    ) -> dict[str, Any]:
        record = db_session.get(SessionRecord, session_id)
        if record is None:
            raise KeyError(session_id)
        namespace = provider_namespace(db_session, session_id=session_id)
        outcome = db_session.get(OutcomePlan, session_id)
        selections = list(
            db_session.scalars(
                select(SessionStageSelection)
                .where(SessionStageSelection.session_id == session_id)
                .order_by(SessionStageSelection.stage_key)
            ).all()
        )
        attachments = list(
            db_session.execute(
                select(SessionSource, SourceAsset)
                .join(
                    SourceAsset,
                    SourceAsset.id == SessionSource.source_asset_id,
                )
                .options(load_only(
                    SourceAsset.id, SourceAsset.revision, SourceAsset.state,
                    SourceAsset.artifact_id, SourceAsset.content_hash, raiseload=True,
                ))
                .where(SessionSource.session_id == session_id)
                .order_by(
                    SessionSource.role,
                    SessionSource.source_asset_id,
                )
            ).all()
        )
        selected_ids = {
            str(item.artifact_id) for item in selections if item.artifact_id
        }
        attached_artifact_ids = {
            str(asset.artifact_id)
            for _attachment, asset in attachments
            if asset.artifact_id
        }
        artifact_filter = Artifact.state == "current"
        included_ids = selected_ids | attached_artifact_ids
        if included_ids:
            artifact_filter = or_(
                artifact_filter,
                Artifact.id.in_(included_ids),
            )
        artifacts = list(
            db_session.scalars(
                select(Artifact)
                .where(
                    Artifact.session_id == session_id,
                    artifact_filter,
                )
                .order_by(Artifact.id)
            ).all()
        )
        session_settings = list(
            db_session.scalars(
                select(SessionSetting)
                .where(SessionSetting.session_id == session_id)
                .order_by(SessionSetting.section)
            ).all()
        )
        app_settings = list(
            db_session.scalars(
                select(WorkspaceSetting)
                .where(
                    WorkspaceSetting.workspace_id == namespace.workspace_id,
                    or_(
                        WorkspaceSetting.key.like("defaults.%"),
                        WorkspaceSetting.key.like("services.%"),
                    )
                )
                .order_by(WorkspaceSetting.key)
            ).all()
        )
        providers = list(
            db_session.scalars(
                select(Provider)
                .where(Provider.workspace_id == namespace.workspace_id)
                .order_by(Provider.id)
                .execution_options(populate_existing=True)
            ).all()
        )
        provider_models = list(
            db_session.scalars(
                select(ProviderModel)
                .join(Provider, Provider.id == ProviderModel.provider_id)
                .where(Provider.workspace_id == namespace.workspace_id)
                .order_by(ProviderModel.id)
                .execution_options(populate_existing=True)
            ).all()
        )
        generation_plan = db_session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        generation_revision = (
            db_session.get(
                GenerationPlanRevision,
                generation_plan.active_revision_id,
            )
            if generation_plan and generation_plan.active_revision_id
            else None
        )
        generation_runs = list(
            db_session.scalars(
                select(GenerationRun)
                .where(GenerationRun.session_id == session_id)
                .order_by(GenerationRun.id)
            ).all()
        )
        output_assemblies = list(
            db_session.scalars(
                select(OutputAssembly)
                .where(OutputAssembly.session_id == session_id)
                .order_by(OutputAssembly.id)
            ).all()
        )
        export_variant = fingerprint_variant == FINGERPRINT_EXPORT
        if export_variant:
            assembly_artifact_ids = {
                item.artifact_id for item in output_assemblies if item.artifact_id
            }
            artifacts = [
                artifact
                for artifact in artifacts
                if not _is_export_execution_output(artifact, assembly_artifact_ids)
            ]
        snapshot = {
            "session": {
                "id": record.id,
                "workflow_kind": record.workflow_kind,
                "source_language": record.source_language,
                "target_language": record.target_language,
                "workflow_preset": record.workflow_preset,
                "included_stages": list(record.included_stages_json or []),
                "status": record.status,
                "revision": record.revision,
                "trashed_at": _iso(record.trashed_at),
            },
            "outcome": {
                "revision": outcome.revision if outcome else 0,
                "value_digest": canonical_digest(outcome.value_json if outcome else {}),
            },
            "selections": [
                {
                    "stage_key": item.stage_key,
                    "artifact_id": item.artifact_id,
                    "revision": item.revision,
                }
                for item in selections
            ],
            "attachments": [
                {
                    "id": attachment.id,
                    "role": attachment.role,
                    "current": attachment.is_current,
                    "revision": attachment.revision,
                    "source_asset_id": asset.id,
                    "source_revision": asset.revision,
                    "source_state": asset.state,
                    "artifact_id": asset.artifact_id,
                    "content_hash": asset.content_hash,
                }
                for attachment, asset in attachments
            ],
            "artifacts": [
                {
                    "id": artifact.id,
                    "role": artifact.role,
                    "kind": artifact.kind,
                    "state": artifact.state,
                    "content_hash": artifact.content_hash,
                    "settings_hash": artifact.settings_hash,
                }
                for artifact in artifacts
            ],
            "session_settings": [
                {
                    "section": item.section,
                    "revision": item.revision,
                    "value_digest": canonical_digest(item.value_json),
                }
                for item in session_settings
            ],
            "app_settings": [
                {
                    "key": item.key,
                    "revision": item.revision,
                    "value_digest": canonical_digest(item.value_json),
                }
                for item in app_settings
            ],
            "providers": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "provider_key": item.provider_key,
                    "enabled": item.enabled,
                    "base_url": item.base_url,
                    "secret_configured": bool(item.secret_ref),
                    "options_digest": canonical_digest(item.options_json or {}),
                    "revision": item.revision,
                }
                for item in providers
            ],
            "provider_models": [
                {
                    "id": item.id,
                    "provider_id": item.provider_id,
                    "model_id": item.model_id,
                    "active": item.is_active,
                    "default": item.is_default,
                    "revision": item.revision,
                }
                for item in provider_models
            ],
            "generation_plan": {
                "id": generation_plan.id if generation_plan else None,
                "updated_at": (
                    _iso(generation_plan.updated_at) if generation_plan else None
                ),
                "active_revision_id": (
                    generation_plan.active_revision_id if generation_plan else None
                ),
                "active_revision_number": (
                    generation_revision.revision_number if generation_revision else None
                ),
                "active_source_revision_id": (
                    generation_revision.source_revision_id
                    if generation_revision
                    else None
                ),
                "active_content_hash": (
                    generation_revision.content_hash if generation_revision else None
                ),
                "active_settings_digest": (
                    canonical_digest(generation_revision.settings_json)
                    if generation_revision
                    else None
                ),
            },
            "generation_runs": [
                {
                    "id": item.id,
                    "plan_revision_id": item.plan_revision_id,
                    "sequence_number": item.sequence_number,
                    "status": item.status,
                    "settings_digest": canonical_digest(
                        item.settings_snapshot_json or {}
                    ),
                    "updated_at": _iso(item.updated_at),
                }
                for item in generation_runs
            ],
            "output_assemblies": [
                {
                    "id": item.id,
                    "generation_run_id": item.generation_run_id,
                    "status": item.status,
                    "artifact_id": item.artifact_id,
                    "settings_hash": item.settings_hash,
                    "settings_digest": canonical_digest(item.settings_json or {}),
                    "updated_at": _iso(item.updated_at),
                }
                for item in output_assemblies
            ],
        }
        if export_variant:
            # Both are resolved again when the export executes.
            snapshot.pop("output_assemblies")
        return snapshot

    @classmethod
    def _state_fingerprint(
        cls,
        db_session,
        session_id: str,
        *,
        fingerprint_variant: str = FINGERPRINT_FULL,
    ) -> str:
        return canonical_digest(
            cls._state_snapshot(
                db_session, session_id, fingerprint_variant=fingerprint_variant
            )
        )

    @staticmethod
    def _plan_fingerprint_variant(target_stage: str) -> str:
        """Return the snapshot variant a plan for this target stage compares with.

        Only an export target can execute an export step, so only that variant
        ignores what a sibling export execution added.
        """

        return (
            FINGERPRINT_EXPORT
            if canonical_stage_key(target_stage) == EXPORT_PLAN_TARGET_STAGE
            else FINGERPRINT_FULL
        )

    @staticmethod
    def _stored_fingerprint_variant(plan_json: Any) -> str:
        """Read the variant a stored plan was created with; old plans used all state."""

        execution = plan_json.get("_execution") if isinstance(plan_json, dict) else None
        if not isinstance(execution, dict):
            return FINGERPRINT_FULL
        variant = str(execution.get("fingerprint_variant") or FINGERPRINT_FULL)
        return variant if variant in FINGERPRINT_VARIANTS else FINGERPRINT_FULL

    @staticmethod
    def _provider_disclosures(
        resolved: ResolvedWorkflowStage,
    ) -> list[dict[str, Any]]:
        snapshot = resolved.payload.get("resolved_settings_snapshot")
        if not isinstance(snapshot, dict):
            return []
        active_service_kinds = {
            value.split(":", 2)[1]
            for value in resolved.resource_keys
            if value.startswith("service:") and len(value.split(":", 2)) >= 2
        }
        section_service_kind = {
            "stt": "stt",
            "tts": "tts",
            "translation": "llm",
            "correction": "llm",
            "source_cleaning": "llm",
            "text": "llm",
        }
        disclosures: list[dict[str, Any]] = []
        for section, raw in sorted(snapshot.items()):
            if not isinstance(raw, dict):
                continue
            required_kind = section_service_kind.get(str(section))
            if required_kind and required_kind not in active_service_kinds:
                continue
            provider = str(
                raw.get("provider")
                or raw.get("provider_id")
                or raw.get("service")
                or raw.get("tts_service")
                or raw.get("stt_engine")
                or raw.get("stt_backend")
                or raw.get("backend")
                or ""
            ).strip()
            model = str(
                raw.get("model") or raw.get("model_id") or raw.get("llm_model") or ""
            ).strip()
            base_url = str(
                raw.get("base_url")
                or raw.get("api_base")
                or raw.get("endpoint")
                or next(
                    (
                        value
                        for key, value in sorted(raw.items())
                        if key.endswith(("_base_url", "_server_url"))
                        and isinstance(value, str)
                        and value.strip()
                    ),
                    "",
                )
                or ""
            ).strip()
            if not any((provider, model, base_url)):
                continue
            disclosures.append(
                {
                    "section": str(section),
                    "provider": provider or None,
                    "model": model or None,
                    "base_url": _safe_endpoint(base_url),
                }
            )
        return disclosures

    @staticmethod
    def _is_external(disclosure: dict[str, Any]) -> bool:
        provider = str(disclosure.get("provider") or "").strip().lower()
        base_url = str(disclosure.get("base_url") or "").strip()
        if base_url:
            host = str(urlsplit(base_url).hostname or "")
            try:
                address = ipaddress.ip_address(host)
                return not (address.is_loopback or address.is_private)
            except ValueError:
                return host not in {"localhost", "host.docker.internal"}
        return bool(provider and provider not in LOCAL_PROVIDER_IDS)

    def _transcription_readiness(
        self,
        *,
        session_id: str,
        resolved: ResolvedWorkflowStage,
        target_stage: str,
    ) -> dict[str, Any]:
        """Add an informational local-file summary without gating the plan."""

        from pandrator.logic.dubbing.transcription_readiness import (
            diagnose_transcription_readiness,
        )

        from .settings_policy import adapt_runtime_settings
        from .workspace_settings import WorkspaceSettingsService

        try:
            payload = resolved.payload if isinstance(resolved.payload, dict) else {}
            snapshot = payload.get("resolved_settings_snapshot")
            stt_snapshot = snapshot.get("stt") if isinstance(snapshot, dict) else None
            if isinstance(stt_snapshot, dict):
                settings = adapt_runtime_settings("stt", stt_snapshot)
            else:
                stored = WorkspaceSettingsService(self.database).get(
                    session_id, "stt"
                )
                settings = adapt_runtime_settings(
                    "stt", dict(stored.get("effective") or {})
                )

            stage_settings = payload.get("stage_settings")
            if isinstance(stage_settings, dict) and isinstance(
                stage_settings.get("transcribe"), dict
            ):
                settings.update(stage_settings["transcribe"])
            if target_stage == "transcribe" and isinstance(
                payload.get("settings"), dict
            ):
                settings.update(payload["settings"])
            return diagnose_transcription_readiness(settings)
        except Exception:
            # This field is a hint only. A filesystem or selection diagnostic
            # must never block creation or execution of an otherwise valid plan.
            return {
                "schema_version": "1",
                "scope": "local_model_files",
                "status": "unresolved",
                "ready": None,
                "engine": "unresolved",
                "assets": [],
                "runtime_check_required": True,
            }

    def _ordered_steps(
        self,
        *,
        session_id: str,
        target_stage: str,
        resolved: ResolvedWorkflowStage,
        reuse_stages: set[str],
        rerun_stages: set[str] | None = None,
        mismatched_stages: dict[str, Any] | None = None,
        stage_freshness: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rerun_stages = set(rerun_stages or ())
        snapshot = self.workflows.snapshot(session_id)
        stages = list(snapshot.get("stages") or [])
        target_index = next(
            (
                index
                for index, item in enumerate(stages)
                if item.get("key") == canonical_stage_key(target_stage)
            ),
            None,
        )
        if target_index is None:
            raise WorkflowPlanError(
                "validation_error",
                f"Stage '{target_stage}' is not available in this workflow.",
                422,
            )
        with self.database.session() as db_session:
            record = db_session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            outcome = db_session.scalar(
                select(OutcomePlan).where(OutcomePlan.session_id == session_id)
            )
            plan_value = (
                outcome.value_json
                if outcome is not None and isinstance(outcome.value_json, dict)
                else None
            )
            input_choices = resolve_workflow_inputs(record, plan_value)
            transformations = workflow_transformations(
                db_session, session_id, outcome, self.database
            )
            primary = _workflow_primary_source(db_session, session_id)
            primary_artifact = primary.artifact
            is_srt_source = False
            if record.workflow_kind != "audiobook" and primary_artifact is not None:
                metadata = (
                    primary_artifact.metadata_json
                    if isinstance(primary_artifact.metadata_json, dict)
                    else {}
                )
                filename = str(
                    metadata.get("original_filename")
                    or primary_artifact.relative_path
                ).lower()
                is_srt_source = filename.endswith((".srt", ".vtt"))
            required = WorkflowPrerequisiteService._continuation_required_stages(
                record.workflow_kind,
                target_stage,
                is_srt_source,
                input_choices,
                transformations,
            )
            allowed_keys = set(record.included_stages_json or []) | required
        includable_stages: set[str] = set()
        for item in stages[:target_index]:
            key = str(item.get("key") or "")
            if key == canonical_stage_key(target_stage):
                key = target_stage
            if key in allowed_keys and item.get("executable"):
                includable_stages.add(key)
        unavailable = sorted(rerun_stages - includable_stages - {target_stage})
        if unavailable:
            raise WorkflowPlanError(
                "validation_error",
                f"Stage '{unavailable[0]}' cannot run in this plan.",
                422,
            )
        steps: list[dict[str, Any]] = []
        for item in stages[: target_index + 1]:
            key = str(item.get("key") or "")
            if key == canonical_stage_key(target_stage):
                key = target_stage
            if key not in allowed_keys or not item.get("executable"):
                continue
            status = str(item.get("status") or "unknown")
            selected_id = item.get("selected_artifact_id")
            if key == target_stage:
                decision = "run"
            elif key in rerun_stages:
                decision = "rerun_explicit"
            elif key in reuse_stages and selected_id:
                decision = "reuse_explicit"
            elif status == "completed" and selected_id:
                if mismatched_stages and key in mismatched_stages:
                    decision = "run"
                else:
                    decision = "reuse_if_unchanged"
            elif item.get("included") or status in {
                "ready",
                "stale",
                "failed",
            }:
                decision = "run"
            else:
                continue
            step = {
                "order": len(steps) + 1,
                "stage": key,
                "title": str(item.get("title") or key),
                "decision": decision,
                "current_status": status,
                "artifact_id": selected_id,
            }
            if decision.startswith("reuse"):
                if decision == "reuse_explicit":
                    step["reuse_basis"] = "explicit"
                else:
                    evaluated = (stage_freshness or {}).get(key)
                    step["reuse_basis"] = (
                        evaluated.get("settings_evidence")
                        if evaluated is not None
                        else "no_prior_check"
                    )
            steps.append(step)
        if not steps or steps[-1]["stage"] != target_stage:
            steps.append(
                {
                    "order": len(steps) + 1,
                    "stage": target_stage,
                    "title": target_stage.replace("_", " ").title(),
                    "decision": "run",
                    "current_status": "ready",
                    "artifact_id": None,
                }
            )
        return steps

    def _direct_steps(
        self,
        *,
        session_id: str,
        target_stage: str,
        resolved: ResolvedWorkflowStage,
    ) -> tuple[list[dict[str, Any]], str]:
        """Describe a direct target-stage plan without implying prerequisite reruns."""

        steps: list[dict[str, Any]] = []
        execution_job_kind = resolved.job_kind
        settings = resolved.payload.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        generation_run_id = str(settings.get("generation_run_id") or "").strip()
        needs_assembly = export_requires_generation_assembly(
            workflow_kind=resolved.workflow_kind,
            settings=settings,
        )
        if target_stage == "export" and needs_assembly:
            snapshot = resolved.payload.get("resolved_settings_snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            from .workspace import find_matching_output_assembly

            with self.database.session() as db_session:
                generation_run = db_session.get(GenerationRun, generation_run_id)
                if generation_run is None or generation_run.session_id != session_id:
                    raise WorkflowPlanError(
                        "validation_error",
                        "The selected generation run does not belong to this session.",
                        422,
                    )
                if generation_run.status != "completed" and generation_run.audio_version_json is None:
                    raise WorkflowPlanError(
                        "generation_run_not_ready",
                        "Only a completed generation run can be exported.",
                        409,
                    )
                assembly, _assembly_snapshot, _assembly_hash = (
                    find_matching_output_assembly(
                        db_session,
                        session_id=session_id,
                        run=generation_run,
                        resolved_settings_snapshot=snapshot,
                        audio_version_side=str(settings.get("audio_version_side") or "after"),
                    )
                )
            steps.append(
                {
                    "order": 1,
                    "stage": "assemble_audio",
                    "title": "Assemble audio",
                    "decision": "reuse_if_unchanged" if assembly else "run",
                    "current_status": "completed" if assembly else "ready",
                    "artifact_id": assembly.artifact_id if assembly else None,
                    "generation_run_id": generation_run_id,
                }
            )
            execution_job_kind = "export.variant"
        steps.append(
            {
                "order": len(steps) + 1,
                "stage": target_stage,
                "title": target_stage.replace("_", " ").title(),
                "decision": "run",
                "current_status": "ready",
                "artifact_id": None,
            }
        )
        return steps, execution_job_kind

    def _export_effective_settings(
        self,
        session_id: str,
        overrides: dict[str, Any],
        resolved: ResolvedWorkflowStage,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Report the export settings this plan resolved, and their raw snapshot.

        The third value is the layered output snapshot, which carries values
        this report does not project, such as ``audio_match_source_duration``.
        """

        from .workspace_settings import WorkspaceSettingsService, settings_provenance

        output_override = overrides.get("output")
        audio_override = overrides.get("audio")
        service = WorkspaceSettingsService(self.database)
        with self.database.snapshot_session() as db_session:
            output = service.get_in_session(db_session, session_id, "output")
            audio = service.get_in_session(db_session, session_id, "audio")
            record = db_session.get(SessionRecord, session_id)
            context = (
                service._output_context(db_session, record)
                if record is not None
                else {}
            )
        resolved_snapshot = resolved.payload.get("resolved_settings_snapshot")
        resolved_snapshot = (
            resolved_snapshot if isinstance(resolved_snapshot, dict) else {}
        )
        effective = {
            **settings_provenance(
                output,
                _EXPORT_OUTPUT_KEYS,
                output_override if isinstance(output_override, dict) else None,
                _resolved_section(resolved_snapshot, "output", _EXPORT_OUTPUT_KEYS),
            ),
            **settings_provenance(
                audio,
                _EXPORT_AUDIO_KEYS,
                audio_override if isinstance(audio_override, dict) else None,
                _resolved_section(resolved_snapshot, "audio", _EXPORT_AUDIO_KEYS),
            ),
        }
        return effective, context, output

    def _export_speech_timing(
        self,
        session_id: str,
        resolved: ResolvedWorkflowStage,
        effective: dict[str, dict[str, Any]],
        output_snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Report the measured speech timing of the assembly this plan reuses.

        The figures appear once an assembly exists: an assembly is the only
        place placement is measured against the source recording's timeline.
        """

        settings = resolved.payload.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        if not export_requires_generation_assembly(
            workflow_kind=resolved.workflow_kind, settings=settings
        ):
            return None
        from pandrator.logic.dubbing.timing_drift import block_lag_metrics

        from .source_resolution import resolve_media_source

        run_id = str(settings.get("generation_run_id") or "").strip()
        resolved_snapshot = resolved.payload.get("resolved_settings_snapshot")
        resolved_snapshot = (
            resolved_snapshot if isinstance(resolved_snapshot, dict) else {}
        )
        assembly_id = ""
        duration_ms = 0
        timing_drift: dict[str, Any] | None = None
        source_id = ""
        try:
            with self.database.snapshot_session() as db_session:
                run = db_session.get(GenerationRun, run_id)
                if run is None or run.session_id != session_id:
                    return None
                from .workspace import find_matching_output_assembly

                assembly, _snapshot, _hash = find_matching_output_assembly(
                    db_session,
                    session_id=session_id,
                    run=run,
                    resolved_settings_snapshot=resolved_snapshot,
                    audio_version_side=str(
                        settings.get("audio_version_side") or "after"
                    ),
                )
                if assembly is None:
                    return None
                assembly_id = assembly.id
                stored = (
                    assembly.settings_json
                    if isinstance(assembly.settings_json, dict)
                    else {}
                )
                duration_ms = int(stored.get("duration_ms") or 0)
                synchronization = stored.get("synchronization")
                if isinstance(synchronization, dict):
                    summary = synchronization.get("timing_drift")
                    if isinstance(summary, dict):
                        timing_drift = dict(summary)
                    elif isinstance(synchronization.get("blocks"), list):
                        timing_drift = block_lag_metrics(synchronization["blocks"])
                source = resolve_media_source(db_session, session_id).artifact
                source_id = source.id if source is not None else ""
        except Exception:
            # A plan is a preview. A hint must never stop an otherwise valid
            # export plan from being prepared and reviewed.
            logger.warning(
                "Speech timing could not be reported for session %s",
                session_id,
                exc_info=True,
            )
            return None
        return {
            "assembly_id": assembly_id,
            "assembly_duration_ms": duration_ms,
            "timing_drift": timing_drift,
            "tail": self._export_tail_projection(
                effective=effective,
                resolved_snapshot=resolved_snapshot,
                output_snapshot=output_snapshot,
                source_id=source_id,
                generated_duration_ms=duration_ms,
            ),
        }

    def _export_tail_projection(
        self,
        *,
        effective: dict[str, dict[str, Any]],
        resolved_snapshot: dict[str, Any],
        output_snapshot: dict[str, Any],
        source_id: str,
        generated_duration_ms: int,
    ) -> dict[str, Any] | None:
        """Predict the export's own tail decision, or say why it cannot."""

        export_mode = str(
            (effective.get("export_mode") or {}).get("effective") or ""
        ).lower()
        if export_mode not in {"media", "audio"}:
            return None
        if not _effective_output_flag(
            resolved_snapshot,
            output_snapshot,
            "audio_match_source_duration",
            True,
        ):
            return None
        if not source_id:
            return {
                "checked": False,
                "reason": "This session has no source recording to measure against.",
            }
        if self.handlers is None:
            return {
                "checked": False,
                "reason": "The source recording could not be resolved.",
            }
        from .soundtrack_export import (
            OVERRUN_TOLERANCE_SECONDS,
            ceil_tail_extension_seconds,
            probe_soundtrack_media,
        )

        try:
            _artifact, source_path = self.handlers._resolve_input(source_id)
        except Exception:
            return {
                "checked": False,
                "reason": "The source recording could not be resolved.",
            }
        try:
            probed = probe_soundtrack_media(source_path)
        except Exception:
            return {
                "checked": False,
                "reason": "The source recording's duration could not be probed.",
            }
        try:
            # The export measures against the video stream first.
            source_duration = float(
                probed.get("video_duration") or probed.get("duration") or 0
            )
        except (TypeError, ValueError):
            source_duration = 0.0
        if source_duration <= 0:
            return {
                "checked": False,
                "reason": "The source recording's duration could not be probed.",
            }
        overrun_seconds = generated_duration_ms / 1000 - source_duration
        extension_ms = 0
        if overrun_seconds > OVERRUN_TOLERANCE_SECONDS:
            extension_ms = round(
                ceil_tail_extension_seconds(overrun_seconds, probed.get("fps")) * 1000
            )
        policy = str(
            (effective.get("video_tail_extension_policy") or {}).get("effective")
            or "ask"
        )
        if overrun_seconds <= OVERRUN_TOLERANCE_SECONDS:
            outcome = "fits"
        elif export_mode == "audio":
            # An audio export never cuts speech, so it cannot extend or stop.
            outcome = "fails"
        elif policy == "extend":
            outcome = "extend"
        else:
            outcome = "stops"
        return {
            "checked": True,
            "source_duration_ms": round(source_duration * 1000),
            "overrun_ms": round(overrun_seconds * 1000),
            "policy": policy,
            "extension_ms": extension_ms,
            "outcome": outcome,
        }

    @staticmethod
    def _speech_timing_warnings(
        speech_timing: dict[str, Any] | None,
        effective: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Name a measured drift or tail overrun before the export is queued."""

        if not isinstance(speech_timing, dict):
            return []
        from pandrator.logic.dubbing.timing_drift import drift_warnings

        warnings: list[str] = []
        metrics = speech_timing.get("timing_drift")
        if isinstance(metrics, dict):
            speed = (effective.get("synchronization_speed") or {}).get("effective")
            warnings.extend(drift_warnings(metrics, speed=speed))
        tail = speech_timing.get("tail")
        if not isinstance(tail, dict) or not tail.get("checked"):
            return warnings
        overrun = float(tail.get("overrun_ms") or 0) / 1000
        extension = float(tail.get("extension_ms") or 0) / 1000
        outcome = str(tail.get("outcome") or "")
        if outcome == "extend":
            warnings.append(
                f"The video will be {extension:.2f} s longer: its last frame is held "
                "so the speech can finish. Usually only that tail is encoded."
            )
        elif outcome == "stops":
            warnings.append(
                f"Speech runs {overrun:.2f} s past the end of the video, so this export "
                "will stop before rendering. Set video_tail_extension_policy=extend to "
                "hold the last frame (usually only that tail is encoded), or raise "
                "synchronization_speed."
            )
        elif outcome == "fails":
            warnings.append(
                f"Speech runs {overrun:.2f} s past the end of the recording, and an "
                "audio export never cuts speech, so this export will fail. Turn off "
                "audio_match_source_duration to export the full speech, or export media "
                "with video_tail_extension_policy=extend."
            )
        return warnings

    def _stage_effective_settings(
        self,
        session_id: str,
        overrides: dict[str, Any],
        resolved: ResolvedWorkflowStage,
        stages: set[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
        """Report the decisive keys of the stages this plan runs, with origins.

        The values are read from the plan's resolved snapshot, as the export
        block does, so a run override is reflected; the layers come from the
        session settings read, which is the only place that knows the service
        connection and provider default layers.
        """

        from .workspace_settings import WorkspaceSettingsService, settings_provenance

        wanted: list[tuple[str, tuple[str, ...]]] = []
        if "transcribe" in stages:
            wanted.append(("stt", _STT_DECISION_KEYS))
        if "generate_audio" in stages:
            wanted.append(("tts", _TTS_DECISION_KEYS))
        if not wanted:
            return {}, None
        resolved_snapshot = resolved.payload.get("resolved_settings_snapshot")
        resolved_snapshot = (
            resolved_snapshot if isinstance(resolved_snapshot, dict) else {}
        )
        service = WorkspaceSettingsService(self.database)
        effective: dict[str, dict[str, Any]] = {}
        fallbacks: dict[str, dict[str, Any]] = {}
        with self.database.snapshot_session() as db_session:
            for section, keys in wanted:
                snapshot = service.get_in_session(db_session, session_id, section)
                fallbacks[section] = dict(snapshot.get("effective") or {})
                override = overrides.get(section)
                effective.update(
                    settings_provenance(
                        snapshot,
                        keys,
                        override if isinstance(override, dict) else None,
                        _resolved_section(resolved_snapshot, section, keys),
                    )
                )
        support: dict[str, Any] | None = None
        if "generate_audio" in stages:
            from pandrator.logic.tts_language_preflight import (
                tts_language_support_decision,
            )

            resolved_tts = resolved_snapshot.get("tts")
            route = dict(fallbacks.get("tts", {}))
            if isinstance(resolved_tts, dict):
                route.update(resolved_tts)
            support = tts_language_support_decision(route)
            effective["language_support"] = {
                "effective": support["decision"],
                "origin": "derived",
                "language": support["language"],
            }
        return effective, support

    @staticmethod
    def _stage_disclosure_warnings(
        effective: dict[str, dict[str, Any]],
        language_support: dict[str, Any] | None,
    ) -> list[str]:
        """Warn about an inherited engine or voice route without blocking it."""

        from .settings_policy import BUILTIN_DEFAULTS

        warnings: list[str] = []
        engine = effective.get("stt_engine") or {}
        engine_origin = str(engine.get("origin") or "")
        engine_value = str(engine.get("effective") or "").strip()
        builtin_engine = str(BUILTIN_DEFAULTS["stt"].get("stt_engine") or "auto")
        if (
            engine_origin in {"global", "service", "provider"}
            and engine_value
            and engine_value.casefold() != builtin_engine.casefold()
        ):
            warnings.append(
                f"Speech recognition will use {engine_value}, inherited from the "
                f"{engine_origin} settings; the built-in default is "
                f"{builtin_engine}. Set stt.stt_engine on this session to choose "
                "explicitly."
            )
        if language_support is None or language_support.get("decision") != "unverified":
            return warnings
        from .workspace_settings import TTS_ROUTE_KEYS, inherited_tts_route_warning

        language = str(language_support.get("language") or "").strip() or str(
            (effective.get("language") or {}).get("effective") or ""
        ).strip()
        warning = inherited_tts_route_warning(
            {key: (effective.get(key) or {}).get("effective") for key in TTS_ROUTE_KEYS},
            {
                key: str((effective.get(key) or {}).get("origin") or "")
                for key in TTS_ROUTE_KEYS
            },
            language_support="unverified",
            language=language,
        )
        if warning:
            warnings.append(warning)
        return warnings

    @staticmethod
    def _effective_export_warnings(
        *,
        workflow_kind: str,
        effective: dict[str, dict[str, Any]],
        context: dict[str, Any],
    ) -> list[str]:
        """Warn about export choices that are valid but easy to misread."""

        warnings: list[str] = []
        audio_mode = effective.get("audio_mode") or {}
        export_mode = str(
            (effective.get("export_mode") or {}).get("effective") or ""
        ).lower()
        generates_speech = workflow_kind == "voiceover" or (
            workflow_kind == "media_edit" and bool(context.get("has_generated_voiceover"))
        )
        if (
            generates_speech
            and export_mode in {"media", "audio"}
            and context.get("has_source_audio")
            and str(audio_mode.get("effective") or "").lower() == "dubbing_only"
        ):
            warnings.append(
                "This export replaces the original soundtrack with the voiceover only "
                f"(audio_mode=dubbing_only, from {audio_mode.get('origin')}). "
                "Set audio_mode=mixed to keep the original under the voice."
            )
        speed = effective.get("synchronization_speed") or {}
        multiplier = _synchronization_multiplier(speed.get("effective"))
        if multiplier is not None and multiplier > 1.2:
            warnings.append(
                f"Synchronization speed {speed.get('effective')} may sound rushed; "
                "above 1.2× is not recommended."
            )
        return warnings

    def create(
        self,
        *,
        principal: Principal,
        target_identity: dict[str, Any],
        session_id: str,
        target_stage: str,
        overrides: dict[str, Any] | None = None,
        continuation: bool = True,
        expires_in_minutes: int = 30,
        reuse_stages: list[str] | None = None,
        rerun_stages: list[str] | None = None,
    ) -> dict[str, Any]:
        context = self._context(principal)
        supplied = dict(overrides or {})
        if contains_inline_secret(supplied) or _contains_url_credentials(supplied):
            raise WorkflowPlanError(
                "validation_error",
                "Credentials cannot be embedded in workflow overrides.",
                422,
            )
        with self.database.session() as db_session:
            require_session_access(db_session, context, session_id, ProjectAction.RUN)
            fingerprint_variant = self._plan_fingerprint_variant(target_stage)
            before = self._state_fingerprint(
                db_session,
                session_id,
                fingerprint_variant=fingerprint_variant,
            )
        requested_reuse_stages = {
            str(value) for value in (reuse_stages or []) if str(value)
        }
        requested_rerun_stages = {
            str(value) for value in (rerun_stages or []) if str(value)
        }
        if not continuation and (requested_reuse_stages or requested_rerun_stages):
            raise WorkflowPlanError(
                "validation_error",
                "reuse_stages and rerun_stages apply only to continuation plans.",
                422,
            )
        if requested_reuse_stages & requested_rerun_stages:
            conflict = sorted(requested_reuse_stages & requested_rerun_stages)[0]
            raise WorkflowPlanError(
                "validation_error",
                f"Stage '{conflict}' cannot be both reused and re-run.",
                422,
            )
        if target_stage in requested_reuse_stages:
            raise WorkflowPlanError(
                "validation_error",
                f"The target stage '{target_stage}' always runs; "
                "remove it from reuse_stages.",
                422,
            )
        planned_reuse_stages = (
            requested_reuse_stages
            | {
                str(value)
                for value in supplied.get("reuse_stages", []) or []
                if str(value)
            }
        ) - requested_rerun_stages
        planned_rerun_stages = requested_rerun_stages - {target_stage}
        if planned_reuse_stages or planned_rerun_stages:
            snapshot = self.workflows.snapshot(session_id)
            workflow_stages = {
                str(item.get("key") or "")
                for item in snapshot.get("stages") or []
                if item.get("key")
            }
            unknown = sorted(
                (planned_reuse_stages | planned_rerun_stages) - workflow_stages
            )
            if unknown:
                raise WorkflowPlanError(
                    "validation_error",
                    f"Unknown workflow stage '{unknown[0]}'. Valid stages: "
                    + ", ".join(sorted(workflow_stages))
                    + ".",
                    422,
                )
        try:
            resolved = self.workflows.resolve_stage(
                session_id,
                target_stage,
                supplied,
                continuation=continuation,
                access_context=context,
            )
        except GenerationPreflightError as error:
            raise WorkflowPlanError(
                "generation_preflight_required",
                str(error),
                409,
            ) from error
        if _contains_url_credentials(resolved.payload):
            raise WorkflowPlanError(
                "validation_error",
                "Resolved workflow settings contain an endpoint with embedded credentials.",
                422,
            )
        if resolved.source_artifact_id and not resolved.source_content_hash:
            raise WorkflowPlanError(
                "source_hash_unavailable",
                "The selected source has no content hash and cannot be bound to an exact execution plan.",
                409,
            )
        effective_export: dict[str, dict[str, Any]] | None = None
        export_context: dict[str, Any] = {}
        export_output_snapshot: dict[str, Any] = {}
        speech_timing: dict[str, Any] | None = None
        if target_stage == "export":
            # Settings reads check access against the execution actor, so read
            # them as the plan's principal, not as an ambient caller.
            with execution_actor_scope(context.actor):
                (
                    effective_export,
                    export_context,
                    export_output_snapshot,
                ) = self._export_effective_settings(
                    session_id, supplied, resolved
                )
                # The measured figures appear once an assembly exists; before
                # that, a plan has nothing placed to report.
                speech_timing = self._export_speech_timing(
                    session_id, resolved, effective_export, export_output_snapshot
                )
        disclosures = self._provider_disclosures(resolved)
        external = [item for item in disclosures if self._is_external(item)]
        resource_services = [
            value.split(":", 1)[1]
            for value in resolved.resource_keys
            if value.startswith("service:")
        ]
        data_categories: list[str] = []
        if any(value.startswith("llm") for value in resource_services):
            data_categories.append("document_or_subtitle_text")
        if any(value.startswith("stt") for value in resource_services):
            data_categories.append("source_audio_or_video")
        if any(value.startswith("tts") for value in resource_services):
            data_categories.extend(["narration_text", "voice_reference_if_configured"])
        network_services = {
            value.split(":", 1)[0]
            for value in resource_services
            if value.split(":", 1)[0] in {"llm", "stt", "tts"}
        }
        disclosed_sections = {str(item.get("section") or "") for item in disclosures}
        for service in sorted(network_services):
            if not any(service in section for section in disclosed_sections):
                external.append(
                    {
                        "section": service,
                        "provider": "unresolved",
                        "model": None,
                        "base_url": None,
                        "classification": "unknown",
                    }
                )
        confirmations: list[str] = []
        if external:
            confirmations.append("external_provider")
        if external or any(value.startswith("llm") for value in resource_services):
            confirmations.append("estimated_cost_unknown")
        plan_id = str(uuid.uuid4())
        now = utcnow()
        expires_at = now + timedelta(minutes=max(1, min(int(expires_in_minutes), 60)))
        warnings: list[str] = []
        freshness: list[dict[str, Any]] = []
        if continuation and self.handlers is not None:
            try:
                freshness = self.handlers.prerequisite_freshness(session_id, target_stage)
            except Exception:
                logger.exception(
                    "Workflow prerequisite freshness could not be checked for session %s",
                    session_id,
                )
                freshness = []
                warnings.append(
                    "Prerequisite freshness could not be checked; existing "
                    "prerequisite results are reused without verification."
                )
        stage_freshness = {
            item["stage"]: item
            for item in freshness
            if isinstance(item, dict) and "stage" in item
        }
        mismatched_stages = {
            stage: item
            for stage, item in stage_freshness.items()
            if not (item["settings_match"] and item["source_match"])
        }
        if continuation:
            with execution_actor_scope(context.actor):
                steps = self._ordered_steps(
                    session_id=session_id,
                    target_stage=target_stage,
                    resolved=resolved,
                    reuse_stages=planned_reuse_stages,
                    rerun_stages=planned_rerun_stages,
                    mismatched_stages=mismatched_stages,
                    stage_freshness=stage_freshness,
                )
            execution_job_kind = resolved.job_kind
        else:
            steps, execution_job_kind = self._direct_steps(
                session_id=session_id,
                target_stage=target_stage,
                resolved=resolved,
            )
        for step in steps:
            if (
                step["stage"] in mismatched_stages
                and step["decision"] == "run"
                and step["stage"] != target_stage
            ):
                mismatch_info = mismatched_stages[step["stage"]]
                reasons = ", ".join(
                    str(r)
                    for r in mismatch_info.get("reasons")
                    or ["settings or source mismatch"]
                )
                warnings.append(
                    f"Prerequisite stage '{step['stage']}' will be re-run ({reasons}). "
                    f"To preserve the existing artifact instead, include '{step['stage']}' in reuse_stages."
                )
            elif (
                str(step.get("decision") or "").startswith("reuse")
                and step.get("reuse_basis") == "current_settings_unavailable"
            ):
                warnings.append(
                    f"Prerequisite stage '{step['stage']}' is reused, but its settings "
                    "could not be compared with the current configuration. Include "
                    f"'{step['stage']}' in rerun_stages to run it again."
                )
        if effective_export is not None:
            warnings.extend(
                self._effective_export_warnings(
                    workflow_kind=resolved.workflow_kind,
                    effective=effective_export,
                    context=export_context,
                )
            )
            warnings.extend(
                self._speech_timing_warnings(speech_timing, effective_export)
            )
        if speech_timing is not None:
            tail = speech_timing.get("tail")
            if isinstance(tail, dict) and tail.get("outcome") == "extend":
                for step in steps:
                    if step["stage"] == target_stage:
                        step["tail_extension"] = tail
        effective_stage: dict[str, dict[str, Any]] = {}
        stage_support: dict[str, Any] | None = None
        with execution_actor_scope(context.actor):
            effective_stage, stage_support = self._stage_effective_settings(
                session_id,
                supplied,
                resolved,
                {str(step.get("stage") or "") for step in steps},
            )
        if effective_stage:
            warnings.extend(
                self._stage_disclosure_warnings(effective_stage, stage_support)
            )
        effective_settings = {**effective_stage, **(effective_export or {})}
        public_plan = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_id": plan_id,
            "warnings": warnings,
            "target": {
                "instance_id": str(target_identity.get("instance_id") or ""),
                "canonical_origin": str(target_identity.get("canonical_origin") or ""),
                "application_version": str(
                    target_identity.get("application_version") or ""
                ),
            },
            "session": {
                "id": session_id,
                "revision": resolved.session_revision,
                "workflow_kind": resolved.workflow_kind,
            },
            "target_stage": target_stage,
            "execution_mode": "continuation" if continuation else "direct",
            "source": {
                "artifact_id": resolved.source_artifact_id,
                "content_hash": resolved.source_content_hash,
            },
            "outcome_plan_revision": resolved.outcome_revision,
            "settings": {
                "hash": str(resolved.payload.get("settings_hash") or ""),
                "resolved_snapshot": _reviewable_settings(
                    resolved.payload.get("resolved_settings_snapshot") or {}
                ),
                **(
                    {"effective": effective_settings}
                    if effective_settings
                    else {}
                ),
            },
            **({"subtitle_review": {
                "provenance": resolved.payload.get("subtitle_settings_provenance", {"status": "unknown/historical_snapshot"}),
                "profiles": resolved.payload.get("subtitle_profiles", {}),
                "profile_scope": resolved.payload.get("subtitle_profile_scope", "source_target_alternatives"),
                "counts_status": "available_after_render",
            }} if target_stage == "export" else {}),
            **(
                {"speech_timing": speech_timing}
                if target_stage == "export"
                else {}
            ),
            "ordered_steps": steps,
            "reuse_decisions": [
                {
                    "stage": item["stage"],
                    "decision": item["decision"],
                    "artifact_id": item["artifact_id"],
                }
                for item in steps
                if item["decision"].startswith("reuse")
            ],
            "selected_providers": disclosures,
            "resource_locks": list(resolved.resource_keys),
            "external_services": external,
            "data_categories": list(dict.fromkeys(data_categories)),
            "estimated_cost": {
                "currency": "USD",
                "amount": None,
                "status": (
                    "unknown"
                    if "estimated_cost_unknown" in confirmations
                    else "not_applicable_or_local"
                ),
            },
            "required_confirmations": confirmations,
            "creator": {
                "subject": principal.subject,
                "kind": principal.kind,
            },
            "created_at": _iso(now),
            "expires_at": _iso(expires_at),
        }
        if any(
            step.get("stage") == "transcribe" and step.get("decision") == "run"
            for step in steps
        ):
            public_plan["transcription_readiness"] = self._transcription_readiness(
                session_id=session_id,
                resolved=resolved,
                target_stage=target_stage,
            )
        execution_payload = dict(resolved.payload)
        if continuation:
            reused = {
                str(item["stage"])
                for item in steps
                if str(item.get("decision") or "").startswith("reuse")
                and item.get("artifact_id")
            }
            if reused:
                execution_payload["reuse_stages"] = sorted(reused)
            else:
                # A legacy overrides.reuse_stages entry must not survive a plan
                # that decided to re-run the stage.
                execution_payload.pop("reuse_stages", None)
            rerun = {
                str(item["stage"])
                for item in steps
                if item.get("decision") == "rerun_explicit"
            }
            if rerun:
                execution_payload["rerun_stages"] = sorted(rerun)
        stored_plan = {
            **public_plan,
            "_execution": {
                "job_kind": execution_job_kind,
                "payload": execution_payload,
                "resource_keys": list(resolved.resource_keys),
                "fingerprint_variant": fingerprint_variant,
            },
        }
        digest = canonical_digest(public_plan)
        with self.database.immediate_session() as db_session:
            require_session_access(db_session, context, session_id, ProjectAction.RUN)
            after = self._state_fingerprint(
                db_session,
                session_id,
                fingerprint_variant=fingerprint_variant,
            )
            if not hmac.compare_digest(before, after):
                raise WorkflowPlanError(
                    "planning_conflict",
                    "The workflow changed while its plan was being created.",
                    retryable=True,
                )
            db_session.add(
                WorkflowExecutionPlan(
                    id=plan_id,
                    session_id=session_id,
                    principal_subject=principal.subject,
                    target_instance_id=str(target_identity.get("instance_id") or ""),
                    plan_digest=digest,
                    plan_json=stored_plan,
                    state_fingerprint=after,
                    required_confirmations_json=confirmations,
                    expires_at=expires_at,
                )
            )
        return {"plan_digest": digest, **public_plan}

    @staticmethod
    def _public_plan(record: WorkflowExecutionPlan) -> dict[str, Any]:
        return {
            key: value
            for key, value in dict(record.plan_json or {}).items()
            if key != "_execution"
        }

    def get(
        self,
        plan_id: str,
        *,
        principal: Principal,
    ) -> dict[str, Any]:
        with self.database.snapshot_session() as db_session:
            record = self._owned(db_session, plan_id, principal, ProjectAction.READ)
            public_plan = self._public_plan(record)
            return {
                "plan_digest": record.plan_digest,
                **public_plan,
                "consumed_at": _iso(record.consumed_at),
                "resulting_work_id": record.resulting_job_id,
            }

    def execute(
        self,
        *,
        principal: Principal,
        target_identity: dict[str, Any],
        plan_id: str,
        supplied_digest: str,
        accepted_confirmations: list[str],
        idempotency_key: object,
        execution_guard: Callable[[Session], None] | None = None,
        authorization_guard: Callable[[Session], None] | None = None,
    ) -> tuple[dict[str, Any], int, bool]:
        operation_payload = {
            "plan_id": plan_id,
            "plan_digest": supplied_digest,
            "accepted_confirmations": sorted(set(accepted_confirmations)),
        }
        with self.database.immediate_session() as db_session:
            record = self._owned(db_session, plan_id, principal, ProjectAction.RUN)
            if authorization_guard is not None:
                authorization_guard(db_session)
            reservation = self.idempotency.begin(
                db_session,
                principal=principal,
                operation_id="executeWorkflowPlan",
                idempotency_key=idempotency_key,
                payload=operation_payload,
            )
            if reservation.replayed:
                replay = reservation.response
                assert replay is not None
                payload, status_code = replay
                return payload, status_code, True
            record = db_session.get(WorkflowExecutionPlan, plan_id)
            if record is None or record.principal_subject != principal.subject:
                raise WorkflowPlanError(
                    "not_found",
                    "Workflow execution plan not found.",
                    404,
                )
            if record.target_instance_id != str(
                target_identity.get("instance_id") or ""
            ):
                raise WorkflowPlanError(
                    "target_identity_mismatch",
                    "The workflow plan belongs to another Pandrator instance.",
                )
            if not hmac.compare_digest(
                record.plan_digest,
                str(supplied_digest or ""),
            ):
                raise WorkflowPlanError(
                    "plan_digest_mismatch",
                    "The supplied workflow plan digest does not match.",
                )
            if _aware(record.expires_at) <= utcnow():
                raise WorkflowPlanError(
                    "plan_expired",
                    "The workflow plan expired and must be recreated.",
                )
            missing = sorted(
                set(record.required_confirmations_json or [])
                - set(accepted_confirmations)
            )
            if missing:
                raise WorkflowPlanError(
                    "confirmation_required",
                    "The workflow plan requires additional confirmations.",
                    details={"required_confirmations": missing},
                )
            if record.consumed_at is not None:
                raise WorkflowPlanError(
                    "plan_consumed",
                    "The workflow plan was already consumed by another request.",
                )
            current_fingerprint = self._state_fingerprint(
                db_session,
                record.session_id,
                fingerprint_variant=self._stored_fingerprint_variant(record.plan_json),
            )
            if not hmac.compare_digest(
                record.state_fingerprint,
                current_fingerprint,
            ):
                raise WorkflowPlanError(
                    "plan_stale",
                    "Relevant workflow state changed after planning.",
                )
            plan = self._public_plan(record)
            if not hmac.compare_digest(
                record.plan_digest,
                canonical_digest(plan),
            ):
                raise WorkflowPlanError(
                    "plan_invalid",
                    "The stored workflow plan failed its integrity check.",
                )
            planned_target = plan.get("target")
            if (
                not isinstance(planned_target, dict)
                or planned_target.get("canonical_origin")
                != target_identity.get("canonical_origin")
                or planned_target.get("application_version")
                != target_identity.get("application_version")
            ):
                raise WorkflowPlanError(
                    "plan_stale",
                    "The target origin or application version changed.",
                )
            execution = dict(record.plan_json or {}).get("_execution")
            if not isinstance(execution, dict):
                raise WorkflowPlanError(
                    "plan_invalid",
                    "The stored workflow plan is incomplete.",
                )
            execution_payload = execution.get("payload")
            resource_keys = execution.get("resource_keys")
            if not isinstance(execution_payload, dict) or not isinstance(
                resource_keys,
                list,
            ):
                raise WorkflowPlanError(
                    "plan_invalid",
                    "The stored workflow execution input is invalid.",
                )
            if execution_guard is not None:
                execution_guard(db_session)
            if (
                str(execution_payload.get("target_stage") or "")
                == "generate_audio"
                and execution_payload.get(
                    "_tts_language_preflight_input_selected"
                )
            ):
                try:
                    with self.database.snapshot_session() as preflight_session:
                        self.workflows.validate_generation_language_payload(
                            preflight_session, execution_payload
                        )
                except GenerationPreflightError as error:
                    raise WorkflowPlanError(
                        "generation_preflight_required",
                        str(error),
                        409,
                    ) from error
            with execution_actor_scope(self._context(principal).actor):
                job = self.jobs.enqueue_in_session(
                    db_session,
                    str(execution.get("job_kind") or "workflow.continue"),
                    execution_payload,
                    session_id=record.session_id,
                    resource_keys=[str(value) for value in resource_keys],
                )
            record.consumed_at = utcnow()
            record.resulting_job_id = job.id
            response = self.work.project_job(job).model_dump(mode="json")
            self.idempotency.complete(
                db_session,
                reservation,
                response=response,
                status_code=202,
                resource_kind="work",
                resource_id=job.id,
            )
            return response, 202, False
