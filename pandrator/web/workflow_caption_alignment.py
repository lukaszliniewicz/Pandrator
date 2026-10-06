"""Authoritative attached-caption alignment with explicit worker dependencies."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pandrator.logic.dubbing.transcript_normalization import load_transcript

from .credentials import redact_inline_secrets
from .workflow_text_protocols import StoreSrtDocumentProtocol, StoreTimedWordsProtocol

if TYPE_CHECKING:
    from .artifacts import ArtifactService
    from .models import Artifact


@dataclass(frozen=True, slots=True)
class CaptionAlignmentContext:
    artifacts: ArtifactService
    _operation_dir: Callable[[str, str], Path]
    _store_srt_document: StoreSrtDocumentProtocol
    _store_timed_words: StoreTimedWordsProtocol


def transcribe_media_edit_with_ctc(
    context: CaptionAlignmentContext,
    *,
    session_id: str,
    source_artifact: Artifact,
    source_path: Path,
    caption_artifact: Artifact,
    submitted_settings: dict[str, Any],
    runtime_settings: dict[str, Any],
    ffmpeg_executable: str,
    crispasr_executable: str,
    progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Align authoritative attached captions without whole-recording ASR."""

    from pandrator.logic.cancellable_process import ProcessCancelled
    from pandrator.logic.dubbing import crispasr, qwen_alignment
    from pandrator.logic.dubbing.caption_alignment import (
        CaptionAlignmentError,
        align_caption_cues,
        normalize_alignment_settings,
        parse_vad_export,
        validate_normalized_wav,
        write_diagnostics,
    )
    from pandrator.logic.dubbing.crispasr import CrispASRError, run_vad_export
    from pandrator.logic.dubbing.transcription import (
        apply_vocal_isolation,
        extract_audio,
    )
    from pandrator.logic.media_edit import (
        MediaWord,
        align_cues_to_words,
        caption_to_srt,
        media_cues_to_transcript,
        parse_caption_text,
    )

    options = normalize_alignment_settings(runtime_settings)
    # Runtime hydration may contain resolved provider credentials for an
    # optional ASR fallback. Those values are execution-only and must
    # never enter artifact settings or their settings hash.
    persisted_settings = redact_inline_secrets(normalize_alignment_settings(submitted_settings))
    operation_dir = context._operation_dir(session_id, "transcribe")
    progress(0.06, "Normalizing source audio")
    if cancel_event.is_set():
        raise ProcessCancelled("Caption alignment was canceled.")
    normalized_path = Path(
        extract_audio(
            source_path,
            operation_dir,
            source_path.stem,
            ffmpeg_executable=ffmpeg_executable,
            cancel_event=cancel_event,
        )
    )
    duration_ms = validate_normalized_wav(normalized_path)
    progress(0.18, "Source audio normalized")
    # Attached-caption transcription settings may request vocal isolation.
    # Alignment evidence (VAD + CTC/Qwen) uses the isolated derivative;
    # the normalized original stays the export source and the ASR-fallback
    # leg below re-applies isolation internally from that original.
    alignment_audio_path, isolation_provenance = apply_vocal_isolation(
        normalized_path,
        operation_dir,
        normalized_path.stem,
        options,
        cancel_event=cancel_event,
        progress=progress,
        ffmpeg_executable=ffmpeg_executable,
    )
    isolation_record: dict[str, Any] | None = None
    if isolation_provenance is not None:
        duration_ms = validate_normalized_wav(alignment_audio_path)
        isolation_record = {
            "transcription_vocal_isolation": isolation_provenance.get("method"),
            "vocal_isolation_model": isolation_provenance.get("model"),
            "vocal_isolation_status": isolation_provenance.get("status"),
            "original_audio_retained": normalized_path.name,
        }
        for key in (
            "model_id",
            "family",
            "cli_family",
            "revision",
            "sha256",
            "size_bytes",
            "method",
            "status",
            "backend",
            "compute_backend",
            "requested_backend",
            "threads",
        ):
            if key in isolation_provenance:
                isolation_record[key] = deepcopy(isolation_provenance[key])
    _caption_record, caption_path = context.artifacts.resolve(caption_artifact.id)
    cues = parse_caption_text(caption_path.read_text(encoding="utf-8-sig"))
    # Caption alignment owns its VAD policy.  It must remain enabled (or
    # disabled) according to the CrispASR VAD setting even when the
    # configured whole-recording STT engine is cloud/MOSS/etc.
    vad_enabled = bool(options.get("crispasr_vad_enabled", True))
    vad_path: Path | None = None
    vad_spans = None
    vad_options = {
        key: options.get(key)
        for key in (
            "crispasr_vad_model",
            "crispasr_vad_threshold",
            "crispasr_vad_min_speech_ms",
            "crispasr_vad_min_silence_ms",
            "crispasr_vad_max_speech_seconds",
            "crispasr_vad_speech_pad_ms",
        )
    }
    if vad_enabled:
        progress(0.22, "Detecting speech")
        if cancel_event.is_set():
            raise ProcessCancelled("Caption alignment was canceled.")
        vad_path = operation_dir / "vad-segments.json"
        try:
            vad_path = run_vad_export(
                alignment_audio_path,
                vad_path,
                options,
                executable=crispasr_executable,
                cancel_event=cancel_event,
            )
            vad_spans = parse_vad_export(vad_path, duration_ms=duration_ms)
        except (CaptionAlignmentError, CrispASRError, OSError, ValueError) as error:
            raise ValueError(f"CrispASR VAD evidence failed: {error}") from error
        if cancel_event.is_set():
            raise ProcessCancelled("Caption alignment was canceled.")
    else:
        progress(0.22, "Speech detection disabled; validating cue timing")
        vad_path = operation_dir / "vad-segments.json"
        vad_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "vad_segments",
                    "sample_rate": 16000,
                    "enabled": False,
                    "segments": [],
                    "crispasr_vad": {
                        "version": 1,
                        "kind": "vad_segments",
                        "sample_rate": 16000,
                        "num_slices": 0,
                        "slices": [],
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def ctc_runner(clip, text, output, run_settings, event):
        return crispasr.run_ctc_alignment(
            clip,
            text,
            output,
            run_settings,
            executable=crispasr_executable,
            cancel_event=event,
        )

    try:
        alignment = align_caption_cues(
            alignment_audio_path,
            cues,
            options,
            vad_spans=vad_spans,
            ctc_runner=None
            if qwen_alignment.uses_qwen(options, " ".join(cue.text for cue in cues))
            else ctc_runner,
            cancel_event=cancel_event,
            progress=lambda value, detail: progress(0.24 + 0.52 * value, detail),
        )
    except ProcessCancelled:
        raise
    except (CaptionAlignmentError, OSError, ValueError, TypeError) as error:
        raise ValueError(f"Caption CTC alignment failed: {error}") from error
    aligned_cues = alignment.cues
    diagnostics = alignment.diagnostics
    diagnostics.vad_model = str(options.get("crispasr_vad_model") or "silero")
    diagnostics.vad_options = vad_options
    output_language = alignment.resolved_language
    output_language_resolution = alignment.language_resolution
    routing: dict[str, Any] = {}
    requested_language = str(
        submitted_settings.get("original_language")
        or submitted_settings.get("stt_language")
        or "auto"
    )

    raw_asr_srt_artifact = None
    raw_asr_words_artifact = None
    if (
        options["caption_alignment_method"] == "ctc_asr_fallback"
        and diagnostics.eligible_alignment_coverage < options["caption_alignment_fallback_coverage"]
    ):
        progress(0.78, "Running ASR fallback for rejected cues")
        if cancel_event.is_set():
            raise ProcessCancelled("Caption alignment was canceled.")
        fallback_dir = operation_dir / "fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        from pandrator.logic.dubbing.transcription import (
            transcribe_source_file_with_metadata,
        )

        fallback_result = transcribe_source_file_with_metadata(
            fallback_dir,
            normalized_path,
            options,
            ffmpeg_executable=ffmpeg_executable,
            crispasr_executable=crispasr_executable,
            cancel_event=cancel_event,
            source_is_normalized=True,
        )
        if cancel_event.is_set():
            raise ProcessCancelled("Caption alignment was canceled.")
        fallback_language = str(getattr(fallback_result, "resolved_language", "") or "")
        if fallback_language.lower() not in {"", "auto", "und", "unknown"}:
            output_language = fallback_language
        routing = deepcopy(dict(getattr(fallback_result, "routing", {}) or {}))
        if fallback_language.lower() not in {"", "auto", "und", "unknown"}:
            output_language_resolution = {
                "requested_language": requested_language,
                "resolved_language": output_language,
                "source": "asr",
                "language_source": routing.get("language_source", "unknown"),
                "is_detection_proof": routing.get("language_source") == "detected",
            }
        fallback_isolation = None
        fallback_payload: dict[str, Any] = {}
        fallback_words_path = Path(fallback_result.word_timestamps_path)
        try:
            fallback_payload = json.loads(fallback_words_path.read_text(encoding="utf-8"))
            fallback_isolation = (fallback_payload.get("metadata") or {}).get("vocal_isolation")
            if isinstance(fallback_isolation, dict) and fallback_isolation.get(
                "original_audio_retained"
            ):
                fallback_isolation["original_audio_retained"] = Path(
                    str(fallback_isolation["original_audio_retained"]).replace("\\", "/")
                ).name
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if fallback_payload:
            payload_metadata = fallback_payload.get("metadata")
            if not isinstance(payload_metadata, dict):
                payload_metadata = {}
                fallback_payload["metadata"] = payload_metadata
            payload_metadata["stt_routing"] = deepcopy(routing)
            payload_metadata["requested_language"] = requested_language
            fallback_words_path.write_text(
                json.dumps(fallback_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        fallback_metadata = {
            "alignment_role": "evidence",
            "source_artifact_id": source_artifact.id,
            "alignment_method": "ctc_asr_fallback",
            "engine": fallback_result.engine,
            "model": fallback_result.engine,
            "compute_backend": fallback_result.compute_backend,
            "language": output_language,
            "resolved_language": output_language,
            "requested_language": requested_language,
            "requested_settings": deepcopy(persisted_settings),
            "stt_routing": deepcopy(routing),
        }
        if isolation_record is not None:
            fallback_metadata["vocal_isolation"] = deepcopy(isolation_record)
        raw_asr_srt_artifact = context.artifacts.register(
            Path(fallback_result.srt_path),
            kind="srt",
            role="transcription_evidence",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=persisted_settings,
            metadata=fallback_metadata,
        )
        raw_asr_words_artifact = context.artifacts.register(
            Path(fallback_result.word_timestamps_path),
            kind="json",
            role="recognition_word_timestamps",
            session_id=session_id,
            parent_ids=[source_artifact.id, raw_asr_srt_artifact.id],
            settings=persisted_settings,
            metadata={
                **fallback_metadata,
                "transcription_evidence_artifact_id": raw_asr_srt_artifact.id,
            },
        )
        fallback_transcript = load_transcript(Path(fallback_result.word_timestamps_path))
        asr_words = tuple(
            MediaWord(
                text=word.text,
                start_ms=word.start_ms,
                end_ms=word.end_ms,
                confidence=word.confidence,
            )
            for word in fallback_transcript.words
        )
        projected = align_cues_to_words(
            cues, asr_words, padding_ms=options["caption_alignment_padding_ms"]
        )
        replaced = 0
        replaced_tokens = 0
        updated: list[Any] = []
        accepted_ids = {cue.id for cue in aligned_cues if cue.words}
        for ctc_cue, asr_cue in zip(aligned_cues, projected, strict=True):
            if ctc_cue.id in accepted_ids or ctc_cue.start_ms >= duration_ms:
                updated.append(ctc_cue)
                continue
            if asr_cue.words:
                updated.append(asr_cue)
                replaced += 1
                replaced_tokens += len(asr_cue.words)
            else:
                updated.append(ctc_cue)
        aligned_cues = tuple(updated)
        diagnostics.fallback_triggered = True
        diagnostics.fallback_engine = fallback_result.engine
        diagnostics.fallback_filled_cue_count = replaced
        diagnostics.fallback_filled_token_count = replaced_tokens
        diagnostics.method = (
            "qwen3_with_asr_fallback"
            if diagnostics.ctc_engine == "audio.cpp"
            else "ctc_with_asr_fallback"
        )
        diagnostics.timing_quality_basis = (
            f"{diagnostics.timing_quality_basis};asr_lexical_projection_for_ctc_rejections"
        )
        diagnostics.alignment_confidence = sum(
            float(cue.timing_confidence or 0) for cue in aligned_cues
        ) / max(1, len(aligned_cues))
        diagnostics.accepted_cue_count = sum(bool(cue.words) for cue in aligned_cues)
        diagnostics.accepted_token_count = sum(
            len(cue.text.split()) if diagnostics.ctc_engine == "audio.cpp" else len(cue.words)
            for cue in aligned_cues
            if cue.words
        )
        diagnostics.alignment_coverage = diagnostics.accepted_token_count / max(
            1, diagnostics.all_token_count
        )
        diagnostics.eligible_alignment_coverage = diagnostics.accepted_token_count / max(
            1, diagnostics.eligible_token_count
        )

    diagnostics.cue_count = len(aligned_cues)
    diagnostics.word_count = sum(len(cue.words) for cue in aligned_cues)
    final_failed_ids = {cue.id for cue in aligned_cues if not cue.words}
    diagnostics.failed_cue_ids = {
        cue_id: reasons
        for cue_id, reasons in diagnostics.failed_cue_ids.items()
        if cue_id in final_failed_ids
    }
    # Evidence is durable even when the final promotion gate rejects the
    # alignment.  This keeps low-coverage runs inspectable without
    # creating a canonical transcription artifact.
    progress(0.82, "Persisting alignment evidence")
    if cancel_event.is_set():
        raise ProcessCancelled("Caption alignment was canceled.")
    diagnostics_path = operation_dir / "alignment-diagnostics.json"
    write_diagnostics(
        replace(alignment, cues=aligned_cues, diagnostics=diagnostics), diagnostics_path
    )
    if isolation_record is not None:
        diagnostic_payload = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        diagnostic_payload["vocal_isolation"] = deepcopy(isolation_record)
        diagnostics_path.write_text(
            json.dumps(diagnostic_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    evidence_ids = [source_artifact.id, caption_artifact.id]
    vad_artifact = None
    if vad_path is not None:
        vad_artifact = context.artifacts.register(
            vad_path,
            kind="json",
            role="transcription_vad_evidence",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=persisted_settings,
            metadata={
                "alignment_method": diagnostics.method,
                "vad_schema_version": 1,
                "vad_enabled": vad_enabled,
                "duration_ms": duration_ms,
            },
        )
        evidence_ids.append(vad_artifact.id)
    diagnostics_artifact = context.artifacts.register(
        diagnostics_path,
        kind="json",
        role="transcription_alignment_diagnostics",
        session_id=session_id,
        parent_ids=[source_artifact.id, caption_artifact.id]
        + ([vad_artifact.id] if vad_artifact else []),
        settings=persisted_settings,
        metadata={
            **diagnostics.as_dict(),
            "requested_language": requested_language,
            "requested_settings": deepcopy(persisted_settings),
            **({"vocal_isolation": isolation_record} if isolation_record is not None else {}),
        },
    )
    evidence_ids.append(diagnostics_artifact.id)
    for artifact in (raw_asr_srt_artifact, raw_asr_words_artifact):
        if artifact is not None:
            evidence_ids.append(artifact.id)
    if diagnostics.alignment_coverage < 0.5 or diagnostics.eligible_alignment_coverage < 0.5:
        raise ValueError(
            f"Aligned transcription coverage is {diagnostics.alignment_coverage:.6f}, "
            f"eligible coverage is {diagnostics.eligible_alignment_coverage:.6f}; "
            "no aligned transcription was promoted."
        )
    progress(0.86, "Persisting aligned transcription")
    if cancel_event.is_set():
        raise ProcessCancelled("Caption alignment cancelled before publication.")
    aligned_srt_path = operation_dir / "aligned-transcription.srt"
    aligned_json_path = operation_dir / "aligned-word-timestamps.json"
    aligned_srt_path.write_text(caption_to_srt(aligned_cues), encoding="utf-8")
    metadata = {
        **diagnostics.as_dict(),
        "language": output_language,
        "resolved_language": output_language,
        "language_resolution": deepcopy(output_language_resolution),
        "alignment_language_resolution": alignment.language_resolution,
        "requested_language": requested_language,
        "requested_settings": deepcopy(persisted_settings),
        "alignment_method": diagnostics.method,
        "authoritative_transcript_artifact_id": caption_artifact.id,
        "source_artifact_id": source_artifact.id,
        "vad_artifact_id": vad_artifact.id if vad_artifact else None,
        "alignment_diagnostics_artifact_id": diagnostics_artifact.id,
        "raw_asr_srt_artifact_id": raw_asr_srt_artifact.id if raw_asr_srt_artifact else None,
        "raw_asr_word_timestamps_artifact_id": raw_asr_words_artifact.id
        if raw_asr_words_artifact
        else None,
        "evidence_artifact_ids": list(dict.fromkeys(evidence_ids)),
    }
    if routing:
        metadata["stt_routing"] = deepcopy(routing)
    if isolation_record is not None:
        metadata["vocal_isolation"] = isolation_record
    aligned_payload = media_cues_to_transcript(
        aligned_cues,
        language=output_language,
        source_format="media_edit_alignment",
        metadata=metadata,
    )
    aligned_json_path.write_text(
        json.dumps(aligned_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    aligned_srt_artifact = context.artifacts.register(
        aligned_srt_path,
        kind="srt",
        role="transcription_alignment",
        session_id=session_id,
        parent_ids=list(dict.fromkeys(evidence_ids)),
        settings=persisted_settings,
        metadata=metadata,
    )
    aligned_words_artifact = context.artifacts.register(
        aligned_json_path,
        kind="json",
        role="word_timestamps",
        session_id=session_id,
        # Keep the word artifact as a sibling derived from the same
        # evidence.  The promoted SRT points to it directly below; making
        # both artifacts parents of one another would create a lineage
        # cycle because the canonical SRT path is promoted in place.
        parent_ids=list(dict.fromkeys(evidence_ids)),
        settings=persisted_settings,
        metadata=metadata,
    )
    metadata.update(
        {
            "transcription_alignment_artifact_id": aligned_srt_artifact.id,
            "aligned_word_timestamps_artifact_id": aligned_words_artifact.id,
        }
    )
    language = output_language or None
    _document_id, revision_id = context._store_srt_document(
        session_id,
        aligned_srt_artifact,
        "transcription",
        language=language,
        parent_artifact=caption_artifact,
        speaker_overrides={
            index: cue.speaker for index, cue in enumerate(aligned_cues, start=1) if cue.speaker
        },
    )
    stored_word_count = context._store_timed_words(
        revision_id,
        aligned_json_path,
        segment_by_source_cue_id={cue.id: index for index, cue in enumerate(aligned_cues)},
    )
    if cancel_event.is_set():
        raise ProcessCancelled("Caption alignment cancelled before publication.")
    stored_artifact, _ = context.artifacts.resolve(aligned_srt_artifact.id)
    final_artifact = context.artifacts.register(
        aligned_srt_path,
        kind="srt",
        role="transcription",
        session_id=session_id,
        # The SRT alignment artifact is the same managed path that is
        # promoted to the singleton transcription role, so it cannot be
        # its own parent.  Keep a direct lineage edge through the aligned
        # word-timestamps artifact instead.
        parent_ids=list(dict.fromkeys(evidence_ids + [aligned_words_artifact.id])),
        settings=persisted_settings,
        metadata={**metadata, **(stored_artifact.metadata_json or {})},
    )
    progress(1.0, "Aligned transcription ready")
    return {
        "artifact_id": final_artifact.id,
        "path": final_artifact.relative_path,
        "word_timestamps_artifact_id": aligned_words_artifact.id,
        "word_timestamps_path": aligned_words_artifact.relative_path,
        "word_count": stored_word_count,
        "speaker_count": len({cue.speaker.casefold() for cue in aligned_cues if cue.speaker}),
        "revision_id": revision_id,
        **metadata,
        "alignment_coverage": diagnostics.alignment_coverage,
        "eligible_alignment_coverage": diagnostics.eligible_alignment_coverage,
        "alignment_confidence": diagnostics.alignment_confidence,
        "confidence": diagnostics.alignment_confidence,
    }
