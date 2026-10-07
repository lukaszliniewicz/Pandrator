"""Export frozen audiobook takes using their exact assembled PCM timeline."""

from __future__ import annotations

import hashlib
import json
import shutil
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any, Callable

from sqlalchemy import select

from pandrator.logic.book_timing import (
    AlignedBookText,
    BookWord,
    align_book_text,
    alignment_identity,
    map_original_text,
)

from .artifacts import sha256_file
from .export_inputs import ExportInputs
from .export_publication import publish_video_export
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    new_id,
)
from .workflow_output_context import OutputWorkflowContext

CACHE_VERSION = 1


@dataclass(frozen=True)
class _FrozenTake:
    manifest: dict[str, Any]
    artifact: Artifact
    path: Path
    original: str
    spoken: str
    frames: int
    rate: int

    @property
    def duration_ms(self) -> int:
        return round(self.frames * 1000 / self.rate)


def _cancel(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("Book export was canceled.")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _pcm(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            if source.getcomptype() != "NONE" or source.getnframes() <= 0:
                raise ValueError("Book export requires nonempty PCM WAV audio.")
            return source.getnframes(), source.getframerate()
    except (wave.Error, EOFError) as error:
        raise ValueError("Book export requires PCM WAV audio.") from error


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(f"Book timeline {name} must be an integer.")
    return value


def _verified_path(context: OutputWorkflowContext, artifact: Artifact) -> Path:
    path = context.paths.managed_path(artifact.relative_path)
    if not artifact.content_hash or sha256_file(path) != artifact.content_hash:
        raise ValueError(f"Book audio content hash changed: {artifact.id}.")
    return path


def _load(
    context: OutputWorkflowContext,
    inputs: ExportInputs,
    audio: Artifact,
    preview_window: tuple[int, int] | None = None,
) -> tuple[list[_FrozenTake], list[Artifact], Path, int, int]:
    metadata = audio.metadata_json or {}
    timeline = metadata.get("audio_timeline")
    manifest = metadata.get("takes")
    if not isinstance(timeline, dict) or timeline.get("version") != 1:
        raise ValueError(
            "Book export requires a version 1 PCM assembly timeline; reassemble the audio."
        )
    rate = _integer(timeline.get("sample_rate_hz"), "sample_rate_hz", positive=True)
    frames = _integer(timeline.get("total_frames"), "total_frames", positive=True)
    if (
        not isinstance(manifest, list)
        or not manifest
        or any(not isinstance(row, dict) for row in manifest)
    ):
        raise ValueError("Book export requires a frozen take manifest.")
    take_ids = [str(row.get("take_id") or "") for row in manifest]
    if not all(take_ids) or len(set(take_ids)) != len(take_ids):
        raise ValueError("Book assembly take IDs are missing or duplicated.")
    artifact_ids = [str(row.get("artifact_id") or "") for row in manifest]
    with context.database.session() as session:
        takes = {
            take.id: take
            for take in session.scalars(
                select(AudioTake)
                .join(GenerationSegment, GenerationSegment.id == AudioTake.generation_segment_id)
                .join(
                    GenerationPlanRevision,
                    GenerationPlanRevision.id == GenerationSegment.plan_revision_id,
                )
                .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
                .where(AudioTake.id.in_(take_ids), GenerationPlan.session_id == inputs.session_id)
            ).all()
        }
        artifacts = {
            item.id: item
            for item in session.scalars(
                select(Artifact).where(Artifact.id.in_([audio.id, *artifact_ids]))
            ).all()
        }
        caches = list(
            session.scalars(
                select(Artifact)
                .where(
                    Artifact.session_id == inputs.session_id,
                    Artifact.role == "speech_timing",
                    Artifact.state == "current",
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            ).all()
        )
    selected_audio = artifacts.get(audio.id)
    if (
        selected_audio is None
        or selected_audio.session_id != inputs.session_id
        or selected_audio.state != "current"
    ):
        raise ValueError("Book assembly is unavailable or belongs to another session.")
    if (
        selected_audio.content_hash != audio.content_hash
        or selected_audio.metadata_json != metadata
    ):
        raise ValueError("Book assembly changed after selection.")
    audio_path = _verified_path(context, selected_audio)
    if _pcm(audio_path) != (frames, rate):
        raise ValueError("Book assembly PCM does not match its frozen frame timeline.")
    frozen = []
    previous_end = 0
    for row in manifest:
        take = takes.get(str(row["take_id"]))
        artifact = artifacts.get(str(row.get("artifact_id") or ""))
        if (
            take is None
            or artifact is None
            or take.status != "completed"
            or take.revision != row.get("take_revision")
            or take.artifact_id != artifact.id
            or take.generation_segment_id != row.get("segment_id")
            or take.kind != row.get("kind")
            or artifact.session_id != inputs.session_id
            or artifact.state != "current"
        ):
            raise ValueError(f"Frozen book take is unavailable or changed: {row['take_id']}.")
        _integer(row.get("segment_revision"), "segment_revision", positive=True)
        _integer(row.get("take_revision"), "take_revision", positive=True)
        start = _integer(row.get("start_frame"), "start_frame")
        end = _integer(row.get("end_frame"), "end_frame", positive=True)
        if start < previous_end or end <= start or end > frames:
            raise ValueError("Book take frame ranges overlap or exceed assembly audio.")
        previous_end = end
        if preview_window is not None:
            preview_start, preview_end = preview_window
            if start * 1000 >= preview_end * rate or end * 1000 <= preview_start * rate:
                continue
        path = _verified_path(context, artifact)
        take_frames, take_rate = _pcm(path)
        if abs((end - start) * 1000 / rate - take_frames * 1000 / take_rate) > 1:
            raise ValueError("Book take PCM duration does not match its assembled frame bounds.")
        original = (artifact.metadata_json or {}).get("source_text")
        spoken = (artifact.metadata_json or {}).get("synthesized_text")
        if (
            not isinstance(original, str)
            or not original.strip()
            or not isinstance(spoken, str)
            or not spoken.strip()
        ):
            raise ValueError(f"Book take has no frozen source/spoken transcript: {take.id}.")
        frozen.append(
            _FrozenTake(dict(row), artifact, path, original, spoken, take_frames, take_rate)
        )
    return frozen, caches, audio_path, frames, rate


def _validated(value: Any, text: str, duration_ms: int) -> AlignedBookText:
    aligned = AlignedBookText.from_dict(value)
    if aligned.text != text:
        raise ValueError("Timing transcript does not match the frozen spoken transcript.")
    recorded = aligned.diagnostics.get("duration_ms")
    if recorded is not None and abs(recorded - duration_ms) > 1:
        raise ValueError("Timing duration does not match the take PCM.")
    if any(word.end_ms > duration_ms + 1 for word in aligned.words):
        raise ValueError("Timing exceeds the take PCM.")
    return replace(
        aligned,
        diagnostics={
            **aligned.diagnostics,
            "duration_ms": max(duration_ms, aligned.words[-1].end_ms),
        },
    )


def _native(take: _FrozenTake) -> tuple[AlignedBookText, str] | None:
    metadata = take.artifact.metadata_json or {}
    timing = metadata.get("speech_timing")
    if isinstance(timing, dict):
        try:
            return _validated(timing, take.spoken, take.duration_ms), _digest(timing)
        except (ValueError, TypeError):
            pass
    parts = metadata.get("render_parts")
    if not isinstance(parts, list) or not parts:
        return None
    words = []
    previous_end = previous_char = 0
    try:
        for part in parts:
            if not isinstance(part, dict):
                return None
            bounds = part.get("range")
            if isinstance(bounds, dict):
                bounds = [bounds.get("start"), bounds.get("end")]
            if not isinstance(bounds, list) or len(bounds) != 2:
                return None
            left = _integer(bounds[0], "part start_char")
            right = _integer(bounds[1], "part end_char", positive=True)
            start = _integer(part.get("start_frame"), "part start_frame")
            end = _integer(part.get("end_frame"), "part end_frame", positive=True)
            if (
                part.get("sample_rate_hz") != take.rate
                or (not words and start != 0)
                or left < previous_char
                or right <= left
                or right > len(take.spoken)
                or start < previous_end
                or end <= start
                or end > take.frames
            ):
                return None
            aligned = _validated(
                part.get("speech_timing"),
                take.spoken[left:right],
                round((end - start) * 1000 / take.rate),
            )
            for word in aligned.words:
                words.append(
                    BookWord(
                        word.text,
                        round(start * 1000 / take.rate + word.start_ms),
                        round(start * 1000 / take.rate + word.end_ms),
                        left + word.start_char,
                        left + word.end_char,
                    )
                )
            previous_end, previous_char = end, right
        if previous_end != take.frames:
            return None
        combined = AlignedBookText(
            take.spoken,
            tuple(words),
            "native_parts",
            {"duration_ms": take.duration_ms, "native_parts": len(parts)},
        )
        return combined, _digest(parts)
    except (ValueError, TypeError, KeyError):
        return None


def _write_json(
    context: OutputWorkflowContext,
    path: Path,
    value: dict,
    *,
    role: str,
    session_id: str,
    parents: list[str],
    metadata: dict,
    cancel_event: Event,
) -> Artifact:
    registered = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        _cancel(cancel_event)
        artifact = context.artifacts.register(
            path,
            kind="json",
            role=role,
            session_id=session_id,
            parent_ids=parents,
            metadata=metadata,
        )
        registered = True
        return artifact
    finally:
        if not registered:
            path.unlink(missing_ok=True)


def _cache_key(take: _FrozenTake, identity: dict) -> str:
    return _digest(
        {
            "version": CACHE_VERSION,
            "audio_hash": take.artifact.content_hash,
            "transcript_hash": hashlib.sha256(take.spoken.encode()).hexdigest(),
            "identity": identity,
        }
    )


def _timed(
    context: OutputWorkflowContext,
    take: _FrozenTake,
    settings: dict,
    cache_inventory: list[Artifact],
    work_dir: Path,
    cancel_event: Event,
    counters: dict,
) -> tuple[AlignedBookText, Artifact]:
    native = _native(take) if settings.get("book_use_native_timings", True) else None
    identity = (
        {"native_source_hash": native[1]} if native else alignment_identity(settings, take.spoken)
    )
    key = _cache_key(take, identity)

    for cached in cache_inventory:
        if (cached.metadata_json or {}).get("cache_key") != key:
            continue
        try:
            path = context.paths.managed_path(cached.relative_path)
            if not cached.content_hash or sha256_file(path) != cached.content_hash:
                continue
            aligned = _validated(
                json.loads(path.read_text(encoding="utf-8")), take.spoken, take.duration_ms
            )
        except (OSError, ValueError, TypeError, KeyError):
            continue
        counters["cache_hits"] += 1
        return aligned, cached
    _cancel(cancel_event)
    if native:
        aligned = native[0]
        counters["native"] += 1
    else:
        aligned = _validated(
            align_book_text(take.path, take.spoken, settings, work_dir, cancel_event).as_dict(),
            take.spoken,
            take.duration_ms,
        )
        identity = alignment_identity(settings, take.spoken)
        key = _cache_key(take, identity)
        counters["aligned"] += 1
    cached = _write_json(
        context,
        context._session_dir(str(take.artifact.session_id)) / "timings" / f"{new_id()}.json",
        aligned.as_dict(),
        role="speech_timing",
        session_id=str(take.artifact.session_id),
        parents=[take.artifact.id],
        metadata={
            "cache_key": key,
            "version": CACHE_VERSION,
            "take_id": take.manifest["take_id"],
            "identity": identity,
        },
        cancel_event=cancel_event,
    )
    return aligned, cached


def export_book(
    context: OutputWorkflowContext,
    inputs: ExportInputs,
    audio: Artifact,
    payload: dict,
    progress: Callable,
    cancel_event: Event,
) -> dict:
    """Publish synchronized book subtitles and optional video without changing audio."""
    from werkzeug.utils import secure_filename

    from pandrator.logic.book_cues import (
        BookPassage,
        clip_book_cues,
        compose_book_cues,
        cues_as_dict,
        write_book_subtitles,
    )

    _cancel(cancel_event)
    settings = dict(inputs.settings)
    mode = settings.get("export_mode")
    if mode not in {"video_book", "subtitles"}:
        raise ValueError("Book export_mode must be video_book or subtitles.")
    cue_mode = settings.get("book_cue_mode", "passages")
    text_mode = settings.get("book_text_mode", "auto")
    if cue_mode not in {"passages", "segments"} or text_mode not in {"auto", "original", "spoken"}:
        raise ValueError("Invalid book cue or text mode.")
    language = str(
        settings.get("language")
        or inputs.record.target_language
        or inputs.record.source_language
        or "und"
    )
    settings["language"] = language
    timeline = (audio.metadata_json or {}).get("audio_timeline")
    if not isinstance(timeline, dict) or timeline.get("version") != 1:
        raise ValueError(
            "Book export requires a version 1 PCM assembly timeline; reassemble the audio."
        )
    expected_rate = _integer(timeline.get("sample_rate_hz"), "sample_rate_hz", positive=True)
    expected_frames = _integer(timeline.get("total_frames"), "total_frames", positive=True)
    total_ms = round(expected_frames * 1000 / expected_rate)
    preview = bool(settings.get("book_preview", False))
    start_ms = round(float(settings.get("book_preview_start_seconds", 0)) * 1000) if preview else 0
    if preview and (start_ms < 0 or start_ms >= total_ms):
        raise ValueError("Book preview start is outside the assembled audio.")
    duration_ms = (
        min(30000, round(float(settings.get("book_preview_duration_seconds", 25)) * 1000))
        if preview
        else total_ms
    )
    if preview and duration_ms <= 0:
        raise ValueError("Book preview duration must be positive.")
    end_ms = min(total_ms, start_ms + duration_ms)
    selected, cache_inventory, audio_path, total_frames, rate = _load(
        context,
        inputs,
        audio,
        (start_ms, end_ms) if preview else None,
    )
    if round(total_frames * 1000 / rate) != total_ms:
        raise ValueError("Book assembly duration changed after preview selection.")
    counters: dict[str, Any] = {
        "cache_hits": 0,
        "aligned": 0,
        "native": 0,
        "original_mapping_fallback_count": 0,
        "original_mapping_fallback_take_ids": [],
        "takes": [],
        "warnings": [],
    }
    work_dir = context._operation_dir(inputs.session_id, f"book-export-{new_id()}")
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        passages = []
        used_caches: list[str] = []
        chapters = sorted(
            (audio.metadata_json or {}).get("chapters") or [],
            key=lambda chapter: chapter["start_ms"],
        )
        for index, take in enumerate(selected):
            _cancel(cancel_event)
            if cue_mode == "segments":
                text = take.spoken if text_mode == "spoken" else take.original
                aligned = AlignedBookText(
                    text,
                    (BookWord(text, 0, take.duration_ms, 0, len(text)),),
                    "whole_segment",
                    {"duration_ms": take.duration_ms},
                )
            else:
                aligned, cached = _timed(
                    context,
                    take,
                    settings,
                    cache_inventory,
                    work_dir / take.manifest["take_id"],
                    cancel_event,
                    counters,
                )
                used_caches.append(cached.id)
                if text_mode != "spoken":
                    mapped = map_original_text(aligned, take.original)
                    if mapped is None:
                        if text_mode == "original":
                            raise ValueError(
                                f"Original wording cannot be mapped reliably to the audio for take {take.manifest['take_id']}. Choose whole segments to retain the original, or use spoken wording and review the changes."
                            )
                        counters["original_mapping_fallback_take_ids"].append(
                            take.manifest["take_id"]
                        )
                        counters["original_mapping_fallback_count"] += 1
                    else:
                        aligned = mapped
            offset = take.manifest["start_frame"] * 1000 / rate
            bound = take.manifest["end_frame"] * 1000 / rate
            words = (
                (BookWord(aligned.text, round(offset), round(bound), 0, len(aligned.text)),)
                if cue_mode == "segments"
                else tuple(
                    BookWord(
                        word.text,
                        round(offset + word.start_ms),
                        round(offset + word.end_ms),
                        word.start_char,
                        word.end_char,
                    )
                    for word in aligned.words
                )
            )
            if any(word.end_ms > bound + 1 or word.end_ms > total_ms + 1 for word in words):
                raise ValueError("Rebased book timing exceeds assembled audio.")
            heading = ""
            for chapter in chapters:
                if chapter["start_ms"] <= offset:
                    heading = str(chapter.get("title") or "")
            passages.append(BookPassage(aligned.text, words, round(offset), round(bound), heading))
            counters["takes"].append(
                {
                    "take_id": take.manifest["take_id"],
                    "engine": aligned.engine,
                    "mapping_method": aligned.diagnostics.get("mapping_type", "identity"),
                    "diagnostics": aligned.diagnostics,
                }
            )
            progress(0.1 + 0.55 * (index + 1) / max(1, len(selected)), "Preparing book timing")
        if counters["original_mapping_fallback_count"]:
            counters["warnings"].append(
                {
                    "severity": "WARNING",
                    "code": "original_mapping_fallback",
                    "count": counters["original_mapping_fallback_count"],
                    "take_ids": counters["original_mapping_fallback_take_ids"],
                }
            )
        layout = None
        if mode == "video_book":
            from pandrator.logic.book_video import prepare_book_layout

            layout = prepare_book_layout(
                settings, "\n".join(passage.text for passage in passages), language
            )
        cues = compose_book_cues(
            passages,
            settings,
            total_duration_ms=total_ms,
            fit_lines=layout.fit_lines if layout is not None else None,
        )
        if preview:
            cues = clip_book_cues(cues, start_ms, end_ms)
        if not cues:
            raise ValueError("Book export range contains no display cues.")
        cue_artifact = _write_json(
            context,
            context._session_dir(inputs.session_id) / "timings" / f"book-cues-{new_id()}.json",
            {"version": 1, "cues": cues_as_dict(cues)},
            role="book_cues",
            session_id=inputs.session_id,
            parents=[audio.id, *dict.fromkeys(used_caches)],
            metadata={
                "timings": used_caches,
                "diagnostics": counters,
                "output_settings": inputs.output_settings_snapshot,
                "layout": layout.as_dict() if layout is not None else None,
            },
            cancel_event=cancel_event,
        )
        metadata = {
            "export_mode": mode,
            "book_export": True,
            "preview": preview,
            "duration_ms": end_ms - start_ms,
            "book_timing_diagnostics": counters,
            "output_settings": inputs.output_settings_snapshot,
            "cue_artifact_id": cue_artifact.id,
        }
        parents = [audio.id, cue_artifact.id, *dict.fromkeys(used_caches)]
        name = secure_filename(inputs.record.name) or inputs.record.storage_key
        if preview:
            name += "-preview"
        subtitle_format = str(settings.get("subtitle_format") or "srt")
        if subtitle_format not in {"srt", "vtt"}:
            raise ValueError("Book subtitle_format must be srt or vtt.")
        output_root = context._session_dir(inputs.session_id) / "exports"
        subtitle_dir = output_root / "subtitles"
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        scratch_subtitle = work_dir / f"book.{subtitle_format}"
        write_book_subtitles(cues, scratch_subtitle, subtitle_format)
        rendered_video = work_dir / "book.mp4"
        if mode == "video_book":
            from pandrator.logic.book_video import render_book_video

            assert layout is not None
            render_book_video(
                audio_path,
                cues,
                rendered_video,
                settings,
                layout=layout,
                title=str(settings.get("title") or inputs.record.name),
                start_ms=start_ms,
                duration_ms=end_ms - start_ms,
                progress=progress,
                cancel_event=cancel_event,
            )
        publish_options = {
            "session_id": inputs.session_id,
            "parent_ids": parents,
            "settings": settings,
            "metadata": metadata,
            "cancel_event": cancel_event,
            "job_id": payload.get("_job_id"),
            "lease_generation": payload.get("_lease_generation"),
        }
        produced = [
            publish_video_export(
                context,
                scratch_subtitle,
                subtitle_dir / f"{name}.{subtitle_format}",
                **publish_options,
            )
        ]
        if mode == "video_book":
            video_dir = output_root / "video"
            video_dir.mkdir(parents=True, exist_ok=True)
            produced.append(
                publish_video_export(
                    context, rendered_video, video_dir / f"{name}.mp4", **publish_options
                )
            )
        progress(1.0, "Book export ready")
        return {
            "artifact_ids": [item.id for item in produced],
            "paths": [item.relative_path for item in produced],
            "subtitle_diagnostics": [],
            "book_timing_diagnostics": counters,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
