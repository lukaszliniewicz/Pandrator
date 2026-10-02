"""Durable selected-language previews composed from existing workflow authorities."""

from __future__ import annotations

import hmac
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from . import models as m
from .artifacts import sha256_file
from .auth import Principal
from .idempotency import IdempotencyConflict
from .multilingual_setup import canonical_language
from .settings_policy import RevisionConflict, stable_hash
from .speech_plan_workspace import plan_signature, selected_text, speech_plan_status
from .workflow_plans import WorkflowPlanError


class ProjectOperationError(RevisionConflict):
    def __init__(
        self, code: str, message: str, status_code: int = 409, *, details: dict | None = None
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class OperationCancelled(ProjectOperationError):
    def __init__(self):
        super().__init__("operation_canceled", "The project operation was canceled.")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _key(operation_id: str, branch_id: str, attempt: int) -> str:
    return f"{operation_id}:{branch_id}:{attempt}"


class TranslationProjectOperationService:
    def __init__(self, services):
        self.services = services
        self.database = services.database

    def _owned(self, db, operation_id, principal, target_identity):
        operation = db.get(m.TranslationProjectOperation, operation_id)
        if operation is None or operation.principal_subject != principal.subject:
            raise ProjectOperationError("not_found", "Project operation not found.", 404)
        if operation.target_instance_id != str(target_identity.get("instance_id") or ""):
            raise ProjectOperationError(
                "target_identity_mismatch", "The operation belongs to another instance."
            )
        return operation

    def _branch_guard(self, db, project_id: str, branch_id: str) -> dict:
        branch = db.get(m.TranslationProjectBranch, branch_id)
        if branch is None or branch.project_id != project_id:
            raise ProjectOperationError(
                "branch_not_owned", "A selected branch does not belong to this project.", 422
            )
        record = db.get(m.SessionRecord, branch.session_id)
        if record is None or record.trashed_at is not None:
            raise RevisionConflict("The language session is unavailable or trashed.")
        checkpoint = db.get(m.Artifact, branch.source_checkpoint_artifact_id)
        if (
            checkpoint is None
            or checkpoint.session_id != branch.session_id
            or not branch.source_content_hash
            or checkpoint.content_hash != branch.source_content_hash
        ):
            raise RevisionConflict("The pinned branch checkpoint is missing or changed.")
        try:
            path = self.services.paths.managed_path(checkpoint.relative_path)
            valid = path.is_file() and sha256_file(path) == branch.source_content_hash
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise RevisionConflict("The pinned branch checkpoint file is missing or changed.")
        settings, _ = self.services.workspace_settings.resolve_in_session(
            db,
            branch.session_id,
            sections=["translation"],
        )
        target = canonical_language(str(settings["translation"].get("target_language") or ""))
        if target != canonical_language(branch.target_language):
            raise RevisionConflict(
                "The effective translation language differs from the branch's fixed language."
            )
        return {
            "branch_id": branch.id,
            "session_id": branch.session_id,
            "target_language": branch.target_language,
            "checkpoint_artifact_id": branch.source_checkpoint_artifact_id,
            "checkpoint_content_hash": branch.source_content_hash,
        }

    @staticmethod
    def _partial_dispatch(db, session_id):
        return db.scalar(
            select(m.DispatchRun)
            .where(
                m.DispatchRun.session_id == session_id,
                m.DispatchRun.kind == "translation",
                m.DispatchRun.status != "completed",
                (m.DispatchRun.completed_batch_count > 0)
                | m.DispatchRun.status.in_(("ready", "running", "finalizing")),
            )
            .order_by(m.DispatchRun.created_at.desc())
            .limit(1)
        )

    def _generation_capture(self, branch):
        status = speech_plan_status(self.services, branch["session_id"], summary=True)
        revision_id = status["selected_revision_id"]
        active = next((row for row in status["items"] if row["id"] == revision_id), None)
        if not status["can_generate"] or not active or not active["reviewed"]:
            raise RevisionConflict(
                status.get("generation_blocked_reason")
                or status.get("warning")
                or "Review the current matching speech plan before generation."
            )
        with self.database.session() as db:
            revision = db.get(m.GenerationPlanRevision, revision_id)
            source = status.get("current_input") or {}
            source_artifact = db.get(m.Artifact, str(source.get("artifact_id") or ""))
            source = {
                **source,
                "content_hash": source_artifact.content_hash if source_artifact else None,
            }
            frozen = revision.settings_json or {}
            if (
                not source.get("artifact_id")
                or not source.get("content_hash")
                or frozen.get("_source_artifact_id") != source["artifact_id"]
                or frozen.get("_source_content_hash") != source["content_hash"]
            ):
                raise RevisionConflict(
                    "The reviewed speech plan does not match the current input ID and hash."
                )
            review = db.get(m.SpeechPlanReview, revision_id)
            if review is None or review.content_hash != status["content_signature"]:
                raise RevisionConflict(
                    "The current speech-plan review no longer matches its content."
                )
        prepared = self.services.generation.prepare_start(
            branch["session_id"],
            speech_plan_revision_id=revision_id,
        )
        return prepared, {
            "revision_id": revision_id,
            "content_signature": status["content_signature"],
            "current_input": {key: source[key] for key in ("artifact_id", "content_hash")},
        }

    def _capture(self, project_id, branch_id, action, export_kind, principal, target_identity):
        with self.database.session() as db:
            guard = self._branch_guard(db, project_id, branch_id)
            dispatch = (
                self._partial_dispatch(db, guard["session_id"]) if action == "translate" else None
            )
            if dispatch:
                raise ProjectOperationError(
                    "manual_resume_required",
                    "Resume the existing translation dispatch authority.",
                    details={"dispatch_run_id": dispatch.id, "manual_resume_required": True},
                )
            record = db.get(m.SessionRecord, guard["session_id"])
            outcome = db.get(m.OutcomePlan, guard["session_id"])
            stage_context = {
                "session_revision": record.revision,
                "workflow_kind": record.workflow_kind,
                "outcome_revision": outcome.revision if outcome else 0,
            }
        capture = {"guard": guard}
        if action == "generate":
            prepared, generation_guard = self._generation_capture(guard)
            capture.update(prepared=prepared, generation_guard=generation_guard)
            snapshot = prepared["frozen_snapshot"]
            public = {
                "speech_plan_revision_id": prepared["speech_plan_revision_id"],
                "input": generation_guard["current_input"],
                "settings": self.services.redactor.redact_value(snapshot),
                "language_preflight": snapshot.get("tts_language_snapshot", {}),
                "required_confirmations": [],
            }
            # Use the existing provider disclosure classifier for the selected TTS
            # snapshot, without creating a generation workflow or preparing text.
            from .workflow_plans import WorkflowExecutionPlanService
            from .workflows import ResolvedWorkflowStage
            from .workspace import generation_resource_keys

            resource_keys = generation_resource_keys(guard["session_id"], snapshot)
            disclosures = WorkflowExecutionPlanService._provider_disclosures(
                ResolvedWorkflowStage(
                    job_kind="generation.run",
                    payload={"resolved_settings_snapshot": snapshot},
                    resource_keys=tuple(resource_keys),
                    source_artifact_id=generation_guard["current_input"]["artifact_id"],
                    source_content_hash=generation_guard["current_input"]["content_hash"],
                    **stage_context,
                )
            )
            external = [
                item for item in disclosures if WorkflowExecutionPlanService._is_external(item)
            ]
            public.update(
                selected_providers=disclosures,
                external_services=external,
                resource_locks=resource_keys,
            )
            if external:
                public["required_confirmations"] = ["external_provider", "estimated_cost_unknown"]
        else:
            overrides = (
                {"source_artifact_id": guard["checkpoint_artifact_id"]}
                if action == "translate"
                else {}
            )
            if action == "export" and export_kind == "subtitles":
                overrides.update(
                    export_mode="subtitles",
                    subtitle_mode="translation",
                    subtitle_format="srt",
                    subtitle_selection="translation",
                )
            public = self.services.workflow_plans.create(
                principal=principal,
                target_identity=target_identity,
                session_id=guard["session_id"],
                target_stage=action,
                overrides=overrides,
                continuation=False,
            )
            capture["plan"] = public
        capture["public"] = public
        return capture

    def preview(
        self,
        project_id: str,
        selected_branch_ids: list[str],
        *,
        expected_project_revision: int,
        action: str,
        principal: Principal,
        target_identity: dict,
        idempotency_key: object,
        export_kind: str = "configured",
        _retry_from: dict | None = None,
    ) -> dict:
        if action not in {"translate", "generate", "export"} or export_kind not in {
            "configured",
            "subtitles",
        }:
            raise ValueError("Choose translate, generate, or export and a supported export kind.")
        if not 1 <= len(selected_branch_ids) <= 20 or len(set(selected_branch_ids)) != len(
            selected_branch_ids
        ):
            raise ValueError("Select between 1 and 20 unique language branches.")
        key = self.services.idempotency.validate_key(idempotency_key)
        request_payload = {
            "project_id": project_id,
            "branch_ids": selected_branch_ids,
            "revision": expected_project_revision,
            "action": action,
            "export_kind": export_kind,
            "retry_from": (_retry_from or {}).get("id"),
            "target": target_identity,
        }
        request_digest = self.services.idempotency.request_digest(
            "previewProjectOperation", request_payload
        )
        with self.database.session() as db:
            receipt = db.scalar(
                select(m.ApiIdempotency).where(
                    m.ApiIdempotency.principal_subject == principal.subject,
                    m.ApiIdempotency.operation_id == "previewProjectOperation",
                    m.ApiIdempotency.idempotency_key == key,
                )
            )
            if receipt and _aware(receipt.expires_at) > m.utcnow():
                if receipt.request_digest != request_digest:
                    raise IdempotencyConflict("This preview key was used with different arguments.")
                if receipt.state == "completed":
                    return self.get(
                        receipt.response_json["id"],
                        principal=principal,
                        target_identity=target_identity,
                    )
            project = db.get(m.TranslationProject, project_id)
            if project is None:
                raise KeyError(project_id)
            if project.revision != expected_project_revision:
                raise RevisionConflict("The translation project changed. Refresh the selection.")
            for branch_id in selected_branch_ids:
                branch = db.get(m.TranslationProjectBranch, branch_id)
                if branch is None or branch.project_id != project_id:
                    raise ProjectOperationError(
                        "branch_not_owned",
                        "A selected branch does not belong to this project.",
                        422,
                    )
            references = {
                branch_id: {
                    "branch_id": branch_id,
                    "session_id": db.get(m.TranslationProjectBranch, branch_id).session_id,
                    "target_language": db.get(
                        m.TranslationProjectBranch, branch_id
                    ).target_language,
                }
                for branch_id in selected_branch_ids
            }
        captures, children = {}, []
        prior_children = {
            child["branch_id"]: child for child in (_retry_from or {}).get("children", [])
        }
        for branch_id in selected_branch_ids:
            prior = prior_children.get(branch_id)
            child = {
                **references[branch_id],
                "attempt": int((prior or {}).get("attempt", 0)) + 1,
                "state": "pending",
                "eligible": True,
            }
            if prior and prior["state"] in {"completed", "existing"}:
                assert _retry_from is not None
                retained = prior
                while retained.get("state") == "existing" and isinstance(retained.get("retained"), dict):
                    retained = retained["retained"]
                child.update(
                    state="existing",
                    eligible=False,
                    attempt=prior["attempt"],
                    retained=deepcopy(retained),
                    previous_operation_id=_retry_from["id"],
                )
            elif prior and prior["state"] not in {"failed", "blocked", "canceled", "skipped"}:
                child.update(
                    state="skipped",
                    eligible=False,
                    reason="This child is active or awaiting an agent; resume its existing authority.",
                )
                for key in ("dispatch_run_id", "run_id", "manual_resume_required"):
                    if key in prior:
                        child[key] = prior[key]
            else:
                try:
                    captures[branch_id] = self._capture(
                        project_id, branch_id, action, export_kind, principal, target_identity
                    )
                    child["preview"] = captures[branch_id]["public"]
                except (RevisionConflict, WorkflowPlanError, ValueError, KeyError) as error:
                    child.update(state="skipped", eligible=False, reason=str(error))
                    if isinstance(error, ProjectOperationError):
                        child.update(error.details)
            children.append(child)
        now = m.utcnow()
        private = {
            "project_revision": expected_project_revision,
            "target": deepcopy(target_identity),
            "export_kind": export_kind,
            "captures": captures,
            "previous_operation_id": (_retry_from or {}).get("id"),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
            "children": deepcopy(children),
        }
        operation_id = str(uuid.uuid4())
        with self.database.immediate_session() as db:
            project = db.get(m.TranslationProject, project_id)
            if project is None or project.revision != expected_project_revision:
                raise RevisionConflict("The project changed during preview.")
            reservation = self.services.idempotency.begin(
                db,
                principal=principal,
                operation_id="previewProjectOperation",
                idempotency_key=key,
                payload=request_payload,
            )
            if reservation.replayed:
                operation_id = reservation.response[0]["id"]
            else:
                db.add(
                    m.TranslationProjectOperation(
                        id=operation_id,
                        project_id=project_id,
                        principal_subject=principal.subject,
                        target_instance_id=str(target_identity.get("instance_id") or ""),
                        action=action,
                        status="preview",
                        preview_digest=stable_hash(private),
                        preview_json=private,
                        children_json=children,
                        expires_at=now + timedelta(minutes=30),
                    )
                )
                self.services.idempotency.complete(
                    db,
                    reservation,
                    response={"id": operation_id},
                    status_code=201,
                    resource_kind="project_operation",
                    resource_id=operation_id,
                )
        return self.get(operation_id, principal=principal, target_identity=target_identity)

    def _validate_operation(self, db, operation, supplied_digest, target_identity):
        if not hmac.compare_digest(operation.preview_digest, str(supplied_digest or "")):
            raise ProjectOperationError(
                "preview_digest_mismatch", "The preview digest does not match."
            )
        if stable_hash(operation.preview_json) != operation.preview_digest:
            raise ProjectOperationError(
                "preview_invalid", "The stored preview failed its integrity check."
            )
        if _aware(operation.expires_at) <= m.utcnow():
            raise ProjectOperationError(
                "preview_expired", "The preview expired; create a new preview."
            )
        target = operation.preview_json["target"]
        if any(
            target.get(key) != target_identity.get(key)
            for key in (
                "instance_id",
                "canonical_origin",
                "application_version",
            )
        ):
            raise ProjectOperationError("target_identity_mismatch", "The preview target changed.")
        project = db.get(m.TranslationProject, operation.project_id)
        if project is None or project.revision != operation.preview_json["project_revision"]:
            raise RevisionConflict("The project changed after preview.")

    def _enqueue_guard(self, db, operation_id, branch_id, principal, target_identity):
        operation = self._owned(db, operation_id, principal, target_identity)
        if operation.cancel_requested:
            raise OperationCancelled()
        self._validate_operation(db, operation, operation.preview_digest, target_identity)
        capture = operation.preview_json["captures"][branch_id]
        if self._branch_guard(db, operation.project_id, branch_id) != capture["guard"]:
            raise RevisionConflict(
                "The branch checkpoint or language identity changed after preview."
            )
        return operation, capture

    def _receipt(self, db, operation, child):
        if child.get("session_deleted"):
            return {}
        capture = (operation.preview_json.get("captures") or {}).get(child["branch_id"]) or {}
        if capture.get("plan"):
            plan = db.get(m.WorkflowExecutionPlan, capture["plan"]["plan_id"])
            return {"job_id": plan.resulting_job_id} if plan and plan.resulting_job_id else {}
        receipt = db.scalar(
            select(m.ApiIdempotency).where(
                m.ApiIdempotency.principal_subject == operation.principal_subject,
                m.ApiIdempotency.operation_id == "executeProjectGenerationChild",
                m.ApiIdempotency.idempotency_key
                == _key(operation.id, child["branch_id"], child["attempt"]),
            )
        )
        result = (receipt.response_json or {}) if receipt and receipt.state == "completed" else {}
        return (
            {"job_id": result["job_id"], "generation_run_id": result["id"]}
            if result.get("job_id")
            else {}
        )

    def _start_child(self, operation_id, child, principal, target_identity, accepted):
        with self.database.session() as db:
            operation = self._owned(db, operation_id, principal, target_identity)
            receipt = self._receipt(db, operation, child)
            if receipt:
                return receipt
            capture = deepcopy(operation.preview_json["captures"][child["branch_id"]])
        required = capture["public"].get("required_confirmations", [])
        if set(required) - set(accepted):
            raise ProjectOperationError(
                "confirmation_required",
                "Accept every confirmation disclosed by this child preview.",
            )
        child_key = _key(operation_id, child["branch_id"], child["attempt"])
        if capture.get("plan"):
            plan = capture["plan"]
            response, _status, _replayed = self.services.workflow_plans.execute(
                principal=principal,
                target_identity=target_identity,
                plan_id=plan["plan_id"],
                supplied_digest=plan["plan_digest"],
                accepted_confirmations=required,
                idempotency_key=child_key,
                execution_guard=lambda db: self._enqueue_guard(
                    db, operation_id, child["branch_id"], principal, target_identity
                ),
            )
            return {"job_id": response["id"]}
        with self.database.immediate_session() as db:
            reservation = self.services.idempotency.begin(
                db,
                principal=principal,
                operation_id="executeProjectGenerationChild",
                idempotency_key=child_key,
                payload={"operation_id": operation_id, "branch_id": child["branch_id"]},
            )
            if reservation.replayed:
                response = reservation.response[0]
            else:
                _operation, capture = self._enqueue_guard(
                    db, operation_id, child["branch_id"], principal, target_identity
                )
                guard = capture["generation_guard"]
                sid = capture["guard"]["session_id"]
                plan = db.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == sid))
                review = db.get(m.SpeechPlanReview, guard["revision_id"])
                source = selected_text(self.services, sid) or {}
                source_artifact = db.get(m.Artifact, str(source.get("artifact_id") or ""))
                source = {
                    **source,
                    "content_hash": source_artifact.content_hash if source_artifact else None,
                }
                if (
                    plan is None
                    or plan.active_revision_id != guard["revision_id"]
                    or review is None
                    or review.content_hash != guard["content_signature"]
                    or plan_signature(db, guard["revision_id"]) != guard["content_signature"]
                    or {key: source.get(key) for key in ("artifact_id", "content_hash")}
                    != guard["current_input"]
                ):
                    raise RevisionConflict(
                        "Speech input, content, or review changed after preview."
                    )
                fresh, _ = self.services.workspace_settings.resolve_in_session(db, sid)
                if stable_hash(fresh) != stable_hash(capture["prepared"]["resolved_for_new"][0]):
                    raise RevisionConflict("Generation settings changed after preview.")
                response = self.services.generation.start_in_session(
                    db, sid, prepared=capture["prepared"]
                )
                self.services.idempotency.complete(
                    db,
                    reservation,
                    response=response,
                    status_code=202,
                    resource_kind="generation_run",
                    resource_id=response["id"],
                )
            return {"job_id": response["job_id"], "generation_run_id": response["id"]}

    def _link_child(self, operation_id, branch_id, outcome, principal, target_identity):
        with self.database.immediate_session() as db:
            operation = self._owned(db, operation_id, principal, target_identity)
            children = deepcopy(operation.children_json)
            child = next(row for row in children if row["branch_id"] == branch_id)
            child.update(outcome)
            if operation.cancel_requested and child.get("job_id"):
                self._request_child_cancel(db, child)
            operation.children_json = children
            operation.updated_at = m.utcnow()
            operation.revision += 1

    def _request_child_cancel(self, db, child):
        job = db.get(m.Job, child["job_id"]) if child.get("job_id") else None
        if job is None or job.status not in {"queued", "running", "retrying", "cancel_requested"}:
            return
        run = (
            db.get(m.GenerationRun, child["generation_run_id"])
            if child.get("generation_run_id")
            else None
        )
        if (
            run
            and run.job_id == job.id
            and run.session_id == child["session_id"]
            and run.status not in {"completed", "failed", "canceled"}
        ):
            self.services.generation.cancel_in_session(db, run.id)
        else:
            self.services.jobs.request_cancel_in_session(db, job.id)

    def execute(
        self,
        operation_id: str,
        supplied_digest: str,
        accepted_confirmations: list[str],
        *,
        principal: Principal,
        target_identity: dict,
        idempotency_key: object,
    ) -> dict:
        with self.database.immediate_session() as db:
            operation = self._owned(db, operation_id, principal, target_identity)
            self._validate_operation(db, operation, supplied_digest, target_identity)
            reservation = self.services.idempotency.begin(
                db,
                principal=principal,
                operation_id="executeProjectOperation",
                idempotency_key=idempotency_key,
                payload={
                    "id": operation_id,
                    "digest": supplied_digest,
                    "accepted_confirmations": sorted(set(accepted_confirmations)),
                },
            )
            children = deepcopy(operation.children_json)
            required = {
                value
                for child in children
                if child["eligible"]
                for value in child.get("preview", {}).get("required_confirmations", [])
            }
            if required - set(accepted_confirmations):
                raise ProjectOperationError(
                    "confirmation_required", "Accept every confirmation disclosed in this preview."
                )
            if not reservation.replayed:
                self.services.idempotency.complete(
                    db,
                    reservation,
                    response={"id": operation_id},
                    status_code=202,
                    resource_kind="project_operation",
                    resource_id=operation_id,
                )
            if not operation.cancel_requested:
                operation.status = "running"
        for child in children:
            if child["state"] != "pending":
                continue
            try:
                outcome = self._start_child(
                    operation_id, child, principal, target_identity, accepted_confirmations
                )
                outcome["state"] = "queued"
            except OperationCancelled as error:
                outcome = {"state": "canceled", "reason": str(error)}
            except (RevisionConflict, WorkflowPlanError, ValueError, KeyError) as error:
                outcome = {"state": "blocked", "reason": str(error)}
            self._link_child(operation_id, child["branch_id"], outcome, principal, target_identity)
        return self.get(operation_id, principal=principal, target_identity=target_identity)

    def _child_view(self, db, operation, stored):
        child = deepcopy(stored)
        if child.get("session_deleted"):
            return child
        if child["state"] == "existing":
            return child
        child.update(self._receipt(db, operation, child))
        job = db.get(m.Job, child["job_id"]) if child.get("job_id") else None
        if not job:
            if child.get("job_id"):
                child.update(state="failed", reason="The child job is unavailable.")
            return child
        child["progress"] = job.progress
        child["error"] = (
            {"code": job.error_code, "message": job.error_message}
            if job.error_code or job.error_message
            else None
        )
        result = job.result_json or {}
        run = (
            db.get(m.GenerationRun, child["generation_run_id"])
            if child.get("generation_run_id")
            else None
        )
        dispatch_id = next(
            (
                str(result.get(key) or "")
                for key in ("dispatch_run_id", "run_id", "id")
                if result.get(key) and db.get(m.DispatchRun, str(result[key]))
            ),
            None,
        )
        dispatch = db.get(m.DispatchRun, dispatch_id) if dispatch_id else None
        if (
            dispatch
            and dispatch.session_id == child["session_id"]
            and dispatch.kind == "translation"
        ):
            child.update(
                dispatch_run_id=dispatch.id,
                run_id=dispatch.id,
                manual_resume_required=dispatch.status != "completed",
            )
            if dispatch.status != "completed":
                child["state"] = "failed" if dispatch.status == "failed" else "awaiting_agent"
                return child
            result = {"artifact_id": dispatch.result_artifact_id}
        state = run.status if run else job.status
        child["state"] = {
            "queued": "queued",
            "running": "running",
            "retrying": "running",
            "cancel_requested": "canceling",
            "canceled": "canceled",
            "failed": "failed",
            "partial": "failed",
            "completed": "completed",
            "succeeded": "completed",
            "paused": "running",
        }.get(state, "running")
        if child["state"] == "completed" and operation.action != "generate":
            if operation.action == "export":
                child.update(self._export_result(db, operation, child, job, result))
                return child
            artifact = db.get(m.Artifact, str(result.get("artifact_id") or ""))
            matches = bool(artifact and artifact.session_id == child["session_id"])
            if operation.action == "translate" and artifact:
                language = (artifact.metadata_json or {}).get("language")
                source_id = (artifact.metadata_json or {}).get("source_artifact_id")
                guard = operation.preview_json["captures"][child["branch_id"]]["guard"]
                matches = matches and artifact.role == "translation" and artifact.state == "current"
                try:
                    language_matches = bool(language) and canonical_language(
                        str(language)
                    ) == canonical_language(child["target_language"])
                    file_exists = self.services.paths.managed_path(artifact.relative_path).is_file()
                except ValueError:
                    language_matches, file_exists = False, False
                matches = (
                    matches and language_matches and file_exists and bool(artifact.content_hash)
                )
                matches = matches and source_id == guard["checkpoint_artifact_id"]
                matches = (
                    matches
                    and db.get(m.ArtifactEdge, (guard["checkpoint_artifact_id"], artifact.id))
                    is not None
                )
                matches = matches and _aware(artifact.created_at) >= _aware(job.created_at)
            if not matches:
                child.update(
                    state="failed", reason="The completed child has no matching produced artifact."
                )
            else:
                child["result"] = {
                    "artifact_id": artifact.id,
                    "content_hash": artifact.content_hash,
                    "download_url": f"/api/v1/artifacts/{artifact.id}/content",
                }
        return child

    def _export_result(self, db, operation, child, job, result):
        capture = operation.preview_json["captures"][child["branch_id"]]
        plan = db.get(m.WorkflowExecutionPlan, capture["plan"]["plan_id"])
        payload = ((plan.plan_json or {}).get("_execution") or {}).get("payload") or {}
        settings = payload.get("settings") or {}
        roots = {
            str(value)
            for value in (
                capture["plan"]["source"].get("artifact_id"),
                payload.get("source_artifact_id"),
                (payload.get("export_contract") or {}).get("source_artifact_id"),
            )
            if value
        }
        generation_run_id = settings.get("generation_run_id")
        if generation_run_id:
            roots.update(
                db.scalars(
                    select(m.OutputAssembly.artifact_id).where(
                        m.OutputAssembly.session_id == child["session_id"],
                        m.OutputAssembly.generation_run_id == generation_run_id,
                    )
                )
            )
        artifact_ids = result.get("artifact_ids") or (
            [result["artifact_id"]] if result.get("artifact_id") else []
        )
        if not isinstance(artifact_ids, list) or not artifact_ids:
            return {"state": "failed", "reason": "The export produced no artifact receipts."}
        outputs = []
        for artifact_id in artifact_ids:
            artifact = db.get(m.Artifact, str(artifact_id))
            if (
                artifact is None
                or artifact.session_id != child["session_id"]
                or artifact.state != "current"
                or not artifact.role.startswith("export")
                or not artifact.content_hash
                or artifact.settings_hash != stable_hash(settings)
                or _aware(artifact.created_at) < _aware(job.created_at)
            ):
                return {
                    "state": "failed",
                    "reason": "An export artifact does not match the captured job settings or output role.",
                }
            try:
                exists = self.services.paths.managed_path(artifact.relative_path).is_file()
            except ValueError:
                exists = False
            pending, ancestors = [artifact.id], set()
            while pending and len(ancestors) < 1000:
                current = pending.pop()
                if current in ancestors:
                    continue
                ancestors.add(current)
                pending.extend(
                    db.scalars(
                        select(m.ArtifactEdge.parent_artifact_id).where(
                            m.ArtifactEdge.child_artifact_id == current,
                        )
                    )
                )
            if not exists or not roots.intersection(ancestors):
                return {
                    "state": "failed",
                    "reason": "An export artifact is missing or does not descend from its captured input.",
                }
            outputs.append(
                {
                    "artifact_id": artifact.id,
                    "content_hash": artifact.content_hash,
                    "download_url": f"/api/v1/artifacts/{artifact.id}/content",
                }
            )
        return {
            "result": {
                **outputs[0],
                "artifact_ids": [item["artifact_id"] for item in outputs],
                "artifacts": outputs,
            }
        }

    def get(self, operation_id: str, *, principal: Principal, target_identity: dict) -> dict:
        with self.database.session() as db:
            operation = self._owned(db, operation_id, principal, target_identity)
            children = [self._child_view(db, operation, child) for child in operation.children_json]
            active = any(
                child["state"] in {"queued", "running", "canceling", "awaiting_agent"}
                for child in children
            )
            success = any(child["state"] in {"completed", "existing"} for child in children)
            failed = any(child["state"] in {"failed", "blocked", "skipped"} for child in children)
            canceled = any(child["state"] == "canceled" for child in children)
            pending = any(child["state"] == "pending" for child in children)
            if operation.status == "preview":
                status = "preview"
            elif active or pending:
                status = "canceling" if operation.cancel_requested else "running"
            elif failed or canceled:
                if canceled and not failed and not success:
                    status = "canceled"
                else:
                    status = "partial" if success else "failed"
            else:
                status = "completed"
            response = {
                "id": operation.id,
                "project_id": operation.project_id,
                "action": operation.action,
                "export_kind": operation.preview_json["export_kind"],
                "status": status,
                "preview_digest": operation.preview_digest,
                "revision": operation.revision,
                "expected_project_revision": operation.preview_json["project_revision"],
                "created_at": operation.created_at.isoformat(),
                "expires_at": operation.expires_at.isoformat(),
                "expired": _aware(operation.expires_at) <= m.utcnow(),
                "cancel_requested": operation.cancel_requested,
                "previous_operation_id": operation.preview_json.get("previous_operation_id"),
                "eligible_count": sum(child["eligible"] for child in children),
                "skipped_count": sum(
                    child["state"] in {"skipped", "blocked"} for child in children
                ),
                "existing_count": sum(child["state"] == "existing" for child in children),
                "retrying_count": sum(
                    child["eligible"] and child["attempt"] > 1 for child in children
                ),
                "child_job_count": sum(child["eligible"] for child in children),
                "children": children,
            }
        return self.services.redactor.redact_value(response)

    def cancel(
        self,
        operation_id: str,
        *,
        principal: Principal,
        target_identity: dict,
        idempotency_key: object,
    ) -> dict:
        with self.database.immediate_session() as db:
            operation = self._owned(db, operation_id, principal, target_identity)
            reservation = self.services.idempotency.begin(
                db,
                principal=principal,
                operation_id="cancelProjectOperation",
                idempotency_key=idempotency_key,
                payload={"id": operation_id},
            )
            if reservation.replayed:
                return self.get(operation_id, principal=principal, target_identity=target_identity)
            children = deepcopy(operation.children_json)
            operation.cancel_requested = True
            operation.status = "canceling"
            for child in children:
                if child["state"] == "existing":
                    continue
                child.update(self._receipt(db, operation, child))
                job = db.get(m.Job, child["job_id"]) if child.get("job_id") else None
                if job and job.status in {"queued", "running", "retrying", "cancel_requested"}:
                    self._request_child_cancel(db, child)
                elif not job and child["state"] == "pending":
                    child["state"] = "canceled"
            operation.children_json = children
            operation.updated_at = m.utcnow()
            operation.revision += 1
            if not reservation.replayed:
                self.services.idempotency.complete(
                    db,
                    reservation,
                    response={"id": operation_id},
                    status_code=200,
                    resource_kind="project_operation",
                    resource_id=operation_id,
                )
        return self.get(operation_id, principal=principal, target_identity=target_identity)

    def retry_preview(
        self,
        operation_id: str,
        *,
        principal: Principal,
        target_identity: dict,
        expected_project_revision: int,
        idempotency_key: object,
    ) -> dict:
        previous = self.get(operation_id, principal=principal, target_identity=target_identity)
        return self.preview(
            previous["project_id"],
            [child["branch_id"] for child in previous["children"] if not child.get("session_deleted")],
            expected_project_revision=expected_project_revision,
            action=previous["action"],
            export_kind=previous["export_kind"],
            principal=principal,
            target_identity=target_identity,
            idempotency_key=idempotency_key,
            _retry_from=previous,
        )
