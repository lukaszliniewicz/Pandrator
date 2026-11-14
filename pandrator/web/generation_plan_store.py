"""Generation plan persistence with explicit dependencies and transaction ownership."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from .credentials import redact_inline_secrets
from .database import Database
from .model_base import new_id
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


def _clean_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in records
        if str(item.get("text") or item.get("original_sentence") or "").strip()
    ]


def _characters_key(characters: Any) -> str:
    return json.dumps(characters, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True, slots=True)
class PreparedGenerationPlanRows:
    """A plan's segment rows, built before a write transaction opens.

    ``rows`` hold every column but the revision id, under pre-assigned segment
    ids, with the final ``speech_plan_json``. They are valid only for ``records``
    and the settings they were built from, and for the cast characters whose
    canonical JSON is ``characters_key``.
    """

    records: list[dict[str, Any]]
    characters_key: str
    rows: list[dict[str, Any]]


def prepare_generation_plan_rows(
    context: GenerationPlanStoreContext,
    records: list[dict[str, Any]],
    *,
    settings: dict[str, Any],
    characters: Any,
) -> PreparedGenerationPlanRows:
    """Normalize records, check and remap their markup, and build segment rows.

    Pure work over the records, the settings and the cast characters, so a caller
    can run it before ``immediate_session`` and keep it out of the writer.
    """
    from pandrator.logic.speech_markup import parse_speech_markup

    from .generation_cast_runtime import remap_markup
    from .speech_boundaries import effective_speech_boundary, record_pause_metadata

    rows: list[dict[str, Any]] = []
    for ordinal, record in enumerate(_clean_records(records)):
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
            boundary = effective_speech_boundary(structure)
            if boundary is not None:
                record["speech_boundary_after"] = boundary
            if record.get("speech_boundary_after") in {"continuation", "dialogue_turn"}:
                record["paragraph_break_after"] = False
                record["paragraph"] = False
        is_subtitle = context._is_subtitle_generation_record(record)
        explicit_language = context._usable_language(record.get("language")) or None
        explicit_voice = str(record.get("voice") or "").strip() or None
        segment_id = new_id()
        text = str(record.get("text") or record.get("original_sentence") or "").strip()
        optimized_text = str(record.get("tts_optimized_sentence") or "").strip() or None
        speech_plan = {
            **dict(record.get("speech_plan") or {}),
            **record_pause_metadata(record, is_subtitle=is_subtitle),
        }
        if source_markup:
            speech_plan = {
                **speech_plan,
                "speech_xml": remap_markup(
                    source_markup, segment_id, optimized_text or text, characters,
                ),
            }
        rows.append({
            "id": segment_id,
            "ordinal": ordinal,
            "source_segment_ids_json": list(
                record.get("source_segment_ids") or record.get("subtitles") or []
            ),
            "speech_block_provenance_json": dict(
                record.get("speech_block_provenance") or record.get("provenance") or {}
            ),
            "alignment_group": str(record.get("alignment_group") or "").strip() or None,
            "node_kind": str(
                record.get("node_kind")
                or (
                    "subtitle_cue"
                    if is_subtitle
                    else "chapter_marker"
                    if str(record.get("chapter") or "").lower() == "yes"
                    else "paragraph"
                )
            ),
            "paragraph_break_after": False
            if is_subtitle
            else bool(
                record.get(
                    "paragraph_break_after",
                    str(record.get("paragraph") or "").lower() == "yes",
                )
            ),
            "speaker": str(record.get("speaker") or "").strip() or None,
            "text": text,
            "optimized_text": optimized_text,
            "speech_plan_json": speech_plan,
            "optimization_status": "optimized" if optimized_text else "not_requested",
            "optimization_source_hash": (
                context._optimization_text_hash(text) if optimized_text else None
            ),
            "optimization_reviewed": False,
            "optimization_model": (
                str((record.get("speech_plan") or {}).get("model") or "").strip() or None
            ),
            "voice_id": record.get("voice_id"),
            "voice": explicit_voice,
            # A missing value is meaningful: it follows the session TTS
            # language and remains responsive to later settings changes.
            "language": explicit_language,
            "silence_after_ms": context._default_silence_after_ms(
                record, settings, is_subtitle=is_subtitle
            ),
            "marked": bool(record.get("marked", False)),
            "removed": False,
            "status": "ready",
            "revision": 1,
        })
    return PreparedGenerationPlanRows(
        records=records, characters_key=_characters_key(characters), rows=rows,
    )


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
    prepared_rows: PreparedGenerationPlanRows | None = None,
) -> tuple[str, list[str]]:
    """Store ``records`` as the session's active plan revision.

    ``prepared_rows``, from ``prepare_generation_plan_rows`` over the same records
    and settings, keeps the per-record work out of the write transaction; they are
    rebuilt here if the cast characters changed since. The segments are inserted
    in one bulk statement.
    """
    if prepared_rows is not None and prepared_rows.records is not records:
        raise ValueError("Prepared plan rows belong to other records.")
    clean = _clean_records(records)
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
        from .generation_controls import get_generation_controls

        characters = get_generation_controls(session, session_id)["characters"]
        if prepared_rows is None or prepared_rows.characters_key != _characters_key(characters):
            prepared_rows = prepare_generation_plan_rows(
                context, records, settings=settings, characters=characters,
            )
        now = utcnow()
        rows = [
            {**row, "plan_revision_id": revision.id, "created_at": now, "updated_at": now}
            for row in prepared_rows.rows
        ]
        if rows:
            # ``render_nulls`` keeps rows whose None columns differ in one batch.
            session.execute(insert(GenerationSegment).execution_options(render_nulls=True), rows)
        plan.active_revision_id = revision.id
        plan.updated_at = utcnow()
        return revision.id, [str(row["id"]) for row in rows]
