"""Passive external subtitle correction and translation dispatch."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.llm_correction import (
    apply_correction_operations,
    build_correction_task_instructions,
    parse_correction_operations,
    validate_correction_operations,
)
from pandrator.logic.dubbing.llm_translation import (
    build_translation_task_instructions,
    merge_glossaries,
    parse_translation_items_details,
    parse_translation_response_details,
)
from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import (
    compose_srt,
    create_translation_blocks,
    normalize_timing_context_mode,
    subtitle_boundary_cue,
    subtitle_task_cue,
)

from .artifact_selection import ROLE_TO_STAGE
from .artifacts import ArtifactService, sha256_file
from .database import Database
from .dispatch_context import (
    context_capsule_for_wave,
    execution_policy,
    normalize_context_capsule,
    normalize_context_delta,
    store_context_delta,
    wave_bounds,
)
from .models import (
    Artifact,
    DispatchBatch,
    DispatchRun,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionRecord,
    SessionStageSelection,
    utcnow,
)


class DispatchError(RuntimeError):
    """A bounded, API-safe failure from the dispatch state machine."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        *,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = int(status)
        self.retryable = bool(retryable)
        self.details = details


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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


def _segment_hash(items: list[dict[str, Any]]) -> str:
    return _canonical_hash(
        [
            {
                "start_ms": int(item["start_ms"]),
                "end_ms": int(item["end_ms"]),
                "text": str(item["text"]),
                "speaker": item.get("speaker") or None,
            }
            for item in items
        ]
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _lease_is_active(value: datetime | None, now: datetime) -> bool:
    expires_at = _aware(value)
    return expires_at is not None and expires_at > now


def _lease_is_expired(value: datetime | None, now: datetime) -> bool:
    expires_at = _aware(value)
    return expires_at is not None and expires_at <= now


class DispatchRunService:
    """Persistence and validation for externally processed subtitle blocks."""

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
    def _context_count(settings: dict[str, Any], key: str, default: int) -> int:
        raw = settings.get(key)
        try:
            return max(0, min(20, int(default if raw is None else raw)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _run_payload(run: DispatchRun) -> dict[str, Any]:
        settings = dict(run.settings_json or {})
        execution_mode, max_parallel_batches = execution_policy(settings)
        timing_context_mode = normalize_timing_context_mode(
            settings.get("timing_context_mode"),
            legacy_enabled=settings.get("include_timing_context"),
        )
        return {
            "id": run.id,
            "session_id": run.session_id,
            "kind": run.kind,
            "output_role": run.output_role,
            "source_artifact_id": run.source_artifact_id,
            "source_revision_id": run.source_revision_id,
            "source_state": run.source_state,
            "source_content_hash": run.source_content_hash,
            "source_language": run.source_language,
            "target_language": run.target_language,
            "execution_mode": execution_mode,
            "max_parallel_batches": max_parallel_batches,
            "char_limit": int(settings.get("char_limit") or 6000),
            "max_segments_per_batch": int(settings.get("max_segments_per_batch") or 40),
            "no_remove_subtitles": bool(settings.get("no_remove_subtitles")),
            "context_before": DispatchRunService._context_count(
                settings, "context_before", 8
            ),
            "context_after": DispatchRunService._context_count(
                settings, "context_after", 2
            ),
            "timing_context_mode": timing_context_mode,
            "substantial_gap_ms": int(
                2000
                if settings.get("substantial_gap_ms") is None
                else settings["substantial_gap_ms"]
            ),
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
        run: DispatchRun,
        batches: list[DispatchBatch],
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
                    select(DispatchRun)
                    .where(DispatchRun.session_id == session_id)
                    .order_by(DispatchRun.created_at.desc(), DispatchRun.id.desc())
                    .limit(max(1, min(int(limit), 100)))
                ).all()
            )
            return [self._run_payload(run) for run in runs]
        with self.database.session() as session:
            return self.list_runs(session_id, limit=limit, db_session=session)

    def get(self, run_id: str, *, db_session: Session | None = None) -> dict[str, Any]:
        if db_session is not None:
            run = db_session.get(DispatchRun, run_id)
            if run is None:
                raise DispatchError("not_found", "Dispatch run not found.", 404)
            batches = list(
                db_session.scalars(
                    select(DispatchBatch)
                    .where(DispatchBatch.dispatch_run_id == run.id)
                    .order_by(DispatchBatch.ordinal)
                ).all()
            )
            return self._run_detail_payload(run, batches)
        with self.database.session() as session:
            return self.get(run_id, db_session=session)

    @staticmethod
    def _validate_kind(value: object) -> str:
        kind = str(value or "").strip().lower()
        if kind not in {"correction", "translation"}:
            raise DispatchError(
                "invalid_kind",
                "Dispatch kind must be correction or translation.",
                422,
            )
        return kind

    @staticmethod
    def _normalize_language(value: object, *, fallback: str = "") -> str:
        return str(value or fallback or "").strip()

    def _load_source(
        self,
        session: Session,
        *,
        session_id: str,
        kind: str,
        source_artifact_id: str | None,
    ) -> tuple[Artifact, DocumentRevision, list[Segment], Path]:
        allowed_roles = (
            ("transcription", "translation")
            if kind == "correction"
            else (
                "correction",
                "transcription",
            )
        )
        if source_artifact_id:
            artifact = session.get(Artifact, source_artifact_id)
            if artifact is None:
                raise DispatchError("not_found", "Source artifact not found.", 404)
            if artifact.session_id != session_id:
                raise DispatchError(
                    "source_session_mismatch",
                    "Source artifact belongs to a different session.",
                    409,
                )
            if artifact.role not in allowed_roles:
                raise DispatchError(
                    "ineligible_source",
                    "The selected source artifact is not eligible for this dispatch.",
                    422,
                )
        else:
            artifact = session.scalar(
                select(Artifact)
                .where(
                    Artifact.session_id == session_id,
                    Artifact.role.in_(allowed_roles),
                    Artifact.state == "current",
                )
                .order_by(
                    case(
                        {"correction": 0, "transcription": 1},
                        value=Artifact.role,
                        else_=2,
                    ),
                    Artifact.created_at.desc(),
                    Artifact.id.desc(),
                )
            )
            if artifact is None:
                raise DispatchError(
                    "source_not_found",
                    "No current eligible subtitle source was found.",
                    422,
                )
        if artifact.state == "deleted":
            raise DispatchError(
                "source_deleted", "The source artifact was deleted.", 409
            )
        revision_id = str((artifact.metadata_json or {}).get("revision_id") or "")
        if not revision_id:
            raise DispatchError(
                "source_unmaterialized",
                "The source artifact has no exact subtitle revision metadata.",
                422,
            )
        revision = session.get(DocumentRevision, revision_id)
        if revision is None:
            raise DispatchError(
                "source_revision_missing", "Source revision not found.", 409
            )
        document = session.get(Document, revision.document_id)
        if document is None or document.session_id != session_id:
            raise DispatchError(
                "source_revision_mismatch",
                "Source revision belongs to a different session.",
                409,
            )
        if document.stage != artifact.role:
            raise DispatchError(
                "source_revision_mismatch",
                "Source artifact and revision stages do not match.",
                409,
            )
        segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == revision.id)
                .order_by(Segment.ordinal, Segment.id)
            ).all()
        )
        if not segments or any(
            item.start_ms is None
            or item.end_ms is None
            or item.end_ms <= item.start_ms
            or not str(item.text or "").strip()
            for item in segments
        ):
            raise DispatchError(
                "source_segments_invalid",
                "The source revision must contain ordered timed subtitle segments.",
                422,
            )
        if not artifact.content_hash:
            raise DispatchError(
                "source_hash_missing",
                "The source artifact has no content hash.",
                409,
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
        return artifact, revision, segments, path

    @staticmethod
    def _subtitle_records(segments: list[Segment]) -> tuple[str, dict[int, str]]:
        values = [
            SubtitleSegment(
                index=segment.ordinal + 1,
                start_ms=int(segment.start_ms or 0),
                end_ms=int(segment.end_ms or 0),
                text=str(segment.text),
                speaker=str(segment.speaker or ""),
            )
            for segment in segments
        ]
        return compose_srt(values), {
            segment.ordinal + 1: str(segment.speaker or "")
            for segment in segments
            if str(segment.speaker or "").strip()
        }

    def create_in_session(
        self,
        session: Session,
        *,
        session_id: str,
        kind: str,
        source_artifact_id: str | None,
        source_language: str | None,
        target_language: str | None,
        instructions: str,
        char_limit: int,
        max_segments_per_batch: int,
        no_remove_subtitles: bool,
        context_before: int,
        context_after: int,
        timing_context_mode: str,
        substantial_gap_ms: int,
        glossary: dict[str, str],
        execution_mode: str = "serial",
        max_parallel_batches: int = 1,
        context_capsule: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = self._validate_kind(kind)
        record = session.get(SessionRecord, session_id)
        if record is None or record.trashed_at is not None:
            raise DispatchError("not_found", "Session not found.", 404)
        source, revision, segments, _path = self._load_source(
            session,
            session_id=session_id,
            kind=kind,
            source_artifact_id=source_artifact_id,
        )
        document = session.get(Document, revision.document_id)
        output_role = (
            "translation"
            if kind == "correction" and source.role == "translation"
            else kind
        )
        if output_role not in ROLE_TO_STAGE:
            raise DispatchError(
                "invalid_output_role",
                "The dispatch result does not map to a subtitle stage.",
                422,
            )
        document_language = self._normalize_language(
            document.language if document else None,
        )
        requested_source_language = self._normalize_language(source_language)
        if kind == "correction" and source.role == "translation":
            if not document_language:
                raise DispatchError(
                    "source_language_missing",
                    "The translation source has no materialized language metadata.",
                    422,
                )
            if (
                requested_source_language
                and requested_source_language.casefold() != document_language.casefold()
            ):
                raise DispatchError(
                    "source_language_mismatch",
                    "Correction must preserve the translation source language.",
                    422,
                )
            selected_source_language = document_language
        else:
            selected_source_language = (
                requested_source_language
                or document_language
                or self._normalize_language(record.source_language)
                or "auto"
            )
        selected_target_language = (
            self._normalize_language(
                target_language,
                fallback=str(record.target_language or ""),
            )
            if kind == "translation"
            else ""
        )
        if kind == "translation" and not selected_target_language:
            raise DispatchError(
                "target_language_required",
                "Translation dispatch requires a nonblank target language.",
                422,
            )
        source_srt, speakers = self._subtitle_records(segments)
        blocks = create_translation_blocks(
            source_srt,
            char_limit,
            selected_source_language,
            max_subtitles_per_block=max_segments_per_batch,
            speaker_by_subtitle=speakers,
        )
        if not blocks:
            raise DispatchError(
                "source_segments_invalid",
                "No dispatchable subtitle blocks were found.",
                422,
            )
        normalized_glossary = merge_glossaries(glossary)
        execution_settings = {
            "execution_mode": str(execution_mode or "serial").strip().lower(),
            "max_parallel_batches": int(max_parallel_batches),
        }
        # The route and MCP schema validate this packet before it reaches the
        # service; normalize again at the persistence boundary so settings
        # remain safe for older/direct callers too.
        execution_mode, max_parallel_batches = execution_policy(execution_settings)
        normalized_capsule = normalize_context_capsule(context_capsule)
        settings = {
            "instructions": str(instructions or ""),
            "char_limit": int(char_limit),
            "max_segments_per_batch": int(max_segments_per_batch),
            "no_remove_subtitles": bool(no_remove_subtitles),
            "context_before": int(context_before),
            "context_after": int(context_after),
            "timing_context_mode": normalize_timing_context_mode(
                timing_context_mode,
            ),
            "substantial_gap_ms": int(substantial_gap_ms),
            "glossary": normalized_glossary,
            "known_speakers": sorted(set(speakers.values()), key=str.casefold),
            "execution_mode": execution_mode,
            "max_parallel_batches": max_parallel_batches,
            "context_capsule": normalized_capsule,
            "context_deltas": {},
            "glossary_deltas": {},
        }
        selection_snapshot = self._selection_snapshot(
            session,
            session_id,
            {ROLE_TO_STAGE[source.role], ROLE_TO_STAGE[output_role]},
        )
        semantic_input = {
            "kind": kind,
            "output_role": output_role,
            "session_id": session_id,
            "source_artifact_id": source.id,
            "source_revision_id": revision.id,
            "source_content_hash": source.content_hash,
            "source_language": selected_source_language,
            "target_language": selected_target_language or None,
            "settings": settings,
            "selection_snapshot": selection_snapshot,
            "blocks": blocks,
        }
        output_head = session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.role == output_role,
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
        run = DispatchRun(
            session_id=session_id,
            kind=kind,
            output_role=output_role,
            source_artifact_id=source.id,
            source_revision_id=revision.id,
            source_state=source.state,
            source_content_hash=str(source.content_hash),
            source_language=selected_source_language,
            target_language=selected_target_language or None,
            settings_json=settings,
            selection_snapshot_json=selection_snapshot,
            input_hash=_canonical_hash(semantic_input),
            output_head_artifact_id=output_head.id if output_head else None,
            glossary_json=normalized_glossary,
            status="ready",
            batch_count=len(blocks),
            completed_batch_count=0,
        )
        session.add(run)
        session.flush()
        for ordinal, block in enumerate(blocks):
            session.add(
                DispatchBatch(
                    dispatch_run_id=run.id,
                    ordinal=ordinal,
                    input_json=block,
                    input_hash=_canonical_hash(block),
                    status="ready",
                )
            )
        session.flush()
        return self._run_payload(run)

    @staticmethod
    def _known_speakers(run: DispatchRun) -> set[str]:
        values = (
            run.settings_json.get("known_speakers")
            if isinstance(run.settings_json, dict)
            else None
        )
        return {str(value).strip() for value in (values or []) if str(value).strip()}

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

    @staticmethod
    def _glossary_for_wave(
        run: DispatchRun,
        settings: dict[str, Any],
        *,
        wave_start: int,
    ) -> dict[str, str]:
        """Return the glossary snapshot shared by one deterministic wave."""

        # Runs created before deterministic glossary deltas were introduced
        # only have the already-merged run column. Preserve their behavior.
        raw_deltas = settings.get("glossary_deltas")
        if not isinstance(raw_deltas, dict):
            return dict(run.glossary_json or {})
        deltas: list[tuple[int, Any]] = []
        for raw_ordinal, value in raw_deltas.items():
            try:
                ordinal = int(raw_ordinal)
            except (TypeError, ValueError):
                continue
            if ordinal < wave_start:
                deltas.append((ordinal, value))
        return merge_glossaries(
            settings.get("glossary"),
            *(value for _ordinal, value in sorted(deltas, key=lambda item: item[0])),
            settings.get("glossary"),
        )

    @classmethod
    def _rebuild_glossary(cls, settings: dict[str, Any]) -> dict[str, str]:
        """Merge accepted glossary updates in ordinal order, never completion order."""

        raw_deltas = settings.get("glossary_deltas")
        if not isinstance(raw_deltas, dict):
            return merge_glossaries(settings.get("glossary"))
        deltas: list[tuple[int, Any]] = []
        for raw_ordinal, value in raw_deltas.items():
            try:
                ordinal = int(raw_ordinal)
            except (TypeError, ValueError):
                continue
            deltas.append((ordinal, value))
        return merge_glossaries(
            settings.get("glossary"),
            *(value for _ordinal, value in sorted(deltas, key=lambda item: item[0])),
            settings.get("glossary"),
        )

    def _claim_response(
        self,
        run: DispatchRun,
        batch: DispatchBatch,
        batches: list[DispatchBatch],
    ) -> dict[str, Any]:
        settings = dict(run.settings_json or {})
        execution_mode, max_parallel_batches = execution_policy(settings)
        wave_index, wave_start, wave_end = wave_bounds(batch.ordinal, settings)
        wave_batch_count = max(
            0,
            min(
                max_parallel_batches,
                sum(1 for item in batches if wave_start <= item.ordinal < wave_end),
            ),
        )
        known = self._known_speakers(run)
        timing_context_mode = normalize_timing_context_mode(
            settings.get("timing_context_mode"),
            legacy_enabled=settings.get("include_timing_context"),
        )
        substantial_gap_raw = settings.get("substantial_gap_ms")
        substantial_gap_ms = int(
            2000 if substantial_gap_raw is None else substantial_gap_raw
        )
        context_before = self._context_count(settings, "context_before", 8)
        context_after = self._context_count(settings, "context_after", 2)
        glossary_snapshot = self._glossary_for_wave(
            run,
            settings,
            wave_start=wave_start,
        )
        block = list(batch.input_json or [])
        if run.kind == "correction":
            instructions = build_correction_task_instructions(
                subtitle_count=len(block),
                correction_instructions=str(settings.get("instructions") or ""),
                no_remove_subtitles=bool(settings.get("no_remove_subtitles")),
                timing_context_mode=timing_context_mode,
                substantial_gap_ms=substantial_gap_ms,
                known_speakers=known,
                dispatch_result=True,
                structured_context=True,
            )
            result_contract: dict[str, Any] = {
                "kind": "correction",
                "operations": {
                    "actions": ["edit", "merge", "split"]
                    + ([] if settings.get("no_remove_subtitles") else ["delete"]),
                    "identity_field": "cue_ids",
                },
                "context_delta": {
                    "placement": "outer",
                    "description": (
                        "Optional newly learned supported metadata; do not put "
                        "context fields in result items."
                    ),
                },
            }
        else:
            instructions = build_translation_task_instructions(
                subtitle_count=len(block),
                source_language=run.source_language,
                target_language=str(run.target_language or ""),
                translation_instructions=str(settings.get("instructions") or ""),
                glossary=glossary_snapshot,
                no_remove_subtitles=bool(settings.get("no_remove_subtitles")),
                timing_context_mode=timing_context_mode,
                substantial_gap_ms=substantial_gap_ms,
                known_speakers=known,
                dispatch_result=True,
                structured_context=True,
            )
            result_contract = {
                "kind": "translation",
                "items": {
                    "identity_field": "cue_id",
                    "required_count": len(block),
                },
                "glossary_updates_field": "glossary_updates",
                "context_delta": {
                    "placement": "outer",
                    "description": (
                        "Optional newly learned supported metadata; do not put "
                        "context fields in result items."
                    ),
                },
            }
        instructions += (
            "\n\nBoundary context policy:\n"
            "- `batch.context.previous_output` and "
            "`batch.context.previous_source` and "
            "`batch.context.following_source` are read-only continuity evidence "
            "only. "
            "Never return operations or translations for those entries; submit "
            "results only for `batch.cues`."
            "\n\nDelegation context policy:\n"
            "- Use the immutable `delegation.context_capsule` snapshot for "
            "consistency across this wave.\n"
            "- Submit only newly learned supported metadata in the outer "
            "`context_delta`; do not include secrets or arbitrary fields."
        )
        cues = [
            subtitle_task_cue(
                item,
                timing_context_mode=timing_context_mode,
            )
            for item in block
        ]
        previous_output: list[dict[str, Any]] = []
        previous_source: list[dict[str, Any]] = []
        if context_before and batch.ordinal > 0:
            previous_batch = batches[batch.ordinal - 1]
            _previous_wave, previous_wave_start, previous_wave_end = wave_bounds(
                previous_batch.ordinal,
                settings,
            )
            same_parallel_wave = (
                execution_mode == "parallel"
                and wave_start == previous_wave_start
                and previous_batch.ordinal < previous_wave_end
            )
            if same_parallel_wave:
                previous_source = [
                    cue
                    for item in list(previous_batch.input_json or [])[-context_before:]
                    if (cue := subtitle_boundary_cue(item)) is not None
                ]
            elif previous_batch.status == "completed":
                previous_items = list(previous_batch.normalized_output_json or [])[
                    -context_before:
                ]
                previous_output = [
                    cue
                    for item in previous_items
                    if str(item.get("text") or "").strip()
                    and str(item.get("text") or "").strip().upper() != "[REMOVE]"
                    and (cue := subtitle_boundary_cue(item)) is not None
                ]
        following_source: list[dict[str, Any]] = []
        if context_after and batch.ordinal + 1 < len(batches):
            following_source = [
                cue
                for item in list(batches[batch.ordinal + 1].input_json or [])[
                    :context_after
                ]
                if (cue := subtitle_boundary_cue(item)) is not None
            ]
        return {
            "schema_version": "1",
            "run_id": run.id,
            "batch_id": batch.id,
            "batch_ordinal": batch.ordinal + 1,
            "status": batch.status,
            "run_status": run.status,
            "batch_status": batch.status,
            "task": {
                "kind": run.kind,
                "output_role": run.output_role,
                "source_language": run.source_language,
                "target_language": run.target_language,
                "instructions": instructions,
                "result_contract": result_contract,
                "no_remove_subtitles": bool(settings.get("no_remove_subtitles")),
                "known_speakers": sorted(known, key=str.casefold),
                "glossary": (glossary_snapshot if run.kind == "translation" else {}),
                "timing_context_mode": timing_context_mode,
                "substantial_gap_ms": (
                    substantial_gap_ms if timing_context_mode == "full" else None
                ),
            },
            "batch": {
                "id_namespace": "source_revision_cue",
                "source_revision_id": run.source_revision_id,
                "cue_count": len(cues),
                "valid_cue_ids": [cue["cue_id"] for cue in cues],
                "cues": cues,
                "context": {
                    "previous_output": previous_output,
                    "previous_source": previous_source,
                    "following_source": following_source,
                },
            },
            "delegation": {
                "execution_mode": execution_mode,
                "max_parallel_batches": max_parallel_batches,
                "wave_number": wave_index + 1,
                "wave_batch_count": wave_batch_count,
                "context_capsule": context_capsule_for_wave(
                    settings,
                    wave_start=wave_start,
                ),
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
        run = session.get(DispatchRun, run_id)
        if run is None:
            raise DispatchError("not_found", "Dispatch run not found.", 404)
        now = utcnow()
        batches = list(
            session.scalars(
                select(DispatchBatch)
                .where(DispatchBatch.dispatch_run_id == run.id)
                .order_by(DispatchBatch.ordinal)
            ).all()
        )
        replayed = next(
            (item for item in batches if item.claim_key == claim_key),
            None,
        )
        if replayed is not None and (
            replayed.status == "completed"
            or (
                replayed.status == "leased"
                and _lease_is_active(replayed.lease_expires_at, now)
            )
        ):
            return self._claim_response(run, replayed, batches)
        if run.status in {"completed", "failed", "cancelled"}:
            raise DispatchError(
                "run_not_claimable", "Dispatch run is no longer claimable.", 409
            )
        for item in batches:
            if item.status == "leased" and _lease_is_expired(
                item.lease_expires_at, now
            ):
                item.status = "ready"
                item.lease_token = None
                item.claim_key = None
                item.lease_expires_at = None
                item.updated_at = now
        incomplete = next(
            (item for item in batches if item.status != "completed"),
            None,
        )
        if incomplete is None:
            raise DispatchError(
                "run_not_claimable", "No ready dispatch batch remains.", 409
            )
        _wave_index, wave_start, wave_end = wave_bounds(
            incomplete.ordinal,
            run.settings_json or {},
        )
        current_wave = batches[wave_start:wave_end]
        ready = [item for item in current_wave if item.status == "ready"]
        active = [
            item
            for item in current_wave
            if item.status == "leased" and _lease_is_active(item.lease_expires_at, now)
        ]
        if not ready:
            if active:
                nearest = min(
                    active,
                    key=lambda item: _aware(item.lease_expires_at) or now,
                )
                nearest_expires_at = _aware(nearest.lease_expires_at)
                raise DispatchError(
                    "dispatch_busy",
                    "Dispatch batches in the current wave are currently leased.",
                    409,
                    retryable=True,
                    details={
                        "batch_id": nearest.id,
                        "retry_after_seconds": max(
                            1,
                            int(((nearest_expires_at or now) - now).total_seconds()),
                        ),
                    },
                )
            raise DispatchError(
                "run_not_claimable",
                "No ready dispatch batch remains in the current wave.",
                409,
                retryable=True,
            )
        next_batch = ready[0]
        next_batch.status = "leased"
        next_batch.claim_key = claim_key
        next_batch.lease_token = secrets.token_urlsafe(32)
        next_batch.lease_expires_at = now + timedelta(seconds=lease_seconds)
        next_batch.updated_at = now
        run.status = "running"
        run.updated_at = now
        session.flush()
        return self._claim_response(run, next_batch, batches)

    def renew_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        batch = session.get(DispatchBatch, batch_id)
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        now = utcnow()
        if batch.status != "leased" or batch.lease_token != lease_token:
            raise DispatchError(
                "lease_conflict", "The lease token is not current.", 409
            )
        lease_expires_at = _aware(batch.lease_expires_at)
        if lease_expires_at is None or lease_expires_at <= now:
            raise DispatchError(
                "lease_expired", "The dispatch lease has expired.", 409, retryable=True
            )
        batch.lease_expires_at = now + timedelta(seconds=lease_seconds)
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
        batch = session.get(DispatchBatch, batch_id)
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
        if batch.status == "ready" and batch.lease_expires_at is None:
            return {
                "batch_id": batch.id,
                "status": batch.status,
                "lease_expires_at": None,
            }
        batch.status = "ready"
        batch.lease_expires_at = None
        batch.updated_at = utcnow()
        session.flush()
        return {"batch_id": batch.id, "status": batch.status, "lease_expires_at": None}

    @staticmethod
    def _normalize_timing(item: dict[str, Any]) -> tuple[int, int]:
        try:
            raw_start_ms = item.get("start_ms")
            raw_end_ms = item.get("end_ms")
            start_ms = int(
                raw_start_ms
                if raw_start_ms is not None
                else round(float(item["start"]) * 1000)
            )
            end_ms = int(
                raw_end_ms
                if raw_end_ms is not None
                else round(float(item["end"]) * 1000)
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DispatchError(
                "invalid_model_response",
                "The model returned invalid subtitle timing.",
                422,
            ) from error
        if start_ms < 0 or end_ms <= start_ms:
            raise DispatchError(
                "invalid_model_response",
                "The model returned invalid subtitle timing.",
                422,
            )
        return start_ms, end_ms

    def _normalize_response(
        self,
        run: DispatchRun,
        batch: DispatchBatch,
        *,
        result: dict[str, Any] | None = None,
        response_text: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        block = list(batch.input_json or [])
        settings = dict(run.settings_json or {})
        known = self._known_speakers(run)
        if result is not None:
            kind = str(result.get("kind") or "").strip().lower()
            if kind != run.kind:
                raise DispatchError(
                    "result_kind_mismatch",
                    f"This batch requires a {run.kind} result, not "
                    f"{kind or 'an untyped'} result.",
                    422,
                )
        if run.kind == "correction":
            try:
                operations = (
                    list(result.get("operations") or [])
                    if result is not None
                    else parse_correction_operations(str(response_text or ""))
                )
                validate_correction_operations(
                    block,
                    operations,
                    no_remove_subtitles=bool(settings.get("no_remove_subtitles")),
                    known_speakers=known,
                )
                output = apply_correction_operations(
                    block,
                    operations,
                    no_remove_subtitles=bool(settings.get("no_remove_subtitles")),
                    known_speakers=known,
                )
            except (TypeError, ValueError) as error:
                raise DispatchError(
                    "invalid_model_response",
                    str(error),
                    422,
                    details={
                        "id_namespace": "source_revision_cue",
                        "valid_cue_ids": [int(item["index"]) for item in block],
                    },
                ) from error
            glossary: dict[str, str] = {}
            values: list[dict[str, Any]] = []
            for item in output:
                start_ms, end_ms = self._normalize_timing(item)
                text = " ".join(str(item.get("text") or "").split()).strip()
                if not text:
                    raise DispatchError(
                        "invalid_model_response",
                        "The model returned an empty subtitle.",
                        422,
                    )
                if text.upper() == "[REMOVE]" and bool(
                    settings.get("no_remove_subtitles")
                ):
                    raise DispatchError(
                        "invalid_model_response",
                        "Subtitle removal is disabled for this run.",
                        422,
                    )
                values.append(
                    {
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "text": text,
                        "speaker": str(item.get("speaker") or "").strip() or None,
                    }
                )
            return values, glossary
        expected_numbers = [int(item["index"]) for item in block]
        try:
            if result is not None:
                translations, glossary, returned_speakers = (
                    parse_translation_items_details(
                        list(result.get("translations") or []),
                        expected_numbers=expected_numbers,
                        known_speakers=known,
                        glossary=dict(result.get("glossary_updates") or {}),
                    )
                )
            else:
                translations, glossary, returned_speakers = (
                    parse_translation_response_details(
                        str(response_text or ""),
                        expected_numbers=expected_numbers,
                        known_speakers=known,
                    )
                )
        except (TypeError, ValueError) as error:
            raise DispatchError(
                "invalid_model_response",
                str(error),
                422,
                details={
                    "id_namespace": "source_revision_cue",
                    "valid_cue_ids": expected_numbers,
                },
            ) from error
        values = []
        for item, text, returned_speaker in zip(
            block, translations, returned_speakers, strict=True
        ):
            start_ms, end_ms = self._normalize_timing(item)
            normalized_text = " ".join(str(text or "").split()).strip()
            if not normalized_text:
                raise DispatchError(
                    "invalid_model_response",
                    "The model returned an empty translation.",
                    422,
                )
            if normalized_text.upper() == "[REMOVE]" and bool(
                settings.get("no_remove_subtitles")
            ):
                raise DispatchError(
                    "invalid_model_response",
                    "Subtitle removal is disabled for this run.",
                    422,
                )
            values.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": normalized_text,
                    "speaker": str(
                        returned_speaker or item.get("speaker") or ""
                    ).strip()
                    or None,
                }
            )
        return values, merge_glossaries(glossary)

    @staticmethod
    def _submit_payload(run: DispatchRun, batch: DispatchBatch) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "batch_id": batch.id,
            "output_role": run.output_role,
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

    @staticmethod
    def _contains_materialized_subtitle(
        outputs: list[list[dict[str, Any]]],
    ) -> bool:
        return any(
            text and text.upper() != "[REMOVE]"
            for output in outputs
            for item in output
            if (text := str(item.get("text") or "").strip())
        )

    def submit_in_session(
        self,
        session: Session,
        *,
        batch_id: str,
        lease_token: str,
        submission_key: str,
        result: dict[str, Any] | None,
        response_text: str | None,
        context_delta: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        batch = session.get(DispatchBatch, batch_id)
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        run = session.get(DispatchRun, batch.dispatch_run_id)
        if run is None:
            raise DispatchError("not_found", "Dispatch run not found.", 404)
        submission: Any = result if result is not None else str(response_text or "")
        try:
            normalized_context_delta = normalize_context_delta(context_delta)
        except (TypeError, ValueError) as error:
            raise DispatchError(
                "invalid_context_delta",
                str(error),
                422,
            ) from error
        has_context_delta = any(
            bool(value) for value in normalized_context_delta.values()
        )
        raw_hash = _response_hash(submission)
        if has_context_delta:
            raw_hash = _canonical_hash(
                {
                    "result": submission,
                    "context_delta": normalized_context_delta,
                }
            )
        if batch.status == "completed":
            if batch.submission_key == submission_key and batch.output_hash == raw_hash:
                if run.status == "finalizing":
                    self._retry_finalize(session, run)
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
        lease_expires_at = _aware(batch.lease_expires_at)
        if lease_expires_at is None or lease_expires_at <= now:
            raise DispatchError(
                "lease_expired", "The dispatch lease has expired.", 409, retryable=True
            )
        normalized_submission = (
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if result is not None
            else str(response_text or "")
        )
        if len(normalized_submission.encode("utf-8")) > 512 * 1024:
            raise DispatchError(
                "response_too_large", "Model response exceeds the 512 KiB limit.", 413
            )
        normalized, glossary = self._normalize_response(
            run,
            batch,
            result=result,
            response_text=response_text,
        )
        completed_outputs = list(
            session.scalars(
                select(DispatchBatch.normalized_output_json).where(
                    DispatchBatch.dispatch_run_id == run.id,
                    DispatchBatch.status == "completed",
                )
            ).all()
        )
        if len(
            completed_outputs
        ) + 1 >= run.batch_count and not self._contains_materialized_subtitle(
            [list(output or []) for output in completed_outputs] + [normalized]
        ):
            raise DispatchError(
                "invalid_model_response",
                "A dispatch run cannot remove every subtitle.",
                422,
            )
        try:
            settings = store_context_delta(
                dict(run.settings_json or {}),
                ordinal=batch.ordinal,
                delta=normalized_context_delta,
            )
            context_capsule_for_wave(settings, wave_start=run.batch_count)
        except ValueError as error:
            raise DispatchError(
                "invalid_context_delta",
                str(error),
                422,
            ) from error
        batch.status = "completed"
        batch.normalized_output_json = normalized
        batch.output_hash = raw_hash
        batch.submission_key = submission_key
        batch.accepted_at = now
        batch.lease_expires_at = None
        batch.updated_at = now
        if run.kind == "translation":
            glossary_deltas = settings.get("glossary_deltas")
            legacy_glossary_state = "glossary_deltas" not in settings
            normalized_glossary_deltas = (
                dict(glossary_deltas) if isinstance(glossary_deltas, dict) else {}
            )
            if legacy_glossary_state and run.glossary_json:
                # Preserve accepted model additions from a pre-wave run; no
                # migration is needed, and the synthetic ordinal sorts first.
                normalized_glossary_deltas["-1"] = dict(run.glossary_json)
            normalized_glossary_deltas[str(batch.ordinal)] = glossary
            settings["glossary_deltas"] = normalized_glossary_deltas
        run.settings_json = settings
        run.completed_batch_count = int(
            session.scalar(
                select(func.count(DispatchBatch.id)).where(
                    DispatchBatch.dispatch_run_id == run.id,
                    DispatchBatch.status == "completed",
                )
            )
            or 0
        )
        if run.kind == "translation":
            # User-supplied mappings are authoritative. Model additions may
            # extend the working glossary, but cannot silently replace them.
            # Rebuild from ordinal deltas so completion order is irrelevant.
            run.glossary_json = self._rebuild_glossary(settings)
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

    def _retry_finalize(self, session: Session, run: DispatchRun) -> None:
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

    def retry_finalization_in_session(
        self,
        session: Session,
        *,
        run_id: str,
    ) -> tuple[dict[str, Any], int]:
        run = session.get(DispatchRun, run_id)
        if run is None:
            raise DispatchError("not_found", "Dispatch run not found.", 404)
        if run.status == "finalizing":
            self._retry_finalize(session, run)
        batch = session.scalar(
            select(DispatchBatch)
            .where(DispatchBatch.dispatch_run_id == run.id)
            .order_by(DispatchBatch.ordinal.desc())
        )
        if batch is None:
            raise DispatchError("not_found", "Dispatch batch not found.", 404)
        return self._submit_payload(
            run, batch
        ), 200 if run.status == "completed" else 202

    def _materialize(self, session: Session, run: DispatchRun) -> None:
        expected_selections = dict(run.selection_snapshot_json or {})
        current_selections = self._selection_snapshot(
            session,
            run.session_id,
            set(expected_selections),
        )
        if current_selections != expected_selections:
            changed_stages = sorted(
                stage_key
                for stage_key in expected_selections
                if current_selections.get(stage_key)
                != expected_selections.get(stage_key)
            )
            raise DispatchError(
                "finalization_conflict",
                "A subtitle stage selection changed while this run was active.",
                409,
                details={"changed_stage_keys": changed_stages},
            )
        source = session.get(Artifact, run.source_artifact_id)
        revision = session.get(DocumentRevision, run.source_revision_id)
        if source is None or revision is None or source.state == "deleted":
            raise DispatchError(
                "source_changed", "The pinned source is no longer available.", 409
            )
        if source.state != run.source_state:
            raise DispatchError(
                "source_changed",
                "The pinned source state changed while this run was active.",
                409,
            )
        metadata_revision = str((source.metadata_json or {}).get("revision_id") or "")
        if (
            metadata_revision != run.source_revision_id
            or source.content_hash != run.source_content_hash
        ):
            raise DispatchError(
                "source_changed", "The pinned source revision or hash changed.", 409
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
                Artifact.role == run.output_role,
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )
        if (current_head.id if current_head else None) != run.output_head_artifact_id:
            raise DispatchError(
                "finalization_conflict",
                "A newer subtitle result already exists for this session.",
                409,
                details={
                    "current_output_head_artifact_id": current_head.id
                    if current_head
                    else None
                },
            )
        batches = list(
            session.scalars(
                select(DispatchBatch)
                .where(DispatchBatch.dispatch_run_id == run.id)
                .order_by(DispatchBatch.ordinal)
            ).all()
        )
        if any(batch.status != "completed" for batch in batches):
            raise DispatchError(
                "finalization_incomplete",
                "Not all dispatch batches are accepted.",
                409,
                retryable=True,
            )
        values = [
            item
            for batch in batches
            for item in (batch.normalized_output_json or [])
            if str(item.get("text") or "").strip().upper() != "[REMOVE]"
        ]
        if not values:
            raise DispatchError(
                "invalid_model_response",
                "A dispatch run cannot remove every subtitle.",
                422,
            )
        srt = compose_srt(
            [
                SubtitleSegment(
                    index=ordinal + 1,
                    start_ms=int(item["start_ms"]),
                    end_ms=int(item["end_ms"]),
                    text=str(item["text"]),
                    speaker=str(item.get("speaker") or ""),
                )
                for ordinal, item in enumerate(values)
            ]
        )
        destination = (
            self.session_dir_resolver(run.session_id)
            / f"dispatch-{run.id}-{run.output_role}.srt"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(srt, encoding="utf-8")
        source_segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == revision.id)
                .order_by(Segment.ordinal)
            ).all()
        )
        reuse_source_document = (
            run.kind == "correction"
            and source.role == "translation"
            and run.output_role == "translation"
        )
        if reuse_source_document:
            document = session.get(Document, revision.document_id)
            if (
                document is None
                or document.session_id != run.session_id
                or document.stage != run.output_role
                or revision.document_id != document.id
            ):
                raise DispatchError(
                    "source_revision_mismatch",
                    "The translation source document is no longer consistent.",
                    409,
                )
            next_revision_number = (
                int(
                    session.scalar(
                        select(func.max(DocumentRevision.revision_number)).where(
                            DocumentRevision.document_id == document.id
                        )
                    )
                    or 0
                )
                + 1
            )
            parent_revision_id = revision.id
        else:
            document = Document(
                session_id=run.session_id,
                stage=run.output_role,
                language=run.target_language
                if run.kind == "translation"
                else run.source_language,
            )
            session.add(document)
            session.flush()
            next_revision_number = 1
            parent_revision_id = None
        output_revision = DocumentRevision(
            document_id=document.id,
            parent_revision_id=parent_revision_id,
            revision_number=next_revision_number,
            content_hash=_segment_hash(values),
        )
        session.add(output_revision)
        session.flush()
        output_segments: list[Segment] = []
        for ordinal, item in enumerate(values):
            segment = Segment(
                revision_id=output_revision.id,
                ordinal=ordinal,
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
                text=str(item["text"]),
                speaker=str(item.get("speaker") or "").strip() or None,
            )
            session.add(segment)
            output_segments.append(segment)
        session.flush()
        document.active_revision_id = output_revision.id
        for child in output_segments:
            for sequence, parent in enumerate(
                parent
                for parent in source_segments
                if parent.start_ms is not None
                and parent.end_ms is not None
                and min(child.end_ms or 0, parent.end_ms)
                > max(child.start_ms or 0, parent.start_ms)
            ):
                session.add(
                    SegmentLineage(
                        parent_segment_id=parent.id,
                        child_segment_id=child.id,
                        relation="temporal_overlap",
                        sequence=sequence,
                    )
                )
        artifact = self.artifacts.register_in_session(
            session,
            destination,
            kind="srt",
            role=run.output_role,
            session_id=run.session_id,
            parent_ids=[source.id],
            metadata={
                "dispatch_run_id": run.id,
                "source_artifact_id": source.id,
                "source_revision_id": revision.id,
                "document_id": document.id,
                "revision_id": output_revision.id,
                "stage": run.output_role,
                "dispatch_kind": run.kind,
                "language": document.language,
            },
        )
        run.result_artifact_id = artifact.id
        run.result_revision_id = output_revision.id
        run.status = "completed"
        run.error_code = None
        run.error_message = None
        run.updated_at = utcnow()
