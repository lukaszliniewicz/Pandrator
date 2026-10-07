"""Generation plan persistence with explicit dependencies and transaction ownership."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .credentials import redact_inline_secrets
from .database import Database
from .models import GenerationPlan, GenerationPlanRevision, GenerationSegment, utcnow


class DefaultSilenceProtocol(Protocol):
    def __call__(
        self, record: dict[str, Any], settings: dict[str, Any], *, is_subtitle: bool = False
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class GenerationPlanStoreContext:
    database: Database
    _is_subtitle_generation_record: Callable[[dict[str, Any]], bool]
    _usable_language: Callable[[Any], str]
    _optimization_text_hash: Callable[[str], str]
    _generation_segmentation_settings: Callable[[dict[str, Any]], dict[str, Any]]
    _secret_free_tts_settings: Callable[[dict[str, Any]], dict[str, Any]]
    _default_silence_after_ms: DefaultSilenceProtocol


def store_generation_plan(
    context: GenerationPlanStoreContext,
    session_id: str,
    records: list[dict[str, Any]],
    *,
    settings: dict[str, Any],
    source_revision_id: str | None = None,
    source_artifact_id: str | None = None,
    db_session: Session | None = None,
    force_new: bool = False,
) -> tuple[str, list[str]]:
    clean = [
        item
        for item in records
        if str(item.get("text") or item.get("original_sentence") or "").strip()
    ]
    digest = hashlib.sha256(
        json.dumps(
            {
                "records": clean,
                "settings": context._generation_segmentation_settings(settings),
                "source_revision_id": source_revision_id,
                "source_artifact_id": source_artifact_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    with (
        nullcontext(db_session) if db_session is not None else context.database.immediate_session()
    ) as session:
        plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
        if plan is not None and plan.active_revision_id:
            active = session.get(GenerationPlanRevision, plan.active_revision_id)
            if not force_new and active is not None and active.content_hash == digest:
                # Identical source content and segmentation settings: keep
                # the existing segments so takes, edits, and run history
                # stay attached instead of being orphaned by a new revision.
                # Legacy plans may contain transient hydrated provider keys.
                active.settings_json = redact_inline_secrets(active.settings_json or {})
                segment_ids = list(
                    session.scalars(
                        select(GenerationSegment.id)
                        .where(GenerationSegment.plan_revision_id == active.id)
                        .order_by(GenerationSegment.ordinal)
                    ).all()
                )
                return active.id, [str(segment_id) for segment_id in segment_ids]
        if plan is None:
            plan = GenerationPlan(session_id=session_id)
            session.add(plan)
            session.flush()
        maximum = (
            session.scalar(
                select(func.max(GenerationPlanRevision.revision_number)).where(
                    GenerationPlanRevision.plan_id == plan.id
                )
            )
            or 0
        )
        stored_settings = redact_inline_secrets(context._secret_free_tts_settings(settings))
        if source_artifact_id:
            stored_settings["_source_artifact_id"] = source_artifact_id
        revision = GenerationPlanRevision(
            plan_id=plan.id,
            parent_revision_id=plan.active_revision_id,
            source_revision_id=source_revision_id,
            revision_number=int(maximum) + 1,
            settings_json=stored_settings,
            content_hash=digest,
        )
        session.add(revision)
        session.flush()
        segment_ids = []
        from pandrator.logic.speech_markup import parse_speech_markup

        from .generation_cast_runtime import remap_markup
        from .generation_controls import get_generation_controls
        from .speech_boundaries import record_pause_metadata

        characters = get_generation_controls(session, session_id)["characters"]
        for ordinal, record in enumerate(clean):
            record = dict(record)
            source_markup = record.get("speech_xml") or (record.get("speech_plan") or {}).get(
                "speech_xml"
            )
            if source_markup:
                spoken = str(
                    record.get("tts_optimized_sentence")
                    or record.get("text")
                    or record.get("original_sentence")
                    or ""
                ).strip()
                normalized = remap_markup(source_markup, str(ordinal + 1), spoken, characters)
                structure = parse_speech_markup(
                    normalized,
                    expected_segment_id=str(ordinal + 1),
                    expected_text=spoken,
                    characters=characters,
                )
                record["speech_boundary_after"] = structure.boundary_after or (
                    "dialogue_turn" if structure.spans and structure.spans[-1].dialogue else None
                )
                if record["speech_boundary_after"] in {"continuation", "dialogue_turn"}:
                    record["paragraph_break_after"] = False
            is_subtitle = context._is_subtitle_generation_record(record)
            explicit_language = context._usable_language(record.get("language")) or None
            explicit_voice = str(record.get("voice") or "").strip() or None
            segment = GenerationSegment(
                plan_revision_id=revision.id,
                ordinal=ordinal,
                source_segment_ids_json=list(
                    record.get("source_segment_ids") or record.get("subtitles") or []
                ),
                speech_block_provenance_json=dict(
                    record.get("speech_block_provenance") or record.get("provenance") or {}
                ),
                alignment_group=str(record.get("alignment_group") or "").strip() or None,
                node_kind=str(
                    record.get("node_kind")
                    or (
                        "subtitle_cue"
                        if is_subtitle
                        else "chapter_marker"
                        if str(record.get("chapter") or "").lower() == "yes"
                        else "paragraph"
                    )
                ),
                paragraph_break_after=False
                if is_subtitle
                else bool(
                    record.get(
                        "paragraph_break_after",
                        str(record.get("paragraph") or "").lower() == "yes",
                    )
                ),
                speaker=str(record.get("speaker") or "").strip() or None,
                text=str(record.get("text") or record.get("original_sentence") or "").strip(),
                optimized_text=(str(record.get("tts_optimized_sentence") or "").strip() or None),
                speech_plan_json=dict(record.get("speech_plan") or {}),
                optimization_status=(
                    "optimized"
                    if str(record.get("tts_optimized_sentence") or "").strip()
                    else "not_requested"
                ),
                optimization_source_hash=(
                    context._optimization_text_hash(
                        str(record.get("text") or record.get("original_sentence") or "").strip()
                    )
                    if str(record.get("tts_optimized_sentence") or "").strip()
                    else None
                ),
                optimization_model=(
                    str((record.get("speech_plan") or {}).get("model") or "").strip() or None
                ),
                voice_id=record.get("voice_id"),
                voice=explicit_voice,
                # A missing value is meaningful: it follows the session TTS
                # language and remains responsive to later settings changes.
                language=explicit_language,
                silence_after_ms=context._default_silence_after_ms(
                    record, settings, is_subtitle=is_subtitle
                ),
                marked=bool(record.get("marked", False)),
            )
            session.add(segment)
            session.flush()
            segment.speech_plan_json = {
                **(segment.speech_plan_json or {}),
                **record_pause_metadata(record, is_subtitle=is_subtitle),
            }
            if source_markup:
                segment.speech_plan_json = {
                    **(segment.speech_plan_json or {}),
                    "speech_xml": remap_markup(
                        source_markup,
                        segment.id,
                        segment.optimized_text or segment.text,
                        characters,
                    ),
                }
            segment_ids.append(segment.id)
        plan.active_revision_id = revision.id
        plan.updated_at = utcnow()
        return revision.id, segment_ids
