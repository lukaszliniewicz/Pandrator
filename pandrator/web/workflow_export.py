"""Export worker workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .export_inputs import resolve_export_inputs, select_media_export
from .export_subtitles import subtitle_track_details
from .export_video import render_video_export
from .models import Artifact, new_id
from .workflow_output_context import OutputWorkflowContext


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
    from pandrator.logic.dubbing.srt_utils import (
        concatenate_subtitle_text,
        srt_to_vtt,
    )
    from pandrator.logic.dubbing.subtitle_finalization import finalize_srt_file

    inputs = resolve_export_inputs(context, payload)
    session_id = inputs.session_id
    settings = inputs.settings
    record = inputs.record
    output_settings_snapshot = inputs.output_settings_snapshot
    by_role = inputs.by_role
    selected_audio = inputs.selected_audio
    output_dir = context._session_dir(session_id) / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    # New exports are grouped by kind so the Output tab stays readable.
    # Legacy files keep their recorded flat paths; only destinations
    # allocated below use these subfolders.
    audio_dir = output_dir / "audio"
    subtitle_dir = output_dir / "subtitles"
    export_name = secure_filename(record.name) or record.storage_key
    progress(0.1, "Preparing export")
    produced: list[Artifact] = []

    if record.workflow_kind == "audiobook":
        audio = (
            selected_audio
            if selected_audio is not None
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
        media_selection = select_media_export(inputs)
        upload_media = media_selection.upload_media
        upload_audio = media_selection.upload_audio
        selected_subtitles = media_selection.selected_subtitles
        dubbing_audio = media_selection.dubbing_audio
        export_mode = media_selection.export_mode
        subtitle_format = media_selection.subtitle_format
        audio_mode = media_selection.audio_mode
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
            return scratch


        def track_details(item: Artifact) -> tuple[str, str, str, bool]:
            return subtitle_track_details(
                item, record=record, settings=settings, track_count=len(selected_subtitles)
            )

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
            produced.append(render_video_export(
                context, inputs, media_selection, output_dir=output_dir,
                export_name=export_name, progress=progress, cancel_event=cancel_event,
            ))
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
