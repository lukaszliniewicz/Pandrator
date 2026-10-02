"""Generation source selection and subtitle binding with explicit dependencies."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .logical_passages import materialize_speech_source, passage_srt, stored_passages
from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    OutcomePlan,
    Segment,
    SessionRecord,
    SpeechPlanReview,
)
from .workflow_inputs import workflow_transformations


class SubtitleRecordsProtocol(Protocol):
    def __call__(
        self,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        language: str,
        session_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, Artifact]: ...


class GenerationPlanStoreProtocol(Protocol):
    def __call__(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        *,
        settings: dict[str, Any],
        source_revision_id: str | None = None,
        source_artifact_id: str | None = None,
        db_session: Session | None = None,
        force_new: bool = False,
    ) -> tuple[str, list[str]]: ...


@dataclass(frozen=True, slots=True)
class GenerationBindingContext:
    database: Database
    artifacts: ArtifactService
    _session_record: Callable[[str], SessionRecord]
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _operation_dir: Callable[[str, str], Path]
    _latest_stage_input: Callable[[str, tuple[str, ...]], Artifact | None]
    _usable_language: Callable[[Any], str]
    _subtitle_speaker_map: Callable[[Artifact, Path | None], dict[int, str]]
    _subtitle_generation_records: SubtitleRecordsProtocol
    _store_generation_plan: GenerationPlanStoreProtocol
    _generation_source_for_plan_refresh: Callable[[str], Artifact | None]
    _generation_language: Callable[[str, Artifact, dict[str, Any]], str]
    _materialize_subtitle_generation_plan: Callable[[str, Artifact, Path, dict[str, Any], str], str]
    _structured_speaker: Callable[[Any], str]
    _speech_block_settings: Callable[[dict[str, Any]], tuple[int, int, int, int, int]]
    _speech_block_generation_mode: Callable[[dict[str, Any]], str]
    _next_available_path: Callable[[Path], Path]
    _generation_segmentation_settings: Callable[[dict[str, Any]], dict[str, Any]]
    _logger: logging.Logger


def generation_language(
    context: GenerationBindingContext,
    session_id: str,
    source_artifact: Artifact,
    settings: dict[str, Any],
) -> str:
    """Resolve TTS language from the artifact actually selected for generation."""
    language = context._usable_language((source_artifact.metadata_json or {}).get("language"))
    role = str(source_artifact.role or "")

    # Whole-document speech optimization preserves the language and role of
    # its source. Older JSON optimization artifacts did not copy language,
    # so follow their explicit source link once for compatibility.
    if not language and role == "tts_optimized":
        parent_id = str((source_artifact.metadata_json or {}).get("source_artifact_id") or "")
        if parent_id:
            with context.database.session() as session:
                parent = session.get(Artifact, parent_id)
                if parent is not None:
                    language = context._usable_language(
                        (parent.metadata_json or {}).get("language")
                    )
                    role = str(parent.role or role)

    record = context._session_record(session_id)
    if not language and role == "translation":
        language = context._usable_language(record.target_language)
    if not language and role in {
        "transcription",
        "correction",
        "upload",
        "prepared_text",
        "source",
    }:
        language = context._usable_language(record.source_language)
    if not language:
        language = context._usable_language(
            settings.get("language") or settings.get("target_language")
        )
    return language or "en"


def subtitle_speaker_map(
    context: GenerationBindingContext,
    artifact: Artifact,
    source_path: Path | None = None,
) -> dict[int, str]:
    """Resolve cue speakers without exposing them as subtitle text."""

    mapping: dict[int, str] = {}
    with context.database.session() as session:
        managed = session.get(Artifact, artifact.id)
        revision_id = str(
            ((managed.metadata_json if managed is not None else artifact.metadata_json) or {}).get(
                "revision_id"
            )
            or ""
        )
    if revision_id:
        with context.database.session() as session:
            records = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == revision_id)
                    .order_by(Segment.ordinal)
                ).all()
            )
        for record in records:
            speaker = context._structured_speaker(record)
            if speaker:
                mapping[record.ordinal + 1] = speaker

    try:
        if source_path is None:
            _record, source_path = context.artifacts.resolve(artifact.id)
        if source_path.suffix.lower() == ".srt":
            from pandrator.logic.dubbing.srt_utils import parse_srt

            for segment in parse_srt(source_path.read_text(encoding="utf-8-sig")):
                if segment.speaker and segment.index not in mapping:
                    mapping[segment.index] = segment.speaker
    except (KeyError, OSError):
        context._logger.warning("Could not resolve speaker metadata for artifact %s", artifact.id)
    return mapping


def subtitle_generation_records(
    context: GenerationBindingContext,
    source_artifact: Artifact,
    source_path: Path,
    settings: dict[str, Any],
    language: str,
    session_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, Artifact]:
    """Build one speaker-safe partition for display and speech text."""
    from pandrator.logic.dubbing.speech_blocks import create_speech_blocks
    from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt

    source_markup = (source_artifact.metadata_json or {}).get("speech_markup") or {}
    (
        min_chars,
        max_chars,
        merge_threshold,
        continuation_threshold,
        max_internal_gap,
    ) = context._speech_block_settings(settings)
    display_artifact = source_artifact
    display_path = source_path
    display_segments = None
    speech_segments = None
    plan_by_position: dict[int, dict[str, Any]] = {}
    logical_source_revision_id = None
    logical_rows = None

    if source_artifact.role == "tts_optimized":
        display_artifact_id = str(
            (source_artifact.metadata_json or {}).get("source_artifact_id") or ""
        )
        if display_artifact_id:
            candidate_artifact, candidate_path = context._resolve_input(display_artifact_id)
            if candidate_path.suffix.lower() == ".srt":
                display_artifact = candidate_artifact
                display_path = candidate_path
                display_segments = parse_srt(display_path.read_text(encoding="utf-8-sig"))
                speech_segments = parse_srt(source_path.read_text(encoding="utf-8-sig"))
                if [item.index for item in display_segments] != [
                    item.index for item in speech_segments
                ]:
                    raise ValueError(
                        "The reviewed speech revision no longer aligns with its display subtitles."
                    )
                plan_artifact_id = str(
                    (source_artifact.metadata_json or {}).get("speech_plan_artifact_id") or ""
                )
                if plan_artifact_id:
                    _plan_artifact, plan_path = context._resolve_input(plan_artifact_id)
                    plan_rows = json.loads(plan_path.read_text(encoding="utf-8-sig"))
                    if isinstance(plan_rows, list):
                        for position, row in enumerate(plan_rows):
                            if isinstance(row, dict):
                                try:
                                    plan_position = int(row.get("index", position))
                                except (TypeError, ValueError):
                                    plan_position = position
                                plan_by_position[plan_position] = dict(row.get("speech_plan") or {})

    if source_artifact.role != "tts_optimized" and not source_markup:
        with context.database.immediate_session() as session:
            managed = session.get(Artifact, source_artifact.id)
            if (
                managed is not None
                and stored_passages(managed) is not None
                and sha256_file(source_path) != managed.content_hash
            ):
                raise ValueError(
                    "The subtitle file changed after its passages were saved. Import the updated file before planning speech."
                )
            prepared = materialize_speech_source(session, managed) if managed else None
            if prepared is not None:
                logical_rows, logical_source_revision_id = prepared
    display_srt = (
        passage_srt(logical_rows)
        if logical_rows is not None
        else display_path.read_text(encoding="utf-8-sig")
    )
    speaker_by_subtitle = (
        {
            index + 1: str(row.get("speaker") or "")
            for index, row in enumerate(logical_rows)
            if row.get("speaker")
        }
        if logical_rows is not None
        else context._subtitle_speaker_map(display_artifact, display_path)
    )
    turn_by_subtitle: dict[int, str] = {}
    if logical_rows is not None:
        turn_by_subtitle = {
            index + 1: str(((row.get("_passage") or row).get("turn_id") or "")).strip()
            for index, row in enumerate(logical_rows)
        }
        if not any(turn_by_subtitle.values()):
            turn_by_subtitle = {}
    if display_segments is None:
        display_segments = parse_srt(display_srt)
    block_options: dict[str, Any] = dict(
        preserve_source_boundaries=logical_rows is not None,
        target_language=language,
        min_chars=min_chars,
        max_chars=max_chars,
        merge_threshold=merge_threshold,
        continuation_threshold_ms=continuation_threshold,
        max_internal_gap_ms=max_internal_gap,
        generation_mode=context._speech_block_generation_mode(settings),
        **({"speaker_by_subtitle": speaker_by_subtitle} if speaker_by_subtitle else {}),
        **({"turn_by_subtitle": turn_by_subtitle} if turn_by_subtitle else {}),
    )
    blocks: list[dict[str, Any]]
    if source_markup:
        # XML belongs to an accepted cue. Keep its timing envelope and
        # wording integral; casting splits only internal synthesis calls.
        spoken_by_index = {item.index: item for item in (speech_segments or display_segments)}

        def one_cue_srt(cue):
            # compose_srt numbers its export from 1; planning must retain
            # the source cue IDs for annotation and timing provenance.
            return f"{cue.index}\n{compose_srt([cue]).partition(chr(10))[2]}"

        blocks = []
        for cue in display_segments:
            spoken = spoken_by_index[cue.index]
            cue_blocks: list[dict[str, Any]] = create_speech_blocks(
                one_cue_srt(cue),
                **{
                    **block_options,
                    "preserve_source_boundaries": True,
                    "generation_mode": "passage",
                    "max_chars": max(max_chars, len(cue.text), len(spoken.text)),
                },
                speech_srt_content=one_cue_srt(spoken) if speech_segments is not None else None,
            )
            for block in cue_blocks:
                block["number"] = str(len(blocks) + 1).zfill(4)
                block["alignment_group"] = f"a{len(blocks) + 1:04d}"
                if blocks:
                    block["provenance"]["boundary_before"] = {
                        "action": "keep_boundary",
                        "reason": "annotated_cue_boundary",
                        "message": "Accepted speech markup retains its source cue window.",
                        "source_references": [cue.index],
                    }
                blocks.append(block)
    else:
        blocks = create_speech_blocks(
            display_srt,
            **block_options,
            speech_srt_content=source_path.read_text(encoding="utf-8-sig")
            if speech_segments is not None
            else None,
        )
    if not blocks:
        raise ValueError("No dubbing speech blocks were produced.")

    position_by_index = {item.index: position for position, item in enumerate(display_segments)}
    records: list[dict[str, Any]] = []
    for block in blocks:
        if logical_rows is not None:
            block["provenance"]["source_reference_namespace"] = "logical_passage_ordinal"
        subtitle_ids = [int(value) for value in block.get("subtitles") or []]
        turn_id = ""
        if turn_by_subtitle:
            block_turn_ids = {turn_by_subtitle.get(subtitle_id, "") for subtitle_id in subtitle_ids}
            if len(block_turn_ids) != 1:
                raise ValueError("A speech block cannot cross a preserved utterance turn boundary.")
            turn_id = next(iter(block_turn_ids))
            if turn_id:
                block_provenance = block.get("provenance")
                if not isinstance(block_provenance, dict):
                    raise ValueError("A speech block with a turn ID must have provenance metadata.")
                block_provenance["turn_id"] = turn_id
        record = {
            **{key: value for key, value in block.items() if not str(key).startswith("_")},
            "source_segment_ids": subtitle_ids,
            "node_kind": "subtitle_cue",
            "language": language,
        }
        if turn_by_subtitle and turn_id:
            record["turn_id"] = turn_id
        if speech_segments is not None:
            optimized_text = str(block.get("_optimized_text") or "").strip()
            record["tts_optimized_sentence"] = optimized_text
            cue_plans = [
                {
                    "subtitle": index,
                    "speech_plan": plan_by_position.get(
                        position_by_index.get(index, -1),
                        {},
                    ),
                }
                for index in subtitle_ids
                if plan_by_position.get(position_by_index.get(index, -1))
            ]
            if len(subtitle_ids) == 1 and cue_plans:
                record["speech_plan"] = cue_plans[0]["speech_plan"]
            elif cue_plans:
                record["speech_plan"] = {
                    "version": 1,
                    "status": "reviewed_aggregate",
                    "mode_used": "document",
                    "compiled_text": optimized_text,
                    "cue_plans": cue_plans,
                }
        if source_markup and any(
            str(position_by_index.get(cue, -1) + 1) in source_markup for cue in subtitle_ids
        ):
            from .generation_cast_runtime import combine_source_markup
            from .generation_controls import get_generation_controls

            control_session_id = session_id or source_artifact.session_id
            if control_session_id is None:
                raise KeyError(control_session_id)
            with context.database.session() as db:
                characters = get_generation_controls(db, control_session_id)["characters"]
            source_rows = {item.index: item for item in (speech_segments or display_segments)}
            combined = combine_source_markup(
                [
                    (source_rows[cue].text, source_markup.get(str(position_by_index[cue] + 1)))
                    for cue in subtitle_ids
                ],
                str(len(records) + 1),
                record.get("tts_optimized_sentence")
                or record.get("text")
                or record.get("original_sentence")
                or "",
                characters,
            )
            record["speech_plan"] = {**record.get("speech_plan", {}), "speech_xml": combined}
        records.append(record)

    source_revision_id = (
        logical_source_revision_id
        or str((display_artifact.metadata_json or {}).get("revision_id") or "")
        or None
    )
    return records, source_revision_id, display_artifact


def materialize_subtitle_generation_plan(
    context: GenerationBindingContext,
    session_id: str,
    source_artifact: Artifact,
    source_path: Path,
    settings: dict[str, Any],
    language: str,
) -> str:
    """Create a new versioned plan revision only when its topology changed."""
    with context.database.session() as session:
        plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
        previous_revision_id = plan.active_revision_id if plan else None
        active_revision = (
            session.get(GenerationPlanRevision, previous_revision_id)
            if previous_revision_id
            else None
        )
        expected_revision_id = str(settings.get("speech_plan_revision_id") or "")
        if expected_revision_id and expected_revision_id != previous_revision_id:
            from .settings_policy import RevisionConflict

            raise RevisionConflict("The selected speech plan changed before workflow execution.")
        reviewed = bool(
            active_revision
            and (
                session.get(SpeechPlanReview, active_revision.id)
                or active_revision.operation_json
                or (active_revision.settings_json or {}).get("_prepared_for_review")
                or session.scalar(
                    select(GenerationSegment.id)
                    .where(
                        GenerationSegment.plan_revision_id == previous_revision_id,
                        GenerationSegment.revision > 1,
                    )
                    .limit(1)
                )
            )
        )
        if active_revision is not None and (expected_revision_id or reviewed):
            planned_source = str(
                (active_revision.settings_json or {}).get("_source_artifact_id") or ""
            )
            if planned_source and planned_source != source_artifact.id:
                raise ValueError(
                    "The selected speech plan belongs to a different subtitle source. Restore the matching source or explicitly prepare a new speech plan."
                )
            return active_revision.id

    records, source_revision_id, display_artifact = context._subtitle_generation_records(
        source_artifact,
        source_path,
        settings,
        language,
        session_id=session_id,
    )
    revision_id, _segment_ids = context._store_generation_plan(
        session_id,
        records,
        settings=settings,
        source_revision_id=source_revision_id,
        source_artifact_id=source_artifact.id,
    )
    if revision_id != previous_revision_id:
        destination = context._next_available_path(
            context._operation_dir(session_id, "speech-blocks")
            / f"{source_path.stem}-speech-blocks.json"
        )
        destination.write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        parent_ids = [source_artifact.id]
        if display_artifact.id != source_artifact.id:
            parent_ids.append(display_artifact.id)
        context.artifacts.register(
            destination,
            kind="json",
            role="speech_blocks",
            session_id=session_id,
            parent_ids=parent_ids,
            settings=settings,
            metadata={
                "generation_plan_revision_id": revision_id,
                "source_artifact_id": source_artifact.id,
                "display_artifact_id": display_artifact.id,
                "segment_count": len(records),
                **context._generation_segmentation_settings(settings),
            },
        )
    return revision_id


def generation_source_for_plan_refresh(
    context: GenerationBindingContext,
    session_id: str,
) -> Artifact | None:
    """Resolve the currently selected source, with legacy plan fallbacks."""
    with context.database.session() as session:
        record = session.get(SessionRecord, session_id)
        if record is None:
            raise KeyError(session_id)
        outcome = session.scalar(select(OutcomePlan).where(OutcomePlan.session_id == session_id))
        outcome_value = (
            dict(outcome.value_json or {})
            if outcome and isinstance(outcome.value_json, dict)
            else {}
        )
        inputs_raw = outcome_value.get("inputs")
        inputs = inputs_raw if isinstance(inputs_raw, dict) else {}
        transformations = workflow_transformations(session, session_id, outcome, context.database)
        generation_input = str(inputs.get("generation") or "translation").strip().lower()
        has_explicit_source_choice = bool(str(inputs.get("generation") or "").strip()) or bool(
            transformations.get("llm_tts_document_optimization")
        )
        plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
        active = (
            session.get(GenerationPlanRevision, plan.active_revision_id)
            if plan and plan.active_revision_id
            else None
        )
        stored_source_id = str(
            ((active.settings_json if active else {}) or {}).get("_source_artifact_id") or ""
        )
        source_revision_id = str((active.source_revision_id if active else "") or "")

    if stored_source_id and not has_explicit_source_choice:
        try:
            source, _path = context._resolve_input(stored_source_id)
            return source
        except KeyError:
            pass
    if record.workflow_kind != "audiobook":
        if bool(transformations.get("llm_tts_document_optimization")):
            roles = ("tts_optimized",)
        elif generation_input == "correction":
            roles = ("correction",)
        elif generation_input in {"source", "media_edit"}:
            roles = (
                ("media_edit_subtitles",)
                if record.workflow_kind == "media_edit"
                else ("transcription", "upload")
            )
        else:
            roles = ("translation",)
        selected = context._latest_stage_input(session_id, roles)
        if selected is not None:
            return selected

    if stored_source_id:
        try:
            source, _path = context._resolve_input(stored_source_id)
            return source
        except KeyError:
            pass
    if source_revision_id:
        with context.database.session() as session:
            candidates = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.state != "deleted",
                    )
                    .order_by(Artifact.created_at.desc())
                ).all()
            )
            for candidate in candidates:
                if (
                    str((candidate.metadata_json or {}).get("revision_id") or "")
                    == source_revision_id
                ):
                    session.expunge(candidate)
                    return candidate
    return None


def refresh_generation_plan(
    context: GenerationBindingContext,
    session_id: str,
    resolved_snapshot: dict[str, Any],
) -> str | None:
    """Apply current subtitle segmentation settings before a full new run."""
    source_artifact = context._generation_source_for_plan_refresh(session_id)
    if source_artifact is None:
        return None
    source_artifact, source_path = context._resolve_input(source_artifact.id)
    resolved_snapshot["source_artifact_id"] = source_artifact.id
    resolved_snapshot["text"] = {
        **dict(resolved_snapshot.get("text") or {}),
        "use_existing_speech_plans": source_artifact.role == "tts_optimized",
    }
    if source_path.suffix.lower() != ".srt":
        return None

    from .settings_policy import adapt_runtime_settings

    settings: dict[str, Any] = {}
    for section in ("text", "subtitles", "tts", "audio", "rvc", "output"):
        value = resolved_snapshot.get(section)
        if isinstance(value, dict):
            settings.update(adapt_runtime_settings(section, value))
    language = context._generation_language(
        session_id,
        source_artifact,
        settings,
    )
    settings = {
        **settings,
        "language": language,
        "target_language": language,
    }
    return context._materialize_subtitle_generation_plan(
        session_id,
        source_artifact,
        source_path,
        settings,
        language,
    )
