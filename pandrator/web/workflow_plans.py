"""Immutable workflow previews and atomic execute-once submissions."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from sqlalchemy import or_, select

from .auth import Principal
from .credentials import contains_inline_secret, is_sensitive_field
from .database import Database
from .idempotency import IdempotencyService
from .jobs import JobQueue
from .models import (
    AppSetting,
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    OutcomePlan,
    Provider,
    ProviderModel,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    WorkflowExecutionPlan,
    utcnow,
)
from .work import WorkService
from .workflows import ResolvedWorkflowStage, WorkflowService

PLAN_SCHEMA_VERSION = "1"
LOCAL_PROVIDER_IDS = frozenset(
    {
        "crispasr",
        "faster-whisper",
        "kokoro",
        "koboldcpp",
        "local",
        "lm-studio",
        "ollama",
        "silero",
        "stable-ts",
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
    def _state_snapshot(db_session, session_id: str) -> dict[str, Any]:
        record = db_session.get(SessionRecord, session_id)
        if record is None:
            raise KeyError(session_id)
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
                select(AppSetting)
                .where(
                    or_(
                        AppSetting.key.like("defaults.%"),
                        AppSetting.key.like("services.%"),
                    )
                )
                .order_by(AppSetting.key)
            ).all()
        )
        providers = list(
            db_session.scalars(select(Provider).order_by(Provider.id)).all()
        )
        provider_models = list(
            db_session.scalars(select(ProviderModel).order_by(ProviderModel.id)).all()
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
        return {
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
        }

    @classmethod
    def _state_fingerprint(
        cls,
        db_session,
        session_id: str,
    ) -> str:
        return canonical_digest(cls._state_snapshot(db_session, session_id))

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
            try:
                host = str(urlsplit(base_url).hostname or "")
                address = ipaddress.ip_address(host)
                if address.is_loopback or address.is_private:
                    return False
            except ValueError:
                return host not in {"localhost", "host.docker.internal"}
        return bool(provider and provider not in LOCAL_PROVIDER_IDS)

    def _ordered_steps(
        self,
        *,
        session_id: str,
        target_stage: str,
        resolved: ResolvedWorkflowStage,
        reuse_stages: set[str],
        mismatched_stages: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        snapshot = self.workflows.snapshot(session_id)
        stages = list(snapshot.get("stages") or [])
        target_index = next(
            (
                index
                for index, item in enumerate(stages)
                if item.get("key") == target_stage
            ),
            None,
        )
        if target_index is None:
            raise WorkflowPlanError(
                "validation_error",
                f"Stage '{target_stage}' is not available in this workflow.",
                422,
            )
        steps: list[dict[str, Any]] = []
        for item in stages[: target_index + 1]:
            if not item.get("executable"):
                continue
            key = str(item.get("key") or "")
            status = str(item.get("status") or "unknown")
            selected_id = item.get("selected_artifact_id")
            if key == target_stage:
                decision = "run"
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
            steps.append(
                {
                    "order": len(steps) + 1,
                    "stage": key,
                    "title": str(item.get("title") or key),
                    "decision": decision,
                    "current_status": status,
                    "artifact_id": selected_id,
                }
            )
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

    def create(
        self,
        *,
        principal: Principal,
        target_identity: dict[str, Any],
        session_id: str,
        target_stage: str,
        overrides: dict[str, Any] | None = None,
        expires_in_minutes: int = 30,
    ) -> dict[str, Any]:
        supplied = dict(overrides or {})
        if contains_inline_secret(supplied) or _contains_url_credentials(supplied):
            raise WorkflowPlanError(
                "validation_error",
                "Credentials cannot be embedded in workflow overrides.",
                422,
            )
        with self.database.session() as db_session:
            before = self._state_fingerprint(
                db_session,
                session_id,
            )
        resolved = self.workflows.resolve_stage(
            session_id,
            target_stage,
            supplied,
            continuation=True,
        )
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
        reuse_stages = {
            str(value) for value in supplied.get("reuse_stages", []) or [] if str(value)
        }
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
        mismatches: list[dict[str, Any]] = []
        if self.handlers is not None:
            try:
                mismatches = self.handlers.settings_mismatches(session_id, target_stage)
            except Exception:
                mismatches = []
        mismatched_stages = {
            item["stage"]: item
            for item in mismatches
            if isinstance(item, dict) and "stage" in item
        }
        steps = self._ordered_steps(
            session_id=session_id,
            target_stage=target_stage,
            resolved=resolved,
            reuse_stages=reuse_stages,
            mismatched_stages=mismatched_stages,
        )
        warnings: list[str] = []
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
            },
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
        stored_plan = {
            **public_plan,
            "_execution": {
                "job_kind": resolved.job_kind,
                "payload": resolved.payload,
                "resource_keys": list(resolved.resource_keys),
            },
        }
        digest = canonical_digest(public_plan)
        with self.database.immediate_session() as db_session:
            after = self._state_fingerprint(
                db_session,
                session_id,
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
        with self.database.session() as db_session:
            record = db_session.get(WorkflowExecutionPlan, plan_id)
            if record is None or record.principal_subject != principal.subject:
                raise WorkflowPlanError(
                    "not_found",
                    "Workflow execution plan not found.",
                    404,
                )
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
    ) -> tuple[dict[str, Any], int, bool]:
        operation_payload = {
            "plan_id": plan_id,
            "plan_digest": supplied_digest,
            "accepted_confirmations": sorted(set(accepted_confirmations)),
        }
        with self.database.immediate_session() as db_session:
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
            payload = execution.get("payload")
            resource_keys = execution.get("resource_keys")
            if not isinstance(payload, dict) or not isinstance(
                resource_keys,
                list,
            ):
                raise WorkflowPlanError(
                    "plan_invalid",
                    "The stored workflow execution input is invalid.",
                )
            job = self.jobs.enqueue_in_session(
                db_session,
                str(execution.get("job_kind") or "workflow.continue"),
                payload,
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
