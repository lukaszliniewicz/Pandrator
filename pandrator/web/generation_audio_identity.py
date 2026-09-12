"""Audio compatibility, independent of speech-plan topology and output settings."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .models import Artifact, AudioTake, GenerationSegment, Voice, VoiceSample

IDENTITY_KEY = "generation_audio_identity"
IDENTITY_VERSION = 1


def _voice_key(value: str) -> str:
    return value.strip().casefold().removesuffix(".wav")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _material_settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    # Imported lazily: workspace owns the persisted settings vocabulary and
    # consumes this module at its generation boundary.
    from pandrator.logic import tts_handler
    from pandrator.logic.audio_cpp_parameters import (
        request_parameters_for_family,
        validate_audio_cpp_model_options,
    )

    from .workspace import (
        BUILTIN_DEFAULTS,
        RUNTIME_SETTING_ALIASES,
        _secret_free,
        adapt_runtime_settings,
    )

    values = {
        **adapt_runtime_settings("tts", snapshot.get("tts") or {}),
        **adapt_runtime_settings("audio", snapshot.get("audio") or {}),
    }
    inline_reference = values.get("audio_cpp_voice_ref")
    if inline_reference is not None:
        values["inline_voice_reference_hash"] = _hash(
            {
                "reference": inline_reference,
                "transcript": values.get("audio_cpp_reference_text"),
            }
        )
    unrelated = {
        key
        for section, defaults in BUILTIN_DEFAULTS.items()
        if section != "tts"
        for key in (*defaults, *RUNTIME_SETTING_ALIASES.get(section, {}).values())
    } - set(BUILTIN_DEFAULTS["tts"])
    unrelated.update(
        {
            "max_attempts",
            "tts_batch_size",
            "tts_concurrent_requests",
            "provider_configs",
            "service_configs",
            "pricing",
            "timeout",
            "request_timeout_seconds",
            "audio_cpp_voice_ref",
            "audio_cpp_voice_ref_hash",
            "audio_cpp_reference_text",
            "selected_segment_override",
            "use_existing_speech_plans",
            "secret_ref",
            "api_key_env",
        }
    )
    selected = str(values.get("service") or values.get("tts_service") or "")
    if selected == tts_handler.OPENAI_COMPAT_SERVICE:
        selected = str(values.get("openai_audio_endpoint") or selected)
    provider = tts_handler.get_service_config(values, selected)
    raw_model_settings = values.pop("audio_cpp_model_settings", None)
    selected_model_options: dict[str, Any] | None = None
    selected_model = str(
        values.get("xtts_model") or values.get("model") or ""
    ).strip()
    if not selected_model and isinstance(provider, dict):
        selected_model = str(provider.get("default_model") or "").strip()
    provider_adapter = str((provider or {}).get("adapter") or "").strip().lower()
    audio_cpp_selected = provider_adapter == "audio_cpp" or selected in {
        "audio_cpp",
        "audio.cpp",
        "audio-cpp",
        "audiocpp",
    }
    if audio_cpp_selected and isinstance(raw_model_settings, dict):
        raw_selected_options = raw_model_settings.get(selected_model)
        if isinstance(raw_selected_options, dict):
            metadata = tts_handler._audio_cpp_model_metadata(
                selected_model,
                provider if isinstance(provider, dict) else {},
            )
            family = str(metadata.get("family") or "").strip()
            selected_model_options = validate_audio_cpp_model_options(
                family,
                raw_selected_options,
            )
    values = {
        key: value
        for key, value in _secret_free(values).items()
        if key not in unrelated
        and not key.startswith(
            ("_", "speech_block_", "speech_plan_", "llm_", "source_")
        )
    }
    if selected_model_options is not None:
        # A selected model map suppresses legacy tuning sources at request
        # time, so ignored values must not invalidate reusable audio either.
        ignored_tuning_keys = {
            "audio_cpp_options",
            "options",
            "seed",
            "speed",
            "audio_cpp_speed",
        }
        ignored_tuning_keys.update(
            f"audio_cpp_{key}"
            for key in request_parameters_for_family(family)
        )
        values = {
            key: value
            for key, value in values.items()
            if key not in ignored_tuning_keys
        }
        values["audio_cpp_model_settings"] = {
            selected_model: selected_model_options,
        }
    # Other installed providers, credentials, labels and catalog ordering do
    # not change the selected service's audio.
    if provider:
        values["provider"] = _secret_free(
            {
                key: value
                for key, value in provider.items()
                if key
                in {
                    "id",
                    "adapter",
                    "api_base",
                    "settings",
                    "request_defaults",
                    "extra_body",
                    "speech_path",
                }
            }
        )
    return values


class AudioIdentityContext:
    """Resolve one settings snapshot and managed voice inventory per inspection."""

    def __init__(self, session: Session, snapshot: dict[str, Any]):
        from .voice_library import sample_file_status
        from .workspace import adapt_runtime_settings

        self.session = session
        # The SQLite database and managed artifacts share the DataPaths root.
        self.paths = DataPaths(Path(str(session.get_bind().engine.url.database)).parent)
        self.settings = {
            **adapt_runtime_settings("tts", snapshot.get("tts") or {}),
            **adapt_runtime_settings("audio", snapshot.get("audio") or {}),
        }
        selected = snapshot.get("selected_segment_override") or {}
        self.selected_tts = dict(selected.get("tts") or {})
        self.selected_rvc = dict(selected.get("rvc") or {})
        self.voices: dict[str, list[dict[str, Any]]] = {}
        samples: dict[str, list[dict[str, Any]]] = {}
        for sample, artifact in session.execute(
            select(VoiceSample, Artifact)
            .join(Artifact, Artifact.id == VoiceSample.artifact_id)
            .order_by(VoiceSample.created_at.desc(), VoiceSample.id)
        ):
            if (
                sample.voice_id in samples
                or sample_file_status(session, self.paths, sample)[0] != "ready"
            ):
                continue
            samples.setdefault(sample.voice_id, []).append(
                {
                    "id": sample.id,
                    "artifact_id": artifact.id,
                    "content_hash": artifact.content_hash,
                    "state": artifact.state,
                    "transcript_hash": _hash(
                        sample.transcript if sample.transcript_reviewed else ""
                    ),
                }
            )
        for voice in session.scalars(select(Voice)):
            providers = dict((voice.metadata_json or {}).get("providers") or {})
            # Voice.revision also changes for descriptions and publication
            # status. Reference samples and provider content hashes carry the
            # audio version without invalidating a harmless rename/republish.
            reference_providers = {
                provider_id: {
                    key: value
                    for key, value in registration.items()
                    if key
                    in {
                        "voice_id",
                        "provider_voice_id",
                        "sample_hash",
                        "source_audio_hash",
                        "reference_text_hash",
                        "model",
                    }
                }
                for provider_id, registration in providers.items()
                if isinstance(registration, dict)
            }
            identity = {
                "id": voice.id,
                "samples": samples.get(voice.id, []),
                "providers": reference_providers,
            }
            names = {voice.id, voice.name}
            for registration in providers.values():
                if isinstance(registration, dict):
                    names.update(
                        str(registration.get(key) or "")
                        for key in ("voice_id", "provider_voice_id")
                    )
            for name in names:
                if name.strip():
                    self.voices.setdefault(_voice_key(name), []).append(identity)
        self.cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def for_segment(self, segment: GenerationSegment) -> dict[str, Any]:
        language = str(segment.language or "").strip()
        if language.casefold() in {"auto", "und", "unknown"}:
            language = ""
        voice = str(segment.voice or "").strip()
        key = (language, voice, segment.voice_id or "")
        if key in self.cache:
            return self.cache[key]
        settings = deepcopy(self.settings)
        if language:
            settings.update(language=language, target_language=language)
        if voice:
            settings.update(voice=voice, speaker=voice)
        if self.selected_tts:
            # These overrides have precedence over persistent segment choices.
            from .workspace import adapt_runtime_settings

            settings = adapt_runtime_settings("tts", {**settings, **self.selected_tts})
            language = str(
                self.selected_tts.get("language")
                or self.selected_tts.get("target_language")
                or ""
            ).strip()
            voice = str(
                self.selected_tts.get("voice") or self.selected_tts.get("speaker") or ""
            ).strip()
            if language:
                settings.update(language=language, target_language=language)
            if voice:
                settings.update(voice=voice, speaker=voice)
        selected_voice = str(
            settings.get("voice") or settings.get("speaker") or ""
        ).strip()
        references = self.voices.get(_voice_key(selected_voice), [])
        if segment.voice_id:
            references = [
                *references,
                *self.voices.get(_voice_key(segment.voice_id), []),
            ]
        from .tts_providers import TtsProviderRegistry
        from .voice_library import resolve_audio_cpp_voice_reference

        if TtsProviderRegistry().service_id_for_settings(settings) == "audio_cpp":
            references = []
            if "audio_cpp_voice_ref" not in settings:
                try:
                    reference = resolve_audio_cpp_voice_reference(
                        self.session, self.paths, settings
                    )
                except ValueError:
                    references = [{"unavailable": True}]
                else:
                    if reference is not None:
                        content_hash, _path, artifact, sample = reference
                        references = [
                            {
                                "sample_id": sample.id,
                                "artifact_id": artifact.id,
                                "content_hash": content_hash,
                                "transcript_hash": _hash(
                                    str(sample.transcript or "").strip()
                                    if sample.transcript_reviewed
                                    else ""
                                ),
                            }
                        ]
        identity = {
            "schema_version": IDENTITY_VERSION,
            "settings_hash": _hash(
                {
                    "tts": _material_settings({"tts": settings}),
                    "rvc": self.selected_rvc
                    if self.selected_rvc.get("enabled")
                    else {},
                }
            ),
            "voice_reference_hash": _hash(sorted({_hash(item) for item in references})),
        }
        self.cache[key] = identity
        return identity


def take_reuse_reason(
    segment: GenerationSegment,
    take: AudioTake | None,
    artifact: Artifact | None,
    expected: dict[str, Any],
) -> str:
    if take is None or artifact is None or artifact.state == "deleted":
        return "audio_unavailable"
    if segment.status != "completed" or take.status != "completed":
        return "not_completed"
    actual = (artifact.metadata_json or {}).get(IDENTITY_KEY)
    if not isinstance(actual, dict) or actual.get("schema_version") != IDENTITY_VERSION:
        return "audio_identity_unknown"
    if actual.get("settings_hash") != expected["settings_hash"]:
        return "generation_settings_changed"
    if actual.get("voice_reference_hash") != expected["voice_reference_hash"]:
        return "voice_reference_changed"
    return "reusable"


def plan_audio_identities(
    session: Session, revision_id: str, snapshot: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    context = AudioIdentityContext(session, snapshot)
    return {
        segment.id: context.for_segment(segment)
        for segment in session.scalars(
            select(GenerationSegment).where(
                GenerationSegment.plan_revision_id == revision_id,
                GenerationSegment.removed.is_(False),
            )
        )
    }
