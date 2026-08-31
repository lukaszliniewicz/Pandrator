"""Passive external whole-document speech-text optimisation dispatch."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt

from .artifact_selection import ROLE_TO_STAGE
from .artifacts import ArtifactService, sha256_file
from .database import Database
from .dispatch import DispatchError
from .models import (
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionRecord,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    SpeechOptimizationDispatchBatch,
    SpeechOptimizationDispatchRun,
    utcnow,
)

_SOURCE_ROLES_BY_WORKFLOW = {
    "audiobook": ("prepared_text", "clean_text", "upload"),
    "voiceover": ("translation", "correction", "transcription", "upload"),
}
_SUPPORTED_SUFFIXES = frozenset({".srt", ".json", ".txt"})


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _response_hash(value: Any) -> str:
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return _canonical_hash(value)


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


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _json_source_text(row: object) -> str:
    if not isinstance(row, dict):
        return str(row)
    return str(
        row.get("source_text")
        or row.get("text")
        or row.get("processed_sentence")
        or row.get("original_sentence")
        or ""
    )


def _partition_units(
    units: list[dict[str, Any]],
    *,
    char_limit: int,
    max_units: int,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for unit in units:
        unit_chars = len(str(unit.get("text") or ""))
        if current and (
            len(current) >= max_units or current_chars + unit_chars > char_limit
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += unit_chars
    if current:
        batches.append(current)
    return batches


class SpeechOptimizationDispatchRunService:
    """Persistence and validation for provider-free speech-text optimisation."""

    def __init__(
        self,
        database: Database,
        artifacts: ArtifactService,
        session_dir_resolver,
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.session_dir_resolver = session_dir_resolver

    @staticmethod
    def _run_payload(run: SpeechOptimizationDispatchRun) -> dict[str, Any]:
        settings = dict(run.settings_json or {})
        return {
            "id": run.id,
            "session_id": run.session_id,
            "kind": "speech_optimization",
            "output_role": "tts_optimized",
            "source_artifact_id": run.source_artifact_id,
            "source_state": run.source_state,
            "source_content_hash": run.source_content_hash,
            "source_format": run.source_format,
            "language": run.language,
            "voice_language": run.voice_language,
            "tts_service": run.tts_service,
            "instructions": str(settings.get("instructions") or ""),
            "char_limit": int(settings.get("char_limit") or 20_000),
            "max_units_per_batch": int(settings.get("max_units_per_batch") or 100),
            "context_before": int(settings.get("context_before") or 4),
            "context_after": int(settings.get("context_after") or 2),
            "include_timing": bool(settings.get("include_timing", True)),
            "status": run.status,
            "batch_count": run.batch_count,
            "total_batches": run.batch_count,
            "completed_batch_count": run.completed_batch_count,
            "accepted_batch_count": run.completed_batch_count,
            "remaining_batch_count": max(
                0, run.batch_count - run.completed_batch_count
            ),
            "result_artifact_id": run.result_artifact_id,
            "final_artifact_id": run.result_artifact_id,
            "result_revision_id": run.result_revision_id,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

    @classmethod
    def _run_detail_payload(
        cls,
        run: SpeechOptimizationDispatchRun,
        batches: list[SpeechOptimizationDispatchBatch],
    ) -> dict[str, Any]:
        payload = cls._run_payload(run)
        payload["batches"] = [
            {
                "id": batch.id,
                "batch_ordinal": batch.ordinal + 1,
                "status": batch.status,
                "lease_expires_at": _iso(batch.lease_expires_at),
                "accepted_at": _iso(batch.accepted_at),
            }
            for batch in batches
        ]
        return payload

    def list_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
        db_session: Session | None = None,
    ) -> list[dict[str, Any]]:
        if db_session is not None:
            runs = list(
                db_session.scalars(
                    select(SpeechOptimizationDispatchRun)
                    .where(SpeechOptimizationDispatchRun.session_id == session_id)
                    .order_by(
                        SpeechOptimizationDispatchRun.created_at.desc(),
                        SpeechOptimizationDispatchRun.id.desc(),
                    )
                    .limit(max(1, min(int(limit), 100)))
                ).all()
            )
            return [self._run_payload(run) for run in runs]
        with self.database.session() as session:
            return self.list_runs(session_id, limit=limit, db_session=session)

    def get(self, run_id: str, *, db_session: Session | None = None) -> dict[str, Any]:
        if db_session is not None:
            run = db_session.get(SpeechOptimizationDispatchRun, run_id)
            if run is None:
                raise DispatchError(
                    "not_found", "Speech-optimisation dispatch run not found.", 404
                )
            batches = list(
                db_session.scalars(
                    select(SpeechOptimizationDispatchBatch)
                    .where(SpeechOptimizationDispatchBatch.dispatch_run_id == run.id)
                    .order_by(SpeechOptimizationDispatchBatch.ordinal)
                ).all()
            )
            return self._run_detail_payload(run, batches)
        with self.database.session() as session:
            return self.get(run_id, db_session=session)

    def _load_source(
        self,
        session: Session,
        *,
        session_id: str,
        workflow_kind: str,
        source_artifact_id: str | None,
    ) -> tuple[Artifact, Path, str]:
        eligible_roles = _SOURCE_ROLES_BY_WORKFLOW[workflow_kind]
        if source_artifact_id:
            artifact = session.get(Artifact, source_artifact_id)
            if artifact is None:
                raise DispatchError("not_found", "Source artifact not found.", 404)
            if artifact.session_id != session_id and not self._is_attached_source(
                session,
                session_id=session_id,
                artifact_id=artifact.id,
            ):
                raise DispatchError(
                    "source_session_mismatch",
                    "Source artifact does not belong to or remain attached to this session.",
                    409,
                )
            if artifact.role not in eligible_roles:
                raise DispatchError(
                    "ineligible_source",
                    "The selected artifact is not an eligible speech-text source "
                    f"for a {workflow_kind} session.",
                    422,
                )
        else:
            candidates = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.role.in_(eligible_roles),
                        Artifact.state == "current",
                    )
                    .order_by(
                        case(
                            {role: index for index, role in enumerate(eligible_roles)},
                            value=Artifact.role,
                            else_=99,
                        ),
                        Artifact.created_at.desc(),
                        Artifact.id.desc(),
                    )
                ).all()
            )
            attached = list(
                session.scalars(
                    select(Artifact)
                    .join(SourceAsset, SourceAsset.artifact_id == Artifact.id)
                    .join(
                        SessionSource,
                        SessionSource.source_asset_id == SourceAsset.id,
                    )
                    .where(
                        SessionSource.session_id == session_id,
                        SessionSource.is_current.is_(True),
                        SourceAsset.state == "current",
                        Artifact.role.in_(eligible_roles),
                        Artifact.state == "current",
                    )
                    .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                ).all()
            )
            known_ids = {candidate.id for candidate in candidates}
            candidates.extend(
                candidate for candidate in attached if candidate.id not in known_ids
            )
            role_priority = {role: index for index, role in enumerate(eligible_roles)}
            candidates.sort(
                key=lambda candidate: (
                    role_priority.get(candidate.role, 99),
                    -candidate.created_at.timestamp(),
                    candidate.id,
                )
            )
            artifact = next(
                (
                    candidate
                    for candidate in candidates
                    if Path(candidate.relative_path).suffix.lower()
                    in _SUPPORTED_SUFFIXES
                ),
                None,
            )
            if artifact is None:
                raise DispatchError(
                    "source_not_found",
                    "No current SRT, JSON, or TXT speech-text source was found. "
                    "Prepare or transcribe the source first, or select an eligible "
                    "artifact explicitly.",
                    422,
                )
        if artifact.state == "deleted":
            raise DispatchError(
                "source_deleted", "The source artifact was deleted.", 409
            )
        if not artifact.content_hash:
            raise DispatchError(
                "source_hash_missing", "The source artifact has no content hash.", 409
            )
        try:
            path = self.artifacts.paths.managed_path(artifact.relative_path)
            actual_hash = sha256_file(path)
        except (OSError, ValueError) as error:
            raise DispatchError(
                "source_unavailable",
                "The source artifact content is unavailable.",
                409,
                retryable=True,
            ) from error
        if actual_hash != artifact.content_hash:
            raise DispatchError(
                "source_changed",
                "The source artifact content no longer matches its stored hash.",
                409,
            )
        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            raise DispatchError(
                "unsupported_source_format",
                "Passive speech optimisation supports SRT, JSON, and TXT sources.",
                422,
            )
        return artifact, path, suffix.removeprefix(".")

    @staticmethod
    def _is_attached_source(
        session: Session,
        *,
        session_id: str,
        artifact_id: str,
    ) -> bool:
        return (
            session.scalar(
                select(SessionSource.id)
                .join(
                    SourceAsset,
                    SourceAsset.id == SessionSource.source_asset_id,
                )
                .where(
                    SessionSource.session_id == session_id,
                    SessionSource.is_current.is_(True),
                    SourceAsset.state == "current",
                    SourceAsset.artifact_id == artifact_id,
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _source_units(
        path: Path,
        source_format: str,
        *,
        language: str,
        include_timing: bool,
    ) -> list[dict[str, Any]]:
        try:
            if source_format == "srt":
                segments = parse_srt(path.read_text(encoding="utf-8-sig"))
                units = []
                for ordinal, segment in enumerate(segments, start=1):
                    text = _clean_text(segment.text)
                    if not text:
                        continue
                    unit: dict[str, Any] = {
                        "unit_id": ordinal,
                        "text": text,
                        "language": language,
                        "speaker": _clean_text(segment.speaker) or None,
                    }
                    if include_timing:
                        unit["timing"] = {
                            "start_ms": int(segment.start_ms),
                            "end_ms": int(segment.end_ms),
                            "duration_ms": int(segment.end_ms - segment.start_ms),
                        }
                    units.append(unit)
                return units
            if source_format == "json":
                rows = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(rows, list):
                    raise DispatchError(
                        "source_invalid",
                        "Speech-optimisation JSON must contain a list of units.",
                        422,
                    )
                units = []
                for ordinal, row in enumerate(rows, start=1):
                    text = _clean_text(_json_source_text(row))
                    if not text:
                        continue
                    row_language = (
                        _clean_text(row.get("language"))
                        if isinstance(row, dict)
                        else ""
                    )
                    units.append(
                        {
                            "unit_id": ordinal,
                            "text": text,
                            "language": row_language or language,
                            "speaker": (
                                _clean_text(row.get("speaker")) or None
                                if isinstance(row, dict)
                                else None
                            ),
                        }
                    )
                return units
            text = path.read_text(encoding="utf-8-sig")
            return (
                [{"unit_id": 1, "text": text.strip(), "language": language}]
                if text.strip()
                else []
            )
        except DispatchError:
            raise
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
            raise DispatchError(
                "source_invalid",
                f"The speech-optimisation source cannot be read: {error}",
                422,
            ) from error

    @staticmethod
    def _selection_snapshot(
        session: Session,
        session_id: str,
        stage_keys: set[str],
    ) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for stage_key in sorted(stage_keys):
            row = session.get(SessionStageSelection, (session_id, stage_key))
            snapshot[stage_key] = {
                "exists": row is not None,
                "artifact_id": row.artifact_id if row is not None else None,
                "revision": int(row.revision) if row is not None else 0,
            }
        return snapshot

    def create_in_session(
        self,
        session: Session,
        *,
        session_id: str,
        source_artifact_id: str | None,
        language: str | None,
        voice_language: str | None,
        tts_service: str | None,
        instructions: str,
        char_limit: int,
        max_units_per_batch: int,
        context_before: int,
        context_after: int,
        include_timing: bool,
    ) -> dict[str, Any]:
        record = session.get(SessionRecord, session_id)
        if record is None or record.trashed_at is not None:
            raise DispatchError("not_found", "Session not found.", 404)
        if record.workflow_kind not in {"audiobook", "voiceover"}:
            raise DispatchError(
                "ineligible_session",
                "Speech optimisation requires an audiobook or voiceover session.",
                422,
            )
        source, source_path, source_format = self._load_source(
            session,
            session_id=session_id,
            workflow_kind=record.workflow_kind,
            source_artifact_id=source_artifact_id,
        )
        selected_language = (
            _clean_text(language)
            or _clean_text((source.metadata_json or {}).get("language"))
            or (
                _clean_text(record.target_language)
                if source.role == "translation"
                else _clean_text(record.source_language)
            )
            or "auto"
        )
        selected_voice_language = _clean_text(voice_language) or selected_language
        selected_service = _clean_text(tts_service) or None
        units = self._source_units(
            source_path,
            source_format,
            language=selected_language,
            include_timing=bool(include_timing),
        )
        if not units:
            raise DispatchError(
                "source_empty",
                "The source contains no non-empty speech-text units.",
                422,
            )
        batches = _partition_units(
            units,
            char_limit=int(char_limit),
            max_units=int(max_units_per_batch),
        )
        settings = {
            "instructions": str(instructions or ""),
            "char_limit": int(char_limit),
            "max_units_per_batch": int(max_units_per_batch),
            "context_before": int(context_before),
            "context_after": int(context_after),
            "include_timing": bool(include_timing),
        }
        stage_keys = {"optimize_tts"}
        source_stage = ROLE_TO_STAGE.get(source.role)
        if source_stage:
            stage_keys.add(source_stage)
        selection_snapshot = self._selection_snapshot(session, session_id, stage_keys)
        output_head = session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.role == "tts_optimized",
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
        semantic_input = {
            "kind": "speech_optimization",
            "session_id": session_id,
            "source_artifact_id": source.id,
            "source_content_hash": source.content_hash,
            "source_format": source_format,
            "language": selected_language,
            "voice_language": selected_voice_language,
            "tts_service": selected_service,
            "settings": settings,
            "selection_snapshot": selection_snapshot,
            "batches": batches,
        }
        run = SpeechOptimizationDispatchRun(
            session_id=session_id,
            source_artifact_id=source.id,
            source_state=source.state,
            source_content_hash=str(source.content_hash),
            source_format=source_format,
            language=selected_language,
            voice_language=selected_voice_language,
            tts_service=selected_service,
            settings_json=settings,
            selection_snapshot_json=selection_snapshot,
            input_hash=_canonical_hash(semantic_input),
            output_head_artifact_id=output_head.id if output_head else None,
            status="ready",
            batch_count=len(batches),
            completed_batch_count=0,
        )
        session.add(run)
        session.flush()
        for ordinal, batch_units in enumerate(batches):
            packet = {"units": batch_units}
            session.add(
                SpeechOptimizationDispatchBatch(
                    dispatch_run_id=run.id,
                    ordinal=ordinal,
                    input_json=packet,
                    input_hash=_canonical_hash(packet),
                    status="ready",
                )
            )
        session.flush()
        return self._run_payload(run)

    @staticmethod
    def _boundary_unit(unit: dict[str, Any]) -> dict[str, Any]:
        return {
            "text": str(unit.get("text") or ""),
            "language": str(unit.get("language") or "auto"),
            "speaker": unit.get("speaker") or None,
        }

    def _claim_response(
        self,
        run: SpeechOptimizationDispatchRun,
        batch: SpeechOptimizationDispatchBatch,
        batches: list[SpeechOptimizationDispatchBatch],
    ) -> dict[str, Any]:
        settings = dict(run.settings_json or {})
        units = [dict(item) for item in (batch.input_json or {}).get("units") or []]
        context_before = max(0, min(20, int(settings.get("context_before") or 4)))
        context_after = max(0, min(20, int(settings.get("context_after") or 2)))
        previous_output: list[dict[str, Any]] = []
        if context_before and batch.ordinal > 0:
            previous = batches[batch.ordinal - 1]
            if previous.status == "completed":
                previous_output = [
                    {
                        "text": str(item.get("text") or ""),
                        "language": str(item.get("language") or run.language),
                        "speaker": item.get("speaker") or None,
                    }
                    for item in list(previous.normalized_output_json or [])[
                        -context_before:
                    ]
                ]
        following_source: list[dict[str, Any]] = []
        if context_after and batch.ordinal + 1 < len(batches):
            following = (batches[batch.ordinal + 1].input_json or {}).get("units") or []
            following_source = [
                self._boundary_unit(dict(item))
                for item in list(following)[:context_after]
            ]
        custom_instructions = str(settings.get("instructions") or "").strip()
        instructions = (
            "Optimise each actionable text unit for natural, intelligible speech "
            "synthesis while preserving its meaning, language, tone, and factual "
            "content. Expand or verbalise abbreviations, numerals, symbols, and "
            "difficult written forms only when that improves spoken delivery; fix "
            "clear speech-affecting punctuation, spelling, or OCR defects. Do not "
            "translate, summarize, censor, merge, split, omit, reorder, or invent "
            "content. Return every supplied unit_id exactly once, with unchanged text "
            "when no improvement is warranted. Context entries are read-only and must "
            "not appear in the result."
        )
        if run.tts_service:
            instructions += f"\n\nTarget TTS service: {run.tts_service}."
        instructions += (
            f"\nText language: {run.language}. Voice language: "
            f"{run.voice_language or run.language}."
        )
        if custom_instructions:
            instructions += f"\n\nUser instructions:\n{custom_instructions}"
        return {
            "schema_version": "1",
            "run_id": run.id,
            "batch_id": batch.id,
            "batch_ordinal": batch.ordinal + 1,
            "status": batch.status,
            "run_status": run.status,
            "batch_status": batch.status,
            "task": {
                "kind": "speech_optimization",
                "output_role": "tts_optimized",
                "language": run.language,
                "voice_language": run.voice_language,
                "tts_service": run.tts_service,
                "instructions": instructions,
                "result_contract": {
                    "kind": "speech_optimization",
                    "items": {
                        "identity_field": "unit_id",
                        "text_field": "text",
                        "required_count": len(units),
                        "required_order": [int(item["unit_id"]) for item in units],
                    },
                },
            },
            "batch": {
                "id_namespace": "speech_optimization_unit",
                "unit_count": len(units),
                "valid_unit_ids": [int(item["unit_id"]) for item in units],
                "units": [
                    {
                        "unit_id": int(item["unit_id"]),
                        "text": str(item.get("text") or ""),
                        "language": str(item.get("language") or run.language),
                        "speaker": item.get("speaker") or None,
                        **(
                            {"timing": dict(item["timing"])}
                            if isinstance(item.get("timing"), dict)
                            else {}
                        ),
                    }
                    for item in units
                ],
                "context": {
                    "previous_output": previous_output,
                    "following_source": following_source,
                },
            },
            "lease_token": batch.lease_token,
            "lease_expires_at": _iso(batch.lease_expires_at),
        }

    def claim_in_session(
        self,
        session: Session,
        *,
        run_id: str,
        claim_key: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        run = session.get(SpeechOptimizationDispatchRun, run_id)
        if run is None:
            raise DispatchError(
                "not_found", "Speech-optimisation dispatch run not found.", 404
            )
        now = utcnow()
        batches = list(
            session.scalars(
                select(SpeechOptimizationDispatchBatch)
                .where(SpeechOptimizationDispatchBatch.dispatch_run_id == run.id)
                .order_by(SpeechOptimizationDispatchBatch.ordinal)
            ).all()
        )
        replayed = next((item for item in batches if item.claim_key == claim_key), None)
        if replayed is not None and (
            replayed.status == "completed"
            or (
                replayed.status == "leased"
                and _active_lease(replayed.lease_expires_at, now)
            )
        ):
            return self._claim_response(run, replayed, batches)
        if run.status == "finalizing":
            raise DispatchError(
                "run_finalizing",
                "The accepted run is finalizing its artifact.",
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
        for candidate in batches:
            if candidate.status == "leased" and _expired_lease(
                candidate.lease_expires_at, now
            ):
                candidate.status = "ready"
                candidate.lease_token = None
                candidate.claim_key = None
                candidate.lease_expires_at = None
                candidate.updated_at = now
        active = next(
            (
                item
                for item in batches
                if item.status == "leased" and _active_lease(item.lease_expires_at, now)
            ),
            None,
        )
        if active is not None:
            if active.claim_key == claim_key:
                return self._claim_response(run, active, batches)
            expires_at = _aware(active.lease_expires_at) or now
            raise DispatchError(
                "run_busy",
                "Another worker holds the active speech-optimisation batch.",
                409,
                retryable=True,
                details={
                    "batch_id": active.id,
                    "retry_after_seconds": max(
                        1, int((expires_at - now).total_seconds())
                    ),
                },
            )
        batch = next((item for item in batches if item.status == "ready"), None)
        if batch is None:
            raise DispatchError(
                "run_finalizing",
                "All batches are accepted and the artifact is finalizing.",
                409,
                retryable=True,
            )
        if any(item.status != "completed" for item in batches[: batch.ordinal]):
            raise DispatchError(
                "batch_not_ready",
                "Speech-optimisation batches must be completed in order.",
                409,
                retryable=True,
            )
        batch.status = "leased"
        batch.lease_token = secrets.token_urlsafe(32)
        batch.claim_key = claim_key
        batch.lease_expires_at = now + timedelta(seconds=int(lease_seconds))
        batch.updated_at = now
        run.status = "running"
        run.updated_at = now
        session.flush()
        return self._claim_response(run, batch, batches)

    def renew_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        batch = session.get(SpeechOptimizationDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        now = utcnow()
        if batch.status != "leased" or batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        if not _active_lease(batch.lease_expires_at, now):
            raise DispatchError(
                "lease_expired",
                "The dispatch lease has expired.",
                409,
                retryable=True,
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
        batch = session.get(SpeechOptimizationDispatchBatch, batch_id)
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
        batch: SpeechOptimizationDispatchBatch,
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if str(result.get("kind") or "") != "speech_optimization":
            raise DispatchError(
                "result_kind_mismatch",
                "This batch requires a speech_optimization result.",
                422,
            )
        source_units = [
            dict(item) for item in (batch.input_json or {}).get("units") or []
        ]
        expected_ids = [int(item["unit_id"]) for item in source_units]
        rows = result.get("items")
        if not isinstance(rows, list):
            raise DispatchError(
                "invalid_model_response",
                "Speech optimisation must return an items list.",
                422,
            )
        normalized: list[dict[str, Any]] = []
        returned_ids: list[int] = []
        for row in rows:
            if not isinstance(row, dict):
                raise DispatchError(
                    "invalid_model_response",
                    "Every optimized item must be an object.",
                    422,
                )
            try:
                unit_id = int(str(row.get("unit_id")))
            except (TypeError, ValueError) as error:
                raise DispatchError(
                    "invalid_model_response",
                    "Every optimized item must retain its unit_id.",
                    422,
                ) from error
            text = str(row.get("text") or "").strip()
            if not text:
                raise DispatchError(
                    "invalid_model_response",
                    f"Speech-optimisation unit {unit_id} is empty.",
                    422,
                )
            source = next(
                (item for item in source_units if int(item["unit_id"]) == unit_id),
                None,
            )
            if source is None:
                raise DispatchError(
                    "invalid_model_response",
                    f"Unknown speech-optimisation unit_id {unit_id}.",
                    422,
                    details={"valid_unit_ids": expected_ids},
                )
            returned_ids.append(unit_id)
            normalized.append(
                {
                    "unit_id": unit_id,
                    "text": text,
                    "language": str(source.get("language") or "auto"),
                    "speaker": source.get("speaker") or None,
                }
            )
        if returned_ids != expected_ids:
            raise DispatchError(
                "invalid_model_response",
                "Return every unit_id exactly once and in the supplied order.",
                422,
                details={
                    "expected_unit_ids": expected_ids,
                    "returned_unit_ids": returned_ids,
                },
            )
        return normalized

    @staticmethod
    def _submit_payload(
        run: SpeechOptimizationDispatchRun,
        batch: SpeechOptimizationDispatchBatch,
    ) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "batch_id": batch.id,
            "output_role": "tts_optimized",
            "status": run.status,
            "run_status": run.status,
            "batch_status": batch.status,
            "accepted": batch.status == "completed",
            "completed_batch_count": run.completed_batch_count,
            "completed_batches": run.completed_batch_count,
            "batch_count": run.batch_count,
            "total_batches": run.batch_count,
            "remaining_batches": max(0, run.batch_count - run.completed_batch_count),
            "result_artifact_id": run.result_artifact_id,
            "final_artifact_id": run.result_artifact_id,
            "finalized": run.status == "completed",
            "result_revision_id": run.result_revision_id,
            "error_code": run.error_code,
            "error_message": run.error_message,
        }

    def submit_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        submission_key: str,
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        batch = session.get(SpeechOptimizationDispatchBatch, batch_id)
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        run = session.get(SpeechOptimizationDispatchRun, batch.dispatch_run_id)
        if run is None:
            raise DispatchError("not_found", "Dispatch run not found.", 404)
        raw_hash = _response_hash(result)
        if batch.status == "completed":
            if batch.submission_key == submission_key and batch.output_hash == raw_hash:
                if run.status == "finalizing":
                    self._retry_finalize(session, run)
                return self._submit_payload(run, batch), (
                    200 if run.status == "completed" else 202
                )
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
                "lease_expired",
                "The dispatch lease has expired.",
                409,
                retryable=True,
            )
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > 4 * 1024 * 1024:
            raise DispatchError(
                "response_too_large",
                "Speech-optimisation response exceeds the 4 MiB limit.",
                413,
            )
        normalized = self._normalize_result(batch, result)
        batch.status = "completed"
        batch.normalized_output_json = normalized
        batch.output_hash = raw_hash
        batch.submission_key = submission_key
        batch.accepted_at = now
        batch.lease_expires_at = None
        batch.updated_at = now
        run.completed_batch_count = int(
            session.scalar(
                select(func.count(SpeechOptimizationDispatchBatch.id)).where(
                    SpeechOptimizationDispatchBatch.dispatch_run_id == run.id,
                    SpeechOptimizationDispatchBatch.status == "completed",
                )
            )
            or 0
        )
        if run.completed_batch_count >= run.batch_count:
            run.status = "finalizing"
            run.error_code = None
            run.error_message = None
            self._retry_finalize(session, run)
            status = 200 if run.status == "completed" else 202
        else:
            run.status = "running"
            run.updated_at = now
            status = 200
        session.flush()
        return self._submit_payload(run, batch), status

    def _retry_finalize(
        self,
        session: Session,
        run: SpeechOptimizationDispatchRun,
    ) -> None:
        if run.status == "completed":
            return
        try:
            with session.begin_nested():
                self._materialize(session, run)
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
        except (OSError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as error:
            run.status = "finalizing"
            run.error_code = "materialization_failed"
            run.error_message = str(error)[:2000]
            run.updated_at = utcnow()
            session.flush()

    def _verify_finalization_source(
        self,
        session: Session,
        run: SpeechOptimizationDispatchRun,
    ) -> tuple[Artifact, Path]:
        current_selections = self._selection_snapshot(
            session,
            run.session_id,
            set(run.selection_snapshot_json or {}),
        )
        if current_selections != dict(run.selection_snapshot_json or {}):
            changed = sorted(
                key
                for key in current_selections
                if current_selections.get(key)
                != (run.selection_snapshot_json or {}).get(key)
            )
            raise DispatchError(
                "finalization_conflict",
                "A relevant stage selection changed while this run was active.",
                409,
                details={"changed_stage_keys": changed},
            )
        source = session.get(Artifact, run.source_artifact_id)
        if source is None or source.state == "deleted":
            raise DispatchError(
                "source_changed", "The pinned source is no longer available.", 409
            )
        if source.session_id != run.session_id and not self._is_attached_source(
            session,
            session_id=run.session_id,
            artifact_id=source.id,
        ):
            raise DispatchError(
                "source_changed",
                "The pinned source is no longer attached to this session.",
                409,
            )
        if (
            source.state != run.source_state
            or source.content_hash != run.source_content_hash
        ):
            raise DispatchError(
                "source_changed", "The pinned source state or hash changed.", 409
            )
        try:
            path = self.artifacts.paths.managed_path(source.relative_path)
            if sha256_file(path) != run.source_content_hash:
                raise DispatchError(
                    "source_changed", "The pinned source content changed.", 409
                )
        except (OSError, ValueError) as error:
            raise DispatchError(
                "source_changed",
                "The pinned source content is no longer available.",
                409,
            ) from error
        current_head = session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == run.session_id,
                Artifact.role == "tts_optimized",
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
        if (current_head.id if current_head else None) != run.output_head_artifact_id:
            raise DispatchError(
                "finalization_conflict",
                "A newer speech-optimisation result already exists for this session.",
                409,
                details={
                    "current_output_head_artifact_id": (
                        current_head.id if current_head else None
                    )
                },
            )
        return source, path

    def _store_srt_revision(
        self,
        session: Session,
        *,
        run: SpeechOptimizationDispatchRun,
        source: Artifact,
        segments,
    ) -> tuple[Document, DocumentRevision]:
        document = Document(
            session_id=run.session_id,
            stage="tts_optimization",
            language=run.language if run.language != "auto" else None,
        )
        session.add(document)
        session.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash=_canonical_hash(
                [
                    {
                        "start_ms": int(item.start_ms),
                        "end_ms": int(item.end_ms),
                        "text": str(item.text),
                        "speaker": _clean_text(item.speaker) or None,
                    }
                    for item in segments
                ]
            ),
        )
        session.add(revision)
        session.flush()
        children: list[Segment] = []
        for ordinal, item in enumerate(segments):
            child = Segment(
                revision_id=revision.id,
                ordinal=ordinal,
                start_ms=int(item.start_ms),
                end_ms=int(item.end_ms),
                text=str(item.text),
                speaker=_clean_text(item.speaker) or None,
                metadata_json={"speaker_source": "timing_inherited"},
            )
            session.add(child)
            children.append(child)
        session.flush()
        document.active_revision_id = revision.id
        parent_revision_id = _clean_text(
            (source.metadata_json or {}).get("revision_id")
        )
        parents = (
            list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == parent_revision_id)
                    .order_by(Segment.ordinal)
                ).all()
            )
            if parent_revision_id
            else []
        )
        for child in children:
            overlaps = [
                parent
                for parent in parents
                if parent.start_ms is not None
                and parent.end_ms is not None
                and min(int(child.end_ms or 0), int(parent.end_ms))
                > max(int(child.start_ms or 0), int(parent.start_ms))
            ]
            for sequence, parent in enumerate(overlaps):
                session.add(
                    SegmentLineage(
                        parent_segment_id=parent.id,
                        child_segment_id=child.id,
                        relation="temporal_overlap",
                        sequence=sequence,
                    )
                )
        return document, revision

    def _materialize(
        self,
        session: Session,
        run: SpeechOptimizationDispatchRun,
    ) -> None:
        source, source_path = self._verify_finalization_source(session, run)
        batches = list(
            session.scalars(
                select(SpeechOptimizationDispatchBatch)
                .where(SpeechOptimizationDispatchBatch.dispatch_run_id == run.id)
                .order_by(SpeechOptimizationDispatchBatch.ordinal)
            ).all()
        )
        if any(batch.status != "completed" for batch in batches):
            raise DispatchError(
                "finalization_incomplete",
                "Not all speech-optimisation batches are accepted.",
                409,
                retryable=True,
            )
        outputs = [
            dict(item)
            for batch in batches
            for item in list(batch.normalized_output_json or [])
        ]
        output_by_id = {int(item["unit_id"]): str(item["text"]) for item in outputs}
        if len(output_by_id) != len(outputs):
            raise DispatchError(
                "invalid_model_response",
                "Optimized unit identities are duplicated.",
                422,
            )
        destination = (
            self.session_dir_resolver(run.session_id)
            / f"speech-optimization-dispatch-{run.id}.{run.source_format}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        document: Document | None = None
        revision: DocumentRevision | None = None
        if run.source_format == "srt":
            source_segments = parse_srt(source_path.read_text(encoding="utf-8-sig"))
            expected_ids = [
                index
                for index, item in enumerate(source_segments, start=1)
                if _clean_text(item.text)
            ]
            if sorted(output_by_id) != expected_ids:
                raise DispatchError(
                    "finalization_incomplete",
                    "Accepted output no longer covers every non-empty source cue.",
                    409,
                )
            revised_segments = [
                replace(item, text=output_by_id.get(index, item.text))
                for index, item in enumerate(source_segments, start=1)
            ]
            destination.write_text(compose_srt(revised_segments), encoding="utf-8")
            document, revision = self._store_srt_revision(
                session,
                run=run,
                source=source,
                segments=revised_segments,
            )
            kind = "srt"
        elif run.source_format == "json":
            rows = json.loads(source_path.read_text(encoding="utf-8-sig"))
            if not isinstance(rows, list):
                raise DispatchError(
                    "source_changed", "The pinned JSON source is no longer a list.", 409
                )
            expected_ids = [
                index
                for index, row in enumerate(rows, start=1)
                if _clean_text(_json_source_text(row))
            ]
            if sorted(output_by_id) != expected_ids:
                raise DispatchError(
                    "finalization_incomplete",
                    "Accepted output no longer covers every non-empty source unit.",
                    409,
                )
            for index, row in enumerate(rows, start=1):
                if index not in output_by_id:
                    continue
                if not isinstance(row, dict):
                    row = {"text": str(row)}
                    rows[index - 1] = row
                row["source_text"] = _json_source_text(row)
                row["tts_optimized_sentence"] = output_by_id[index]
            destination.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            kind = "json"
        else:
            if set(output_by_id) != {1}:
                raise DispatchError(
                    "finalization_incomplete",
                    "Plain-text optimisation requires exactly one output unit.",
                    409,
                )
            destination.write_text(output_by_id[1], encoding="utf-8")
            kind = "text"
        metadata = {
            "speech_optimization_dispatch_run_id": run.id,
            "source_artifact_id": source.id,
            "mode": "whole_document",
            "speech_optimization_mode": "passive",
            "language": run.language,
            "voice_language": run.voice_language,
            "tts_service": run.tts_service,
            "batch_count": run.batch_count,
            "unit_count": len(outputs),
            "provider": None,
            "model": None,
            "document_id": document.id if document is not None else None,
            "revision_id": revision.id if revision is not None else None,
            "stage": "tts_optimization" if revision is not None else None,
        }
        artifact = self.artifacts.register_in_session(
            session,
            destination,
            kind=kind,
            role="tts_optimized",
            session_id=run.session_id,
            parent_ids=[source.id],
            settings={
                **dict(run.settings_json or {}),
                "passive_dispatch": True,
                "language": run.language,
                "voice_language": run.voice_language,
                "tts_service": run.tts_service,
            },
            metadata=metadata,
        )
        run.result_artifact_id = artifact.id
        run.result_revision_id = revision.id if revision is not None else None
        run.status = "completed"
        run.error_code = None
        run.error_message = None
        run.updated_at = utcnow()
