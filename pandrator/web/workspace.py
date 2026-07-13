"""Revisioned settings, outcome plans, sources, and generation workspace services."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select

from .database import Database
from .jobs import JobQueue
from .models import (
    AppSetting,
    Artifact,
    AudioTake,
    DocumentRevision,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    GenerationSegmentRevision,
    OutcomePlan,
    OutcomePlanHistory,
    OutputAssembly,
    ResourceClaim,
    Segment,
    SessionRecord,
    SessionSetting,
    SessionSettingHistory,
    SessionSource,
    SourceAsset,
    SourceRecord,
    TimedWord,
    new_id,
    utcnow,
)


SETTING_SECTIONS = (
    "text",
    "stt",
    "subtitles",
    "correction",
    "translation",
    "tts",
    "audio",
    "rvc",
    "source_cleaning",
    "output",
)

BUILTIN_DEFAULTS: dict[str, dict[str, Any]] = {
    "text": {
        "enable_sentence_splitting": True,
        "max_sentence_length": 200,
        "enable_sentence_appending": True,
        "remove_diacritics": False,
        "remove_quotation_marks": False,
        "disable_paragraph_detection": False,
        "remove_footnotes": False,
        "filter_citations": True,
        "enable_nemo_normalization": True,
        "normalize_all_caps": True,
        "llm_tts_optimization": False,
        "llm_processing_enabled": False,
        "llm_concurrent_calls": 1,
        "llm_multi_stage": False,
        "combined_prompt": "",
        "first_prompt": "",
        "second_prompt": "",
        "third_prompt": "",
    },
    "stt": {
        "stt_engine": "whisper",
        "stt_model_quantization": "f16",
        "stt_compute_backend": "auto",
        "stt_language": "auto",
        "stt_compute_device": 0,
        "whisper_prompt": "",
        "stt_threads": 0,
        "stt_chunk_seconds": 0,
        "stt_chunk_overlap_seconds": 3.0,
        "stt_hotwords": "",
        "stt_lid_backend": "whisper",
        "stt_beam_size": 1,
        "parakeet_decoder": "tdt",
        "crispasr_vad_enabled": True,
        "crispasr_vad_model": "silero",
        "crispasr_vad_threshold": 0.5,
        "crispasr_vad_min_speech_ms": 250,
        "crispasr_vad_min_silence_ms": 100,
        "crispasr_vad_speech_pad_ms": 30,
        "crispasr_vad_max_speech_seconds": 300,
        "diarization_enabled": False,
    },
    "subtitles": {
        "max_lines": 2,
        "max_chars_per_line": 48,
        "max_cps": 20.0,
        "min_duration_ms": 833,
        "max_duration_ms": 7000,
        "min_gap_ms": 80,
        "phrase_gap_ms": 600,
        "boundary_correction_enabled": False,
        "merge_threshold_ms": 250,
    },
    "correction": {"enabled": False, "model_name": "", "instructions": "", "preserve_timing": True, "max_subtitles_per_call": 40, "context_before": 2, "context_after": 2, "request_timeout_seconds": 600},
    "translation": {
        "enabled": False,
        "backend": "llm",
        "source_language": "auto",
        "target_language": "en",
        "professional_cleanup": True,
        "model_name": "",
        "instructions": "",
        "glossary": "",
        "max_subtitles_per_call": 40,
        "max_line_length": 0,
        "context_before": 2,
        "context_after": 2,
        "no_remove_subtitles": False,
        "llm_char": 6000,
        "context": True,
        "glossary_enabled": False,
        "request_timeout_seconds": 600,
    },
    "tts": {
        "service": "XTTS",
        "use_external_server": False,
        "external_server_url": "",
        "model": "",
        "language": "en",
        "voice": "",
        "speed": 1.0,
        "max_attempts": 3,
        "temperature": 0.75,
        "length_penalty": 1.0,
        "repetition_penalty": 5.0,
        "top_k": 50,
        "top_p": 0.85,
        "do_sample": True,
        "num_beams": 1,
        "enable_text_splitting": True,
        "stream_chunk_size": 100,
        "gpt_cond_len": 12,
        "gpt_cond_chunk_len": 4,
        "max_ref_len": 12,
        "sound_norm_refs": False,
        "overlap_wav_len": 1024,
        "xtts_send_temperature": False,
        "xtts_send_length_penalty": False,
        "xtts_send_repetition_penalty": False,
        "xtts_send_top_k": False,
        "xtts_send_top_p": False,
        "xtts_send_do_sample": False,
        "xtts_send_num_beams": False,
        "xtts_send_stream_chunk_size": False,
        "xtts_send_enable_text_splitting": False,
        "xtts_send_gpt_cond_len": False,
        "xtts_send_gpt_cond_chunk_len": False,
        "xtts_send_max_ref_len": False,
        "xtts_send_sound_norm_refs": False,
        "xtts_send_overlap_wav_len": False,
        "voxcpm_cfg_value": 1.5,
        "voxcpm_inference_timesteps": 15,
        "voxcpm_normalize": False,
        "voxcpm_denoise": False,
        "voxcpm_retry_badcase": True,
        "voxcpm_retry_badcase_max_times": 3,
        "voxcpm_retry_badcase_ratio_threshold": 6.0,
        "voxcpm_min_len": 2,
        "voxcpm_max_len": 4096,
        "fishs2_temperature": 0.7,
        "fishs2_top_p": 0.7,
        "fishs2_chunk_length": 200,
        "fishs2_latency": "balanced",
        "fishs2_normalize": True,
        "fishs2_prosody_volume": 0,
        "fishs2_normalize_loudness": True,
        "kokoro_default_voices": {"en": "af_heart"},
        "voxtral_max_frames": 1024,
        "voxtral_euler_steps": 8,
        "voxtral_chunk": False,
        "voxtral_max_chunk_chars": 500,
        "voxtral_chunk_silence_ms": 0,
        "voxtral_strip_quotes": False,
        "voxtral_strip_diacritics": False,
        "voxtral_level_audio": False,
        "chatterbox_temperature": 0.8,
        "chatterbox_repetition_penalty": 1.2,
        "chatterbox_min_p": 0.05,
        "chatterbox_top_p": 0.95,
        "chatterbox_top_k": 1000,
        "chatterbox_exaggeration": 0.5,
        "chatterbox_cfg_weight": 0.5,
        "chatterbox_norm_loudness": True,
        "openai_audio_endpoint": "",
        "openai_audio_instructions": "",
        "speech_block_min_chars": 10,
        "speech_block_max_chars": 160,
        "speech_block_merge_threshold": 250,
    },
    "audio": {
        "sentence_silence_ms": 250,
        "paragraph_silence_ms": 700,
        "fade_enabled": False,
        "fade_in_ms": 0,
        "fade_out_ms": 0,
        "synchronization_delay_ms": 0,
        "synchronization_speed": 1.0,
    },
    "rvc": {"enabled": False, "model": "", "pitch": 0, "filter_radius": 3, "index_rate": 0.3, "volume_envelope": 1.0, "protect": 0.3, "f0_method": "rmvpe"},
    "source_cleaning": {
        "agentic": False,
        "max_iterations": 53,
        "pdf_ocr_mode": "auto",
        "pdf_ocr_language": "auto",
        "pdf_ocr_dpi": 200,
        "pdf_remove_toc": True,
        "pdf_remove_repeated_marginals": True,
        "remove_footnotes": False,
        "filter_citations": True,
        "phase_max_iterations": {},
        "request_timeout_seconds": 600,
    },
    "output": {
        "format": "wav",
        "bitrate": "192k",
        "audio_mode": "preserve",
        "subtitle_mode": "none",
        "subtitle_selection": "translation",
        "title": "",
        "artist": "",
        "album": "",
        "genre": "Audiobook",
        "language": "",
        "cover_artifact_id": "",
    },
}


# The web settings API deliberately uses concise, presentation-friendly names.
# Existing generation/transcription logic predates that API and consumes the Qt
# names below.  Keep the translation at the service boundary so persisted web
# records remain stable and every handler receives one canonical runtime shape.
RUNTIME_SETTING_ALIASES: dict[str, dict[str, str]] = {
    "tts": {"model": "xtts_model", "voice": "speaker"},
    "rvc": {"enabled": "enable_rvc", "model": "rvc_model"},
    "audio": {
        "sentence_silence_ms": "silence_between_sentences",
        "paragraph_silence_ms": "silence_for_paragraphs",
        "fade_enabled": "enable_fade",
        "fade_in_ms": "fade_in_duration",
        "fade_out_ms": "fade_out_duration",
    },
    "subtitles": {
        "max_lines": "subtitle_max_lines",
        "max_chars_per_line": "subtitle_max_chars_per_line",
        "max_cps": "subtitle_max_cps",
        "min_duration_ms": "subtitle_min_duration_ms",
        "max_duration_ms": "subtitle_max_duration_ms",
        "min_gap_ms": "subtitle_min_gap_ms",
        "phrase_gap_ms": "subtitle_phrase_gap_ms",
        "merge_threshold_ms": "subtitle_merge_threshold",
    },
    "correction": {
        "enabled": "correction_enabled",
        "model_name": "correction_model",
        "instructions": "custom_correction_prompt",
    },
    "translation": {
        "enabled": "translation_enabled",
        "backend": "translation_backend",
        "source_language": "original_language",
        "model_name": "translation_model",
        "instructions": "translate_prompt",
    },
}


def adapt_runtime_settings(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """Add legacy runtime aliases without overwriting explicit expert values."""
    result = deepcopy(values or {})
    for web_key, runtime_key in RUNTIME_SETTING_ALIASES.get(section, {}).items():
        if runtime_key not in result and web_key in result:
            result[runtime_key] = deepcopy(result[web_key])
    return result

SECRET_KEYS = {"secret", "password", "api_key", "token", "access_token", "refresh_token", "credential", "credentials"}


def _merge(*values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        for key, item in (value or {}).items():
            if isinstance(item, dict) and isinstance(result.get(key), dict):
                result[key] = _merge(result[key], item)
            else:
                result[key] = deepcopy(item)
    return result


def _secret_free(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _secret_free(item)
            for key, item in value.items()
            if not (
                key.lower() in SECRET_KEYS
                or key.lower().endswith(("_secret", "_password", "_api_key", "_access_token", "_refresh_token", "_credential", "_credentials"))
            )
        }
    if isinstance(value, list):
        return [_secret_free(item) for item in value]
    return value


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def mark_output_assemblies_stale(session, session_id: str) -> None:
    """Invalidate completed assemblies and their exports after audio-plan changes."""
    from .artifacts import ArtifactService

    records = list(
        session.scalars(
            select(OutputAssembly).where(
                OutputAssembly.session_id == session_id,
                OutputAssembly.status == "completed",
            )
        ).all()
    )
    for record in records:
        record.status = "stale"
        record.updated_at = utcnow()
        if record.artifact_id:
            artifact = session.get(Artifact, record.artifact_id)
            if artifact is not None:
                artifact.state = "stale"
                ArtifactService._mark_descendants_stale(session, artifact.id)


class RevisionConflict(ValueError):
    pass


class WorkspaceSettingsService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validate_section(section: str) -> str:
        normalized = str(section or "").strip().lower()
        if normalized not in SETTING_SECTIONS:
            raise ValueError(f"Unknown settings section: {section}")
        return normalized

    def get(self, session_id: str, section: str) -> dict[str, Any]:
        section = self._validate_section(section)
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            global_record = session.get(AppSetting, f"defaults.{section}")
            override = session.get(SessionSetting, (session_id, section))
            global_value = global_record.value_json if global_record and isinstance(global_record.value_json, dict) else {}
            override_value = override.value_json if override else {}
            return {
                "section": section,
                "builtin": deepcopy(BUILTIN_DEFAULTS[section]),
                "global": deepcopy(global_value),
                "override": deepcopy(override_value),
                "effective": _merge(BUILTIN_DEFAULTS[section], global_value, override_value),
                "revision": override.revision if override else 0,
                "global_revision": global_record.revision if global_record else 0,
            }

    def update(self, session_id: str, section: str, expected_revision: int, value: dict[str, Any]) -> dict[str, Any]:
        section = self._validate_section(section)
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            record = session.get(SessionSetting, (session_id, section))
            if record is None:
                if expected_revision != 0:
                    raise RevisionConflict("Session settings were created in another client.")
                record = SessionSetting(session_id=session_id, section=section, value_json=value, revision=1)
                session.add(record)
            else:
                if expected_revision != record.revision:
                    raise RevisionConflict("Session settings changed in another client.")
                session.add(SessionSettingHistory(session_id=session_id, section=section, value_json=record.value_json, revision=record.revision))
                record.value_json = value
                record.revision += 1
                record.updated_at = utcnow()
            session.flush()
            revision = record.revision
        return self.get(session_id, section) | {"revision": revision}

    def resolve(self, session_id: str, sections: list[str] | None = None, run_override: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
        requested = sections or list(SETTING_SECTIONS)
        override = run_override or {}
        resolved = {
            section: _merge(self.get(session_id, section)["effective"], override.get(section, {}))
            for section in requested
        }
        if "tts" in resolved:
            with self.database.session() as session:
                connections = session.get(AppSetting, "services.tts")
                connection_value = connections.value_json if connections and isinstance(connections.value_json, dict) else {}
            if connection_value:
                resolved["tts"] = _merge(resolved["tts"], connection_value)
        safe = _secret_free(resolved)
        return safe, stable_hash(safe)


def derive_legacy_outcome(record: SessionRecord) -> dict[str, Any]:
    included = set(record.included_stages_json or [])
    kind = record.workflow_kind
    return {
        "version": 1,
        "workflow_kind": kind,
        "focus": "custom" if record.workflow_preset == "custom" else "guided",
        "deliverables": {
            "audiobook": kind == "audiobook",
            "subtitles": kind == "subtitles" or "export" in included,
            "voiceover": kind == "voiceover" or "generate_audio" in included,
        },
        "transformations": {
            "transcribe": "transcribe" in included,
            "correct": "correct" in included,
            "translate": "translate" in included,
            "deterministic_normalization": True,
            "llm_tts_optimization": False,
            "generate_audio": "generate_audio" in included or kind == "audiobook",
            "rvc": False,
        },
        "inputs": {"translation": "correction" if "correct" in included else "source", "generation": "translation" if "translate" in included else "correction" if "correct" in included else "source"},
        "export": {"audio": "generated" if kind in {"audiobook", "voiceover"} else "preserve", "subtitles": "translation" if "translate" in included else "source"},
    }


def resolve_pipeline(plan: dict[str, Any], *, source_requires_transcription: bool = False) -> list[dict[str, str]]:
    kind = str(plan.get("workflow_kind") or "audiobook")
    transformations = plan.get("transformations") if isinstance(plan.get("transformations"), dict) else {}
    deliverables = plan.get("deliverables") if isinstance(plan.get("deliverables"), dict) else {}
    stages: list[tuple[str, str]] = []
    if kind == "audiobook":
        stages.append(("clean_source", "Clean source"))
    elif source_requires_transcription or transformations.get("transcribe"):
        stages.append(("transcribe", "Transcribe"))
    if transformations.get("correct"):
        stages.append(("correct", "Correct subtitles"))
    if transformations.get("translate"):
        stages.append(("translate", "Translate"))
    if transformations.get("deterministic_normalization", True) and (transformations.get("generate_audio") or kind == "audiobook"):
        stages.append(("normalize_text", "Normalize for speech"))
    if transformations.get("llm_tts_optimization"):
        stages.append(("optimize_tts", "LLM speech optimization"))
    if transformations.get("generate_audio") or deliverables.get("audiobook") or deliverables.get("voiceover"):
        stages.extend((("build_generation_plan", "Prepare generation segments"), ("generate_audio", "Generate audio")))
    if transformations.get("rvc"):
        stages.append(("apply_rvc", "Apply RVC"))
    if any(bool(value) for value in deliverables.values()):
        stages.append(("export", "Export"))
    return [{"key": key, "title": title} for key, title in stages]


class OutcomePlanService:
    def __init__(self, database: Database):
        self.database = database

    def get(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            plan = session.get(OutcomePlan, session_id)
            if plan is None:
                plan = OutcomePlan(session_id=session_id, value_json=derive_legacy_outcome(record))
                session.add(plan)
                session.flush()
            value = deepcopy(plan.value_json)
            revision = plan.revision
        return {"value": value, "revision": revision, "pipeline": resolve_pipeline(value)}

    def update(self, session_id: str, expected_revision: int, value: dict[str, Any]) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            plan = session.get(OutcomePlan, session_id)
            if plan is None:
                if expected_revision != 0:
                    raise RevisionConflict("The workflow plan was created in another client.")
                plan = OutcomePlan(session_id=session_id, value_json=value, revision=1)
                session.add(plan)
            else:
                if expected_revision != plan.revision:
                    raise RevisionConflict("The workflow plan changed in another client.")
                session.add(OutcomePlanHistory(session_id=session_id, value_json=plan.value_json, revision=plan.revision))
                plan.value_json = value
                plan.revision += 1
                plan.updated_at = utcnow()
            record.workflow_kind = str(value.get("workflow_kind") or record.workflow_kind)
            record.workflow_preset = "custom"
            pipeline_keys = {item["key"] for item in resolve_pipeline(value)}
            record.included_stages_json = [
                key
                for key in ("transcribe", "correct", "translate", "optimize_tts", "generate_audio", "export")
                if key in pipeline_keys
            ]
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            revision = plan.revision
        return {"value": deepcopy(value), "revision": revision, "pipeline": resolve_pipeline(value)}


class SourceLibraryService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _asset_payload(asset: SourceAsset, *, reference_count: int = 0, current_reference_count: int = 0) -> dict[str, Any]:
        return {
            "id": asset.id,
            "artifact_id": asset.artifact_id,
            "display_name": asset.display_name,
            "kind": asset.kind,
            "mime_type": asset.mime_type,
            "external_path": asset.external_path,
            "size_bytes": asset.size_bytes,
            "content_hash": asset.content_hash,
            "state": asset.state,
            "metadata": asset.metadata_json,
            "revision": asset.revision,
            "reference_count": reference_count,
            "current_reference_count": current_reference_count,
            "created_at": asset.created_at.isoformat(),
            "updated_at": asset.updated_at.isoformat(),
        }

    def ensure_for_artifact(self, artifact_id: str, *, display_name: str | None = None, kind: str | None = None) -> SourceAsset:
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None:
                raise KeyError(artifact_id)
            asset = session.scalar(select(SourceAsset).where(SourceAsset.artifact_id == artifact_id))
            if asset is None:
                name = display_name or str((artifact.metadata_json or {}).get("original_filename") or Path(artifact.relative_path).name)
                asset = SourceAsset(
                    artifact_id=artifact.id,
                    display_name=name,
                    kind=kind or Path(name).suffix.lower().lstrip(".") or artifact.kind,
                    mime_type=artifact.mime_type,
                    size_bytes=artifact.size_bytes,
                    content_hash=artifact.content_hash,
                    metadata_json={"legacy_session_id": artifact.session_id} if artifact.session_id else {},
                )
                session.add(asset)
                session.flush()
            session.expunge(asset)
            return asset

    def attach(self, session_id: str, source_asset_id: str, *, role: str = "primary") -> dict[str, Any]:
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None or session.get(SourceAsset, source_asset_id) is None:
                raise KeyError(session_id)
            for current in session.scalars(select(SessionSource).where(SessionSource.session_id == session_id, SessionSource.role == role, SessionSource.is_current.is_(True))).all():
                current.is_current = False
                current.revision += 1
                current.updated_at = utcnow()
            attachment = session.scalar(select(SessionSource).where(SessionSource.session_id == session_id, SessionSource.source_asset_id == source_asset_id, SessionSource.role == role))
            if attachment is None:
                attachment = SessionSource(session_id=session_id, source_asset_id=source_asset_id, role=role)
                session.add(attachment)
            else:
                attachment.is_current = True
                attachment.revision += 1
                attachment.updated_at = utcnow()
            session.flush()
            return {"id": attachment.id, "session_id": session_id, "source_asset_id": source_asset_id, "role": role, "is_current": True, "revision": attachment.revision}

    def detach(self, session_id: str, attachment_id: str, expected_revision: int) -> None:
        with self.database.session() as session:
            attachment = session.get(SessionSource, attachment_id)
            if attachment is None or attachment.session_id != session_id:
                raise KeyError(attachment_id)
            if attachment.revision != expected_revision:
                raise RevisionConflict("The source attachment changed in another client.")
            session.delete(attachment)

    def rename(self, source_asset_id: str, expected_revision: int, display_name: str) -> dict[str, Any]:
        with self.database.session() as session:
            asset = session.get(SourceAsset, source_asset_id)
            if asset is None:
                raise KeyError(source_asset_id)
            if asset.revision != expected_revision:
                raise RevisionConflict("The source asset changed in another client.")
            asset.display_name = display_name.strip()
            asset.revision += 1
            asset.updated_at = utcnow()
            session.flush()
            references = int(session.scalar(select(func.count()).select_from(SessionSource).where(SessionSource.source_asset_id == asset.id)) or 0)
            current = int(session.scalar(select(func.count()).select_from(SessionSource).where(SessionSource.source_asset_id == asset.id, SessionSource.is_current.is_(True))) or 0)
            return self._asset_payload(asset, reference_count=references, current_reference_count=current)

    def set_state(self, source_asset_id: str, expected_revision: int, state: str) -> dict[str, Any]:
        if state not in {"current", "trashed"}:
            raise ValueError("Unsupported source lifecycle state.")
        with self.database.session() as session:
            asset = session.get(SourceAsset, source_asset_id)
            if asset is None:
                raise KeyError(source_asset_id)
            if asset.revision != expected_revision:
                raise RevisionConflict("The source asset changed in another client.")
            references = int(session.scalar(select(func.count()).select_from(SessionSource).where(SessionSource.source_asset_id == asset.id)) or 0)
            current = int(session.scalar(select(func.count()).select_from(SessionSource).where(SessionSource.source_asset_id == asset.id, SessionSource.is_current.is_(True))) or 0)
            if state == "trashed" and references:
                raise ValueError(f"Detach this source from {references} session attachment(s) before moving it to trash.")
            asset.state = state
            asset.revision += 1
            asset.updated_at = utcnow()
            session.flush()
            return self._asset_payload(asset, reference_count=references, current_reference_count=current)

    def list(self, *, session_id: str | None = None, include_trashed: bool = False) -> list[dict[str, Any]]:
        with self.database.session() as session:
            if session_id:
                rows = session.execute(
                    select(SessionSource, SourceAsset)
                    .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
                    .where(SessionSource.session_id == session_id)
                    .order_by(SessionSource.updated_at.desc())
                ).all()
                asset_ids = {asset.id for _link, asset in rows}
                counts = dict(
                    session.execute(
                        select(SessionSource.source_asset_id, func.count())
                        .where(SessionSource.source_asset_id.in_(asset_ids))
                        .group_by(SessionSource.source_asset_id)
                    ).all()
                ) if asset_ids else {}
                current_counts = dict(
                    session.execute(
                        select(SessionSource.source_asset_id, func.count())
                        .where(SessionSource.source_asset_id.in_(asset_ids), SessionSource.is_current.is_(True))
                        .group_by(SessionSource.source_asset_id)
                    ).all()
                ) if asset_ids else {}
                return [
                    self._asset_payload(
                        asset,
                        reference_count=int(counts.get(asset.id, 0)),
                        current_reference_count=int(current_counts.get(asset.id, 0)),
                    )
                    | {"attachment": {"id": link.id, "role": link.role, "is_current": link.is_current, "revision": link.revision}}
                    for link, asset in rows
                ]
            statement = select(SourceAsset).order_by(SourceAsset.updated_at.desc())
            if not include_trashed:
                statement = statement.where(SourceAsset.state != "trashed")
            assets = list(session.scalars(statement).all())
            counts = dict(session.execute(select(SessionSource.source_asset_id, func.count()).group_by(SessionSource.source_asset_id)).all())
            current_counts = dict(session.execute(select(SessionSource.source_asset_id, func.count()).where(SessionSource.is_current.is_(True)).group_by(SessionSource.source_asset_id)).all())
            return [self._asset_payload(asset, reference_count=int(counts.get(asset.id, 0)), current_reference_count=int(current_counts.get(asset.id, 0))) for asset in assets]

    def backfill_legacy(self) -> int:
        created = 0
        with self.database.session() as session:
            records = list(session.scalars(select(SourceRecord).where(SourceRecord.artifact_id.is_not(None))).all())
            for legacy in records:
                artifact = session.get(Artifact, legacy.artifact_id)
                if artifact is None:
                    continue
                asset = session.scalar(select(SourceAsset).where(SourceAsset.artifact_id == artifact.id))
                if asset is None:
                    asset = SourceAsset(
                        artifact_id=artifact.id,
                        display_name=legacy.display_name,
                        kind=legacy.kind,
                        mime_type=artifact.mime_type,
                        size_bytes=artifact.size_bytes,
                        content_hash=artifact.content_hash,
                        metadata_json={"legacy_source_id": legacy.id},
                    )
                    session.add(asset)
                    session.flush()
                    created += 1
                link = session.scalar(select(SessionSource).where(SessionSource.session_id == legacy.session_id, SessionSource.source_asset_id == asset.id, SessionSource.role == "primary"))
                if link is None:
                    session.add(SessionSource(session_id=legacy.session_id, source_asset_id=asset.id, role="primary"))
        return created


class GenerationService:
    def __init__(self, database: Database, jobs: JobQueue, settings: WorkspaceSettingsService):
        self.database = database
        self.jobs = jobs
        self.settings = settings

    def create_plan(self, session_id: str, *, source_revision_id: str | None, segments: list[dict[str, Any]], settings: dict[str, Any] | None = None) -> dict[str, Any]:
        clean_segments = [item for item in segments if str(item.get("text") or "").strip()]
        if not clean_segments:
            raise ValueError("At least one generation segment is required.")
        content = {"source_revision_id": source_revision_id, "segments": clean_segments, "settings": settings or {}}
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            if source_revision_id and session.get(DocumentRevision, source_revision_id) is None:
                raise KeyError(source_revision_id)
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            if plan is None:
                plan = GenerationPlan(session_id=session_id)
                session.add(plan)
                session.flush()
            revision_number = int(session.scalar(select(func.max(GenerationPlanRevision.revision_number)).where(GenerationPlanRevision.plan_id == plan.id)) or 0) + 1
            revision = GenerationPlanRevision(plan_id=plan.id, source_revision_id=source_revision_id, revision_number=revision_number, settings_json=settings or {}, content_hash=stable_hash(content))
            session.add(revision)
            session.flush()
            for index, item in enumerate(clean_segments):
                session.add(
                    GenerationSegment(
                        plan_revision_id=revision.id,
                        ordinal=index,
                        source_segment_ids_json=list(item.get("source_segment_ids") or []),
                        node_kind=str(item.get("node_kind") or ("chapter_marker" if str(item.get("chapter") or "").lower() == "yes" else "paragraph")),
                        text=str(item.get("text") or "").strip(),
                        voice_id=item.get("voice_id"),
                        language=item.get("language"),
                        silence_after_ms=max(0, int(item.get("silence_after_ms") or 0)),
                    )
                )
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            session.flush()
            return {"id": plan.id, "active_revision_id": revision.id, "revision_number": revision_number, "segment_count": len(clean_segments)}

    def list_segments(self, session_id: str, *, cursor: int = 0, limit: int = 100, status: str | None = None, marked: bool | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 250))
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            if plan is None or not plan.active_revision_id:
                return {"items": [], "next_cursor": None, "total": 0, "plan_revision_id": None}
            filters = [GenerationSegment.plan_revision_id == plan.active_revision_id, GenerationSegment.ordinal >= max(0, cursor)]
            if status:
                filters.append(GenerationSegment.status == status)
            if marked is not None:
                filters.append(GenerationSegment.marked.is_(marked))
            rows = list(session.scalars(select(GenerationSegment).where(*filters).order_by(GenerationSegment.ordinal).limit(limit + 1)).all())
            has_more = len(rows) > limit
            rows = rows[:limit]
            takes_by_segment: dict[str, list[AudioTake]] = {}
            if rows:
                takes = list(session.scalars(select(AudioTake).where(AudioTake.generation_segment_id.in_([item.id for item in rows])).order_by(AudioTake.created_at.desc())).all())
                for take in takes:
                    takes_by_segment.setdefault(take.generation_segment_id, []).append(take)
            items = [
                {
                    "id": item.id,
                    "ordinal": item.ordinal,
                    "node_kind": item.node_kind,
                    "text": item.text,
                    "voice_id": item.voice_id,
                    "language": item.language,
                    "silence_after_ms": item.silence_after_ms,
                    "marked": item.marked,
                    "removed": item.removed,
                    "status": item.status,
                    "revision": item.revision,
                    "takes": [
                        {"id": take.id, "artifact_id": take.artifact_id, "parent_take_id": take.parent_take_id, "kind": take.kind, "status": take.status, "duration_ms": take.duration_ms, "is_active": take.is_active, "revision": take.revision}
                        for take in takes_by_segment.get(item.id, [])
                    ],
                }
                for item in rows
            ]
            total = int(session.scalar(select(func.count()).select_from(GenerationSegment).where(GenerationSegment.plan_revision_id == plan.active_revision_id)) or 0)
            return {"items": items, "next_cursor": rows[-1].ordinal + 1 if rows and has_more else None, "total": total, "plan_revision_id": plan.active_revision_id}

    def update_segment(self, segment_id: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"text", "node_kind", "voice_id", "language", "silence_after_ms", "marked", "removed"}
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            if segment is None:
                raise KeyError(segment_id)
            if segment.revision != expected_revision:
                raise RevisionConflict("The generation segment changed in another client.")
            session.add(GenerationSegmentRevision(generation_segment_id=segment.id, revision=segment.revision, node_kind=segment.node_kind, text=segment.text, marked=segment.marked, removed=segment.removed, voice_id=segment.voice_id, language=segment.language, silence_after_ms=segment.silence_after_ms))
            text_changed = "text" in changes and str(changes["text"]).strip() != segment.text
            for key, value in changes.items():
                if key not in allowed:
                    continue
                if key == "text":
                    value = str(value).strip()
                    if not value:
                        raise ValueError("Generation text cannot be blank; remove the segment instead.")
                if key == "silence_after_ms":
                    value = max(0, int(value))
                if key == "node_kind" and value not in {"paragraph", "heading", "chapter_marker", "subtitle_cue"}:
                    raise ValueError("Unsupported generation segment type.")
                setattr(segment, key, value)
            if text_changed or any(key in changes for key in ("voice_id", "language")):
                segment.status = "stale"
                for take in session.scalars(select(AudioTake).where(AudioTake.generation_segment_id == segment.id, AudioTake.status == "completed")).all():
                    take.status = "stale"
            if any(key in changes for key in ("text", "node_kind", "voice_id", "language", "silence_after_ms", "removed")):
                plan_revision = session.get(GenerationPlanRevision, segment.plan_revision_id)
                plan = session.get(GenerationPlan, plan_revision.plan_id) if plan_revision else None
                if plan is not None:
                    mark_output_assemblies_stale(session, plan.session_id)
            segment.revision += 1
            segment.updated_at = utcnow()
            session.flush()
            return {"id": segment.id, "node_kind": segment.node_kind, "text": segment.text, "marked": segment.marked, "removed": segment.removed, "status": segment.status, "revision": segment.revision}

    def select_take(self, segment_id: str, take_id: str, expected_revision: int) -> dict[str, Any]:
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            take = session.get(AudioTake, take_id)
            if segment is None or take is None or take.generation_segment_id != segment_id:
                raise KeyError(take_id)
            if segment.revision != expected_revision:
                raise RevisionConflict("The generation segment changed in another client.")
            if take.status not in {"completed", "stale"} or not take.artifact_id:
                raise ValueError("Only an available audio take can be selected.")
            session.add(GenerationSegmentRevision(generation_segment_id=segment.id, revision=segment.revision, node_kind=segment.node_kind, text=segment.text, marked=segment.marked, removed=segment.removed, voice_id=segment.voice_id, language=segment.language, silence_after_ms=segment.silence_after_ms))
            for item in session.scalars(select(AudioTake).where(AudioTake.generation_segment_id == segment_id)).all():
                item.is_active = item.id == take_id
                item.revision += 1
            segment.revision += 1
            segment.updated_at = utcnow()
            plan_revision = session.get(GenerationPlanRevision, segment.plan_revision_id)
            plan = session.get(GenerationPlan, plan_revision.plan_id) if plan_revision else None
            if plan is not None:
                mark_output_assemblies_stale(session, plan.session_id)
            return {"id": segment.id, "active_take_id": take.id, "revision": segment.revision}

    def start(self, session_id: str, *, run_override: dict[str, Any] | None = None, segment_ids: list[str] | None = None, operation: str = "generate") -> dict[str, Any]:
        snapshot, settings_hash = self.settings.resolve(session_id, run_override=run_override)
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            if plan is None or not plan.active_revision_id:
                raise ValueError("Create generation segments before starting audio generation.")
            run = GenerationRun(session_id=session_id, plan_revision_id=plan.active_revision_id, status="queued", settings_snapshot_json=snapshot, settings_hash=settings_hash)
            session.add(run)
            session.flush()
            run_id = run.id
        resource_keys = self._resource_keys(session_id, snapshot)
        job = self.jobs.enqueue("generation.run", {"generation_run_id": run_id, "segment_ids": segment_ids or [], "operation": operation}, session_id=session_id, resource_keys=resource_keys)
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            run.job_id = job.id
            run.updated_at = utcnow()
        return {"id": run_id, "job_id": job.id, "status": "queued", "settings_hash": settings_hash}

    @staticmethod
    def _resource_keys(session_id: str, snapshot: dict[str, Any]) -> list[str]:
        tts = snapshot.get("tts", {})
        service = str(tts.get("service") or "tts").lower().replace(" ", "_")
        resource_keys = [f"session:{session_id}", f"service:tts:{service}"]
        compute = str(tts.get("compute_backend") or tts.get("device") or "auto").lower()
        if compute in {"cuda", "vulkan", "metal", "gpu"}:
            resource_keys.append(f"gpu:{compute}")
        return resource_keys

    def request_pause(self, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status not in {"queued", "running", "pausing"}:
                raise ValueError(f"Run cannot be paused from {run.status}.")
            run.pause_requested = True
            run.status = "pausing"
            run.updated_at = utcnow()
            return {"id": run.id, "job_id": run.job_id, "status": run.status}

    def resume(self, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status != "paused":
                raise ValueError("Only a paused generation run can be resumed.")
            run.pause_requested = False
            run.cancel_requested = False
            run.status = "queued"
            run.updated_at = utcnow()
            session_id = run.session_id
            snapshot = dict(run.settings_snapshot_json or {})
        job = self.jobs.enqueue("generation.run", {"generation_run_id": run_id, "segment_ids": [], "operation": "resume"}, session_id=session_id, resource_keys=self._resource_keys(session_id, snapshot))
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            run.job_id = job.id
        return {"id": run_id, "job_id": job.id, "status": "queued"}

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            run.cancel_requested = True
            run.status = "cancel_requested"
            run.updated_at = utcnow()
            job_id = run.job_id
        if job_id:
            try:
                self.jobs.request_cancel(job_id)
            except KeyError:
                pass
        return {"id": run_id, "job_id": job_id, "status": "cancel_requested"}

    def latest_run(self, session_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            run = session.scalar(select(GenerationRun).where(GenerationRun.session_id == session_id).order_by(GenerationRun.created_at.desc()))
            if run is None:
                return None
            return {"id": run.id, "job_id": run.job_id, "status": run.status, "pause_requested": run.pause_requested, "cancel_requested": run.cancel_requested, "settings_hash": run.settings_hash, "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat()}

    @staticmethod
    def _assembly_payload(record: OutputAssembly) -> dict[str, Any]:
        return {
            "id": record.id,
            "session_id": record.session_id,
            "generation_run_id": record.generation_run_id,
            "job_id": record.job_id,
            "artifact_id": record.artifact_id,
            "status": record.status,
            "settings_hash": record.settings_hash,
            "error_message": record.error_message,
            "settings": deepcopy(record.settings_json or {}),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    def create_assembly(
        self,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        run_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot, settings_hash = self.settings.resolve(
            session_id,
            sections=["audio", "output"],
            run_override=run_override,
        )
        output_format = str((snapshot.get("output") or {}).get("format") or "wav").lower()
        from .audio_assembly import OUTPUT_FORMATS

        if output_format not in OUTPUT_FORMATS:
            raise ValueError(f"Unsupported audio output format: {output_format}")
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            run = session.get(GenerationRun, generation_run_id) if generation_run_id else None
            if generation_run_id and (run is None or run.session_id != session_id):
                raise KeyError(generation_run_id)
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
            plan_revision_id = run.plan_revision_id if run else plan.active_revision_id if plan else None
            if not plan_revision_id:
                raise ValueError("Create generation segments before assembling audio.")
            record = OutputAssembly(
                session_id=session_id,
                generation_run_id=run.id if run else None,
                status="queued",
                settings_json={"resolved": snapshot, "plan_revision_id": plan_revision_id},
                settings_hash=settings_hash,
            )
            session.add(record)
            session.flush()
            assembly_id = record.id
        job = self.jobs.enqueue(
            "generation.assemble",
            {"output_assembly_id": assembly_id},
            session_id=session_id,
            resource_keys=[f"session:{session_id}"],
        )
        with self.database.session() as session:
            record = session.get(OutputAssembly, assembly_id)
            record.job_id = job.id
            record.updated_at = utcnow()
            payload = self._assembly_payload(record)
        return payload

    def latest_assembly(self, session_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            record = session.scalar(
                select(OutputAssembly)
                .where(OutputAssembly.session_id == session_id)
                .order_by(OutputAssembly.created_at.desc())
            )
            return self._assembly_payload(record) if record is not None else None


class ResourceClaimService:
    def __init__(self, database: Database):
        self.database = database

    def acquire(self, job_id: str, owner: str, keys: list[str], lease_seconds: int = 60) -> bool:
        expires = utcnow() + timedelta(seconds=max(10, lease_seconds))
        with self.database.session() as session:
            active = list(session.scalars(select(ResourceClaim).where(ResourceClaim.resource_key.in_(keys), ResourceClaim.expires_at > utcnow(), ResourceClaim.job_id != job_id)).all()) if keys else []
            if active:
                return False
            for key in keys:
                claim = session.get(ResourceClaim, key)
                if claim is None:
                    session.add(ResourceClaim(resource_key=key, job_id=job_id, lease_owner=owner, expires_at=expires))
                else:
                    claim.job_id = job_id
                    claim.lease_owner = owner
                    claim.expires_at = expires
            return True

    def release(self, job_id: str, owner: str) -> None:
        with self.database.session() as session:
            for claim in session.scalars(select(ResourceClaim).where(ResourceClaim.job_id == job_id, ResourceClaim.lease_owner == owner)).all():
                session.delete(claim)
