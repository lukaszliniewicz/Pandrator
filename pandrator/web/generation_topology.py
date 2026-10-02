"""Immutable generation-plan creation and topology revisions."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import Database
from .jobs import JobQueue
from .models import (
    AudioTake,
    DocumentRevision,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
    utcnow,
)
from .output_assembly_lifecycle import mark_output_assemblies_stale
from .settings_policy import RevisionConflict, stable_hash


class GenerationTopologyService:
    """Create plans and topology revisions using independent transaction inputs."""

    def __init__(self, database: Database, jobs: JobQueue):
        self.database = database
        self.jobs = jobs

    def create_plan(
        self,
        session_id: str,
        *,
        source_revision_id: str | None,
        segments: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_segments = [
            item for item in segments if str(item.get("text") or "").strip()
        ]
        if not clean_segments:
            raise ValueError("At least one generation segment is required.")
        content = {
            "source_revision_id": source_revision_id,
            "segments": clean_segments,
            "settings": settings or {},
        }
        with self.database.immediate_session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            if (
                source_revision_id
                and session.get(DocumentRevision, source_revision_id) is None
            ):
                raise KeyError(source_revision_id)
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            if plan is None:
                plan = GenerationPlan(session_id=session_id)
                session.add(plan)
                session.flush()
            revision_number = (
                int(
                    session.scalar(
                        select(func.max(GenerationPlanRevision.revision_number)).where(
                            GenerationPlanRevision.plan_id == plan.id
                        )
                    )
                    or 0
                )
                + 1
            )
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                source_revision_id=source_revision_id,
                revision_number=revision_number,
                settings_json=settings or {},
                content_hash=stable_hash(content),
            )
            session.add(revision)
            session.flush()
            for index, item in enumerate(clean_segments):
                provenance = dict(
                    item.get("speech_block_provenance")
                    or item.get("provenance")
                    or {}
                )
                turn_id = item.get("turn_id")
                if turn_id is None:
                    for key in ("_passage", "logical_passage"):
                        passage = item.get(key)
                        if isinstance(passage, dict) and passage.get("turn_id") is not None:
                            turn_id = passage.get("turn_id")
                            break
                if turn_id is not None:
                    if not isinstance(turn_id, str):
                        raise ValueError("Speech-plan turn_id must be a string.")
                    turn_id = turn_id.strip()
                    if turn_id:
                        existing_turn_id = provenance.get("turn_id")
                        if (
                            existing_turn_id
                            and str(existing_turn_id).strip() != turn_id
                        ):
                            raise ValueError(
                                "Speech-plan turn_id conflicts with its block provenance."
                            )
                        provenance["turn_id"] = turn_id
                session.add(
                    GenerationSegment(
                        plan_revision_id=revision.id,
                        ordinal=index,
                        source_segment_ids_json=list(
                            item.get("source_segment_ids") or []
                        ),
                        speech_block_provenance_json=provenance,
                        alignment_group=str(item.get("alignment_group") or "").strip()
                        or None,
                        node_kind=str(
                            item.get("node_kind")
                            or (
                                "chapter_marker"
                                if str(item.get("chapter") or "").lower() == "yes"
                                else "paragraph"
                            )
                        ),
                        paragraph_break_after=bool(
                            item.get(
                                "paragraph_break_after",
                                str(item.get("paragraph") or "").lower() == "yes",
                            )
                        ),
                        speaker=str(item.get("speaker") or "").strip() or None,
                        text=str(item.get("text") or "").strip(),
                        optimized_text=(
                            str(
                                item.get("tts_optimized_sentence")
                                or item.get("optimized_text")
                                or ""
                            ).strip()
                            or None
                        ),
                        speech_plan_json=dict(item.get("speech_plan") or {}),
                        optimization_status=(
                            "optimized"
                            if str(
                                item.get("tts_optimized_sentence")
                                or item.get("optimized_text")
                                or ""
                            ).strip()
                            else "not_requested"
                        ),
                        optimization_source_hash=(
                            hashlib.sha256(
                                str(item.get("text") or "").strip().encode("utf-8")
                            ).hexdigest()
                            if str(
                                item.get("tts_optimized_sentence")
                                or item.get("optimized_text")
                                or ""
                            ).strip()
                            else None
                        ),
                        optimization_model=(
                            str(
                                (item.get("speech_plan") or {}).get("model") or ""
                            ).strip()
                            or None
                        ),
                        voice_id=item.get("voice_id"),
                        voice=item.get("voice"),
                        language=item.get("language"),
                        silence_after_ms=max(0, int(item.get("silence_after_ms") or 0)),
                    )
                )
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            session.flush()
            return {
                "id": plan.id,
                "active_revision_id": revision.id,
                "revision_number": revision_number,
                "segment_count": len(clean_segments),
            }

    @staticmethod
    def _segment_copy_values(segment: GenerationSegment) -> dict[str, Any]:
        """Return the persisted, user-visible fields for a new plan segment."""

        return {
            "ordinal": segment.ordinal,
            "source_segment_ids_json": list(segment.source_segment_ids_json or []),
            "speech_block_provenance_json": deepcopy(
                segment.speech_block_provenance_json or {}
            ),
            "alignment_group": segment.alignment_group,
            "node_kind": segment.node_kind,
            "paragraph_break_after": segment.paragraph_break_after,
            "speaker": segment.speaker,
            "text": segment.text,
            "optimized_text": segment.optimized_text,
            "speech_plan_json": deepcopy(segment.speech_plan_json or {}),
            "optimization_status": segment.optimization_status,
            "optimization_source_hash": segment.optimization_source_hash,
            "optimization_reviewed": segment.optimization_reviewed,
            "optimization_model": segment.optimization_model,
            "voice_id": segment.voice_id,
            "voice": segment.voice,
            "language": segment.language,
            "silence_after_ms": segment.silence_after_ms,
            "marked": segment.marked,
            "removed": segment.removed,
            "status": segment.status,
            "revision": 1,
        }


    @staticmethod
    def _provenance_source_refs(provenance: dict[str, Any]) -> list[Any]:
        refs: list[Any] = []
        for cue in provenance.get("source_cues") or []:
            if not isinstance(cue, dict):
                continue
            reference = cue.get("reference")
            if isinstance(reference, bool) or not isinstance(reference, (int, str)):
                continue
            if isinstance(reference, str) and not reference.strip():
                continue
            if reference not in refs:
                refs.append(reference)
        return refs


    @staticmethod
    def _clip_local_ranges(
        ranges: object,
        start: int,
        end: int,
        *,
        trim_start: int = 0,
        trim_end: int | None = None,
    ) -> list[list[int]]:
        """Clip local provenance ranges and rebase them to one child string."""

        if trim_end is None:
            trim_end = end
        output: list[list[int]] = []
        for value in ranges if isinstance(ranges, list) else []:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                continue
            try:
                span_start, span_end = int(value[0]), int(value[1])
            except (TypeError, ValueError):
                continue
            clipped_start = max(span_start, start, trim_start)
            clipped_end = min(span_end, end, trim_end)
            if clipped_end <= clipped_start:
                continue
            output.append([clipped_start - trim_start, clipped_end - trim_start])
        return output


    @classmethod
    def _split_provenance(
        cls,
        provenance: dict[str, Any],
        *,
        display_cursor: int,
        speech_cursor: int,
        display_length: int,
        speech_length: int,
        display_value: str,
        speech_value: str,
        operation_event: dict[str, Any],
        parent_segment_id: str,
        parent_speech_plan_ids: list[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Partition persisted provenance without manufacturing whole-cue spans."""

        base = deepcopy(provenance or {})
        source_cues = base.get("source_cues")
        if not isinstance(source_cues, list):
            source_cues = []
        children: list[list[dict[str, Any]]] = [[], []]
        refs: list[list[Any]] = [[], []]
        for raw_cue in source_cues:
            if not isinstance(raw_cue, dict):
                continue
            reference = raw_cue.get("reference")
            if isinstance(reference, bool) or not isinstance(reference, (int, str)):
                continue
            if isinstance(reference, str) and not reference.strip():
                continue
            display_spans = raw_cue.get("display_spans") or []
            speech_spans = raw_cue.get("speech_spans") or []
            for index, (start, end) in enumerate(
                (
                    (0, display_cursor),
                    (display_cursor, display_length),
                )
            ):
                child = deepcopy(raw_cue)
                display_raw = display_value[start:end]
                display_trim_start = (
                    start + len(display_raw) - len(display_raw.lstrip())
                )
                display_trim_end = start + len(display_raw.rstrip())
                child["display_spans"] = cls._clip_local_ranges(
                    display_spans,
                    start,
                    end,
                    trim_start=display_trim_start,
                    trim_end=display_trim_end,
                )
                speech_start, speech_end = (
                    (0, speech_cursor) if index == 0 else (speech_cursor, speech_length)
                )
                speech_raw = speech_value[speech_start:speech_end]
                speech_trim_start = (
                    speech_start + len(speech_raw) - len(speech_raw.lstrip())
                )
                speech_trim_end = speech_start + len(speech_raw.rstrip())
                child["speech_spans"] = cls._clip_local_ranges(
                    speech_spans,
                    speech_start,
                    speech_end,
                    trim_start=speech_trim_start,
                    trim_end=speech_trim_end,
                )
                if child["display_spans"] or child["speech_spans"]:
                    children[index].append(child)
                    refs[index].append(reference)
        fallback_refs = cls._provenance_source_refs(base)
        if not fallback_refs:
            fallback_refs = [
                value
                for value in operation_event.get("source_references") or []
                if not isinstance(value, bool)
                and isinstance(value, (int, str))
                and (not isinstance(value, str) or value.strip())
            ]
        source_reference_namespace = base.get("source_reference_namespace")
        if not source_reference_namespace:
            source_reference_namespace = (
                "subtitle_ordinal"
                if fallback_refs
                and all(isinstance(value, int) for value in fallback_refs)
                else "generation_source_reference"
            )
        for index in range(2):
            if not children[index] and fallback_refs:
                # Legacy rows have no spans.  Keep their source references on
                # both children rather than pretending an exact range exists.
                children[index] = [
                    {
                        "reference": reference,
                        "start_ms": None,
                        "end_ms": None,
                        "display_text": None,
                        "speech_text": None,
                        "display_spans": [],
                        "speech_spans": [],
                    }
                    for reference in fallback_refs
                ]
                refs[index] = list(fallback_refs)

        outputs: list[dict[str, Any]] = []
        for index in range(2):
            child = {
                "schema_version": int(base.get("schema_version") or 1),
                "origin": base.get("origin") or "manual",
                "source_reference_namespace": source_reference_namespace,
                "source_cues": children[index],
                "formation_events": [
                    *[
                        deepcopy(value)
                        for value in base.get("formation_events") or []
                        if isinstance(value, dict)
                    ],
                    deepcopy(operation_event),
                ],
                "boundary_before": deepcopy(
                    base.get("boundary_before")
                    or {
                        "action": "keep_boundary",
                        "reason_code": "manual_topology",
                        "summary": "Manual topology revision boundary.",
                        "measurements": {},
                        "source_references": fallback_refs,
                    }
                ),
                "risk_flags": sorted(
                    {str(value) for value in base.get("risk_flags") or [] if value}
                ),
            }
            if index == 1:
                child["boundary_before"] = {
                    "action": "keep_boundary",
                    "reason_code": "manual_split",
                    "summary": "Manual split boundary retained.",
                    "measurements": {
                        "display_offset": display_cursor,
                        "speech_offset": speech_cursor,
                    },
                    "source_references": list(refs[index]),
                }
            child["manual_topology"] = {
                "operation": "split",
                "parent_segment_id": parent_segment_id,
                "parent_speech_plan_ids": list(parent_speech_plan_ids),
            }
            outputs.append(child)
        return outputs[0], outputs[1]


    @staticmethod
    def _speech_plan_ids(value: object) -> list[str]:
        if not isinstance(value, dict):
            return []
        ids: list[str] = []
        for key in ("id", "plan_id", "speech_plan_id"):
            item = str(value.get(key) or "").strip()
            if item and item not in ids:
                ids.append(item)
        return ids


    @staticmethod
    def _companion_offset(
        selected_text: str,
        companion_text: str,
        selected_offset: int,
        *,
        selected_layer: str,
        provenance: dict[str, Any],
    ) -> tuple[int, str]:
        if not companion_text:
            return 0, "empty_companion"
        if companion_text == selected_text:
            return min(max(selected_offset, 1), len(companion_text) - 1), "identity"
        selected_key = (
            "display_spans" if selected_layer == "display" else "speech_spans"
        )
        companion_key = (
            "speech_spans" if selected_layer == "display" else "display_spans"
        )
        mapped: list[int] = []
        for cue in provenance.get("source_cues") or []:
            if not isinstance(cue, dict):
                continue
            for selected_span, companion_span in zip(
                cue.get(selected_key) or [],
                cue.get(companion_key) or [],
                strict=False,
            ):
                if not (
                    isinstance(selected_span, (list, tuple))
                    and len(selected_span) == 2
                    and isinstance(companion_span, (list, tuple))
                    and len(companion_span) == 2
                ):
                    continue
                try:
                    selected_start, selected_end = map(int, selected_span)
                    companion_start, companion_end = map(int, companion_span)
                except (TypeError, ValueError):
                    continue
                if selected_start <= selected_offset <= selected_end:
                    ratio = (selected_offset - selected_start) / max(
                        selected_end - selected_start, 1
                    )
                    mapped.append(
                        round(
                            companion_start + ratio * (companion_end - companion_start)
                        )
                    )
        if mapped:
            target = min(max(mapped[0], 1), len(companion_text) - 1)
            return target, "source_span"
        ratio = selected_offset / max(len(selected_text), 1)
        target = round(ratio * len(companion_text))
        target = min(max(target, 1), len(companion_text) - 1)
        candidates = [
            index
            for index in range(1, len(companion_text))
            if companion_text[index - 1].isspace()
            or companion_text[index].isspace()
            or companion_text[index - 1] in ".!?;,:\u2014\u2013"
        ]
        if candidates:
            target = min(candidates, key=lambda index: (abs(index - target), index))
            return target, "proportional_boundary"
        return target, "proportional"


    @classmethod
    def _merge_provenance(
        cls,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        display_offset: int,
        speech_offset: int,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        result = deepcopy(left or {})
        result.setdefault("schema_version", 1)
        result.setdefault("origin", "manual")
        source_references = [
            value
            for value in event.get("source_references") or []
            if not isinstance(value, bool) and isinstance(value, (int, str))
        ]
        namespace = (left or {}).get("source_reference_namespace") or (right or {}).get(
            "source_reference_namespace"
        )
        if not namespace:
            namespace = (
                "subtitle_ordinal"
                if source_references
                and all(isinstance(value, int) for value in source_references)
                else "generation_source_reference"
            )
        result["source_reference_namespace"] = namespace
        by_reference: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for source, display_shift, speech_shift in (
            (left or {}, 0, 0),
            (right or {}, display_offset, speech_offset),
        ):
            for raw_cue in source.get("source_cues") or []:
                if not isinstance(raw_cue, dict):
                    continue
                reference = raw_cue.get("reference")
                if isinstance(reference, bool) or not isinstance(reference, (int, str)):
                    continue
                if isinstance(reference, str) and not reference.strip():
                    continue
                reference_key = (type(reference).__name__, str(reference))
                cue = by_reference.setdefault(
                    reference_key,
                    {
                        "reference": reference,
                        "start_ms": raw_cue.get("start_ms"),
                        "end_ms": raw_cue.get("end_ms"),
                        "display_text": raw_cue.get("display_text"),
                        "speech_text": raw_cue.get("speech_text"),
                        "display_spans": [],
                        "speech_spans": [],
                    },
                )
                if reference_key not in order:
                    order.append(reference_key)
                for key, shift in (
                    ("display_spans", display_shift),
                    ("speech_spans", speech_shift),
                ):
                    for span in raw_cue.get(key) or []:
                        if not isinstance(span, (list, tuple)) or len(span) != 2:
                            continue
                        try:
                            start, end = int(span[0]) + shift, int(span[1]) + shift
                        except (TypeError, ValueError):
                            continue
                        if end > start and [start, end] not in cue[key]:
                            cue[key].append([start, end])
                for timing_key in ("start_ms", "end_ms"):
                    if cue.get(timing_key) is None:
                        cue[timing_key] = raw_cue.get(timing_key)
                cue["display_text"] = cue.get("display_text") or raw_cue.get(
                    "display_text"
                )
                cue["speech_text"] = cue.get("speech_text") or raw_cue.get(
                    "speech_text"
                )
        result["source_cues"] = [by_reference[reference] for reference in order]
        result["formation_events"] = [
            *[
                deepcopy(value)
                for source in (left or {}, right or {})
                for value in source.get("formation_events") or []
                if isinstance(value, dict)
            ],
            deepcopy(event),
        ]
        result["risk_flags"] = sorted(
            {
                str(value)
                for source in (left or {}, right or {})
                for value in source.get("risk_flags") or []
                if value
            }
        )
        return result


    @staticmethod
    def _clone_available_takes(
        session: Session,
        source_segment_id: str,
        target_segment_id: str,
    ) -> list[str]:
        source_takes = list(
            session.scalars(
                select(AudioTake)
                .where(
                    AudioTake.generation_segment_id == source_segment_id,
                    AudioTake.status.in_(("completed", "stale")),
                    AudioTake.artifact_id.is_not(None),
                )
                .order_by(AudioTake.created_at, AudioTake.id)
            ).all()
        )
        new_ids: list[str] = []
        for source in source_takes:
            target = AudioTake(
                generation_segment_id=target_segment_id,
                generation_run_id=None,
                artifact_id=source.artifact_id,
                parent_take_id=source.id,
                kind=source.kind,
                status=source.status,
                settings_hash=source.settings_hash,
                duration_ms=source.duration_ms,
                is_active=source.is_active,
                revision=source.revision,
            )
            session.add(target)
            session.flush()
            new_ids.append(target.id)
        return new_ids


    @staticmethod
    def _clone_available_takes_batch(
        session: Session, pairs: list[tuple[str, str]]
    ) -> None:
        """Preserve unchanged audio without one SELECT/flush per segment.

        This runs inside the immutable topology transaction. Keeping its write
        lock brief matters: the generation worker renews its lease through the
        same SQLite database while an early-timing repair is staged.
        """
        target_by_source = dict(pairs)
        source_ids = list(target_by_source)
        with session.no_autoflush:
            for offset in range(0, len(source_ids), 400):
                sources = session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_segment_id.in_(source_ids[offset:offset + 400]),
                        AudioTake.status.in_(("completed", "stale")),
                        AudioTake.artifact_id.is_not(None),
                    ).order_by(AudioTake.created_at, AudioTake.id)
                ).all()
                session.add_all([
                    AudioTake(
                        generation_segment_id=target_by_source[source.generation_segment_id],
                        generation_run_id=None,
                        artifact_id=source.artifact_id,
                        parent_take_id=source.id,
                        kind=source.kind,
                        status=source.status,
                        settings_hash=source.settings_hash,
                        duration_ms=source.duration_ms,
                        is_active=source.is_active,
                        revision=source.revision,
                    )
                    for source in sources
                ])
        session.flush()


    @staticmethod
    def _recompute_alignment_groups(segments: list[GenerationSegment]) -> None:
        previous_refs: set[str] = set()
        group_number = 0
        for segment in segments:
            refs = {str(value) for value in segment.source_segment_ids_json or []}
            if not refs:
                previous_refs = set()
                continue
            if not previous_refs.intersection(refs):
                group_number += 1
            segment.alignment_group = f"a{group_number:04d}"
            previous_refs = refs


    @staticmethod
    def _recompute_alignment_group_values(
        segments: list[dict[str, Any]],
    ) -> None:
        """Normalize alignment groups before immutable plan content is hashed."""

        previous_refs: set[str] = set()
        group_number = 0
        for segment in segments:
            refs = {
                str(value) for value in segment.get("source_segment_ids_json") or []
            }
            if not refs:
                previous_refs = set()
                continue
            if not previous_refs.intersection(refs):
                group_number += 1
            segment["alignment_group"] = f"a{group_number:04d}"
            previous_refs = refs


    def revise_topology(
        self,
        session_id: str,
        expected_revision_id: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            return self.revise_topology_in_session(
                session, session_id, expected_revision_id, operation
            )


    def revise_topology_in_session(
        self,
        session: Session,
        session_id: str,
        expected_revision_id: str,
        operation: dict[str, Any],
        *,
        activate: bool = True,
    ) -> dict[str, Any]:
        """Create one immutable typed topology revision in a caller transaction."""

        expected_revision_id = str(expected_revision_id or "").strip()
        if not expected_revision_id:
            raise ValueError("An expected plan revision ID is required.")
        if not isinstance(operation, dict):
            raise TypeError("A topology operation object is required.")
        action = str(operation.get("action") or "").strip().lower()
        if action not in {"split", "merge", "restore", "resegment"}:
            raise ValueError("Topology action must be split, merge, restore, or resegment.")
        if action == "resegment":
            activate = False
        plan = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        if plan is None or not plan.active_revision_id:
            raise KeyError(session_id)
        if str(plan.active_revision_id) != expected_revision_id:
            raise RevisionConflict("The generation plan changed in another client.")
        current = session.get(GenerationPlanRevision, plan.active_revision_id)
        if current is None or current.plan_id != plan.id:
            raise KeyError(expected_revision_id)
        current_segments = list(
            session.scalars(
                select(GenerationSegment)
                .where(GenerationSegment.plan_revision_id == current.id)
                .order_by(GenerationSegment.ordinal)
            ).all()
        )
        active_segments = [
            segment for segment in current_segments if not segment.removed
        ]
        source_by_id = {segment.id: segment for segment in current_segments}
        mappings: list[dict[str, Any]] = []
        replacement_by_old: dict[str, list[dict[str, Any]]] = {}
        target_segments: list[GenerationSegment] = []
        revision_source = current
        merge_pair: tuple[GenerationSegment, GenerationSegment] | None = None
        if action == "split":
            segment_id = str(operation.get("segment_id") or "").strip()
            segment = source_by_id.get(segment_id)
            if segment is None or segment.removed:
                raise KeyError(segment_id)
            layer = str(operation.get("text_layer") or "display").strip().lower()
            if layer not in {"display", "speech"}:
                raise ValueError("text_layer must be display or speech.")
            cursor_value = operation.get("cursor")
            if cursor_value is None:
                raise ValueError("Split cursor must be an integer.")
            try:
                cursor = int(cursor_value)
            except (TypeError, ValueError) as error:
                raise ValueError("Split cursor must be an integer.") from error
            selected_text = (
                segment.text
                if layer == "display"
                else (segment.optimized_text or segment.text)
            )
            if cursor <= 0 or cursor >= len(selected_text):
                raise ValueError("Split cursor must be inside the selected text layer.")
            selected_left, selected_right = (
                selected_text[:cursor].strip(),
                selected_text[cursor:].strip(),
            )
            if not selected_left or not selected_right:
                raise ValueError("A split cannot produce an empty trimmed half.")
            provenance = dict(segment.speech_block_provenance_json or {})
            companion_text = (
                segment.optimized_text
                if layer == "display" and segment.optimized_text
                else segment.text
                if layer == "speech"
                else segment.optimized_text or segment.text
            )
            if operation.get("passage_boundary_id"):
                from .passage_markers import validate_passage_split

                verified_display, verified_speech = validate_passage_split(
                    segment, operation["passage_boundary_id"], layer, cursor,
                )
                companion_cursor = verified_speech if layer == "display" else verified_display
                mapping_rule = "verified_passage_boundary"
            else:
                companion_cursor, mapping_rule = self._companion_offset(
                    selected_text,
                    companion_text,
                    cursor,
                    selected_layer=layer,
                    provenance=provenance,
                )
            if layer == "display":
                display_cursor, speech_cursor = cursor, companion_cursor
            else:
                display_cursor, speech_cursor = companion_cursor, cursor
            effective_speech = segment.optimized_text or segment.text
            speech_plan_ids = self._speech_plan_ids(segment.speech_plan_json)
            source_references: list[Any] = self._provenance_source_refs(provenance)
            if not source_references:
                source_references = list(segment.source_segment_ids_json or [])
            event = {
                "action": "manual_split",
                "reason_code": "manual_topology_split",
                "summary": "Generation segment split at a requested text offset.",
                "measurements": {
                    "text_layer": layer,
                    "selected_offset": cursor,
                    "companion_offset": companion_cursor,
                    "display_offset": display_cursor,
                    "speech_offset": speech_cursor,
                    "mapping_rule": mapping_rule,
                },
                "source_references": source_references,
            }
            if operation.get("reason") == "early_timing_repair":
                event.update(
                    action="automatic_split",
                    reason_code="early_timing_repair",
                    summary="Short voiceover block split at a source cue boundary to reduce early speech.",
                )
            left_provenance, right_provenance = self._split_provenance(
                provenance,
                display_cursor=display_cursor,
                speech_cursor=speech_cursor,
                display_length=len(segment.text),
                speech_length=len(effective_speech),
                display_value=segment.text,
                speech_value=effective_speech,
                operation_event=event,
                parent_segment_id=segment.id,
                parent_speech_plan_ids=speech_plan_ids,
            )
            for provenance_value in (left_provenance, right_provenance):
                provenance_value["manual_topology"]["parent_revision_id"] = current.id
            display_left = segment.text[:display_cursor].strip()
            display_right = segment.text[display_cursor:].strip()
            speech_left = effective_speech[:speech_cursor].strip()
            speech_right = effective_speech[speech_cursor:].strip()
            if (
                not display_left
                or not display_right
                or not speech_left
                or not speech_right
            ):
                raise ValueError("A split cannot produce an empty trimmed half.")
            left_values = self._segment_copy_values(segment)
            right_values = self._segment_copy_values(segment)
            for values, text, speech, provenance_value in (
                (left_values, display_left, speech_left, left_provenance),
                (right_values, display_right, speech_right, right_provenance),
            ):
                values["text"] = text
                values["optimized_text"] = speech if segment.optimized_text else None
                values["speech_block_provenance_json"] = provenance_value
                values["source_segment_ids_json"] = self._provenance_source_refs(provenance_value) or list(segment.source_segment_ids_json or [])
                values["speech_plan_json"] = {
                    "version": 1,
                    "status": "manual_topology",
                    "parent_segment_id": segment.id,
                    "parent_speech_plan_ids": speech_plan_ids,
                }
                values["status"] = "stale"
                values["optimization_status"] = "stale"
                values["optimization_source_hash"] = None
                values["optimization_reviewed"] = False
                values["optimization_model"] = None
            right_values["silence_after_ms"] = segment.silence_after_ms
            right_values["paragraph_break_after"] = segment.paragraph_break_after
            left_values["silence_after_ms"] = 0
            left_values["paragraph_break_after"] = False
            from .audiobook_resegmentation import project_split_markup
            project_split_markup(session, segment, [left_values, right_values], speech_cursor, session_id)
            replacement_by_old[segment.id] = [left_values, right_values]
            mappings.append(
                {
                    "source_segment_id": segment.id,
                    "new_segments": ["left", "right"],
                    "text_layer": layer,
                    "selected_offset": cursor,
                    "display_offset": display_cursor,
                    "speech_offset": speech_cursor,
                    "mapping_rule": mapping_rule,
                }
            )
        elif action == "merge":
            left_id = str(operation.get("left_segment_id") or "").strip()
            right_id = str(operation.get("right_segment_id") or "").strip()
            left = source_by_id.get(left_id)
            right = source_by_id.get(right_id)
            if left is None or right is None or left.removed or right.removed:
                raise KeyError(left_id if left is None else right_id)
            merge_pair = (left, right)
            if left.ordinal >= right.ordinal:
                raise ValueError(
                    "Merge segments must be supplied in left-to-right order."
                )
            active_positions = {
                segment.id: index for index, segment in enumerate(active_segments)
            }
            if active_positions.get(left_id, -1) + 1 != active_positions.get(
                right_id, -2
            ):
                raise ValueError(
                    "Merge segments must be adjacent active-plan segments."
                )
            if left.speaker and right.speaker and left.speaker != right.speaker:
                raise ValueError("Segments with different speakers cannot be merged.")
            for field_name in ("voice_id", "voice", "language"):
                if getattr(left, field_name) != getattr(right, field_name):
                    raise ValueError(f"Merge delivery override mismatch: {field_name}.")
            left_speech = left.optimized_text or left.text
            right_speech = right.optimized_text or right.text
            merged_speech = f"{left_speech} {right_speech}".strip()
            settings = dict(current.settings_json or {})
            max_chars = settings.get("speech_block_max_chars")
            if max_chars is not None:
                try:
                    max_chars = int(max_chars)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "speech_block_max_chars must be an integer."
                    ) from error
                if len(merged_speech) > max_chars:
                    raise ValueError(
                        "Merged speech text exceeds speech_block_max_chars."
                    )
            left_source_references = self._provenance_source_refs(
                dict(left.speech_block_provenance_json or {})
            ) or list(left.source_segment_ids_json or [])
            right_source_references = self._provenance_source_refs(
                dict(right.speech_block_provenance_json or {})
            ) or list(right.source_segment_ids_json or [])
            event = {
                "action": "manual_merge",
                "reason_code": "manual_topology_merge",
                "summary": "Adjacent generation segments merged at a requested boundary.",
                "measurements": {
                    "left_length": len(left.text),
                    "right_length": len(right.text),
                    "speech_length": len(merged_speech),
                    "max_chars": max_chars,
                    "left_segment_id": left.id,
                    "right_segment_id": right.id,
                    "left_revision_id": current.id,
                    "right_revision_id": current.id,
                },
                "source_references": list(
                    dict.fromkeys([*left_source_references, *right_source_references])
                ),
            }
            merged_provenance = self._merge_provenance(
                dict(left.speech_block_provenance_json or {}),
                dict(right.speech_block_provenance_json or {}),
                display_offset=len(left.text) + 1,
                speech_offset=len(left_speech) + 1,
                event=event,
            )
            merged_provenance["manual_topology"] = {
                "operation": "merge",
                "parent_segment_ids": [left.id, right.id],
                "parent_revision_ids": [current.id, current.id],
                "parent_speech_plan_ids": [
                    *self._speech_plan_ids(left.speech_plan_json),
                    *self._speech_plan_ids(right.speech_plan_json),
                ],
            }
            merged_values = self._segment_copy_values(left)
            merged_values.update(
                {
                    "text": f"{left.text} {right.text}".strip(),
                    "optimized_text": (
                        merged_speech
                        if left.optimized_text or right.optimized_text
                        else None
                    ),
                    "source_segment_ids_json": list(
                        dict.fromkeys(
                            [
                                *left.source_segment_ids_json,
                                *right.source_segment_ids_json,
                            ]
                        )
                    ),
                    "speech_block_provenance_json": merged_provenance,
                    "speech_plan_json": {
                        "version": 1,
                        "status": "manual_topology",
                        "parent_segment_ids": [left.id, right.id],
                        "parent_revision_ids": [current.id, current.id],
                        "removed_boundary_before": deepcopy(
                            (right.speech_block_provenance_json or {}).get(
                                "boundary_before"
                            )
                        ),
                    },
                    "speaker": left.speaker or right.speaker,
                    "marked": left.marked or right.marked,
                    "status": "stale",
                    "optimization_status": "stale",
                    "optimization_source_hash": None,
                    "optimization_reviewed": False,
                    "optimization_model": None,
                    "silence_after_ms": right.silence_after_ms,
                    "paragraph_break_after": right.paragraph_break_after,
                }
            )
            from .audiobook_resegmentation import project_merge_markup
            project_merge_markup(session, left, right, merged_values, session_id)
            replacement_by_old[left.id] = [merged_values]
            replacement_by_old[right.id] = []
            mappings.append(
                {
                    "source_segment_ids": [left.id, right.id],
                    "new_segments": ["merged"],
                }
            )
        elif action == "resegment":
            from .audiobook_resegmentation import prepare_resegmentation
            from .speech_plan_workspace import plan_signature
            replacement_by_old, mappings = prepare_resegmentation(
                self, session, session_id, current, active_segments, operation,
            )
            operation = {**operation, "draft": True, "base_signature": plan_signature(session, current.id)}
        else:
            target_id = str(operation.get("target_revision_id") or "").strip()
            target = session.get(GenerationPlanRevision, target_id)
            if target is None or target.plan_id != plan.id:
                raise KeyError(target_id)
            revision_source = target
            if (target.operation_json or {}).get("draft"):
                from .source_management import assert_session_idle
                from .speech_plan_workspace import plan_signature
                assert_session_idle(session, session_id)
                if target.parent_revision_id != current.id or (target.operation_json or {}).get("base_signature") != plan_signature(session, current.id):
                    raise RevisionConflict("The audiobook changed after this draft was prepared. Create a fresh resegmentation draft.")
            target_segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == target.id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            if not target_segments:
                raise ValueError("The target plan revision has no segments.")
            for segment in target_segments:
                values = self._segment_copy_values(segment)
                replacement_by_old[segment.id] = [values]
                mappings.append(
                    {"source_segment_id": segment.id, "new_segments": ["restored"]}
                )

        operation_json = {
            "action": action,
            "expected_revision_id": expected_revision_id,
            **{
                key: value
                for key, value in operation.items()
                if key not in {"expected_revision_id", "action"}
            },
            "mapping": mappings,
        }
        if action == "restore":
            values_sequence = [
                values
                for segment in target_segments
                for values in replacement_by_old.get(segment.id, [])
            ]
            # target_segments are already in ordinal order, unlike the mapping
            # dictionary's insertion order in malformed legacy rows.
        else:
            values_sequence = []
            for segment in current_segments:
                values_sequence.extend(
                    replacement_by_old.get(
                        segment.id,
                        [self._segment_copy_values(segment)],
                    )
                )
        persisted_values = []
        for ordinal, values in enumerate(values_sequence):
            persisted = deepcopy(values)
            persisted["ordinal"] = ordinal
            persisted_values.append(persisted)
        if action != "restore":
            self._recompute_alignment_group_values(persisted_values)
        revision_number = (
            int(
                session.scalar(
                    select(func.max(GenerationPlanRevision.revision_number)).where(
                        GenerationPlanRevision.plan_id == plan.id
                    )
                )
                or 0
            )
            + 1
        )
        revision = GenerationPlanRevision(
            plan_id=plan.id,
            parent_revision_id=current.id,
            source_revision_id=revision_source.source_revision_id,
            revision_number=revision_number,
            settings_json=deepcopy(revision_source.settings_json or {}),
            operation_json=deepcopy(operation_json),
            content_hash=stable_hash(
                {
                    "parent_revision_id": current.id,
                    "operation": operation_json,
                    "segments": persisted_values,
                }
            ),
        )
        session.add(revision)
        session.flush()
        new_segments: list[GenerationSegment] = []
        for values in persisted_values:
            new_segment = GenerationSegment(plan_revision_id=revision.id, **values)
            if new_segment.status == "running":
                # The worker owns the source row, not this new editorial copy.
                new_segment.status = "ready"
            session.add(new_segment)
            new_segments.append(new_segment)
        # One flush, not a growing-unit-of-work flush for every speech block.
        session.flush()
        if action != "restore":
            self._recompute_alignment_groups(new_segments)
        session.flush()

        speech_markup_characters: list[dict[str, Any]] = []
        speech_markup_controls_loaded = False
        if any(
            isinstance((segment.speech_plan_json or {}).get("speech_xml"), str)
            and (segment.speech_plan_json or {}).get("speech_xml")
            for segment in new_segments
        ):
            from .generation_cast_runtime import remap_markup
            from .generation_controls import get_generation_controls

            speech_markup_characters = (
                get_generation_controls(session, session_id).get("characters") or []
            )
            speech_markup_controls_loaded = True
            for persisted, segment in zip(persisted_values, new_segments, strict=True):
                speech_plan = deepcopy(segment.speech_plan_json or {})
                xml = speech_plan.get("speech_xml")
                if not isinstance(xml, str) or not xml:
                    continue
                speech_plan["speech_xml"] = remap_markup(
                    xml,
                    segment.id,
                    segment.optimized_text or segment.text,
                    speech_markup_characters,
                )
                segment.speech_plan_json = speech_plan
                persisted["speech_plan_json"] = deepcopy(speech_plan)

        # Reuse takes only for byte-for-byte unchanged segments and exact
        # restore copies.  Split/merge replacements intentionally have no
        # source take mapping.
        take_copy_pairs: list[tuple[str, str]] = []
        if action == "restore":
            take_copy_pairs.extend(
                (source.id, target_segment.id)
                for source, target_segment in zip(target_segments, new_segments, strict=True)
            )
        else:
            new_index = 0
            for source in current_segments:
                replacements = replacement_by_old.get(source.id)
                if replacements is None:
                    replacements = [self._segment_copy_values(source)]
                if len(replacements) != 1:
                    new_index += len(replacements)
                    continue
                source_values = self._segment_copy_values(source)
                target_values = self._segment_copy_values(new_segments[new_index])
                source_plan = dict(source_values.get("speech_plan_json") or {})
                target_plan = dict(target_values.get("speech_plan_json") or {})
                source_xml = source_plan.get("speech_xml")
                target_xml = target_plan.get("speech_xml")
                if (
                    isinstance(source_xml, str)
                    and source_xml
                    and isinstance(target_xml, str)
                    and target_xml
                    and speech_markup_controls_loaded
                ):
                    from .generation_cast_runtime import remap_markup

                    source_plan["speech_xml"] = remap_markup(
                        source_xml,
                        new_segments[new_index].id,
                        source.optimized_text or source.text,
                        speech_markup_characters,
                    )
                    source_values["speech_plan_json"] = source_plan
                # Alignment groups are assembly bookkeeping, not TTS input.
                for ignored_key in ("ordinal", "revision", "alignment_group"):
                    source_values.pop(ignored_key, None)
                    target_values.pop(ignored_key, None)
                unchanged = source_values == target_values
                if unchanged:
                    take_copy_pairs.append((source.id, new_segments[new_index].id))
                new_index += 1
        self._clone_available_takes_batch(session, take_copy_pairs)
        if activate:
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            mark_output_assemblies_stale(
                session,
                session_id,
                cancel_active=True,
                jobs=self.jobs,
            )
        session.flush()
        lineage: dict[str, list[str]] = {}
        affected_ids: list[str] = []
        new_index = 0
        for source in target_segments if action == "restore" else current_segments:
            replacement_count = len(replacement_by_old.get(source.id, [None]))
            children = [item.id for item in new_segments[new_index:new_index + replacement_count]]
            lineage[source.id] = children
            if source.id in replacement_by_old:
                affected_ids.extend(children)
            new_index += replacement_count
        if merge_pair is not None:
            left, right = merge_pair
            lineage[right.id] = list(lineage[left.id])
        if action == "resegment":
            for source_id in replacement_by_old:
                lineage[source_id] = [item.id for item in new_segments if any(
                    span["segment_id"] == source_id
                    for span in (item.speech_block_provenance_json or {}).get("resegmentation", {}).get("source_spans", [])
                )]
        operation_json["lineage"] = lineage
        revision.operation_json = deepcopy(operation_json)
        revision.content_hash = stable_hash({"parent_revision_id": current.id, "operation": operation_json, "segments": persisted_values})
        session.flush()
        return {
            "id": plan.id,
            "lineage": lineage,
            "plan_revision_id": revision.id,
            "revision_number": revision.revision_number,
            "parent_revision_id": revision.parent_revision_id,
            "operation_json": dict(revision.operation_json or {}),
            "segment_ids": [segment.id for segment in new_segments],
            "affected_segment_ids": affected_ids,
            "is_draft": not activate,
            "active_plan_revision_id": plan.active_revision_id,
            "preview": mappings[0] if action == "resegment" else None,
            "review_required": {"performance": True, "changed_audio": len(affected_ids)},
        }

