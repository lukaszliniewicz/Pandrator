"""Input resolution and selection for export worker workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .artifact_selection import selected_artifacts
from .export_contract import (
    ExportContract,
    export_uses_generated_audio,
    normalize_audio_mode,
)
from .models import (
    Artifact,
    GenerationRun,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutputAssembly,
    SessionRecord,
)
from .output_settings_snapshot import build_output_settings_snapshot
from .source_resolution import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, resolve_media_source
from .workflow_output_context import OutputWorkflowContext


@dataclass(frozen=True, slots=True)
class ExportInputs:
    """Resolved artifact and settings inputs for one export execution."""

    session_id: str
    settings: dict[str, Any]
    record: SessionRecord
    output_settings_snapshot: dict[str, Any]
    contract: ExportContract | None
    attached_sources: list[Artifact]
    by_role: dict[str, Artifact]
    selected_audio: Artifact | None
    assembled_audio_candidates: tuple[Artifact, ...]
    media_edit_plan: MediaEditPlan | None
    active_media_edit_revision: MediaEditPlanRevision | None


@dataclass(frozen=True, slots=True)
class MediaExportSelection:
    """Selected tracks and modes consumed by the media renderer."""

    upload_media: Artifact | None
    upload_audio: Artifact | None
    selected_subtitles: list[Artifact]
    dubbing_audio: Artifact | None
    export_mode: str
    subtitle_format: str
    subtitle_mode: str
    audio_mode: str


def resolve_export_inputs(
    context: OutputWorkflowContext, payload: dict[str, Any]
) -> ExportInputs:
    """Load queued sources and the selected assembly before creating outputs."""
    session_id = str(payload.get("session_id") or "")
    settings = dict(payload.get("settings") or {})
    from .source_management import require_recording_timing_review

    require_recording_timing_review(context.database, session_id, settings)
    raw_export_contract = payload.get("export_contract")
    if raw_export_contract is not None and not isinstance(
        raw_export_contract, dict
    ):
        raise ValueError(
            "The queued export contract is malformed; submit the export again."
        )
    resolved_settings_snapshot = payload.get("resolved_settings_snapshot")
    output_settings_snapshot = build_output_settings_snapshot(
        settings,
        (
            resolved_settings_snapshot
            if isinstance(resolved_settings_snapshot, dict)
            else None
        ),
    )
    record = context._session_record(session_id)
    uses_generated_audio = export_uses_generated_audio(
        workflow_kind=record.workflow_kind,
        settings=settings,
    )
    media_edit_plan: MediaEditPlan | None = None
    active_media_edit_revision: MediaEditPlanRevision | None = None
    with context.database.session() as session:
        if record.workflow_kind in {"media_edit", "voiceover"}:
            media_edit_plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
            )
            active_media_edit_revision = (
                session.get(MediaEditPlanRevision, media_edit_plan.active_revision_id)
                if media_edit_plan is not None and media_edit_plan.active_revision_id
                else None
            )
        current = list(
            session.scalars(
                select(Artifact)
                .where(
                    Artifact.session_id == session_id,
                    Artifact.state == "current",
                )
                .order_by(
                    Artifact.created_at.desc(),
                    Artifact.id.desc(),
                )
            ).all()
        )
        selected_text = selected_artifacts(session, session_id)
        contract_source_id = (
            str(raw_export_contract.get("source_artifact_id") or "")
            if isinstance(raw_export_contract, dict)
            else ""
        )
        if isinstance(raw_export_contract, dict) and contract_source_id:
            contract_source = session.get(Artifact, contract_source_id)
            if contract_source is None:
                raise ValueError(
                    "The source captured by this export contract is no longer available. "
                    "Submit the export again."
                )
            expected_source_hash = str(
                raw_export_contract.get("source_content_hash") or ""
            )
            if (
                expected_source_hash
                and contract_source.content_hash != expected_source_hash
            ):
                raise ValueError(
                    "The source captured by this export contract changed. Submit the export again."
                )
            attached_sources = [contract_source]
        elif isinstance(raw_export_contract, dict):
            # An explicit no-source contract must stay no-source even if the
            # session is edited before the queued worker starts.
            attached_sources = []
        else:
            compatibility_source = resolve_media_source(
                session, session_id
            ).artifact
            attached_sources = (
                [compatibility_source] if compatibility_source else []
            )
        known_ids = {item.id for item in current}
        current.extend(
            item for item in attached_sources if item.id not in known_ids
        )
        selected_assembly = None
        selected_audio = None
        selected_run_id = str(settings.get("generation_run_id") or "").strip()
        pinned_assembly_artifact_id = str(
            payload.get("pinned_assembly_artifact_id") or ""
        ).strip()
        if pinned_assembly_artifact_id and not selected_run_id:
            raise ValueError(
                "A pinned assembly requires a selected generation run."
            )
        if pinned_assembly_artifact_id and not uses_generated_audio:
            raise ValueError(
                "A pinned assembly is not valid for an export that does not use "
                "generated audio."
            )
        if selected_run_id and uses_generated_audio:
            generation_run = session.get(GenerationRun, selected_run_id)
            if generation_run is None or generation_run.session_id != session_id:
                raise ValueError(
                    "The selected generation run does not belong to this session."
                )
            if generation_run.status != "completed":
                raise ValueError(
                    "Only a completed generation run can be exported."
                )
            if (
                isinstance(resolved_settings_snapshot, dict)
                or pinned_assembly_artifact_id
            ):
                from .workspace import find_matching_output_assembly

                selected_assembly, _expected_snapshot, _expected_hash = (
                    find_matching_output_assembly(
                        session,
                        session_id=session_id,
                        run=generation_run,
                        resolved_settings_snapshot=(
                            resolved_settings_snapshot
                            if isinstance(resolved_settings_snapshot, dict)
                            else {}
                        ),
                        artifact_id=pinned_assembly_artifact_id or None,
                    )
                )
            else:
                # Compatibility for older direct API/MCP callers that pinned a
                # generation run before resolved export snapshots were queued.
                # Without the immutable snapshot we cannot prove output-setting
                # equivalence, so reuse is safe only when exactly one current
                # assembly exists for this run and its plan revision still
                # matches the run.
                legacy_candidates = list(
                    session.scalars(
                        select(OutputAssembly)
                        .join(Artifact, Artifact.id == OutputAssembly.artifact_id)
                        .where(
                            OutputAssembly.session_id == session_id,
                            OutputAssembly.generation_run_id == selected_run_id,
                            OutputAssembly.status == "completed",
                            Artifact.state == "current",
                        )
                        .order_by(
                            OutputAssembly.created_at.desc(),
                            OutputAssembly.id.desc(),
                        )
                    ).all()
                )
                legacy_candidates = [
                    candidate
                    for candidate in legacy_candidates
                    if (
                        not str(
                            (candidate.settings_json or {}).get("plan_revision_id")
                            or ""
                        )
                        or str(
                            (candidate.settings_json or {}).get("plan_revision_id")
                            or ""
                        )
                        == generation_run.plan_revision_id
                    )
                ]
                if len(legacy_candidates) > 1:
                    raise ValueError(
                        "Multiple assemblies are available for the selected audio "
                        "version. Submit the export again from the Output page."
                    )
                selected_assembly = (
                    legacy_candidates[0] if legacy_candidates else None
                )
            if selected_assembly is None:
                previous_assembly = session.scalar(
                    select(OutputAssembly.id).where(
                        OutputAssembly.session_id == session_id,
                        OutputAssembly.generation_run_id == selected_run_id,
                        OutputAssembly.status.in_(("completed", "stale")),
                        OutputAssembly.artifact_id.is_not(None),
                    )
                )
                if previous_assembly is not None:
                    raise ValueError(
                        "The selected audio assembly is no longer current or its "
                        "synchronization/output settings changed. Reassemble it "
                        "before exporting."
                    )
                raise ValueError(
                    "Assemble the selected generation run before exporting it."
                )
            selected_audio = session.get(Artifact, selected_assembly.artifact_id)
            if selected_audio is None or selected_audio.state != "current":
                raise ValueError(
                    "The selected generation run assembly is unavailable."
                )
    by_role: dict[str, Artifact] = {}
    for item in current:
        by_role.setdefault(item.role, item)
    for item in selected_text.values():
        by_role[item.role] = item
    assembled_audio_candidates = tuple(
        item for item in current if item.role == "assembled_audio"
    )
    if (
        not selected_run_id
        and len(assembled_audio_candidates) > 1
        and uses_generated_audio
    ):
        raise ValueError(
            "Multiple assembled audio versions are available. Select a completed "
            "audio version and export again."
        )
    # Every workflow, including audiobook, must honor queued export intent.
    # Legacy direct calls without a contract retain their existing behavior.
    contract = (
        ExportContract.verify(
            raw_export_contract,
            workflow_kind=record.workflow_kind,
            settings=settings,
        )
        if isinstance(raw_export_contract, dict)
        else None
    )
    return ExportInputs(
        session_id=session_id,
        settings=settings,
        record=record,
        output_settings_snapshot=output_settings_snapshot,
        contract=contract,
        attached_sources=attached_sources,
        by_role=by_role,
        selected_audio=selected_audio,
        assembled_audio_candidates=assembled_audio_candidates,
        media_edit_plan=media_edit_plan,
        active_media_edit_revision=active_media_edit_revision,
    )


def select_generated_audio(
    inputs: ExportInputs,
    *,
    legacy_role: str,
) -> Artifact | None:
    """Resolve generated audio without guessing between historical assemblies."""

    if inputs.selected_audio is not None:
        return inputs.selected_audio
    candidates = inputs.assembled_audio_candidates
    if len(candidates) > 1:
        raise ValueError(
            "Multiple assembled audio versions are available. Select a completed "
            "audio version and export again."
        )
    if candidates:
        return candidates[0]
    return inputs.by_role.get(legacy_role)


def select_media_export(inputs: ExportInputs) -> MediaExportSelection:
    """Choose tracks and validate media requirements before probing or rendering."""
    attached_sources = inputs.attached_sources
    by_role = inputs.by_role
    record = inputs.record
    contract = inputs.contract
    settings = inputs.settings
    media_edit_plan = inputs.media_edit_plan
    active_media_edit_revision = inputs.active_media_edit_revision
    selected_audio = inputs.selected_audio

    upload_media = next(
        (
            item
            for item in attached_sources
            if Path(item.relative_path).suffix.lower() in VIDEO_EXTENSIONS
        ),
        None,
    )
    upload_audio = next(
        (
            item
            for item in attached_sources
            if Path(item.relative_path).suffix.lower() in AUDIO_EXTENSIONS
        ),
        None,
    )
    translated = by_role.get("translation")
    source_subtitle = (
        by_role.get("correction")
        or by_role.get("media_edit_subtitles")
        or by_role.get("transcription")
        or next(
            (
                item
                for item in attached_sources
                if Path(item.relative_path).suffix.lower() == ".srt"
            ),
            None,
        )
    )
    export_mode = str(
        settings.get("export_mode")
        or ("subtitles" if record.workflow_kind == "subtitles" else "media")
    ).lower()
    if record.workflow_kind == "subtitles" and export_mode not in {
        "subtitles",
        "text",
    }:
        export_mode = "subtitles"
    if export_mode not in {"media", "audio", "subtitles", "text"}:
        export_mode = "media"
    if contract is not None:
        export_mode = contract.export_mode
    requires_edited_media = (
        record.workflow_kind == "media_edit" and export_mode == "media"
    ) or (
        record.workflow_kind == "voiceover"
        and export_mode in {"media", "audio"}
        and (
            media_edit_plan is not None
            or any(item.role == "media_edit_media" for item in attached_sources)
        )
    )
    if requires_edited_media:
        edited_media = by_role.get("media_edit_media")
        if contract is None or edited_media is None:
            raise ValueError(
                "The rendered media edit is no longer available; render it again before exporting."
            )
        if edited_media.id != contract.source_artifact_id:
            raise ValueError(
                "The selected media edit changed after this export was queued."
            )
        if (
            contract.source_content_hash
            and edited_media.content_hash != contract.source_content_hash
        ):
            raise ValueError(
                "The rendered media edit no longer matches its immutable export contract."
            )
        edited_metadata = (
            edited_media.metadata_json
            if isinstance(edited_media.metadata_json, dict)
            else {}
        )
        if not (
            active_media_edit_revision is not None
            and str(edited_metadata.get("revision_id") or "")
            == active_media_edit_revision.id
            and str(edited_metadata.get("content_hash") or "")
            == active_media_edit_revision.content_hash
        ):
            raise ValueError(
                "The media-edit revision changed after this export was queued; "
                "render and submit the export again."
            )
        upload_media = edited_media
        upload_audio = None
    subtitle_format = str(settings.get("subtitle_format") or "srt").lower()
    if subtitle_format not in {"srt", "vtt"}:
        subtitle_format = "srt"
    subtitle_mode = str(settings.get("subtitle_mode") or "none").lower()
    subtitle_mode = {"burn": "burned"}.get(subtitle_mode, subtitle_mode)
    subtitle_selection = str(
        settings.get("subtitle_selection")
        or ("dual" if translated and source_subtitle else "translation")
    ).lower()
    subtitle_selection = {"both": "dual"}.get(
        subtitle_selection, subtitle_selection
    )
    if (
        subtitle_selection == "translation"
        and translated is None
        and source_subtitle is not None
    ):
        subtitle_selection = "source"
    elif (
        subtitle_selection == "source"
        and source_subtitle is None
        and translated is not None
    ):
        subtitle_selection = "translation"
    selected_subtitles = (
        [source_subtitle]
        if source_subtitle and subtitle_selection in {"source", "dual"}
        else []
    ) + (
        [translated]
        if translated and subtitle_selection in {"translation", "dual"}
        else []
    )
    # "No subtitles" only suppresses tracks on a media render. A
    # subtitle/text-only request still uses the selected document.
    selected_subtitles = (
        [item for item in selected_subtitles if item]
        if export_mode != "media"
        or subtitle_mode != "none"
        or upload_media is None
        else []
    )
    if record.workflow_kind == "voiceover" and export_mode in {"media", "audio"}:
        canonical_audio_mode = (
            contract.audio_mode
            if contract is not None
            else normalize_audio_mode(settings.get("audio_mode"))
        )
        if canonical_audio_mode in {"preserve", "mixed"} and not (
            upload_media or upload_audio
        ):
            raise ValueError(
                "The requested source-audio export has no attached source. "
                "Attach the intended source or choose Voiceover only."
            )
        audio_mode_by_setting: dict[str | None, str] = {
            "preserve": "source",
            "dubbing_only": "dubbed",
            "mixed": "mixed",
        }
        audio_mode = audio_mode_by_setting[canonical_audio_mode]
    else:
        audio_mode = "source"
    dubbing_audio = (
        select_generated_audio(inputs, legacy_role="dubbing_audio")
        if export_mode in {"media", "audio"}
        and audio_mode in {"dubbed", "mixed"}
        else selected_audio
    )
    if (
        export_mode in {"media", "audio"}
        and audio_mode in {"dubbed", "mixed"}
        and dubbing_audio is None
    ):
        raise ValueError(
            "This media export requires assembled generated audio. Select a completed audio version and assemble it before exporting."
        )
    return MediaExportSelection(
        upload_media=upload_media,
        upload_audio=upload_audio,
        selected_subtitles=selected_subtitles,
        dubbing_audio=dubbing_audio,
        export_mode=export_mode,
        subtitle_format=subtitle_format,
        subtitle_mode=subtitle_mode,
        audio_mode=audio_mode,
    )
