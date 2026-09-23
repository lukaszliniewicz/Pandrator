"""Export worker workflow."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from pandrator.logic.dubbing.languages import (
    normalize_language_code,
    subtitle_language_title,
)

from .artifact_selection import selected_artifacts
from .export_contract import ExportContract, normalize_audio_mode
from .models import (
    Artifact,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutputAssembly,
    new_id,
)
from .output_settings_snapshot import build_output_settings_snapshot
from .source_resolution import resolve_media_source
from .workflow_output_context import OutputWorkflowContext


def _effective_subtitle_language(*candidates: object) -> str:
    """Choose the first concrete language without letting ``auto`` mask it."""
    for candidate in candidates:
        normalized = normalize_language_code(str(candidate or ""), default="")
        if normalized and normalized not in {"auto", "und", "unknown"}:
            return normalized
    return "und"


def export(
    context: OutputWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Create immutable, managed exports from the explicitly selected inputs."""
    from werkzeug.utils import secure_filename

    from pandrator.logic.dubbing.audio_sync import (
        build_mix_audio_command,
        media_has_audio_stream,
    )
    from pandrator.logic.dubbing.bilingual_ass import write_bilingual_ass
    from pandrator.logic.dubbing.srt_utils import (
        concatenate_subtitle_text,
        srt_to_vtt,
    )
    from pandrator.logic.dubbing.subtitle_finalization import finalize_srt_file
    from pandrator.logic.dubbing.video_muxing import (
        build_add_subtitles_command,
        build_multi_soft_subtitle_command,
        build_replace_video_audio_command,
        build_video_tail_extension_command,
        build_video_transcode_command,
        build_web_optimized_remux_command,
        normalize_video_resolution,
    )
    from pandrator.logic.dubbing_handler import resolve_ffmpeg_for_burned_subtitles
    from pandrator.web.capabilities import ffmpeg_video_encoder_ids

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
    expected_assembly_settings_hash = None
    if isinstance(resolved_settings_snapshot, dict):
        from .workspace import output_assembly_settings_hash

        expected_assembly_settings_hash = output_assembly_settings_hash(
            resolved_settings_snapshot
        )
    record = context._session_record(session_id)
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
                select(Artifact).where(
                    Artifact.session_id == session_id, Artifact.state == "current"
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
        if selected_run_id:
            assembly_candidates = list(
                session.scalars(
                    select(OutputAssembly)
                    .join(Artifact, Artifact.id == OutputAssembly.artifact_id)
                    .where(
                        OutputAssembly.session_id == session_id,
                        OutputAssembly.generation_run_id == selected_run_id,
                        OutputAssembly.status == "completed",
                        Artifact.state == "current",
                    )
                    .order_by(OutputAssembly.created_at.desc())
                ).all()
            )
            if expected_assembly_settings_hash:
                from .workspace import output_assembly_settings_hash

                selected_assembly = next(
                    (
                        candidate
                        for candidate in assembly_candidates
                        if candidate.settings_hash
                        == expected_assembly_settings_hash
                        or output_assembly_settings_hash(
                            dict(
                                (candidate.settings_json or {}).get("resolved")
                                or {}
                            )
                        )
                        == expected_assembly_settings_hash
                    ),
                    None,
                )
            else:
                selected_assembly = (
                    assembly_candidates[0] if assembly_candidates else None
                )
            if selected_assembly is None:
                if expected_assembly_settings_hash:
                    stale_assembly = session.scalar(
                        select(OutputAssembly.id).where(
                            OutputAssembly.session_id == session_id,
                            OutputAssembly.generation_run_id == selected_run_id,
                            OutputAssembly.status == "completed",
                            OutputAssembly.artifact_id.is_not(None),
                        )
                    )
                    if stale_assembly is not None:
                        raise ValueError(
                            "Synchronization or output settings changed after this audio version was assembled. Reassemble it before exporting."
                        )
                raise ValueError(
                    "Assemble the selected generation run before exporting it."
                )
            selected_audio = session.get(Artifact, selected_assembly.artifact_id)
            if selected_audio is None:
                raise ValueError(
                    "The selected generation run assembly is unavailable."
                )
    by_role: dict[str, Artifact] = {}
    for item in current:
        by_role.setdefault(item.role, item)
    for item in selected_text.values():
        by_role[item.role] = item
    output_dir = context._session_dir(session_id) / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    # New exports are grouped by kind so the Output tab stays readable.
    # Legacy files keep their recorded flat paths; only destinations
    # allocated below use these subfolders.
    audio_dir = output_dir / "audio"
    video_dir = output_dir / "video"
    subtitle_dir = output_dir / "subtitles"
    export_name = secure_filename(record.name) or record.storage_key
    progress(0.1, "Preparing export")
    produced: list[Artifact] = []
    # Finalized-then-converted subtitle scratch files (VTT/text mux
    # inputs). Text/VTT conversions remove their own scratch in a
    # branch-local try/finally; video-mux scratch is finalized inside
    # the render try so its finally sweeps every failure path.
    scratch_subtitle_paths: list[Path] = []

    if record.workflow_kind == "audiobook":
        audio = (
            selected_audio
            if selected_assembly is not None
            else by_role.get("assembled_audio") or by_role.get("audiobook_audio")
        )
        if audio is None:
            raise ValueError("Audiobook export requires generated audio.")
        _audio_record, audio_path = context._resolve_input(audio.id)
        audio_dir.mkdir(parents=True, exist_ok=True)
        destination = context.artifacts.next_available_path(
            audio_dir / f"{export_name}{audio_path.suffix.lower()}"
        )
        progress(0.25, "Copying assembled audiobook")
        shutil.copy2(audio_path, destination)
        progress(0.9, "Registering audiobook export")
        produced.append(
            context.artifacts.register(
                destination,
                kind="export",
                role="export",
                session_id=session_id,
                parent_ids=[audio.id],
                settings=settings,
            )
        )
    else:
        upload_media = next(
            (
                item
                for item in attached_sources
                if Path(item.relative_path).suffix.lower()
                in {
                    ".mp4",
                    ".mkv",
                    ".mov",
                    ".avi",
                    ".webm",
                    ".m4v",
                    ".mpeg",
                    ".mpg",
                }
            ),
            None,
        )
        upload_audio = next(
            (
                item
                for item in attached_sources
                if Path(item.relative_path).suffix.lower()
                in {
                    ".wav",
                    ".mp3",
                    ".flac",
                    ".m4a",
                    ".aac",
                    ".ogg",
                    ".opus",
                    ".wma",
                }
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
        contract = (
            ExportContract.verify(
                raw_export_contract,
                workflow_kind=record.workflow_kind,
                settings=settings,
            )
            if isinstance(raw_export_contract, dict)
            else None
        )
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
        dubbing_audio = (
            selected_audio
            if selected_assembly is not None
            else by_role.get("assembled_audio") or by_role.get("dubbing_audio")
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
        if (
            export_mode in {"media", "audio"}
            and audio_mode in {"dubbed", "mixed"}
            and dubbing_audio is None
        ):
            raise ValueError(
                "This media export requires assembled generated audio. Select a completed audio version and assemble it before exporting."
            )
        if (
            export_mode in {"media", "audio"}
            and upload_media
            and audio_mode in {"source", "mixed"}
        ):
            _source_record, source_media_path = context._resolve_input(upload_media.id)
            ffprobe_executable = str(
                os.environ.get("PANDRATOR_FFPROBE_EXE")
                or shutil.which("ffprobe")
                or ""
            )
            if not ffprobe_executable:
                raise RuntimeError(
                    "FFprobe is required to verify the requested source soundtrack."
                )
            try:
                source_has_audio = media_has_audio_stream(
                    source_media_path,
                    ffprobe_executable=ffprobe_executable,
                )
            except subprocess.CalledProcessError as error:
                raise ValueError(
                    "The source video soundtrack could not be inspected. "
                    "Verify the source file and submit the export again."
                ) from error
            if not source_has_audio:
                action = "preserve" if audio_mode == "source" else "mix"
                raise ValueError(
                    f"The source video has no audio stream to {action}. "
                    "Choose Voiceover only and submit the export again."
                )
        if export_mode == "audio":
            selected_subtitles = []
        # Subtitle finalization runs inside each branch below so every
        # scratch file lives in a bounded try/finally: conversions and
        # mux inputs can never strand hidden duplicates when they fail.
        # Branches producing an SRT export finalize directly into the
        # export destination and only register it: no second
        # byte-identical copy is ever created. Exports keep the source
        # artifact as their direct parent so provenance survives
        # without an intermediate row.
        if not selected_subtitles:
            progress(0.12, "Subtitle selection complete")
        for index in range(1, len(selected_subtitles) + 1):
            progress(
                0.12 + 0.18 * (index / len(selected_subtitles)),
                f"Prepared subtitle track {index} of {len(selected_subtitles)}",
            )

        def _finalize_track_scratch(track_name: str) -> Path:
            scratch = (
                output_dir
                / f".{record.storage_key}-{track_name}-final-{new_id()}.srt"
            )
            scratch_subtitle_paths.append(scratch)
            return scratch

        def is_translation_track(item: Artifact) -> bool:
            return (
                str((item.metadata_json or {}).get("source_role") or item.role)
                == "translation"
            )

        def track_details(item: Artifact) -> tuple[str, str, str, bool]:
            translation_track = is_translation_track(item)
            track_name = "translation" if translation_track else "source"
            language = _effective_subtitle_language(
                (item.metadata_json or {}).get("language"),
                record.target_language
                if translation_track
                else record.source_language,
                (
                    settings.get("target_language")
                    if translation_track
                    else settings.get("original_language")
                    or settings.get("source_language")
                ),
            )
            title = subtitle_language_title(language)
            is_default = translation_track or len(selected_subtitles) == 1
            return track_name, language, title, is_default

        if export_mode in {"subtitles", "text"}:
            if not selected_subtitles:
                raise ValueError(
                    "No subtitle artifact is available for this export."
                )
            subtitle_dir.mkdir(parents=True, exist_ok=True)
            for index, item in enumerate(selected_subtitles, start=1):
                progress(
                    0.35 + 0.5 * ((index - 1) / len(selected_subtitles)),
                    f"Writing export track {index} of {len(selected_subtitles)}",
                )
                track_name, language, title, _default = track_details(item)
                source_role = (item.metadata_json or {}).get(
                    "source_role"
                ) or item.role
                _subtitle_record, subtitle_path = context._resolve_input(item.id)
                if export_mode == "text" or subtitle_format == "vtt":
                    # Converted outputs finalize into a scratch file whose
                    # try/finally below guarantees removal even when the
                    # conversion, write, or registration fails.
                    scratch = _finalize_track_scratch(track_name)
                    try:
                        finalize_srt_file(subtitle_path, scratch, {**settings, "subtitle_language": track_details(item)[1]})
                        if export_mode == "text":
                            destination = context.artifacts.next_available_path(
                                subtitle_dir / f"{export_name}_{track_name}.txt"
                            )
                            destination.write_text(
                                concatenate_subtitle_text(
                                    scratch.read_text(encoding="utf-8-sig")
                                ),
                                encoding="utf-8",
                            )
                            kind = "text"
                            role = f"export_text_{track_name}"
                        else:
                            destination = context.artifacts.next_available_path(
                                subtitle_dir / f"{export_name}_{track_name}.vtt"
                            )
                            destination.write_text(
                                srt_to_vtt(
                                    scratch.read_text(encoding="utf-8-sig")
                                ),
                                encoding="utf-8",
                            )
                            kind = subtitle_format
                            role = f"export_subtitle_{track_name}"
                        produced.append(
                            context.artifacts.register(
                                destination,
                                kind=kind,
                                role=role,
                                session_id=session_id,
                                parent_ids=[item.id],
                                settings=settings,
                                metadata={
                                    "language": language,
                                    "title": title,
                                    "source_role": source_role,
                                },
                            )
                        )
                    finally:
                        scratch.unlink(missing_ok=True)
                        if scratch in scratch_subtitle_paths:
                            scratch_subtitle_paths.remove(scratch)
                else:
                    # SRT exports finalize directly into the export
                    # destination: one physical file, registered once.
                    destination = context.artifacts.next_available_path(
                        subtitle_dir / f"{export_name}_{track_name}.srt"
                    )
                    finalize_srt_file(subtitle_path, destination, {**settings, "subtitle_language": language})
                    kind = subtitle_format
                    role = f"export_subtitle_{track_name}"
                    produced.append(
                        context.artifacts.register(
                            destination,
                            kind=kind,
                            role=role,
                            session_id=session_id,
                            parent_ids=[item.id],
                            settings=settings,
                            metadata={
                                "language": language,
                                "title": title,
                                "source_role": source_role,
                            },
                        )
                    )
                progress(
                    0.35 + 0.55 * (index / len(selected_subtitles)),
                    f"Exported track {index} of {len(selected_subtitles)}",
                )
        elif export_mode == "audio":
            from .soundtrack_export import (
                ensure_soundtrack_master,
                export_soundtrack_file,
            )

            progress(0.35, "Preparing the selected soundtrack without rendering video")
            master = ensure_soundtrack_master(
                context, session_id=session_id, source=upload_media or upload_audio,
                speech=dubbing_audio, audio_mode=audio_mode, settings=settings,
                cancel_event=cancel_event,
            )
            format_name = str(settings.get("format") or "wav").lower()
            audio_dir.mkdir(parents=True, exist_ok=True)
            destination = context.artifacts.next_available_path(audio_dir / f"{export_name}_{audio_mode}.{format_name}")
            produced.append(export_soundtrack_file(
                context, session_id=session_id, master=master, destination=destination,
                settings=settings, cancel_event=cancel_event,
            ))
            progress(0.92, "Audio-only soundtrack ready")
        elif upload_media:
            progress(0.35, "Preparing source media")
            _media_record, media_path = context._resolve_input(upload_media.id)
            working_video = media_path
            audio_parent_ids: list[str] = [upload_media.id]
            temporary_video: Path | None = None
            tail_video: Path | None = None
            tail_extension_ms = 0
            ffmpeg_executable = str(
                os.environ.get("PANDRATOR_FFMPEG_EXE")
                or shutil.which("ffmpeg")
                or "ffmpeg"
            )
            video_audio_bitrate = str(
                settings.get("burn_audio_bitrate") or "192k"
            ).strip()
            video_audio_codec = (
                str(settings.get("burn_audio_codec") or "copy").strip().lower()
            )
            if (
                audio_mode in {"dubbed", "mixed"} or video_audio_codec == "aac"
            ) and not re.fullmatch(
                r"[1-9][0-9]*(?:[kKmM])?",
                video_audio_bitrate,
            ):
                raise ValueError("Video AAC bitrate must look like 192k or 2M.")
            video_encoder = (
                str(settings.get("burn_video_encoder") or "libx264").strip().lower()
            )
            if dubbing_audio and audio_mode in {"dubbed", "mixed"}:
                from .soundtrack_export import (
                    ensure_soundtrack_master,
                    probe_soundtrack_media,
                    resolve_video_tail_extension_ms,
                )

                reference_info = probe_soundtrack_media(media_path)
                _speech_record, speech_probe_path = context._resolve_input(
                    dubbing_audio.id
                )
                speech_info = probe_soundtrack_media(speech_probe_path)
                # Freeze math runs against the VIDEO stream duration (not the
                # container duration). resolve_video_tail_extension_ms is the
                # single shared computation: 0 when the timeline fits, the
                # frame-ceiled extension when approved, otherwise a clear
                # duration warning. Its result is passed to both the video
                # tail step and the soundtrack master below so neither
                # double-extends nor clips.
                reference_duration = reference_info.get(
                    "video_duration"
                ) or reference_info["duration"]
                # Any true-positive speech overrun must survive the final
                # mux: sub-tolerance overruns ride the full audio track
                # (shortest=False, no freeze/reencode), larger ones use the
                # shared frozen-tail extension or raise. The -shortest fast
                # path is only kept when the audio cannot exceed the video.
                overrun = speech_info["duration"] - reference_duration
                tail_extension_ms = 0
                if (
                    reference_info["has_video"]
                    and bool(settings.get("audio_match_source_duration", True))
                ):
                    tail_extension_ms = resolve_video_tail_extension_ms(
                        reference_duration=reference_duration,
                        generated_duration=speech_info["duration"],
                        fps=reference_info.get("fps"),
                        settings=settings,
                    )
                preserve_audio_tail = tail_extension_ms > 0 or overrun > 0
                if tail_extension_ms:
                    if video_encoder not in ffmpeg_video_encoder_ids(
                        ffmpeg_executable
                    ):
                        raise RuntimeError(
                            f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                        )
                    tail_video = (
                        output_dir / f".{record.storage_key}-tail-{new_id()}.mp4"
                    )
                    tail_command = build_video_tail_extension_command(
                        str(media_path),
                        str(tail_video),
                        tail_extension_ms / 1000,
                        ffmpeg_executable=ffmpeg_executable,
                        video_encoder=video_encoder,
                        video_quality=settings.get("burn_video_quality", 18),
                        video_speed=str(
                            settings.get("burn_video_speed") or "balanced"
                        ),
                        audio_codec="copy",
                        audio_bitrate=video_audio_bitrate,
                        video_resolution=settings.get(
                            "burn_video_resolution", "source"
                        ),
                    )
                    progress(
                        0.38,
                        f"Extending the final video frame by {tail_extension_ms} ms "
                        "to cover the voiceover tail",
                    )
                    subprocess.run(
                        tail_command, check=True, capture_output=True, text=True
                    )
                    working_video = tail_video
                _audio_record, audio_path = context._resolve_input(dubbing_audio.id)
                audio_video = (
                    output_dir / f".{record.storage_key}-audio-{new_id()}.mp4"
                )
                if audio_mode == "dubbed":
                    command = build_replace_video_audio_command(
                        str(working_video),
                        str(audio_path),
                        str(audio_video),
                        ffmpeg_executable=ffmpeg_executable,
                        audio_bitrate=video_audio_bitrate,
                        shortest=not preserve_audio_tail,
                    )
                    progress(0.4, "Replacing source audio with generated speech")
                else:
                    master = ensure_soundtrack_master(
                        context, session_id=session_id, source=upload_media,
                        speech=dubbing_audio, audio_mode="mixed", settings=settings,
                        cancel_event=cancel_event,
                        tail_extension_ms=tail_extension_ms,
                    )
                    _master, master_path = context._resolve_input(master.id)
                    audio_parent_ids.append(master.id)
                    command = build_replace_video_audio_command(
                        str(working_video), str(master_path), str(audio_video),
                        ffmpeg_executable=ffmpeg_executable,
                        audio_bitrate=video_audio_bitrate,
                        shortest=not preserve_audio_tail,
                    )
                    progress(0.4, "Using the selected mixed soundtrack")
                subprocess.run(command, check=True, capture_output=True, text=True)
                progress(0.58, "Media audio track ready")
                working_video = audio_video
                temporary_video = audio_video
                audio_parent_ids.append(dubbing_audio.id)
            user_video_transcode = bool(settings.get("video_transcode")) or (
                subtitle_mode == "burned" and bool(selected_subtitles)
            )
            # A frozen tail already reencoded the video with the selected
            # encoder. Force the transcoded flag/metadata without paying
            # for a second full transcode when the user did not request one.
            video_transcode = user_video_transcode or tail_extension_ms > 0
            output_video_resolution = (
                normalize_video_resolution(
                    settings.get("burn_video_resolution", "source")
                )
                if video_transcode
                else "source"
            )
            # Replacement and mixed soundtracks were encoded to AAC in the
            # preparation step above. Copy them during the video render so
            # the user's chosen bitrate is not lost to a second encode.
            render_audio_codec = (
                video_audio_codec if audio_mode == "source" else "copy"
            )
            variant = (
                f"_{subtitle_mode}"
                if subtitle_mode in {"soft", "burned"} and selected_subtitles
                else ""
            )
            video_dir.mkdir(parents=True, exist_ok=True)
            destination = context.artifacts.next_available_path(
                video_dir / f"{export_name}{variant}.mp4"
            )
            render_destination = (
                output_dir / f".{record.storage_key}-render-{new_id()}.mp4"
            )
            video_track_artifacts: list[Artifact] = []
            # Playback/render helpers (player VTT sidecars, bilingual ASS
            # overlays) are registered but kept out of the deliverable
            # export folders; only the mp4 and explicit subtitle/text
            # exports land under exports/.
            intermediates_subtitle_dir = (
                output_dir.parent / "intermediates" / "subtitles"
            )
            try:
                finalized_subtitle_paths: dict[str, Path] = {}
                for item in selected_subtitles:
                    track_name = track_details(item)[0]
                    _subtitle_record, subtitle_path = context._resolve_input(
                        item.id
                    )
                    scratch = _finalize_track_scratch(track_name)
                    finalize_srt_file(subtitle_path, scratch, {**settings, "subtitle_language": track_details(item)[1]})
                    finalized_subtitle_paths[item.id] = scratch
                if subtitle_mode == "soft" and selected_subtitles:
                    tracks = []
                    for index, item in enumerate(selected_subtitles, start=1):
                        subtitle_path = finalized_subtitle_paths[item.id]
                        track_name, language, title, is_default = track_details(
                            item
                        )
                        tracks.append(
                            {
                                "path": str(subtitle_path),
                                "language": language,
                                "title": title,
                                "default": is_default,
                            }
                        )
                        intermediates_subtitle_dir.mkdir(
                            parents=True, exist_ok=True
                        )
                        vtt_path = context.artifacts.next_available_path(
                            intermediates_subtitle_dir
                            / f"{record.storage_key}_{track_name}_player.vtt"
                        )
                        vtt_path.write_text(
                            srt_to_vtt(
                                subtitle_path.read_text(encoding="utf-8-sig")
                            ),
                            encoding="utf-8",
                        )
                        video_track_artifacts.append(
                            context.artifacts.register(
                                vtt_path,
                                kind="vtt",
                                role=f"video_subtitle_track_{track_name}",
                                session_id=session_id,
                                parent_ids=[item.id],
                                settings=settings,
                                metadata={
                                    "language": language,
                                    "title": title,
                                    "default": is_default,
                                },
                            )
                        )
                        progress(
                            0.58 + 0.06 * (index / len(selected_subtitles)),
                            f"Prepared selectable subtitle track {index} of {len(selected_subtitles)}",
                        )
                    if (
                        user_video_transcode
                        and video_encoder
                        not in ffmpeg_video_encoder_ids(ffmpeg_executable)
                    ):
                        raise RuntimeError(
                            f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                        )
                    command = build_multi_soft_subtitle_command(
                        str(working_video),
                        tracks,
                        str(render_destination),
                        ffmpeg_executable=ffmpeg_executable,
                        transcode_video=user_video_transcode,
                        video_encoder=video_encoder,
                        video_resolution=output_video_resolution,
                        video_quality=settings.get("burn_video_quality", 18),
                        video_speed=str(
                            settings.get("burn_video_speed") or "balanced"
                        ),
                        audio_codec=render_audio_codec,
                        audio_bitrate=video_audio_bitrate,
                    )
                    progress(
                        0.65,
                        "Transcoding media with selectable subtitles"
                        if user_video_transcode
                        else "Rendering web-optimized media with selectable subtitles",
                    )
                    try:
                        subprocess.run(
                            command, check=True, capture_output=True, text=True
                        )
                    except subprocess.CalledProcessError as remux_error:
                        if user_video_transcode:
                            raise
                        if video_encoder not in ffmpeg_video_encoder_ids(
                            ffmpeg_executable
                        ):
                            detail = (
                                str(remux_error.stderr or remux_error.stdout or "")
                                .strip()
                                .splitlines()
                            )
                            reason = (
                                detail[-1]
                                if detail
                                else "FFmpeg could not stream-copy the source into MP4."
                            )
                            raise RuntimeError(
                                "Selectable-subtitle MP4 remuxing failed and the selected "
                                f"{video_encoder} fallback encoder is unavailable: {reason}"
                            ) from remux_error
                        progress(
                            0.68,
                            "Stream-copy subtitle mux was unavailable; transcoding a web-compatible MP4",
                        )
                        command = build_multi_soft_subtitle_command(
                            str(working_video),
                            tracks,
                            str(render_destination),
                            ffmpeg_executable=ffmpeg_executable,
                            transcode_video=True,
                            video_encoder=video_encoder,
                            video_resolution="source",
                            video_quality=settings.get("burn_video_quality", 18),
                            video_speed=str(
                                settings.get("burn_video_speed") or "balanced"
                            ),
                            audio_codec="aac",
                            audio_bitrate=video_audio_bitrate,
                        )
                        try:
                            subprocess.run(
                                command,
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                        except subprocess.CalledProcessError as transcode_error:
                            detail = (
                                str(
                                    transcode_error.stderr
                                    or transcode_error.stdout
                                    or ""
                                )
                                .strip()
                                .splitlines()
                            )
                            reason = (
                                detail[-1]
                                if detail
                                else "FFmpeg returned a non-zero exit status."
                            )
                            raise RuntimeError(
                                f"Selectable-subtitle fallback with {video_encoder} failed: {reason}"
                            ) from transcode_error
                        video_transcode = True
                elif subtitle_mode == "burned" and selected_subtitles:
                    subtitle_paths = [
                        finalized_subtitle_paths[item.id]
                        for item in selected_subtitles
                    ]
                    burn_path = subtitle_paths[-1]
                    if len(subtitle_paths) == 2:
                        intermediates_subtitle_dir.mkdir(
                            parents=True, exist_ok=True
                        )
                        burn_path = Path(
                            write_bilingual_ass(
                                str(subtitle_paths[0]),
                                str(subtitle_paths[1]),
                                str(
                                    context.artifacts.next_available_path(
                                        intermediates_subtitle_dir
                                        / "bilingual_subtitles.ass"
                                    )
                                ),
                            )
                        )
                        context.artifacts.register(
                            burn_path,
                            kind="ass",
                            role="bilingual_subtitle_overlay",
                            session_id=session_id,
                            parent_ids=[item.id for item in selected_subtitles],
                            settings=settings,
                        )
                    burn_ffmpeg = resolve_ffmpeg_for_burned_subtitles()
                    if not burn_ffmpeg:
                        raise RuntimeError(
                            "Burned subtitles require an FFmpeg build with the subtitles/libass filter. Install or select Pandrator's bundled FFmpeg, or use soft subtitles."
                        )
                    if video_encoder not in ffmpeg_video_encoder_ids(burn_ffmpeg):
                        raise RuntimeError(
                            f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                        )
                    command = build_add_subtitles_command(
                        str(working_video),
                        str(burn_path),
                        str(render_destination),
                        subtitle_mode="burned",
                        subtitle_language=str(
                            settings.get("target_language") or "und"
                        ),
                        ffmpeg_executable=burn_ffmpeg,
                        video_encoder=video_encoder,
                        video_resolution=output_video_resolution,
                        video_quality=settings.get("burn_video_quality", 18),
                        video_speed=str(
                            settings.get("burn_video_speed") or "balanced"
                        ),
                        audio_codec=render_audio_codec,
                        audio_bitrate=video_audio_bitrate,
                    )
                    progress(0.65, "Rendering burned subtitles into video")
                    try:
                        subprocess.run(
                            command, check=True, capture_output=True, text=True
                        )
                    except subprocess.CalledProcessError as error:
                        detail = (
                            str(error.stderr or error.stdout or "")
                            .strip()
                            .splitlines()
                        )
                        reason = (
                            detail[-1]
                            if detail
                            else "FFmpeg returned a non-zero exit status."
                        )
                        raise RuntimeError(
                            f"Burned-subtitle transcoding with {video_encoder} failed: {reason}"
                        ) from error
                elif user_video_transcode:
                    if video_encoder not in ffmpeg_video_encoder_ids(
                        ffmpeg_executable
                    ):
                        raise RuntimeError(
                            f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                        )
                    command = build_video_transcode_command(
                        str(working_video),
                        str(render_destination),
                        ffmpeg_executable=ffmpeg_executable,
                        video_encoder=video_encoder,
                        video_resolution=output_video_resolution,
                        video_quality=settings.get("burn_video_quality", 18),
                        video_speed=str(
                            settings.get("burn_video_speed") or "balanced"
                        ),
                        audio_codec=render_audio_codec,
                        audio_bitrate=video_audio_bitrate,
                    )
                    progress(0.65, "Transcoding video output")
                    try:
                        subprocess.run(
                            command, check=True, capture_output=True, text=True
                        )
                    except subprocess.CalledProcessError as error:
                        detail = (
                            str(error.stderr or error.stdout or "")
                            .strip()
                            .splitlines()
                        )
                        reason = (
                            detail[-1]
                            if detail
                            else "FFmpeg returned a non-zero exit status."
                        )
                        raise RuntimeError(
                            f"Video transcoding with {video_encoder} failed: {reason}"
                        ) from error
                else:
                    if tail_extension_ms > 0:
                        progress(
                            0.65,
                            f"Optimizing frozen-tail media for web playback (+{tail_extension_ms} ms)",
                        )
                    else:
                        progress(
                            0.65,
                            "Optimizing media for web playback without video transcoding",
                        )
                    command = build_web_optimized_remux_command(
                        str(working_video),
                        str(render_destination),
                        ffmpeg_executable=ffmpeg_executable,
                        audio_codec=render_audio_codec,
                        audio_bitrate=video_audio_bitrate,
                    )
                    try:
                        subprocess.run(
                            command, check=True, capture_output=True, text=True
                        )
                    except subprocess.CalledProcessError as remux_error:
                        # A source codec/container combination may not be
                        # legal inside MP4 even though stream-copy is the
                        # preferred web-optimization path. Fall back to a
                        # standards-friendly transcode only when remuxing
                        # cannot produce the export.
                        if video_encoder not in ffmpeg_video_encoder_ids(
                            ffmpeg_executable
                        ):
                            detail = (
                                str(remux_error.stderr or remux_error.stdout or "")
                                .strip()
                                .splitlines()
                            )
                            reason = (
                                detail[-1]
                                if detail
                                else "FFmpeg could not stream-copy the source into MP4."
                            )
                            raise RuntimeError(
                                "Web-optimized MP4 remuxing failed and the selected "
                                f"{video_encoder} fallback encoder is unavailable: {reason}"
                            ) from remux_error
                        progress(
                            0.68,
                            "Stream-copy remux was unavailable; transcoding a web-compatible MP4",
                        )
                        command = build_video_transcode_command(
                            str(working_video),
                            str(render_destination),
                            ffmpeg_executable=ffmpeg_executable,
                            video_encoder=video_encoder,
                            video_resolution="source",
                            video_quality=settings.get("burn_video_quality", 18),
                            video_speed=str(
                                settings.get("burn_video_speed") or "balanced"
                            ),
                            audio_codec="aac",
                            audio_bitrate=video_audio_bitrate,
                        )
                        try:
                            subprocess.run(
                                command,
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                        except subprocess.CalledProcessError as transcode_error:
                            detail = (
                                str(
                                    transcode_error.stderr
                                    or transcode_error.stdout
                                    or ""
                                )
                                .strip()
                                .splitlines()
                            )
                            reason = (
                                detail[-1]
                                if detail
                                else "FFmpeg returned a non-zero exit status."
                            )
                            raise RuntimeError(
                                f"Web-compatible video fallback with {video_encoder} failed: {reason}"
                            ) from transcode_error
                        video_transcode = True
                os.replace(render_destination, destination)
                if tail_extension_ms > 0:
                    progress(
                        0.9,
                        f"Rendered media output ready (last frame frozen +{tail_extension_ms} ms)",
                    )
                else:
                    progress(0.9, "Rendered media output ready")
            finally:
                if render_destination.exists():
                    render_destination.unlink()
                if temporary_video is not None and temporary_video.exists():
                    temporary_video.unlink()
                if tail_video is not None and tail_video.exists():
                    tail_video.unlink()
                for scratch_subtitle in scratch_subtitle_paths:
                    scratch_subtitle.unlink(missing_ok=True)
                scratch_subtitle_paths.clear()
            subtitle_track_metadata = [
                {
                    "artifact_id": item.id,
                    "language": str(
                        (item.metadata_json or {}).get("language") or "und"
                    ),
                    "title": str(
                        (item.metadata_json or {}).get("title") or "Subtitles"
                    ),
                    "default": bool((item.metadata_json or {}).get("default")),
                }
                for item in video_track_artifacts
            ]
            produced.append(
                # Register after the potentially long render so 100% is
                # reserved for a durable, discoverable output.
                context.artifacts.register(
                    destination,
                    kind="export",
                    role="export",
                    session_id=session_id,
                    parent_ids=audio_parent_ids
                    + [item.id for item in selected_subtitles]
                    + [item.id for item in video_track_artifacts],
                    settings=settings,
                    metadata={
                        "audio_mode": audio_mode,
                        "subtitle_mode": subtitle_mode,
                        "video_resolution": output_video_resolution,
                        "video_transcoded": video_transcode,
                        "video_encoder": video_encoder if video_transcode else None,
                        "tail_extension_ms": tail_extension_ms,
                        "audio_bitrate": (
                            video_audio_bitrate
                            if audio_mode in {"dubbed", "mixed"}
                            or render_audio_codec == "aac"
                            else None
                        ),
                        "subtitle_tracks": subtitle_track_metadata,
                        "mix": {
                            "source_gain_db": settings.get(
                                "mix_source_gain_db", 0.0
                            ),
                            "voice_gain_db": settings.get("mix_voice_gain_db", 0.0),
                            "voice_lufs": settings.get("mix_voice_lufs", -16.0),
                            "ducking": settings.get("mix_ducking", "strong"),
                            "attack_ms": settings.get("mix_attack_ms", 25),
                            "release_ms": settings.get("mix_release_ms", 350),
                        }
                        if audio_mode == "mixed"
                        else None,
                    },
                )
            )
        else:
            # Preserve the historical behavior for SRT/audio-only sessions:
            # a media export falls back to managed standalone artifacts.
            # Each track is finalized directly into its export destination:
            # one physical file, registered once.
            subtitle_dir.mkdir(parents=True, exist_ok=True)
            for index, item in enumerate(selected_subtitles, start=1):
                progress(
                    0.35 + 0.3 * ((index - 1) / max(1, len(selected_subtitles))),
                    f"Writing standalone subtitle {index} of {len(selected_subtitles)}",
                )
                track_name, language, title, _default = track_details(item)
                _subtitle_record, subtitle_path = context._resolve_input(item.id)
                destination = context.artifacts.next_available_path(
                    subtitle_dir / f"{export_name}_{track_name}.srt"
                )
                finalize_srt_file(subtitle_path, destination, {**settings, "subtitle_language": language})
                produced.append(
                    context.artifacts.register(
                        destination,
                        kind="srt",
                        role=f"export_subtitle_{track_name}",
                        session_id=session_id,
                        parent_ids=[item.id],
                        settings=settings,
                        metadata={
                            "language": language,
                            "title": title,
                            "source_role": (item.metadata_json or {}).get(
                                "source_role"
                            )
                            or item.role,
                        },
                    )
                )
                progress(
                    0.35 + 0.3 * (index / len(selected_subtitles)),
                    f"Exported standalone subtitle {index} of {len(selected_subtitles)}",
                )
            if upload_audio and audio_mode == "source":
                progress(0.7, "Copying source audio")
                _source_record, source_audio_path = context._resolve_input(
                    upload_audio.id
                )
                audio_dir.mkdir(parents=True, exist_ok=True)
                destination = context.artifacts.next_available_path(
                    audio_dir / f"{export_name}{source_audio_path.suffix.lower()}"
                )
                shutil.copy2(source_audio_path, destination)
                produced.append(
                    context.artifacts.register(
                        destination,
                        kind="export",
                        role="export_source_audio",
                        session_id=session_id,
                        parent_ids=[upload_audio.id],
                        settings=settings,
                    )
                )
            elif dubbing_audio and audio_mode != "source":
                _artifact, item_path = context._resolve_input(dubbing_audio.id)
                if upload_audio and audio_mode == "mixed":
                    _source_record, source_audio_path = context._resolve_input(
                        upload_audio.id
                    )
                    output_format = str(settings.get("format") or "wav").lower()
                    if output_format not in {"wav", "mp3", "opus", "flac"}:
                        output_format = "wav"
                    audio_dir.mkdir(parents=True, exist_ok=True)
                    destination = context.artifacts.next_available_path(
                        audio_dir / f"{export_name}_mixed.{output_format}"
                    )
                    ffmpeg_executable = str(
                        os.environ.get("PANDRATOR_FFMPEG_EXE")
                        or shutil.which("ffmpeg")
                        or "ffmpeg"
                    )
                    command = build_mix_audio_command(
                        str(source_audio_path),
                        str(item_path),
                        str(destination),
                        source_gain_db=settings.get("mix_source_gain_db", 0.0),
                        voice_gain_db=settings.get("mix_voice_gain_db", 0.0),
                        voice_lufs=settings.get("mix_voice_lufs", -16.0),
                        ducking=str(settings.get("mix_ducking") or "strong"),
                        attack_ms=settings.get("mix_attack_ms", 25),
                        release_ms=settings.get("mix_release_ms", 350),
                        ffmpeg_executable=ffmpeg_executable,
                    )
                    progress(0.7, "Mixing standalone audio")
                    subprocess.run(
                        command, check=True, capture_output=True, text=True
                    )
                    parents = [upload_audio.id, dubbing_audio.id]
                    role = "export_mixed_audio"
                else:
                    progress(0.7, "Copying generated speech audio")
                    audio_dir.mkdir(parents=True, exist_ok=True)
                    destination = context.artifacts.next_available_path(
                        audio_dir / f"{export_name}{item_path.suffix.lower()}"
                    )
                    shutil.copy2(item_path, destination)
                    parents = [dubbing_audio.id]
                    role = f"export_{dubbing_audio.role}"
                produced.append(
                    context.artifacts.register(
                        destination,
                        kind="export",
                        role=role,
                        session_id=session_id,
                        parent_ids=parents,
                        settings=settings,
                    )
                )
                progress(0.92, "Standalone audio export ready")
            if not produced:
                raise ValueError(
                    "No subtitle or audio artifact is available to export."
                )
    with context.database.session() as session:
        for produced_artifact in produced:
            managed = session.get(Artifact, produced_artifact.id)
            if managed is None:
                continue
            managed.metadata_json = {
                **dict(managed.metadata_json or {}),
                "output_settings": output_settings_snapshot,
            }
    progress(
        0.98,
        f"Registered {len(produced)} export artifact{'s' if len(produced) != 1 else ''}",
    )
    progress(1.0, "Export ready")
    return {
        "artifact_ids": [item.id for item in produced],
        "paths": [item.relative_path for item in produced],
    }