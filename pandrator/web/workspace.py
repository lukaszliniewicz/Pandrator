"""Generation workspace services and compatibility exports."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import Database
from .generation_history_reads import GenerationHistoryReader
from .generation_scheduling import (
    canonical_regeneration_root,
    enqueue_generation_resume,
    generation_resource_keys,
    interrupt_generation,
    revoke_regeneration_batons,
)
from .generation_segment_reads import GenerationSegmentReader, updated_segment_payload
from .generation_topology import GenerationTopologyService
from .jobs import JobQueue
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    GenerationSegmentRevision,
    Job,
    OutputAssembly,
    SessionRecord,
    SpeechPlanReview,
    utcnow,
)
from .outcome_plans import OutcomePlanService as OutcomePlanService
from .outcome_plans import derive_legacy_outcome as derive_legacy_outcome
from .outcome_plans import resolve_pipeline as resolve_pipeline

# Compatibility exports; new callers import from the domain owners.
from .output_assembly_lifecycle import mark_output_assemblies_stale as mark_output_assemblies_stale
from .settings_policy import BUILTIN_DEFAULTS as BUILTIN_DEFAULTS
from .settings_policy import RUNTIME_SETTING_ALIASES as RUNTIME_SETTING_ALIASES
from .settings_policy import SECRET_KEYS as SECRET_KEYS
from .settings_policy import SETTING_SECTIONS as SETTING_SECTIONS
from .settings_policy import RevisionConflict as RevisionConflict
from .settings_policy import _merge as _merge
from .settings_policy import _secret_free as _secret_free
from .settings_policy import adapt_runtime_settings as adapt_runtime_settings
from .settings_policy import normalize_subtitle_limit_override as normalize_subtitle_limit_override
from .settings_policy import stable_hash as stable_hash
from .settings_policy import validate_output_settings as validate_output_settings
from .settings_policy import validate_stt_settings as validate_stt_settings
from .settings_policy import (
    validate_voiceover_repair_settings as validate_voiceover_repair_settings,
)
from .source_library import SourceLibraryService as SourceLibraryService

# Public compatibility re-export used by source-cleaning dispatch.
from .source_resolution import resolve_primary_source as resolve_primary_source
from .workspace_settings import WorkspaceSettingsService as WorkspaceSettingsService


def output_assembly_settings_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the settings that materially define assembled audio.

    ``generation_run_id`` selects the take snapshot and is stored separately on
    ``OutputAssembly``. It must not change the settings hash for otherwise
    identical assembly parameters.
    """

    output = dict(snapshot.get("output") or {})
    assembly_output_keys = (
        "format",
        "bitrate",
        "title",
        "artist",
        "album",
        "genre",
        "language",
        "cover_artifact_id",
    )
    return {
        "audio": dict(snapshot.get("audio") or {}),
        "output": {key: output.get(key) for key in assembly_output_keys},
    }


def output_assembly_settings_hash(snapshot: dict[str, Any]) -> str:
    return stable_hash(output_assembly_settings_snapshot(snapshot))


