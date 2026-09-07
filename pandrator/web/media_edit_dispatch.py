"""Passive, provider-free whole-recording media-edit dispatch."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .database import Database
from .dispatch import DispatchError
from .media_edit import MediaEditRevisionConflict, MediaEditService
from .models import (
    MediaEditDispatchBatch,
    MediaEditDispatchRun,
    MediaEditPlan,
    MediaEditPlanRevision,
    SessionRecord,
    utcnow,
)

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _active_lease(value: datetime | None, now: datetime) -> bool:
    expires_at = _aware(value)
    return expires_at is not None and expires_at > now


def _expired_lease(value: datetime | None, now: datetime) -> bool:
    expires_at = _aware(value)
    return expires_at is not None and expires_at <= now


class MediaEditDispatchRunService:
    """Durable one-batch dispatch for global media-edit reasoning."""

    def __init__(self, database: Database, media_edit: MediaEditService):
        self.database = database
        self.media_edit = media_edit

    @staticmethod
    def _run_payload(run: MediaEditDispatchRun) -> dict[str, Any]:
        result_revision = (
            run.source_revision_number + 1 if run.result_revision_id else None
        )
        return {
            "id": run.id,
            "run_id": run.id,
            "session_id": run.session_id,
            "kind": "media_edit",
            "source_revision_id": run.source_revision_id,
            "source_revision": run.source_revision_number,
            "source_revision_number": run.source_revision_number,
            "source_content_hash": run.source_content_hash,
            "instructions": run.instructions,
            "input_hash": run.input_hash,
            "status": run.status,
            "batch_count": run.batch_count,
            "total_batches": run.batch_count,
            "completed_batch_count": run.completed_batch_count,
            "completed_batches": run.completed_batch_count,
            "accepted_batch_count": run.completed_batch_count,
            "remaining_batch_count": max(
                0, run.batch_count - run.completed_batch_count
            ),
            "result_revision_id": run.result_revision_id,
            "result_revision": result_revision,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

    @classmethod
    def _detail_payload(
        cls,
        run: MediaEditDispatchRun,
        batch: MediaEditDispatchBatch | None,
    ) -> dict[str, Any]:
        payload = cls._run_payload(run)
        payload["batches"] = (
            [
                {
                    "id": batch.id,
                    "batch_id": batch.id,
                    "batch_ordinal": batch.ordinal + 1,
                    "status": batch.status,
                    "lease_expires_at": _iso(batch.lease_expires_at),
                    "accepted_at": _iso(batch.accepted_at),
                }
            ]
            if batch is not None
            else []
        )
        return payload

    def list_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
        db_session: Session | None = None,
    ) -> list[dict[str, Any]]:
        if db_session is None:
            with self.database.session() as session:
                return self.list_runs(session_id, limit=limit, db_session=session)
        runs = list(
            db_session.scalars(
                select(MediaEditDispatchRun)
                .where(MediaEditDispatchRun.session_id == session_id)
                .order_by(
                    MediaEditDispatchRun.created_at.desc(),
                    MediaEditDispatchRun.id.desc(),
                )
                .limit(max(1, min(int(limit), 100)))
            ).all()
        )
        return [self._run_payload(run) for run in runs]

    def get(
        self,
        run_id: str,
        *,
        db_session: Session | None = None,
    ) -> dict[str, Any]:
        if db_session is None:
            with self.database.session() as session:
                return self.get(run_id, db_session=session)
        run = db_session.get(MediaEditDispatchRun, run_id)
        if run is None:
            raise DispatchError("not_found", "Media-edit dispatch run not found.", 404)
        batch = db_session.scalar(
            select(MediaEditDispatchBatch).where(
                MediaEditDispatchBatch.dispatch_run_id == run.id
            )
        )
        return self._detail_payload(run, batch)

    @staticmethod
    def _cue_evidence(revision: MediaEditPlanRevision) -> list[dict[str, Any]]:
        cues: list[dict[str, Any]] = []
        for raw in revision.cues_json or []:
            if not isinstance(raw, dict):
                continue
            start_ms = int(raw.get("start_ms") or 0)
            end_ms = int(raw.get("end_ms") or 0)
            if end_ms <= 0 or start_ms >= revision.duration_ms:
                continue
            # Deliberately project cue metadata only. Word arrays are private
            # boundary-refinement evidence and never cross the dispatch API.
            # Cues wholly outside the recording cannot produce a meaningful cut.
            cues.append(
                {
                    "id": str(raw.get("id") or ""),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": str(raw.get("text") or ""),
                    "speaker": raw.get("speaker"),
                    "timing_source": str(raw.get("timing_source") or "caption"),
                    "timing_confidence": raw.get("timing_confidence"),
                }
            )
        return cues

    @staticmethod
    def _artifact_ids(revision: MediaEditPlanRevision) -> dict[str, str | None]:
        return {
            "source_media_artifact_id": revision.source_media_artifact_id,
            "editorial_transcript_artifact_id": revision.editorial_transcript_artifact_id,
            "timing_artifact_id": revision.timing_artifact_id,
        }

    @staticmethod
    def _alignment_evidence(revision: MediaEditPlanRevision) -> dict[str, Any]:
        source = dict(revision.evidence_json or {})
        scalar_keys = (
            "source_kind",
            "alignment_coverage",
            "alignment_eligible_coverage",
            "alignment_confidence",
            "alignment_quality",
            "timing_available",
            "alignment_method",
            "alignment_engine",
            "alignment_model",
            "timing_quality_basis",
            "alignment_request_strategy",
            "alignment_endpoint_strategy",
            "alignment_artifact_reused",
            "word_count",
            "cue_count",
            "fallback_triggered",
        )
        evidence = {key: source[key] for key in scalar_keys if key in source}
        counts = source.get("alignment_counts")
        if isinstance(counts, dict):
            evidence["alignment_counts"] = dict(counts)
        evidence["warnings"] = list(source.get("warnings") or [])
        return evidence

    def create_in_session(
        self,
        session: Session,
        *,
        session_id: str,
        revision: int,
        instructions: str,
    ) -> dict[str, Any]:
        record = session.get(SessionRecord, session_id)
        if record is None or record.trashed_at is not None:
            raise DispatchError("not_found", "Session not found.", 404)
        if record.workflow_kind != "media_edit":
            raise DispatchError(
                "ineligible_session",
                "Media-edit dispatch requires a media_edit workflow session.",
                422,
            )
        plan = session.scalar(
            select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
        )
        active = (
            session.get(MediaEditPlanRevision, plan.active_revision_id)
            if plan and plan.active_revision_id
            else None
        )
        if active is None or active.revision_number != int(revision):
            raise DispatchError(
                "revision_conflict",
                "The requested revision is not the active prepared media-edit revision.",
                409,
                details={
                    "requested_revision": int(revision),
                    "current_revision": active.revision_number if active else None,
                },
            )
        clean_instructions = str(instructions or "").strip()
        if not clean_instructions:
            raise DispatchError(
                "invalid_instructions",
                "Media-edit instructions must not be blank.",
                422,
            )
        packet = {
            "source_revision": {
                "id": active.id,
                "revision": active.revision_number,
                "content_hash": active.content_hash,
            },
            "duration_ms": int(active.duration_ms),
            "keep_ranges": list(active.keep_ranges_json or []),
            "cues": self._cue_evidence(active),
            "evidence": self._alignment_evidence(active),
            "artifact_ids": self._artifact_ids(active),
        }
        semantic_input = {
            "kind": "media_edit",
            "session_id": session_id,
            "source_revision": packet["source_revision"],
            "instructions": clean_instructions,
            "packet": packet,
        }
        run = MediaEditDispatchRun(
            session_id=session_id,
            source_revision_id=active.id,
            source_revision_number=active.revision_number,
            source_content_hash=active.content_hash,
            instructions=clean_instructions,
            settings_json={"instructions": clean_instructions},
            input_hash=_canonical_hash(semantic_input),
            status="ready",
            batch_count=1,
            completed_batch_count=0,
        )
        session.add(run)
        session.flush()
        session.add(
            MediaEditDispatchBatch(
                dispatch_run_id=run.id,
                ordinal=0,
                input_json=packet,
                input_hash=_canonical_hash(packet),
                status="ready",
            )
        )
        session.flush()
        return self._run_payload(run)

    @staticmethod
    def _claim_response(
        run: MediaEditDispatchRun,
        batch: MediaEditDispatchBatch,
    ) -> dict[str, Any]:
        packet = dict(batch.input_json or {})
        cues = list(packet.get("cues") or [])
        return {
            "schema_version": "1",
            "run_id": run.id,
            "batch_id": batch.id,
            "batch_ordinal": 1,
            "status": batch.status,
            "run_status": run.status,
            "batch_status": batch.status,
            "source_revision": dict(packet.get("source_revision") or {}),
            "lease_token": batch.lease_token,
            "lease_expires_at": _iso(batch.lease_expires_at),
            "task": {
                "kind": "media_edit",
                "instructions": (
                    "Reason globally over the complete recording and return only "
                    "whole-cue removal spans. The packet omits transcript cues wholly "
                    "outside the source-media duration. Do not return word arrays, "
                    "revised transcript text, or provider/model data. Use "
                    "start_at_media_start=true for captionless material before the "
                    "first cue, and end_at_media_end=true for trailing material after "
                    "the last cue. Empty cuts is valid when no removal is warranted."
                    + (
                        f"\n\nUser instructions:\n{run.instructions}"
                        if run.instructions
                        else ""
                    )
                ),
                "result_contract": {
                    "kind": "media_edit",
                    "cuts": [
                        {
                            "start_cue_id": "string (omit when start_at_media_start=true)",
                            "start_at_media_start": "boolean, default false",
                            "end_cue_id": "string (omit when end_at_media_end=true)",
                            "end_at_media_end": "boolean, default false",
                            "reason": "nonblank string, max 500 characters",
                        }
                    ],
                    "max_cuts": 1000,
                    "allow_empty": True,
                },
            },
            "batch": {
                "duration_ms": packet.get("duration_ms"),
                "keep_ranges": packet.get("keep_ranges") or [],
                "cues": cues,
                "cue_count": len(cues),
                "valid_cue_ids": [str(item.get("id")) for item in cues],
                "evidence": packet.get("evidence") or {},
                "artifact_ids": packet.get("artifact_ids") or {},
            },
        }

    def claim_in_session(
        self,
        session: Session,
        *,
        run_id: str,
        claim_key: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        run = session.get(MediaEditDispatchRun, run_id)
        if run is None:
            raise DispatchError("not_found", "Media-edit dispatch run not found.", 404)
        batch = session.scalar(
            select(MediaEditDispatchBatch).where(
                MediaEditDispatchBatch.dispatch_run_id == run.id
            )
        )
        if batch is None:
            raise DispatchError(
                "batch_not_ready", "The dispatch batch is missing.", 409
            )
        now = utcnow()
        if batch.claim_key == claim_key and (
            batch.status == "completed"
            or (batch.status == "leased" and _active_lease(batch.lease_expires_at, now))
        ):
            return self._claim_response(run, batch)
        if run.status == "finalizing":
            raise DispatchError(
                "run_finalizing",
                "The accepted run is finalizing its media-edit revision.",
                409,
                retryable=True,
            )
        if run.status == "completed":
            raise DispatchError("run_completed", "The dispatch run is complete.", 409)
        if run.status == "failed":
            raise DispatchError(
                run.error_code or "run_failed",
                run.error_message or "The dispatch run failed.",
                409,
            )
        if batch.status == "leased" and _expired_lease(batch.lease_expires_at, now):
            batch.status = "ready"
            batch.lease_token = None
            batch.claim_key = None
            batch.lease_expires_at = None
            batch.updated_at = now
        if batch.status == "leased":
            expires_at = _aware(batch.lease_expires_at) or now
            raise DispatchError(
                "run_busy",
                "Another worker holds the active media-edit batch.",
                409,
                retryable=True,
                details={
                    "batch_id": batch.id,
                    "retry_after_seconds": max(
                        1, int((expires_at - now).total_seconds())
                    ),
                },
            )
        if batch.status == "completed":
            raise DispatchError(
                "run_finalizing", "The dispatch run is finalizing.", 409
            )
        batch.status = "leased"
        batch.lease_token = secrets.token_urlsafe(32)
        batch.claim_key = claim_key
        batch.lease_expires_at = now + timedelta(seconds=int(lease_seconds))
        batch.updated_at = now
        run.status = "running"
        run.updated_at = now
        session.flush()
        return self._claim_response(run, batch)

    def renew_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        batch = session.get(MediaEditDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        now = utcnow()
        if batch.status != "leased" or batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if not _active_lease(batch.lease_expires_at, now):
            raise DispatchError(
                "lease_expired", "The dispatch lease has expired.", 409, retryable=True
            )
        batch.lease_expires_at = now + timedelta(seconds=int(lease_seconds))
        batch.updated_at = now
        session.flush()
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "lease_expires_at": _iso(batch.lease_expires_at),
        }

    def release_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        batch = session.get(MediaEditDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        if batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if batch.status == "completed":
            raise DispatchError(
                "batch_completed", "The dispatch batch is already completed.", 409
            )
        batch.status = "ready"
        batch.lease_token = None
        batch.claim_key = None
        batch.lease_expires_at = None
        batch.updated_at = utcnow()
        session.flush()
        return {"batch_id": batch.id, "status": "ready", "lease_expires_at": None}

    @staticmethod
    def _normalize_result(
        batch: MediaEditDispatchBatch,
        result: object,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise DispatchError(
                "invalid_model_response", "Media-edit result must be an object.", 422
            )
        if result.get("kind") != "media_edit":
            raise DispatchError(
                "result_kind_mismatch", "This batch requires a media_edit result.", 422
            )
        cuts = result.get("cuts")
        if not isinstance(cuts, list):
            raise DispatchError(
                "invalid_model_response", "Media-edit result cuts must be a list.", 422
            )
        if len(cuts) > 1000:
            raise DispatchError(
                "invalid_model_response",
                "Media-edit results may contain at most 1000 cuts.",
                422,
            )
        valid_cues = list((batch.input_json or {}).get("cues") or [])
        cue_positions = {
            str(cue.get("id")): index for index, cue in enumerate(valid_cues)
        }
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in cuts:
            if not isinstance(item, dict):
                raise DispatchError(
                    "invalid_model_response",
                    "Every media-edit cut must be an object.",
                    422,
                )
            start_at_media_start = bool(item.get("start_at_media_start", False))
            end_at_media_end = bool(item.get("end_at_media_end", False))
            start_id = item.get("start_cue_id")
            end_id = item.get("end_cue_id")
            reason = item.get("reason")
            if (start_id is not None) == start_at_media_start:
                raise DispatchError(
                    "invalid_model_response",
                    "Exactly one start_cue_id or start_at_media_start=true is required.",
                    422,
                )
            if (end_id is not None) == end_at_media_end:
                raise DispatchError(
                    "invalid_model_response",
                    "Exactly one end_cue_id or end_at_media_end=true is required.",
                    422,
                )
            if (not start_at_media_start and not isinstance(start_id, str)) or (
                not end_at_media_end and not isinstance(end_id, str)
            ):
                raise DispatchError(
                    "invalid_model_response", "Cut cue IDs must be strings.", 422
                )
            if not isinstance(reason, str):
                raise DispatchError(
                    "invalid_model_response", "Cut reason must be a string.", 422
                )
            start_id = start_id.strip() if isinstance(start_id, str) else ""
            end_id = end_id.strip() if isinstance(end_id, str) else ""
            reason = reason.strip()
            if not start_at_media_start and start_id not in cue_positions:
                raise DispatchError(
                    "invalid_model_response", "Cut references an unknown cue ID.", 422
                )
            if not end_at_media_end and end_id not in cue_positions:
                raise DispatchError(
                    "invalid_model_response", "Cut references an unknown cue ID.", 422
                )
            start_position = -1 if start_at_media_start else cue_positions[start_id]
            end_position = (
                len(valid_cues) if end_at_media_end else cue_positions[end_id]
            )
            if start_position > end_position:
                raise DispatchError(
                    "invalid_model_response", "Cut cue IDs are out of order.", 422
                )
            if not reason:
                raise DispatchError(
                    "invalid_model_response", "Cut reasons must not be empty.", 422
                )
            if len(reason) > 500:
                raise DispatchError(
                    "invalid_model_response",
                    "Cut reasons must be at most 500 characters.",
                    422,
                )
            pair = (
                "__media_start__" if start_at_media_start else start_id,
                "__media_end__" if end_at_media_end else end_id,
            )
            if pair in seen:
                raise DispatchError(
                    "invalid_model_response", "Cut cue-ID pairs must be unique.", 422
                )
            seen.add(pair)
            normalized.append(
                {
                    "start_cue_id": None if start_at_media_start else start_id,
                    "start_at_media_start": start_at_media_start,
                    "end_cue_id": None if end_at_media_end else end_id,
                    "end_at_media_end": end_at_media_end,
                    "reason": reason,
                }
            )
        return {"kind": "media_edit", "cuts": normalized}

    @staticmethod
    def _submit_payload(
        run: MediaEditDispatchRun,
        batch: MediaEditDispatchBatch,
    ) -> dict[str, Any]:
        result_revision = (
            run.source_revision_number + 1 if run.result_revision_id else None
        )
        return {
            "run_id": run.id,
            "batch_id": batch.id,
            "session_id": run.session_id,
            "kind": "media_edit",
            "status": run.status,
            "run_status": run.status,
            "batch_status": batch.status,
            "accepted": batch.status == "completed",
            "batch_count": run.batch_count,
            "total_batches": run.batch_count,
            "completed_batch_count": run.completed_batch_count,
            "completed_batches": run.completed_batch_count,
            "remaining_batches": max(0, run.batch_count - run.completed_batch_count),
            "finalized": run.status == "completed",
            "source_revision_number": run.source_revision_number,
            "result_revision_id": run.result_revision_id,
            "result_revision": result_revision,
            "error_code": run.error_code,
            "error_message": run.error_message,
        }

    def _materialize(
        self,
        session: Session,
        run: MediaEditDispatchRun,
        batch: MediaEditDispatchBatch,
    ) -> None:
        plan = session.scalar(
            select(MediaEditPlan).where(MediaEditPlan.session_id == run.session_id)
        )
        active = (
            session.get(MediaEditPlanRevision, plan.active_revision_id)
            if plan and plan.active_revision_id
            else None
        )
        if (
            active is None
            or active.id != run.source_revision_id
            or active.revision_number != run.source_revision_number
            or active.content_hash != run.source_content_hash
        ):
            raise DispatchError(
                "finalization_conflict",
                "The pinned media-edit revision is no longer active; the dispatch was not rebased.",
                409,
                details={
                    "batch_accepted": True,
                    "source_revision_id": run.source_revision_id,
                    "source_revision_number": run.source_revision_number,
                },
            )
        proposal = dict(
            batch.normalized_output_json or {"kind": "media_edit", "cuts": []}
        )
        try:
            state = self.media_edit.apply_proposal_in_session(
                session,
                session_id=run.session_id,
                expected_revision=run.source_revision_number,
                cuts=list(proposal.get("cuts") or []),
                allow_empty=True,
                provenance={"dispatch_run_id": run.id},
                proposal_instructions=run.instructions,
                reject_duplicate_pairs=True,
            )
        except MediaEditRevisionConflict as error:
            raise DispatchError(
                "finalization_conflict",
                str(error),
                409,
                details={"batch_accepted": True},
            ) from error
        new_revision = (state.get("plan") or {}).get("revision_id")
        if not new_revision:
            raise DispatchError(
                "materialization_failed",
                "The media-edit revision was not created.",
                409,
            )
        run.result_revision_id = str(new_revision)
        run.status = "completed"
        run.error_code = None
        run.error_message = None
        run.updated_at = utcnow()

    def _retry_finalize(
        self,
        session: Session,
        run: MediaEditDispatchRun,
        batch: MediaEditDispatchBatch,
    ) -> None:
        try:
            with session.begin_nested():
                self._materialize(session, run, batch)
        except DispatchError as error:
            run.status = "failed"
            run.error_code = error.code
            run.error_message = str(error)[:2000]
            run.updated_at = utcnow()
            details = dict(error.details) if isinstance(error.details, dict) else {}
            details.update({"batch_accepted": True, "run_id": run.id})
            error.details = details
            session.flush()
            raise
        except (TypeError, ValueError) as error:
            run.status = "failed"
            run.error_code = "materialization_rejected"
            run.error_message = str(error)[:2000]
            run.updated_at = utcnow()
            session.flush()
            raise DispatchError(
                "materialization_rejected",
                str(error),
                422,
                details={"batch_accepted": True, "run_id": run.id},
            ) from error
        except (OSError, RuntimeError, SQLAlchemyError) as error:
            run.status = "finalizing"
            run.error_code = "materialization_failed"
            run.error_message = str(error)[:2000]
            run.updated_at = utcnow()
            session.flush()

    def submit_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        submission_key: str,
        result: object,
    ) -> tuple[dict[str, Any], int]:
        batch = session.get(MediaEditDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        run = session.get(MediaEditDispatchRun, batch.dispatch_run_id)
        if run is None:
            raise DispatchError("not_found", "Media-edit dispatch run not found.", 404)
        raw = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise DispatchError(
                "response_too_large",
                "Media-edit response exceeds the 4 MiB limit.",
                413,
            )
        raw_hash = _canonical_hash(result)
        if batch.status == "completed":
            if batch.submission_key == submission_key and batch.output_hash == raw_hash:
                if run.status == "finalizing":
                    self._retry_finalize(session, run, batch)
                    session.flush()
                return self._submit_payload(
                    run, batch
                ), 200 if run.status == "completed" else 202
            raise DispatchError(
                "batch_completed",
                "The dispatch batch already has a different accepted submission.",
                409,
            )
        now = utcnow()
        if batch.status != "leased" or batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if not _active_lease(batch.lease_expires_at, now):
            raise DispatchError(
                "lease_expired", "The dispatch lease has expired.", 409, retryable=True
            )
        normalized = self._normalize_result(batch, result)
        batch.status = "completed"
        batch.normalized_output_json = normalized
        batch.output_hash = raw_hash
        batch.submission_key = submission_key
        batch.accepted_at = now
        batch.lease_expires_at = None
        batch.updated_at = now
        run.completed_batch_count = 1
        run.status = "finalizing"
        run.error_code = None
        run.error_message = None
        run.updated_at = now
        self._retry_finalize(session, run, batch)
        session.flush()
        return self._submit_payload(
            run, batch
        ), 200 if run.status == "completed" else 202