def expected_output_assembly_snapshot(
    session: Session,
    run: GenerationRun,
    resolved_settings_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return the material assembly settings plus the selected run's pauses."""

    snapshot = output_assembly_settings_snapshot(resolved_settings_snapshot)
    boundary_snapshot = deepcopy(run.settings_snapshot_json or {})
    if "speech_boundaries" not in boundary_snapshot:
        from .speech_boundaries import freeze_boundaries

        freeze_boundaries(session, run.plan_revision_id, boundary_snapshot)
    snapshot["speech_boundaries"] = deepcopy(
        boundary_snapshot.get("speech_boundaries") or {}
    )
    return snapshot


def _legacy_assembly_boundaries_match(
    settings_container: dict[str, Any],
    expected_boundaries: dict[str, Any],
) -> bool:
    """Validate pre-boundary-snapshot assemblies against their stored manifest."""

    if not expected_boundaries:
        return True
    takes = settings_container.get("takes")
    if not isinstance(takes, list) or not takes:
        return False
    manifest_ids = [
        str(item.get("segment_id") or "")
        for item in takes
        if isinstance(item, dict) and item.get("segment_id")
    ]
    if set(manifest_ids) != set(expected_boundaries):
        return False
    if any(
        isinstance(value, dict)
        and (
            value.get("text_hash") is not None
            or value.get("boundary_after") is not None
        )
        for value in expected_boundaries.values()
    ):
        # Pre-boundary manifests did not record enough information to prove
        # equivalence once speech markup or text-hash guards are involved.
        return False
    for index, item in enumerate(takes):
        if not isinstance(item, dict):
            return False
        segment_id = str(item.get("segment_id") or "")
        expected = expected_boundaries.get(segment_id)
        if not isinstance(expected, dict):
            return False
        # Assembly deliberately omits trailing padding after the final segment.
        if index == len(takes) - 1:
            continue
        try:
            stored_pause = int(item.get("silence_after_ms") or 0)
            expected_pause = int(expected.get("silence_after_ms") or 0)
        except (TypeError, ValueError):
            return False
        if stored_pause != expected_pause:
            return False
    return True


def output_assembly_matches(
    assembly: OutputAssembly,
    *,
    expected_snapshot: dict[str, Any],
    expected_settings_hash: str,
    expected_plan_revision_id: str,
) -> bool:
    """Use one compatibility-aware freshness rule for every export path."""

    if assembly.status != "completed" or not assembly.artifact_id:
        return False
    settings_container = dict(assembly.settings_json or {})
    if (
        str(settings_container.get("plan_revision_id") or "")
        != str(expected_plan_revision_id or "")
    ):
        return False
    stored_resolved = settings_container.get("resolved")
    stored_resolved = stored_resolved if isinstance(stored_resolved, dict) else {}
    expected_boundaries = expected_snapshot.get("speech_boundaries")
    expected_boundaries = (
        expected_boundaries if isinstance(expected_boundaries, dict) else {}
    )
    stored_boundaries = stored_resolved.get("speech_boundaries")
    if isinstance(stored_boundaries, dict):
        if stored_boundaries != expected_boundaries:
            return False
    elif not _legacy_assembly_boundaries_match(
        settings_container,
        expected_boundaries,
    ):
        return False
    return (
        assembly.settings_hash == expected_settings_hash
        or output_assembly_settings_hash(stored_resolved)
        == expected_settings_hash
    )


def find_matching_output_assembly(
    session: Session,
    *,
    session_id: str,
    run: GenerationRun,
    resolved_settings_snapshot: dict[str, Any],
    artifact_id: str | None = None,
) -> tuple[OutputAssembly | None, dict[str, Any], str]:
    """Find the current assembly that exactly matches one selected generation run."""

    expected_snapshot = expected_output_assembly_snapshot(
        session,
        run,
        resolved_settings_snapshot,
    )
    expected_settings_hash = output_assembly_settings_hash(
        resolved_settings_snapshot
    )
    filters = [
        OutputAssembly.session_id == session_id,
        OutputAssembly.generation_run_id == run.id,
        OutputAssembly.status == "completed",
        Artifact.state == "current",
    ]
    if artifact_id:
        filters.append(OutputAssembly.artifact_id == artifact_id)
    candidates = list(
        session.scalars(
            select(OutputAssembly)
            .join(Artifact, Artifact.id == OutputAssembly.artifact_id)
            .where(*filters)
            .order_by(
                OutputAssembly.created_at.desc(),
                OutputAssembly.id.desc(),
            )
        ).all()
    )
    match = next(
        (
            candidate
            for candidate in candidates
            if output_assembly_matches(
                candidate,
                expected_snapshot=expected_snapshot,
                expected_settings_hash=expected_settings_hash,
                expected_plan_revision_id=run.plan_revision_id,
            )
        ),
        None,
    )
    return match, expected_snapshot, expected_settings_hash


class GenerationService(GenerationHistoryReader):
    def __init__(
        self,
        database: Database,
        jobs: JobQueue,
        settings: WorkspaceSettingsService,
        artifacts=None,
        plan_refresher: Callable[[str, dict[str, Any]], str | None] | None = None,
    ):
        super().__init__(database)
        self.jobs = jobs
        self.settings = settings
        self.artifacts = artifacts
        self.plan_refresher = plan_refresher

    def _selected_tts_provider_binding(
        self,
        session_id: str,
        selected_segment_override: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Copy only the selected provider's current, secret-free catalogue record.

        Targeted regeneration starts from an immutable historical run.  Its
        TTS catalogue may predate a provider that is now configured in the
        session, so carry a binding for the selected provider into the new run
        without copying the current provider catalogue or any inline secret.
        Runtime hydration resolves the retained ``secret_ref`` when the worker
        executes the run.
        """
        selected_tts = selected_segment_override.get("tts")
        if not isinstance(selected_tts, dict) or not selected_tts:
            return None

        from pandrator.logic import tts_handler

        resolved, _settings_hash = self.settings.resolve(
            session_id,
            ["tts"],
            run_override={"tts": selected_tts},
        )
        current_tts = resolved.get("tts")
        if not isinstance(current_tts, dict):
            return None
        service_value = str(
            selected_tts.get("service") or selected_tts.get("tts_service") or ""
        ).strip()
        endpoint_value = str(selected_tts.get("openai_audio_endpoint") or "").strip()
        generic_service = service_value.casefold().replace("-", "_") in {
            "custom",
            "openai_compatible",
            "openai_compatible_service",
        }
        candidates = (
            [endpoint_value, service_value]
            if generic_service and endpoint_value
            else [service_value]
        )
        selected = next(
            (
                service
                for candidate in candidates
                if candidate
                for service in [tts_handler.get_service_config(current_tts, candidate)]
                if service is not None
            ),
            None,
        )
        if selected is None:
            return None
        selected_id = str(selected.get("id") or "").strip()
        if not selected_id:
            return None

        configured_records = [
            item
            for key in ("provider_configs", "service_configs")
            for item in (current_tts.get(key) or [])
            if isinstance(item, dict)
        ]
        configured = next(
            (
                item
                for item in configured_records
                if str(item.get("id") or item.get("name") or item.get("provider") or "")
                .strip()
                .casefold()
                .replace("-", "_")
                == selected_id.casefold().replace("-", "_")
            ),
            None,
        )
        # A built-in provider without an explicit current record should remain
        # eligible for its normal defaults/Manager binding.  Custom providers,
        # and explicitly configured built-ins, need the copied record.
        if not bool(selected.get("is_custom")) and configured is None:
            return None
        return _secret_free(deepcopy(configured or selected))

    def create_plan(
        self,
        session_id: str,
        *,
        source_revision_id: str | None,
        segments: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return GenerationTopologyService(self.database, self.jobs).create_plan(
            session_id, source_revision_id=source_revision_id, segments=segments, settings=settings
        )

    def list_segments(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        status: str | None = None,
        marked: bool | None = None,
        verification: str | None = None,
        generation_run_id: str | None = None,
        plan_revision_id: str | None = None,
        view: str = "full",
        fields: list[str] | None = None,
        end_ordinal: int | None = None,
        around_ordinal: int | None = None,
        source_cue_id: str | None = None,
        radius: int = 2,
        q: str | None = None,
        match_case: bool = False,
        whole_word: bool = False,
        text_field: str = "text",
        boundary_flags: bool | None = None,
    ) -> dict[str, Any]:
        return GenerationSegmentReader(self.database, self.settings).list_segments(
            session_id,
            cursor=cursor,
            limit=limit,
            status=status,
            marked=marked,
            verification=verification,
            generation_run_id=generation_run_id,
            plan_revision_id=plan_revision_id,
            view=view,
            fields=fields,
            end_ordinal=end_ordinal,
            around_ordinal=around_ordinal,
            source_cue_id=source_cue_id,
            radius=radius,
            q=q,
            match_case=match_case,
            whole_word=whole_word,
            text_field=text_field,
            boundary_flags=boundary_flags,
        )

    @staticmethod
    def _updated_segment_payload(segment: GenerationSegment) -> dict[str, Any]:
        return updated_segment_payload(segment)

    def _apply_segment_changes(
        self,
        session,
        segment: GenerationSegment,
        changes: dict[str, Any],
        *,
        completed_takes: list[AudioTake] | None = None,
        mark_assemblies: bool = True,
    ) -> dict[str, Any]:
        allowed = {
            "text",
            "optimized_text",
            "node_kind",
            "paragraph_break_after",
            "voice_id",
            "voice",
            "language",
            "silence_after_ms",
            "marked",
            "removed",
        }
        session.add(
            GenerationSegmentRevision(
                generation_segment_id=segment.id,
                revision=segment.revision,
                ordinal=segment.ordinal,
                source_segment_ids_json=list(segment.source_segment_ids_json or []),
                speech_block_provenance_json=dict(
                    segment.speech_block_provenance_json or {}
                ),
                alignment_group=segment.alignment_group,
                node_kind=segment.node_kind,
                paragraph_break_after=segment.paragraph_break_after,
                speaker=segment.speaker,
                text=segment.text,
                optimized_text=segment.optimized_text,
                speech_plan_json=dict(segment.speech_plan_json or {}),
                optimization_status=segment.optimization_status,
                optimization_reviewed=segment.optimization_reviewed,
                marked=segment.marked,
                removed=segment.removed,
                voice_id=segment.voice_id,
                voice=segment.voice,
                language=segment.language,
                silence_after_ms=segment.silence_after_ms,
            )
        )
        old_effective_speech = segment.optimized_text or segment.text
        old_speech_xml = (segment.speech_plan_json or {}).get("speech_xml")
        explicit_optimized = "optimized_text" in changes
        text_changed = (
            "text" in changes and str(changes["text"]).strip() != segment.text
        )
        optimized_changed = (
            "optimized_text" in changes
            and (str(changes["optimized_text"] or "").strip() or None)
            != segment.optimized_text
        )
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "text":
                value = str(value).strip()
                if not value:
                    raise ValueError(
                        "Generation text cannot be blank; remove the segment instead."
                    )
            if key == "optimized_text":
                value = str(value or "").strip() or None
            if key == "silence_after_ms":
                value = max(0, int(value))
            if key == "node_kind" and value not in {
                "paragraph",
                "heading",
                "chapter_marker",
                "subtitle_cue",
            }:
                raise ValueError("Unsupported generation segment type.")
            setattr(segment, key, value)
        if explicit_optimized:
            # A combined PATCH honors its explicit optimized_text instead of
            # the legacy text-change clear. Voiceover cue-only replacement
            # sends {text: newCue, optimized_text: existingSpoken} to keep a
            # TTS override while editing display text.
            if text_changed or optimized_changed:
                if text_changed:
                    segment.optimization_model = None
                segment.optimization_status = (
                    "reviewed" if segment.optimized_text else "pending"
                )
                segment.optimization_source_hash = hashlib.sha256(
                    segment.text.encode("utf-8")
                ).hexdigest()
                segment.optimization_reviewed = bool(segment.optimized_text)
                speech_plan = dict(segment.speech_plan_json or {})
                if segment.optimized_text:
                    segment.speech_plan_json = {
                        **speech_plan,
                        "version": int(speech_plan.get("version") or 1),
                        "status": "manual_override",
                        "compiled_text": segment.optimized_text,
                        "reviewed": True,
                    }
                else:
                    segment.speech_plan_json = {}
        elif text_changed:
            segment.optimized_text = None
            segment.speech_plan_json = {}
            segment.optimization_status = "stale"
            segment.optimization_source_hash = None
            segment.optimization_reviewed = False
            segment.optimization_model = None
        elif optimized_changed:
            segment.optimization_status = (
                "reviewed" if segment.optimized_text else "pending"
            )
            segment.optimization_source_hash = hashlib.sha256(
                segment.text.encode("utf-8")
            ).hexdigest()
            segment.optimization_reviewed = bool(segment.optimized_text)
            speech_plan = dict(segment.speech_plan_json or {})
            if segment.optimized_text:
                segment.speech_plan_json = {
                    **speech_plan,
                    "version": int(speech_plan.get("version") or 1),
                    "status": "manual_override",
                    "compiled_text": segment.optimized_text,
                    "reviewed": True,
                }
            else:
                segment.speech_plan_json = {}
        new_effective_speech = segment.optimized_text or segment.text
        if (
            new_effective_speech != old_effective_speech
            and isinstance(old_speech_xml, str)
            and not (explicit_optimized and segment.optimized_text is None)
        ):
            from pandrator.logic.speech_markup import (
                parse_speech_markup,
                plain_speech_markup,
            )

            from .generation_controls import get_generation_controls

            plan_revision = session.get(GenerationPlanRevision, segment.plan_revision_id)
            plan = session.get(GenerationPlan, plan_revision.plan_id) if plan_revision else None
            characters = (
                get_generation_controls(session, plan.session_id).get("characters") or []
                if plan is not None
                else []
            )
            parsed_markup = parse_speech_markup(
                old_speech_xml,
                expected_segment_id=segment.id,
                expected_text=old_effective_speech,
                characters=characters,
            )
            old_root = ET.fromstring(parsed_markup.xml)
            if list(old_root):
                raise ValueError(
                    "Speech text with speaker, delivery, or event markup cannot be "
                    "changed by a plain text edit. Clear and reapply the annotations explicitly first."
                )
            elif segment.optimized_text:
                new_root = ET.fromstring(plain_speech_markup(segment.id, new_effective_speech))
                for attribute in ("version", "boundary_after"):
                    if attribute in old_root.attrib:
                        new_root.set(attribute, old_root.attrib[attribute])
                speech_plan = dict(segment.speech_plan_json or {})
                speech_plan["speech_xml"] = parse_speech_markup(
                    ET.tostring(new_root, encoding="unicode"),
                    expected_segment_id=segment.id,
                    expected_text=new_effective_speech,
                    characters=characters,
                ).xml
                segment.speech_plan_json = speech_plan
        audio_stale = (
            new_effective_speech != old_effective_speech
            or any(key in changes for key in ("voice_id", "voice", "language"))
        )
        if audio_stale:
            segment.status = "stale"
            takes = completed_takes
            if takes is None:
                takes = list(
                    session.scalars(
                        select(AudioTake).where(
                            AudioTake.generation_segment_id == segment.id,
                            AudioTake.status == "completed",
                        )
                    ).all()
                )
            for take in takes:
                take.status = "stale"
        assembly_changed = any(
            key in changes
            for key in (
                "text",
                "node_kind",
                "paragraph_break_after",
                "voice_id",
                "voice",
                "language",
                "silence_after_ms",
                "removed",
            )
        )
        if mark_assemblies and assembly_changed:
            plan_revision = session.get(
                GenerationPlanRevision, segment.plan_revision_id
            )
            plan = (
                session.get(GenerationPlan, plan_revision.plan_id)
                if plan_revision
                else None
            )
            if plan is not None:
                mark_output_assemblies_stale(session, plan.session_id)
        segment.revision += 1
        segment.updated_at = utcnow()
        session.flush()
        return self._updated_segment_payload(segment)

    def update_segment(
        self, segment_id: str, expected_revision: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            return self.update_segment_in_session(
                session, segment_id, expected_revision, changes
            )

    def update_segment_in_session(
        self,
        session: Session,
        segment_id: str,
        expected_revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Update one segment without committing a caller-owned transaction."""
        segment = session.get(GenerationSegment, segment_id)
        if segment is None:
            raise KeyError(segment_id)
        if segment.revision != expected_revision:
            raise RevisionConflict("The generation segment changed in another client.")
        from .speech_plan_workspace import prepare_segment_edit_targets

        original_id = segment.id
        targets = prepare_segment_edit_targets(self, session, {original_id: segment}, [{"id": original_id, "changes": changes}])
        result = self._apply_segment_changes(session, targets[original_id], changes)
        if result["id"] != original_id:
            result["previous_segment_id"] = original_id
        return result

    def update_segments(
        self, session_id: str, updates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not updates:
            raise ValueError("At least one generation segment update is required.")
        segment_ids = [str(item.get("id") or "").strip() for item in updates]
        if any(not segment_id for segment_id in segment_ids):
            raise ValueError("Every generation segment update requires an ID.")
        if len(set(segment_ids)) != len(segment_ids):
            raise ValueError(
                "A generation segment can only be updated once per request."
            )
        if any(not dict(item.get("changes") or {}) for item in updates):
            raise ValueError(
                "Every generation segment update requires at least one change."
            )

        with self.database.immediate_session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            segments = {
                segment.id: segment
                for segment in session.scalars(
                    select(GenerationSegment).where(
                        GenerationSegment.id.in_(segment_ids)
                    )
                ).all()
            }
            if len(segments) != len(segment_ids):
                missing = next(
                    segment_id
                    for segment_id in segment_ids
                    if segment_id not in segments
                )
                raise KeyError(missing)

            revision_ids = {segment.plan_revision_id for segment in segments.values()}
            plan_revisions = {
                revision.id: revision
                for revision in session.scalars(
                    select(GenerationPlanRevision).where(
                        GenerationPlanRevision.id.in_(revision_ids)
                    )
                ).all()
            }
            plan_ids = {revision.plan_id for revision in plan_revisions.values()}
            plans = {
                plan.id: plan
                for plan in session.scalars(
                    select(GenerationPlan).where(GenerationPlan.id.in_(plan_ids))
                ).all()
            }
            for segment_id in segment_ids:
                segment = segments[segment_id]
                revision = plan_revisions.get(segment.plan_revision_id)
                plan = plans.get(revision.plan_id) if revision is not None else None
                if plan is None or plan.session_id != session_id:
                    raise KeyError(segment_id)

            for item in updates:
                segment = segments[str(item["id"])]
                if segment.revision != int(item["revision"]):
                    raise RevisionConflict(
                        "One or more generation segments changed in another client; no replacements were applied."
                    )

            from .speech_plan_workspace import prepare_segment_edit_targets

            segments = prepare_segment_edit_targets(self, session, segments, updates)
            segment_ids = [segment.id for segment in segments.values()]
            completed_takes_by_segment: dict[str, list[AudioTake]] = {}
            for take in session.scalars(
                select(AudioTake).where(
                    AudioTake.generation_segment_id.in_(segment_ids),
                    AudioTake.status == "completed",
                )
            ).all():
                completed_takes_by_segment.setdefault(
                    take.generation_segment_id, []
                ).append(take)

            assembly_keys = {
                "text",
                "optimized_text",
                "speech_plan",
                "node_kind",
                "paragraph_break_after",
                "voice_id",
                "voice",
                "language",
                "silence_after_ms",
                "removed",
            }
            assembly_changed = False
            results = []
            for item in updates:
                segment = segments[str(item["id"])]
                changes = dict(item["changes"])
                assembly_changed = assembly_changed or bool(
                    assembly_keys.intersection(changes)
                )
                results.append(
                    self._apply_segment_changes(
                        session,
                        segment,
                        changes,
                        completed_takes=completed_takes_by_segment.get(segment.id, []),
                        mark_assemblies=False,
                    )
                )
            for request_item, result in zip(updates, results, strict=True):
                if result["id"] != str(request_item["id"]):
                    result["previous_segment_id"] = str(request_item["id"])
            if assembly_changed:
                mark_output_assemblies_stale(session, session_id)
            session.flush()
            return {"items": results}

    _segment_copy_values = staticmethod(GenerationTopologyService._segment_copy_values)
    _provenance_source_refs = staticmethod(GenerationTopologyService._provenance_source_refs)
    _clip_local_ranges = staticmethod(GenerationTopologyService._clip_local_ranges)
    _split_provenance = GenerationTopologyService._split_provenance
    _speech_plan_ids = staticmethod(GenerationTopologyService._speech_plan_ids)
    _companion_offset = staticmethod(GenerationTopologyService._companion_offset)
    _merge_provenance = GenerationTopologyService._merge_provenance
    _clone_available_takes = staticmethod(GenerationTopologyService._clone_available_takes)
    _clone_available_takes_batch = staticmethod(GenerationTopologyService._clone_available_takes_batch)
    _recompute_alignment_groups = staticmethod(GenerationTopologyService._recompute_alignment_groups)
    _recompute_alignment_group_values = staticmethod(GenerationTopologyService._recompute_alignment_group_values)

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
        return GenerationTopologyService(self.database, self.jobs).revise_topology_in_session(
            session, session_id, expected_revision_id, operation, activate=activate
        )

    def select_take(
        self, segment_id: str, take_id: str, expected_revision: int
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            return self.select_take_in_session(
                session, segment_id, take_id, expected_revision
            )

    def select_take_in_session(
        self, session: Session, segment_id: str, take_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Select a take without committing a caller-owned transaction."""
        segment = session.get(GenerationSegment, segment_id)
        take = session.get(AudioTake, take_id)
        if segment is None or take is None or take.generation_segment_id != segment_id:
            raise KeyError(take_id)
        if segment.revision != expected_revision:
            raise RevisionConflict("The generation segment changed in another client.")
        if take.status not in {"completed", "stale"} or not take.artifact_id:
            raise ValueError("Only an available audio take can be selected.")
        session.add(
            GenerationSegmentRevision(
                generation_segment_id=segment.id,
                revision=segment.revision,
                ordinal=segment.ordinal,
                source_segment_ids_json=list(segment.source_segment_ids_json or []),
                speech_block_provenance_json=dict(
                    segment.speech_block_provenance_json or {}
                ),
                alignment_group=segment.alignment_group,
                node_kind=segment.node_kind,
                paragraph_break_after=segment.paragraph_break_after,
                speaker=segment.speaker,
                text=segment.text,
                optimized_text=segment.optimized_text,
                speech_plan_json=dict(segment.speech_plan_json or {}),
                optimization_status=segment.optimization_status,
                optimization_reviewed=segment.optimization_reviewed,
                marked=segment.marked,
                removed=segment.removed,
                voice_id=segment.voice_id,
                voice=segment.voice,
                language=segment.language,
                silence_after_ms=segment.silence_after_ms,
            )
        )
        for item in session.scalars(
            select(AudioTake).where(AudioTake.generation_segment_id == segment_id)
        ).all():
            item.is_active = item.id == take_id
            item.revision += 1
        segment.revision += 1
        segment.updated_at = utcnow()
        plan_revision = session.get(GenerationPlanRevision, segment.plan_revision_id)
        plan = (
            session.get(GenerationPlan, plan_revision.plan_id)
            if plan_revision
            else None
        )
        if plan is not None:
            mark_output_assemblies_stale(session, plan.session_id)
        return {
            "id": segment.id,
            "active_take_id": take.id,
            "revision": segment.revision,
        }

    def prepare_start(
        self,
        session_id: str,
        *,
        run_override: dict[str, Any] | None = None,
        selected_segment_override: dict[str, Any] | None = None,
        segment_ids: list[str] | None = None,
        generation_run_id: str | None = None,
        settings_source_run_id: str | None = None,
        operation: str = "generate",
        speech_plan_revision_id: str | None = None,
        stale_only: bool = False,
        missing_only: bool = False,
        expected_selection_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve delivery settings and bind one immutable plan before queuing.

        A selected revision is never refreshed. Legacy callers may still refresh
        an untouched automatic plan, but reviewed topology is always preserved.
        The final write transaction rechecks the captured revision.
        """
        requested_segment_ids = list(dict.fromkeys(str(value) for value in (segment_ids or []) if str(value)))
        explicit_segment_ids = list(requested_segment_ids)
        run_override = dict(run_override or {})
        selected_segment_override = _secret_free(deepcopy(selected_segment_override or {}))
        settings_source_run_id = str(settings_source_run_id) if settings_source_run_id else None
        if stale_only and missing_only:
            raise ValueError("Stale-only and missing-only generation are mutually exclusive.")
        automatic = stale_only or missing_only
        if automatic and (requested_segment_ids or generation_run_id or operation != "generate" or selected_segment_override):
            label = "Missing-only" if missing_only else "Stale-only"
            raise ValueError(f"{label} generation cannot be combined with selected segments, an older run, or alternate segment settings.")
        if expected_selection_hash and (requested_segment_ids or generation_run_id or operation != "generate" or selected_segment_override):
            raise ValueError("A selection hash cannot be combined with selected segments, an older run, alternate segment settings, or a non-generate operation.")
        if selected_segment_override and not requested_segment_ids:
            raise ValueError("An alternate segment setting set requires one or more selected segments.")
        if selected_segment_override:
            selected_tts = selected_segment_override.get("tts")
            if isinstance(selected_tts, dict):
                selected_tts.pop("provider_configs", None)
                selected_tts.pop("service_configs", None)
                binding = self._selected_tts_provider_binding(session_id, selected_segment_override)
                if binding is not None:
                    selected_tts["provider_configs"] = [binding]

        prepared_source = {
            "run_override": run_override,
            "selected_segment_override": selected_segment_override,
            "settings_source_run_id": settings_source_run_id,
            "settings_source_snapshot": None,
            "settings_source_identity": None,
        }
        settings_source_run = None
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            current_id = plan.active_revision_id if plan else None
            current = session.get(GenerationPlanRevision, current_id) if current_id else None
            if settings_source_run_id:
                settings_source_run = session.get(GenerationRun, settings_source_run_id)
                if (
                    settings_source_run is None
                    or settings_source_run.session_id != session_id
                ):
                    raise ValueError("The selected settings source generation run is not available in this session.")
                if settings_source_run.operation == "rvc":
                    raise ValueError("An RVC run cannot be used as a generation settings source.")
                source_revision = session.get(
                    GenerationPlanRevision, settings_source_run.plan_revision_id
                )
                if plan is None or source_revision is None or source_revision.plan_id != plan.id:
                    raise ValueError("The selected settings source generation run does not match this session's speech plan.")
                source_snapshot = deepcopy(settings_source_run.settings_snapshot_json or {})
                if not isinstance(source_snapshot, dict):
                    raise ValueError("The selected settings source generation run has an invalid settings snapshot.")
                prepared_source["settings_source_snapshot"] = source_snapshot
                prepared_source["settings_source_identity"] = {
                    "id": settings_source_run.id,
                    "session_id": settings_source_run.session_id,
                    "plan_revision_id": settings_source_run.plan_revision_id,
                    "operation": settings_source_run.operation,
                    "sequence_number": settings_source_run.sequence_number,
                    "settings_hash": settings_source_run.settings_hash,
                    "settings_snapshot_hash": stable_hash(source_snapshot),
                }
            reviewed = bool(current and (session.get(SpeechPlanReview, current.id) or current.operation_json or (current.settings_json or {}).get("_prepared_for_review") or session.scalar(
                select(GenerationSegment.id).where(GenerationSegment.plan_revision_id == current_id, GenerationSegment.revision > 1).limit(1)
            )))
            if speech_plan_revision_id and speech_plan_revision_id != current_id:
                historical_revision = session.get(
                    GenerationPlanRevision, speech_plan_revision_id
                )
                if (
                    settings_source_run is None
                    or settings_source_run.plan_revision_id != speech_plan_revision_id
                    or explicit_segment_ids
                    or historical_revision is None
                    or plan is None
                    or historical_revision.plan_id != plan.id
                ):
                    raise RevisionConflict("The selected speech plan changed. Refresh the revision list before generating.")
        if settings_source_run_id:
            inherited_snapshot = self._start_snapshot(prepared_source, None)
            resolved_for_new = (inherited_snapshot, stable_hash(inherited_snapshot))
        else:
            resolved_for_new = self.settings.resolve(session_id, run_override=run_override)
        if operation == "generate" and not requested_segment_ids and not speech_plan_revision_id and not automatic and not expected_selection_hash and not reviewed and self.plan_refresher is not None:
            self.plan_refresher(session_id, resolved_for_new[0])
            resolved_for_new = (resolved_for_new[0], stable_hash(resolved_for_new[0]))
        reusable_take_ids: dict[str, str] = {}
        preserved_take_ids: dict[str, str] = {}
        selection_hash: str | None = None
        selection_mode = "all"
        selection_state: dict[str, Any] | None = None
        selection_settings_hash: str | None = None
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            bound_revision_id = plan.active_revision_id if plan else None
            if speech_plan_revision_id and speech_plan_revision_id != bound_revision_id:
                source_run = session.get(GenerationRun, settings_source_run_id) if settings_source_run_id else None
                historical_revision = session.get(
                    GenerationPlanRevision, speech_plan_revision_id
                )
                if (
                    source_run is None
                    or source_run.session_id != session_id
                    or source_run.operation == "rvc"
                    or source_run.plan_revision_id != speech_plan_revision_id
                    or requested_segment_ids
                    or historical_revision is None
                    or plan is None
                    or historical_revision.plan_id != plan.id
                ):
                    raise RevisionConflict("The selected speech plan changed while generation was being prepared.")
                bound_revision_id = speech_plan_revision_id
            if automatic or expected_selection_hash:
                from .generation_selection import compute_selection, resolve_mode

                selection_mode = resolve_mode(stale_only=stale_only, missing_only=missing_only)
                selection = compute_selection(session, bound_revision_id, resolved_for_new[0], selection_mode)
                selection_hash = selection["selection_hash"]
                selection_state = selection["row_state"]
                selection_settings_hash = stable_hash(resolved_for_new[0])
                if expected_selection_hash and expected_selection_hash != selection_hash:
                    raise RevisionConflict("The speech plan, audio selection, or settings changed. Refresh the preview before generating.")
                if automatic:
                    if not selection["generate_segment_ids"]:
                        raise ValueError(selection["blocked_reason"] or "There are no speech blocks to generate.")
                    requested_segment_ids.extend(selection["generate_segment_ids"])
                    if selection_mode == "stale":
                        reusable_take_ids = dict(selection["preserve_take_ids"])
                    else:
                        preserved_take_ids = dict(selection["preserve_take_ids"])
                elif selection["total_count"] == 0:
                    raise ValueError(selection["blocked_reason"] or "There are no speech blocks to generate.")
        prepared = {
            "requested_segment_ids": requested_segment_ids,
            "explicit_segment_ids": explicit_segment_ids,
            "run_override": run_override,
            "selected_segment_override": selected_segment_override,
            "generation_run_id": generation_run_id,
            "settings_source_run_id": settings_source_run_id,
            "settings_source_snapshot": prepared_source["settings_source_snapshot"],
            "settings_source_identity": prepared_source["settings_source_identity"],
            "operation": operation,
            "resolved_for_new": resolved_for_new,
            "speech_plan_revision_id": bound_revision_id,
            "explicit_speech_plan_revision_id": speech_plan_revision_id,
            "stale_only": stale_only,
            "missing_only": missing_only,
            "expected_selection_hash": expected_selection_hash,
            "selection_mode": selection_mode,
            "selection_hash": selection_hash,
            "selection_state": selection_state,
            "selection_settings_hash": selection_settings_hash,
            "reusable_take_ids": reusable_take_ids,
            "preserved_take_ids": preserved_take_ids,
        }
        from .generation_audio_identity import plan_audio_identities
        from .generation_start_preparation import snapshot_guard
        from .speech_plan_workspace import freeze_speech_snapshot

        with self.database.session() as session:
            revision_id, source_run = self._start_source(session, session_id, prepared)
            snapshot = self._start_snapshot(prepared, source_run)
            prepared["snapshot_source_run_id"] = source_run.id if source_run else None
            prepared["snapshot_input_hash"] = stable_hash(snapshot)
            prepared["snapshot_guard"] = snapshot_guard(session, session_id, revision_id)
            snapshot["speech_plan_revision_id"] = revision_id
            freeze_speech_snapshot(
                session,
                revision_id,
                snapshot,
                explicit=bool(speech_plan_revision_id),
                segment_ids=requested_segment_ids or None,
            )
            snapshot["generation_audio_identities"] = plan_audio_identities(session, revision_id, snapshot)
            prepared["frozen_snapshot"] = snapshot
        return prepared

    def start(
        self,
        session_id: str,
        *,
        run_override: dict[str, Any] | None = None,
        selected_segment_override: dict[str, Any] | None = None,
        segment_ids: list[str] | None = None,
        generation_run_id: str | None = None,
        settings_source_run_id: str | None = None,
        operation: str = "generate",
        speech_plan_revision_id: str | None = None,
        stale_only: bool = False,
        missing_only: bool = False,
        expected_selection_hash: str | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_start(
            session_id,
            run_override=run_override,
            selected_segment_override=selected_segment_override,
            segment_ids=segment_ids,
            generation_run_id=generation_run_id,
            settings_source_run_id=settings_source_run_id,
            operation=operation,
            speech_plan_revision_id=speech_plan_revision_id,
            stale_only=stale_only,
            missing_only=missing_only,
            expected_selection_hash=expected_selection_hash,
        )
        with self.database.immediate_session() as session:
            return self.start_in_session(session, session_id, prepared=prepared)

    def preview_selection(
        self,
        session_id: str,
        *,
        run_override: dict[str, Any] | None = None,
        selected_segment_override: dict[str, Any] | None = None,
        segment_ids: list[str] | None = None,
        generation_run_id: str | None = None,
        settings_source_run_id: str | None = None,
        operation: str = "generate",
        speech_plan_revision_id: str | None = None,
        stale_only: bool = False,
        missing_only: bool = False,
    ) -> dict[str, Any]:
        """Read-only preflight for automatic generation modes.

        Never refreshes the speech plan, never writes, never enqueues. Only
        automatic ``generate`` selections are supported; targeted, regenerate,
        and RVC combinations are rejected like in ``prepare_start``.
        """
        from .generation_selection import compute_selection, resolve_mode

        run_override = dict(run_override or {})
        requested = [str(value) for value in (segment_ids or []) if str(value)]
        selected_override = dict(selected_segment_override or {})
        settings_source_run_id = str(settings_source_run_id) if settings_source_run_id else None
        if stale_only and missing_only:
            raise ValueError("Stale-only and missing-only generation are mutually exclusive.")
        if requested or generation_run_id or operation != "generate" or selected_override:
            raise ValueError("Selection preview supports only automatic generate modes, not selected segments, an older run, or alternate segment settings.")
        mode = resolve_mode(stale_only=stale_only, missing_only=missing_only)
        prepared_source = {
            "run_override": run_override,
            "selected_segment_override": {},
            "settings_source_run_id": settings_source_run_id,
            "settings_source_snapshot": None,
        }
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            bound_revision_id = plan.active_revision_id if plan else None
            if bound_revision_id is None:
                raise ValueError("Create generation segments before starting audio generation.")
            assert plan is not None
            settings_source_run = None
            if settings_source_run_id:
                settings_source_run = session.get(GenerationRun, settings_source_run_id)
                if settings_source_run is None or settings_source_run.session_id != session_id:
                    raise ValueError("The selected settings source generation run is not available in this session.")
                if settings_source_run.operation == "rvc":
                    raise ValueError("An RVC run cannot be used as a generation settings source.")
                source_revision = session.get(
                    GenerationPlanRevision, settings_source_run.plan_revision_id
                )
                if source_revision is None or source_revision.plan_id != plan.id:
                    raise ValueError("The selected settings source generation run does not match this session's speech plan.")
                source_snapshot = deepcopy(settings_source_run.settings_snapshot_json or {})
                if not isinstance(source_snapshot, dict):
                    raise ValueError("The selected settings source generation run has an invalid settings snapshot.")
                prepared_source["settings_source_snapshot"] = source_snapshot
            if speech_plan_revision_id and speech_plan_revision_id != bound_revision_id:
                historical_revision = session.get(
                    GenerationPlanRevision, speech_plan_revision_id
                )
                if (
                    settings_source_run is None
                    or settings_source_run.plan_revision_id != speech_plan_revision_id
                    or historical_revision is None
                    or historical_revision.plan_id != plan.id
                ):
                    raise RevisionConflict("The selected speech plan changed. Refresh the revision list before generating.")
                bound_revision_id = speech_plan_revision_id
            if settings_source_run_id:
                resolved = self._start_snapshot(prepared_source, None)
            else:
                resolved, _ = self.settings.resolve(session_id, run_override=run_override)
            selection = compute_selection(session, bound_revision_id, resolved, mode)
        public = {
            key: selection[key]
            for key in (
                "mode",
                "speech_plan_revision_id",
                "selection_hash",
                "total_count",
                "generate_count",
                "preserve_count",
                "replace_count",
                "missing_count",
                "reasons",
                "first_generate_ordinal",
                "settings_summary",
            )
        }
        if selection["blocked_reason"] is not None:
            public["blocked_reason"] = selection["blocked_reason"]
        return public

    def _start_source(
        self, session: Session, session_id: str, prepared: dict[str, Any]
    ) -> tuple[str, GenerationRun | None]:
        """Resolve the same immutable source in preparation and final commit."""
        requested_segment_ids = list(prepared["requested_segment_ids"])
        generation_run_id = prepared["generation_run_id"]
        operation = prepared["operation"]
        settings_source_run_id = prepared.get("settings_source_run_id")
        plan = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        if plan is None or not plan.active_revision_id:
            raise ValueError(
                "Create generation segments before starting audio generation."
            )
        settings_source_run = None
        if settings_source_run_id:
            settings_source_run = session.get(GenerationRun, settings_source_run_id)
            expected_identity = prepared.get("settings_source_identity")
            if settings_source_run is None:
                if expected_identity is not None:
                    raise RevisionConflict("The settings source generation run changed while generation was being prepared.")
                raise ValueError("The selected settings source generation run is not available in this session.")
            if settings_source_run.session_id != session_id or settings_source_run.operation == "rvc":
                if expected_identity is not None:
                    raise RevisionConflict("The settings source generation run changed while generation was being prepared.")
                raise ValueError("The selected settings source generation run is not available for synthesis in this session.")
            source_revision = session.get(
                GenerationPlanRevision, settings_source_run.plan_revision_id
            )
            source_snapshot = deepcopy(settings_source_run.settings_snapshot_json or {})
            if (
                source_revision is None
                or source_revision.plan_id != plan.id
                or not isinstance(source_snapshot, dict)
            ):
                if expected_identity is not None:
                    raise RevisionConflict("The settings source generation run changed while generation was being prepared.")
                raise ValueError("The selected settings source generation run does not match this session's speech plan.")
            source_identity = {
                "id": settings_source_run.id,
                "session_id": settings_source_run.session_id,
                "plan_revision_id": settings_source_run.plan_revision_id,
                "operation": settings_source_run.operation,
                "sequence_number": settings_source_run.sequence_number,
                "settings_hash": settings_source_run.settings_hash,
                "settings_snapshot_hash": stable_hash(source_snapshot),
            }
            if expected_identity is not None and source_identity != expected_identity:
                raise RevisionConflict("The settings source generation run changed while generation was being prepared.")
            prepared["settings_source_identity"] = source_identity
            prepared["settings_source_snapshot"] = source_snapshot
        plan_revision_id = plan.active_revision_id
        bound_revision_id = prepared.get("speech_plan_revision_id")
        if bound_revision_id and bound_revision_id != plan.active_revision_id:
            explicit_revision_id = prepared.get("explicit_speech_plan_revision_id")
            explicitly_selected_ids = prepared.get(
                "explicit_segment_ids", requested_segment_ids
            )
            if (
                explicit_revision_id != bound_revision_id
                or settings_source_run is None
                or settings_source_run.plan_revision_id != bound_revision_id
                or explicitly_selected_ids
            ):
                raise RevisionConflict("The speech plan changed before generation could be queued.")
            plan_revision_id = bound_revision_id
        if requested_segment_ids:
            unique_requested_ids = set(requested_segment_ids)
            requested_rows = list(
                session.execute(
                    select(
                        GenerationSegment.id,
                        GenerationSegment.plan_revision_id,
                    ).where(GenerationSegment.id.in_(requested_segment_ids))
                ).all()
            )
            requested_revisions = {value for _id, value in requested_rows}
            if (
                len(requested_rows) != len(unique_requested_ids)
                or len(requested_revisions) != 1
            ):
                raise ValueError(
                    "Selected generation segments must belong to one available plan revision."
                )
            requested_revision_id = next(iter(requested_revisions))
            requested_revision = session.get(
                GenerationPlanRevision, requested_revision_id
            )
            if requested_revision is None or requested_revision.plan_id != plan.id:
                raise ValueError(
                    "Selected generation segments do not belong to this session."
                )
            if prepared.get("explicit_speech_plan_revision_id") and requested_revision_id != bound_revision_id:
                raise RevisionConflict("Selected segments do not belong to the selected speech-plan revision.")
            plan_revision_id = requested_revision_id
        source_run = None
        if requested_segment_ids and generation_run_id:
            source_run = session.get(GenerationRun, generation_run_id)
            if (
                source_run is None
                or source_run.session_id != session_id
                or source_run.plan_revision_id != plan_revision_id
                or source_run.operation == "rvc"
            ):
                raise ValueError(
                    "The selected source generation run does not match these segments."
                )
            if source_run.output_generation_run_id:
                output_run = session.get(
                    GenerationRun, source_run.output_generation_run_id
                )
                if (
                    output_run is None
                    or output_run.session_id != session_id
                    or output_run.plan_revision_id != plan_revision_id
                    or output_run.operation == "rvc"
                ):
                    raise ValueError(
                        "The selected output generation run does not match this session and plan."
                    )
                source_run = output_run
        elif requested_segment_ids and operation != "rvc" and not prepared.get("stale_only") and not prepared.get("missing_only"):
            source_run = session.scalar(
                select(GenerationRun)
                .where(
                    GenerationRun.session_id == session_id,
                    GenerationRun.plan_revision_id == plan_revision_id,
                    GenerationRun.operation != "rvc",
                    GenerationRun.output_generation_run_id.is_(None),
                )
                .order_by(
                    GenerationRun.sequence_number.desc(),
                    GenerationRun.created_at.desc(),
                )
            )
            if source_run is not None:
                source_run = canonical_regeneration_root(
                    session,
                    source_run,
                    session_id,
                    plan_revision_id,
                )
        return plan_revision_id, source_run

    @staticmethod
    def _start_snapshot(
        prepared: dict[str, Any], source_run: GenerationRun | None
    ) -> dict[str, Any]:
        run_override = dict(prepared["run_override"])
        selected_segment_override = deepcopy(prepared["selected_segment_override"])
        if prepared.get("settings_source_run_id"):
            snapshot = deepcopy(prepared.get("settings_source_snapshot") or {})
            for key in (
                "selected_segment_override",
                "speech_plan_revision_id",
                "speech_plan_frozen",
                "speech_plan_signature",
                "speech_boundaries",
                "generation_audio_identities",
                "generation_selection_guards",
                "generation_request_segment_ids",
                "generation_selection_hash",
                "generation_selection_mode",
                "selection_hash",
                "selection_mode",
                "selection_settings_hash",
                "expected_selection_hash",
                "interrupted_generation_run_id",
                "stale_only",
                "missing_only",
            ):
                snapshot.pop(key, None)
            snapshot = _merge(snapshot, run_override)
        elif source_run is not None:
            source_snapshot = dict(source_run.settings_snapshot_json or {})
            # A previous alternate take is a useful source for ordinary
            # settings, but its *selected-only* precedence must not leak
            # into a later regeneration unless it is requested again.
            source_snapshot.pop("selected_segment_override", None)
            snapshot = _merge(source_snapshot, run_override)
        else:
            resolved_for_new = prepared["resolved_for_new"]
            snapshot, _ = resolved_for_new
            snapshot = deepcopy(snapshot)
        if selected_segment_override:
            from .generation_cast_runtime import filter_segment_tts_override

            selected_tts = selected_segment_override.get("tts")
            if isinstance(selected_tts, dict):
                selected_segment_override["tts"] = filter_segment_tts_override(
                    snapshot.get("tts"), selected_tts
                )
            snapshot = _merge(snapshot, selected_segment_override)
            snapshot["selected_segment_override"] = deepcopy(
                selected_segment_override
            )
        return snapshot

    def start_in_session(
        self, session: Session, session_id: str, *, prepared: dict[str, Any]
    ) -> dict[str, Any]:
        """Create the durable run and its queued job in one caller transaction."""
        requested_segment_ids = list(prepared["requested_segment_ids"])
        generation_run_id = prepared["generation_run_id"]
        operation = prepared["operation"]
        plan_revision_id, source_run = self._start_source(session, session_id, prepared)
        snapshot = self._start_snapshot(prepared, source_run)
        from .generation_start_preparation import snapshot_guard

        if ((source_run.id if source_run else None) != prepared["snapshot_source_run_id"]
                or stable_hash(snapshot) != prepared["snapshot_input_hash"]
                or snapshot_guard(session, session_id, plan_revision_id) != prepared["snapshot_guard"]):
            raise RevisionConflict("Speech, cast, or voice references changed while generation was being prepared. Refresh and try again.")
        if prepared.get("selection_state") is not None:
            # Lightweight write-stage guard: recheck captured row/take/run
            # state and fresh resolved settings without recompiling per-row
            # audio identities under the lock. Identity-input drift
            # (voices, cast, performance) is covered by the snapshot guard.
            from .generation_selection import collect_row_state

            if prepared.get("settings_source_run_id"):
                fresh_resolved = self._start_snapshot(prepared, source_run)
            else:
                fresh_resolved, _ = self.settings.resolve(session_id, run_override=dict(prepared["run_override"]))
            if stable_hash(fresh_resolved) != prepared["selection_settings_hash"]:
                raise RevisionConflict("The speech plan, audio selection, or settings changed while generation was being prepared. Refresh and try again.")
            if collect_row_state(session, plan_revision_id) != prepared["selection_state"]:
                raise RevisionConflict("The speech plan, audio selection, or settings changed while generation was being prepared. Refresh and try again.")
            if prepared.get("expected_selection_hash") and prepared["expected_selection_hash"] != prepared["selection_hash"]:
                raise RevisionConflict("The speech plan, audio selection, or settings changed since the preview. Refresh the preview before generating.")
        snapshot = deepcopy(prepared["frozen_snapshot"])
        auto_resume_source_id = None
        scheduling_run = source_run
        if requested_segment_ids and operation == "regenerate" and not generation_run_id:
            from .generation_edit_audio import running_edit_ancestor

            scheduling_run = running_edit_ancestor(session, plan_revision_id) or source_run
        if scheduling_run is not None:
            auto_resume_source_id = interrupt_generation(session, scheduling_run, operation)
        snapshot.pop("interrupted_generation_run_id", None)
        if auto_resume_source_id and (source_run is None or source_run.id != auto_resume_source_id):
            snapshot["interrupted_generation_run_id"] = auto_resume_source_id
        snapshot["speech_plan_revision_id"] = plan_revision_id
        from .generation_audio_identity import take_reuse_reason
        from .generation_edit_audio import (
            capture_selection_guards,
            inherit_edit_copy_audio,
        )

        inherit_edit_copy_audio(session, plan_revision_id)
        snapshot["generation_selection_guards"] = capture_selection_guards(session, plan_revision_id)
        snapshot["generation_request_segment_ids"] = list(requested_segment_ids)
        if prepared.get("stale_only"):
            snapshot["stale_only"] = True
        if prepared.get("missing_only"):
            snapshot["missing_only"] = True
        settings_hash = stable_hash(snapshot)
        sequence_number = (
            int(
                session.scalar(
                    select(func.max(GenerationRun.sequence_number)).where(
                        GenerationRun.session_id == session_id
                    )
                )
                or 0
            )
            + 1
        )
        run = GenerationRun(
            session_id=session_id,
            plan_revision_id=plan_revision_id,
            source_generation_run_id=source_run.id if source_run else None,
            output_generation_run_id=(
                source_run.id
                if operation == "regenerate"
                and requested_segment_ids
                and source_run is not None
                else None
            ),
            sequence_number=sequence_number,
            operation=operation,
            status="queued",
            resume_source_on_completion=bool(auto_resume_source_id),
            settings_snapshot_json=snapshot,
            settings_hash=settings_hash,
        )
        session.add(run)
        session.flush()
        preserved_items = dict(prepared.get("reusable_take_ids") or {})
        preserved_items.update(dict(prepared.get("preserved_take_ids") or {}))
        missing_only = bool(prepared.get("missing_only"))
        for segment_id, take_id in preserved_items.items():
            source_take = session.get(AudioTake, take_id)
            segment = session.get(GenerationSegment, segment_id)
            artifact = session.get(Artifact, source_take.artifact_id) if source_take and source_take.artifact_id else None
            if missing_only:
                if (source_take is None or segment is None or artifact is None
                        or segment.plan_revision_id != plan_revision_id or segment.status != "completed"
                        or source_take.generation_segment_id != segment_id
                        or source_take.status != "completed" or not source_take.is_active
                        or artifact.state == "deleted"):
                    raise RevisionConflict("A preserved take changed while missing-only generation was being prepared.")
            elif (source_take is None or segment is None or artifact is None
                    or segment.plan_revision_id != plan_revision_id or segment.status != "completed"
                    or source_take.generation_segment_id != segment_id
                    or source_take.status != "completed" or not source_take.is_active
                    or take_reuse_reason(segment, source_take, artifact, snapshot["generation_audio_identities"][segment_id]) != "reusable"):
                raise RevisionConflict("A reusable take changed while stale-only generation was being prepared.")
            source_take.is_active = False
            session.add(AudioTake(
                generation_segment_id=segment_id,
                generation_run_id=run.id,
                artifact_id=source_take.artifact_id,
                parent_take_id=source_take.id,
                kind=source_take.kind,
                status="completed",
                settings_hash=source_take.settings_hash,
                duration_ms=source_take.duration_ms,
                is_active=True,
            ))
        session.flush()
        job_payload = {
            "generation_run_id": run.id,
            "speech_plan_revision_id": plan_revision_id,
            "segment_ids": requested_segment_ids,
            "operation": operation,
        }
        if auto_resume_source_id:
            job_payload["auto_resume_source_generation_run_id"] = auto_resume_source_id
        job = self.jobs.enqueue_in_session(
            session,
            "generation.run",
            job_payload,
            session_id=session_id,
            resource_keys=generation_resource_keys(session_id, snapshot),
        )
        run.job_id = job.id
        run.updated_at = utcnow()
        session.flush()
        result = self._run_payload(session, run)
        # Kept for compatibility with clients that used the old response flag.
        result["reused_run"] = False
        return result

    def request_pause(self, run_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status not in {"queued", "running", "pausing", "paused"}:
                raise ValueError(f"Run cannot be paused from {run.status}.")
            run.pause_requested = True
            if run.status != "paused":
                run.status = "pausing"
            run.updated_at = utcnow()
            # An explicit pause wins over a temporary pause requested by a
            # targeted regeneration. Remove the child's durable resume marker
            # so it cannot revive the source run behind the user's back.
            revoke_regeneration_batons(session, run.id)
            return {"id": run.id, "job_id": run.job_id, "status": run.status}

    def resume(self, run_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status != "paused":
                raise ValueError("Only a paused generation run can be resumed.")
            job = enqueue_generation_resume(session, self.jobs, run)
        return {"id": run_id, "job_id": job.id, "status": "queued"}

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            return self.cancel_in_session(session, run_id)

    def cancel_in_session(self, session: Session, run_id: str) -> dict[str, Any]:
        """Request cancellation inside a caller's existing write transaction."""
        run = session.get(GenerationRun, run_id)
        if run is None:
            raise KeyError(run_id)
        run.cancel_requested = True
        run.status = "cancel_requested"
        run.updated_at = utcnow()
        job_id = run.job_id
        if job_id:
            try:
                self.jobs.request_cancel_in_session(session, job_id)
            except KeyError:
                run.status = "canceled"
                run.cancel_requested = False
                run.updated_at = utcnow()
        else:
            run.status = "canceled"
            run.cancel_requested = False
            run.updated_at = utcnow()
        return {"id": run.id, "job_id": job_id, "status": run.status}

    def delete_run(self, run_id: str) -> dict[str, Any]:
        paths_to_remove: list[Path] = []
        with self.database.immediate_session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status in {"queued", "running", "pausing", "cancel_requested"}:
                raise ValueError("Stop or cancel this run before deleting it.")
            active_job_statuses = {"queued", "running", "cancel_requested"}
            current_job = session.get(Job, run.job_id) if run.job_id else None
            if current_job is not None and current_job.status in active_job_statuses:
                raise ValueError("Wait for this run's job to stop before deleting it.")
            grouped_children = []
            if run.output_generation_run_id is None:
                history = self._run_payload(session, run) or {}
                repair_ids = {
                    version["generation_run_id"]
                    for version in (history.get("timing_repair") or {}).get("versions", [])
                }
                output_ids = {run.id, *repair_ids}
                grouped_children = list(
                    session.scalars(
                        select(GenerationRun).where(
                            GenerationRun.session_id == run.session_id,
                            (GenerationRun.id.in_(repair_ids))
                            | (GenerationRun.output_generation_run_id.in_(output_ids)),
                        )
                    ).all()
                )
                active_statuses = {
                    "queued",
                    "running",
                    "pausing",
                    "cancel_requested",
                }
                active_child = next(
                    (
                        child
                        for child in grouped_children
                        if child.status in active_statuses
                        or (
                            child.job_id
                            and (child_job := session.get(Job, child.job_id)) is not None
                            and child_job.status in active_job_statuses
                        )
                    ),
                    None,
                )
                if active_child is not None:
                    raise ValueError(
                        "Stop or cancel grouped regeneration before deleting its output run."
                    )
            run_ids = [run.id, *(child.id for child in grouped_children)]
            takes = list(
                session.scalars(
                    select(AudioTake).where(AudioTake.generation_run_id.in_(run_ids))
                ).all()
            )
            assemblies = list(
                session.scalars(
                    select(OutputAssembly).where(
                        OutputAssembly.generation_run_id.in_(run_ids)
                    )
                ).all()
            )
            affected_segment_ids = {take.generation_segment_id for take in takes}
            artifact_ids = {take.artifact_id for take in takes if take.artifact_id}
            artifact_ids.update(
                item.artifact_id for item in assemblies if item.artifact_id
            )
            # Repair versions clone takes and share their audio artifacts. A
            # later independent run may still use one of those same files.
            if artifact_ids:
                shared_take_artifacts = session.scalars(
                    select(AudioTake.artifact_id).where(
                        AudioTake.artifact_id.in_(artifact_ids),
                        (AudioTake.generation_run_id.is_(None))
                        | (AudioTake.generation_run_id.not_in(run_ids)),
                    )
                ).all()
                shared_assembly_artifacts = session.scalars(
                    select(OutputAssembly.artifact_id).where(
                        OutputAssembly.artifact_id.in_(artifact_ids),
                        (OutputAssembly.generation_run_id.is_(None))
                        | (OutputAssembly.generation_run_id.not_in(run_ids)),
                    )
                ).all()
                artifact_ids.difference_update(shared_take_artifacts)
                artifact_ids.difference_update(shared_assembly_artifacts)
            artifacts = (
                list(
                    session.scalars(
                        select(Artifact).where(Artifact.id.in_(artifact_ids))
                    ).all()
                )
                if artifact_ids
                else []
            )
            if self.artifacts is not None:
                for artifact in artifacts:
                    try:
                        paths_to_remove.append(
                            self.artifacts.paths.managed_path(artifact.relative_path)
                        )
                    except ValueError:
                        pass
            from .artifacts import ArtifactService

            for artifact in artifacts:
                ArtifactService._mark_descendants_stale(session, artifact.id)
            for assembly in assemblies:
                session.delete(assembly)
            for take in takes:
                session.delete(take)
            session.flush()
            for segment_id in affected_segment_ids:
                remaining = session.scalar(
                    select(AudioTake)
                    .where(
                        AudioTake.generation_segment_id == segment_id,
                        AudioTake.artifact_id.is_not(None),
                    )
                    .order_by(AudioTake.created_at.desc())
                )
                for candidate in session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_segment_id == segment_id
                    )
                ).all():
                    candidate.is_active = (
                        remaining is not None and candidate.id == remaining.id
                    )
            for artifact in artifacts:
                session.delete(artifact)
            child_job_ids = {
                child.job_id for child in grouped_children
                if child.job_id and child.job_id != run.job_id
            }
            for job_id in child_job_ids:
                outside_group = session.scalar(
                    select(GenerationRun.id).where(
                        GenerationRun.job_id == job_id,
                        GenerationRun.id.not_in(run_ids),
                    ).limit(1)
                )
                child_job = session.get(Job, job_id)
                if outside_group is None and child_job is not None:
                    session.delete(child_job)
            # Flush output-owned tasks first so their owners' cascades do not
            # delete the same rows again in an unordered ORM delete batch.
            for child in grouped_children:
                if child.output_generation_run_id:
                    session.delete(child)
            session.flush()
            # Repair lineage uses source links, not the output-owner cascade.
            for child in grouped_children:
                if not child.output_generation_run_id:
                    session.delete(child)
            session.flush()
            session.delete(run)
        for path in paths_to_remove:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"id": run_id, "status": "deleted"}

    def prepare_assembly(
        self,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        run_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot, _settings_hash = self.settings.resolve(
            session_id,
            sections=["audio", "output"],
            run_override=run_override,
        )
        snapshot = output_assembly_settings_snapshot(snapshot)
        settings_hash = stable_hash(snapshot)
        output_format = str(
            (snapshot.get("output") or {}).get("format") or "wav"
        ).lower()
        from .audio_assembly import OUTPUT_FORMATS

        if output_format not in OUTPUT_FORMATS:
            raise ValueError(f"Unsupported audio output format: {output_format}")
        return {"snapshot": snapshot, "settings_hash": settings_hash}

    def create_assembly(
        self,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        run_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_assembly(session_id, run_override=run_override)
        with self.database.session() as session:
            return self.create_assembly_in_session(
                session,
                session_id,
                generation_run_id=generation_run_id,
                prepared=prepared,
            )

    def create_assembly_in_session(
        self,
        session: Session,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        """Create the durable output assembly and queued job atomically."""
        snapshot = deepcopy(prepared["snapshot"])
        settings_hash = str(prepared["settings_hash"])
        if session.get(SessionRecord, session_id) is None:
            raise KeyError(session_id)
        run = (
            session.get(GenerationRun, generation_run_id) if generation_run_id else None
        )
        if generation_run_id and (run is None or run.session_id != session_id):
            raise KeyError(generation_run_id)
        if run is not None and run.status != "completed":
            raise ValueError("Only a completed generation run can be assembled.")
        plan = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        plan_revision_id = (
            run.plan_revision_id if run else plan.active_revision_id if plan else None
        )
        if not plan_revision_id:
            raise ValueError("Create generation segments before assembling audio.")
        from .speech_boundaries import freeze_boundaries
        boundary_snapshot = deepcopy(run.settings_snapshot_json or {}) if run else {"audio": snapshot.get("audio") or {}}
        if not run:
            from .models import PerformancePlan
            from .performance_plans import freeze_performance_snapshot
            tts_settings = self.settings.get_in_session(session, session_id, "tts")["effective"]
            if (tts_settings.get("casting_enabled") or tts_settings.get("performance_enabled")) and session.scalar(select(PerformancePlan.id).where(PerformancePlan.plan_revision_id == plan_revision_id, PerformancePlan.status == "adopted")):
                freeze_performance_snapshot(session, plan_revision_id, boundary_snapshot)
        if "speech_boundaries" not in boundary_snapshot:
            freeze_boundaries(session, plan_revision_id, boundary_snapshot)
        snapshot["speech_boundaries"] = boundary_snapshot.get("speech_boundaries", {})
        settings_hash = stable_hash(snapshot)
        record = OutputAssembly(
            session_id=session_id,
            generation_run_id=run.id if run else None,
            status="queued",
            settings_json={"resolved": snapshot, "plan_revision_id": plan_revision_id},
            settings_hash=settings_hash,
        )
        session.add(record)
        session.flush()
        job = self.jobs.enqueue_in_session(
            session,
            "generation.assemble",
            {"output_assembly_id": record.id},
            session_id=session_id,
            resource_keys=[f"session:{session_id}"],
        )
        record.job_id = job.id
        record.updated_at = utcnow()
        session.flush()
        return self._assembly_payload(record, job)

    def latest_assembly(self, session_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            record = session.scalar(
                select(OutputAssembly)
                .where(OutputAssembly.session_id == session_id)
                .order_by(OutputAssembly.created_at.desc())
            )
            job = (
                session.get(Job, record.job_id)
                if record is not None and record.job_id
                else None
            )
            return self._assembly_payload(record, job) if record is not None else None


class ResourceClaimService:
    """Compatibility facade over the queue's fenced resource-lease operations."""

    def __init__(self, database: Database):
        self.queue = JobQueue(database)

    def acquire(
        self,
        job_id: str,
        owner: str,
        keys: list[str],
        lease_seconds: int = 60,
        *,
        lease_generation: int,
    ) -> bool:
        return self.queue.acquire_resources(
            job_id,
            owner,
            keys,
            lease_generation=lease_generation,
            lease_seconds=lease_seconds,
        )

    def release(
        self,
        job_id: str,
        owner: str,
        *,
        lease_generation: int,
    ) -> None:
        self.queue.release_resources(
            job_id,
            owner,
            lease_generation=lease_generation,
        )
