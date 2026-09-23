"""Revisioned session settings and coherent effective-settings resolution."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.tts_provider_switch import (
    normalize_tts_voice_aliases,
    prepare_tts_provider_switch,
)

from .database import Database
from .models import (
    AppSetting,
    Artifact,
    GenerationRun,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SessionSettingHistory,
    utcnow,
)
from .settings_policy import (
    BUILTIN_DEFAULTS,
    SETTING_SECTIONS,
    RevisionConflict,
    _merge,
    _secret_free,
    normalize_subtitle_limit_override,
    stable_hash,
    validate_output_settings,
    validate_stt_settings,
    validate_voiceover_repair_settings,
)
from .source_resolution import classify_source, resolve_media_source


class WorkspaceSettingsService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validate_section(section: str) -> str:
        normalized = str(section or "").strip().lower()
        if normalized not in SETTING_SECTIONS:
            raise ValueError(f"Unknown settings section: {section}")
        return normalized

    @staticmethod
    def _output_context(session, session_record: SessionRecord) -> dict[str, Any]:
        source = resolve_media_source(session, session_record.id)
        workflow_kind = session_record.workflow_kind
        source_artifact = source.artifact
        source_name = source.name
        source_kind = source.kind
        source_mime_type = source.mime_type
        source_resolution = source.resolution
        if workflow_kind == "media_edit":
            edited_media = session.scalar(
                select(Artifact)
                .where(
                    Artifact.session_id == session_record.id,
                    Artifact.role == "media_edit_media",
                    Artifact.state == "current",
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            )
            if edited_media is not None:
                source_artifact = edited_media
                source_name = str(
                    (edited_media.metadata_json or {}).get("original_filename")
                    or Path(edited_media.relative_path).name
                )
                source_kind = str(edited_media.kind or "video")
                source_mime_type = str(edited_media.mime_type or "")
                source_resolution = "derived_media_edit"
        source_profile = classify_source(
            name=source_name,
            kind=source_kind,
            mime_type=source_mime_type,
        )
        has_source_video = source_profile == "video"
        has_source_audio = source_profile in {"video", "audio"}
        available_subtitle_roles = set(
            session.scalars(
                select(Artifact.role).where(
                    Artifact.session_id == session_record.id,
                    Artifact.state == "current",
                    Artifact.role.in_(
                        (
                            "media_edit_subtitles",
                            "transcription",
                            "correction",
                            "translation",
                        )
                    ),
                )
            ).all()
        )
        subtitle_selection = next(
            (
                selection
                for role, selection in (
                    ("translation", "translation"),
                    ("correction", "correction"),
                    ("media_edit_subtitles", "source"),
                    ("transcription", "source"),
                )
                if role in available_subtitle_roles
            ),
            "",
        )
        has_generated_voiceover = (
            session.scalar(
                select(GenerationRun.id)
                .where(
                    GenerationRun.session_id == session_record.id,
                    GenerationRun.status == "completed",
                )
                .limit(1)
            )
            is not None
        )
        if not has_generated_voiceover:
            has_generated_voiceover = (
                session.scalar(
                    select(Artifact.id)
                    .where(
                        Artifact.session_id == session_record.id,
                        Artifact.state == "current",
                        Artifact.role.in_(("assembled_audio", "dubbing_audio")),
                    )
                    .limit(1)
                )
                is not None
            )
        if workflow_kind == "audiobook":
            applicable_groups = ["audiobook_audio", "audiobook_metadata", "cover"]
        elif workflow_kind == "subtitles":
            applicable_groups = ["export_target", "subtitle_document"]
        else:
            applicable_groups = ["export_target", "subtitle_document"]
            if has_source_video:
                applicable_groups.extend(["video_audio", "video_subtitles", "mix"])
            elif has_source_audio:
                applicable_groups.extend(["standalone_audio", "mix"])
            else:
                applicable_groups.append("standalone_audio")
        return {
            "workflow_kind": workflow_kind,
            "source_profile": source_profile,
            "source_name": source_name,
            "source_kind": source_kind,
            "source_mime_type": source_mime_type,
            "source_artifact_id": source_artifact.id if source_artifact else None,
            "source_resolution": source_resolution,
            "has_source_video": has_source_video,
            "has_source_audio": has_source_audio,
            "has_generated_voiceover": has_generated_voiceover,
            "subtitle_selection": subtitle_selection or None,
            "applicable_groups": applicable_groups,
        }

    def get(self, session_id: str, section: str) -> dict[str, Any]:
        section = self._validate_section(section)
        with self.database.session() as session:
            return self.get_in_session(session, session_id, section)

    def get_in_session(
        self,
        session: Session,
        session_id: str,
        section: str,
    ) -> dict[str, Any]:
        """Return the complete settings representation from one transaction."""

        section = self._validate_section(section)
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        global_record = session.get(AppSetting, f"defaults.{section}")
        override = session.get(SessionSetting, (session_id, section))
        global_value = (
            global_record.value_json
            if global_record and isinstance(global_record.value_json, dict)
            else {}
        )
        override_value = override.value_json if override else {}
        source_language = str(session_record.source_language or "auto")
        target_language = str(session_record.target_language or "")
        outcome = session.get(OutcomePlan, session_id)
        outcome_value = (
            outcome.value_json
            if outcome and isinstance(outcome.value_json, dict)
            else {}
        )
        inputs = outcome_value.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        generation_input = str(inputs.get("generation") or "").strip().lower()
        if not generation_input:
            has_translation = (
                session.scalar(
                    select(Artifact.id)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.role == "translation",
                        Artifact.state == "current",
                    )
                    .limit(1)
                )
                is not None
            )
            generation_input = "translation" if has_translation else "source"
        speech_language = (
            target_language
            if generation_input == "translation" and target_language
            else source_language
            if source_language != "auto"
            else ""
        )
        session_context: dict[str, Any] = {}
        output_context: dict[str, Any] = {}
        if section == "text":
            # Before provider-aware budgets existed, an explicitly stored
            # max_sentence_length was the user's manual policy.  Infer that
            # mode only from persisted global/session values; never copy an
            # inherited default into the session context.  An explicit mode
            # always wins, including a value stored in either layer.
            explicit_text = {**global_value, **override_value}
            if (
                "audiobook_chunking" not in explicit_text
                and "max_sentence_length" in explicit_text
                and explicit_text.get("max_sentence_length") is not None
            ):
                from pandrator.logic.audiobook_chunking import _manual_length

                _manual_length(explicit_text["max_sentence_length"])
                session_context = {"audiobook_chunking": "manual"}
        elif section == "stt":
            session_context = {"stt_language": source_language}
        elif section == "translation":
            session_context = {
                "source_language": source_language,
                **({"target_language": target_language} if target_language else {}),
            }
        elif section == "tts" and speech_language:
            session_context = {"language": speech_language}
        elif section == "output":
            output_context = self._output_context(session, session_record)
            # A subtitle workspace should produce a portable subtitle file
            # without requiring users to opt out of application-wide media
            # defaults. Session overrides still win when deliberately set.
            if session_record.workflow_kind == "subtitles":
                session_context = {
                    "export_mode": "subtitles",
                    "audio_mode": "preserve",
                    "subtitle_mode": "none",
                    "subtitle_selection": "source",
                }
            elif session_record.workflow_kind == "media_edit":
                session_context = {
                    "export_mode": "media",
                    "audio_mode": "preserve",
                    "subtitle_selection": "source",
                    "subtitle_mode": (
                        "soft" if output_context["has_source_video"] else "none"
                    ),
                }
            elif session_record.workflow_kind == "voiceover":
                subtitle_first = bool(
                    output_context["has_source_audio"]
                    and output_context["subtitle_selection"]
                    and not output_context["has_generated_voiceover"]
                )
                session_context = {
                    "export_mode": "media",
                    "audio_mode": (
                        "preserve"
                        if subtitle_first
                        else "mixed"
                        if output_context["has_source_audio"]
                        else "dubbing_only"
                    ),
                    "format": "wav",
                }
                if subtitle_first:
                    session_context.update(
                        {
                            "subtitle_selection": output_context["subtitle_selection"],
                            "subtitle_mode": (
                                "soft" if output_context["has_source_video"] else "none"
                            ),
                        }
                    )
            if speech_language:
                session_context["language"] = speech_language
        effective = _merge(
            BUILTIN_DEFAULTS[section],
            normalize_tts_voice_aliases(global_value) if section == "tts" else global_value,
            session_context,
            normalize_tts_voice_aliases(override_value) if section == "tts" else override_value,
        )
        if section == "subtitles":
            # Old saved custom limits are deliberate; default-valued legacy
            # snapshots may adopt automatic language profiles without a DB rewrite.
            explicit = {**global_value, **override_value}
            if "language_defaults" not in explicit and any(
                key in explicit and explicit[key] is not None
                and explicit[key] != BUILTIN_DEFAULTS["subtitles"][key]
                for key in ("max_chars_per_line", "max_cps")
            ):
                effective["language_defaults"] = False
        if section == "output" and session_record.workflow_kind == "subtitles":
            if str(effective.get("export_mode") or "").lower() not in {
                "subtitles",
                "text",
            }:
                effective["export_mode"] = "subtitles"
            effective["audio_mode"] = "preserve"
            effective["subtitle_mode"] = "none"
        elif section == "output" and session_record.workflow_kind == "voiceover":
            if str(effective.get("export_mode") or "").lower() not in {
                "media",
                "audio",
                "subtitles",
                "text",
            }:
                effective["export_mode"] = "media"
            if not output_context["has_source_audio"]:
                effective["audio_mode"] = "dubbing_only"
            elif str(effective.get("audio_mode") or "").lower() not in {
                "preserve",
                "mixed",
                "dubbing_only",
            }:
                effective["audio_mode"] = "mixed"
            if output_context["has_source_video"] and effective["export_mode"] == "media":
                # Video uses a lossless assembly intermediate; the final MP4
                # path encodes new audio as AAC when replacement/mixing occurs.
                effective["format"] = "wav"
            elif str(effective.get("format") or "").lower() not in {
                "wav",
                "mp3",
                "opus",
                "flac",
            }:
                effective["format"] = "wav"
        return {
            "section": section,
            "builtin": deepcopy(BUILTIN_DEFAULTS[section]),
            "global": deepcopy(global_value),
            "override": deepcopy(override_value),
            "session_context": session_context,
            "effective": effective,
            "context": output_context,
            "revision": override.revision if override else 0,
            "global_revision": global_record.revision if global_record else 0,
        }

    def update(
        self,
        session_id: str,
        section: str,
        expected_revision: int,
        value: dict[str, Any],
        *,
        db_session: Session | None = None,
    ) -> dict[str, Any]:
        section = self._validate_section(section)
        if db_session is not None:
            self.update_in_session(
                db_session,
                session_id,
                section,
                expected_revision,
                value,
            )
            return self.get_in_session(db_session, session_id, section)
        with self.database.immediate_session() as session:
            self.update_in_session(
                session,
                session_id,
                section,
                expected_revision,
                value,
            )
            return self.get_in_session(session, session_id, section)

    def patch(
        self,
        session_id: str,
        section: str,
        expected_revision: int,
        value: dict[str, Any],
        *,
        db_session: Session | None = None,
    ) -> dict[str, Any]:
        """Merge top-level fields into one stored session override."""

        section = self._validate_section(section)
        if db_session is not None:
            self.patch_in_session(
                db_session,
                session_id,
                section,
                expected_revision,
                value,
            )
            return self.get_in_session(db_session, session_id, section)
        with self.database.immediate_session() as session:
            self.patch_in_session(
                session,
                session_id,
                section,
                expected_revision,
                value,
            )
            return self.get_in_session(session, session_id, section)

    def patch_in_session(
        self,
        session: Session,
        session_id: str,
        section: str,
        expected_revision: int,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        """Shallow-merge submitted fields with the existing stored override.

        Inherited defaults are deliberately excluded: only the persisted
        override participates in this merge.  Nested values are replaced as
        whole fields, and ``None`` remains a literal stored value.
        """

        section = self._validate_section(section)
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        record = session.get(SessionSetting, (session_id, section))
        current_revision = record.revision if record is not None else 0
        if current_revision != expected_revision:
            raise RevisionConflict("Session settings changed in another client.")
        existing = record.value_json if record is not None else {}
        if section == "tts":
            # Normalize the submitted layer before merging: a speaker-only
            # patch must win over the previously stored voice alias.
            value = normalize_tts_voice_aliases(value)
        merged = {
            **(existing if isinstance(existing, dict) else {}),
            **dict(value),
        }
        return self.update_in_session(
            session,
            session_id,
            section,
            expected_revision,
            merged,
        )

    def update_in_session(
        self,
        session: Session,
        session_id: str,
        section: str,
        expected_revision: int,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        section = self._validate_section(section)
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        value = dict(value)
        if section == "text":
            from pandrator.logic.audiobook_chunking import (
                validate_audiobook_chunking_settings,
            )

            validate_audiobook_chunking_settings(value)
        if section == "stt":
            validate_stt_settings(value)
        if section == "tts":
            validate_voiceover_repair_settings(value)
            previous = self.get_in_session(session, session_id, section)["effective"]
            value = normalize_tts_voice_aliases(
                prepare_tts_provider_switch(previous, value)
            )
        if section == "output":
            validate_output_settings(value)
        if section == "source_passages":
            from pandrator.logic.dubbing.source_passage_settings import (
                SOURCE_PASSAGE_DEFAULTS,
                normalize_source_passage_settings,
            )

            unknown = set(value) - set(SOURCE_PASSAGE_DEFAULTS)
            if unknown:
                raise ValueError(
                    f"Unknown source_passages keys: {sorted(unknown)}"
                )
            # PUT replaces the override, including {} to restore inheritance.
            # Validate the effective candidate without storing inherited values.
            snapshot = self.get_in_session(session, session_id, section)
            candidate = {
                **snapshot["builtin"],
                **snapshot["global"],
                **snapshot.get("session_context", {}),
                **value,
            }
            try:
                normalize_source_passage_settings(candidate)
            except TypeError as error:
                raise ValueError(str(error)) from error
        if section == "output" and session_record.workflow_kind != "audiobook":
            for key in (
                "title",
                "artist",
                "album",
                "genre",
                "cover_artifact_id",
            ):
                value.pop(key, None)
            output_context = self._output_context(
                session,
                session_record,
            )
            if session_record.workflow_kind == "subtitles":
                allowed = {
                    "export_mode",
                    "subtitle_selection",
                    "subtitle_format",
                    "language",
                }
                value = {key: item for key, item in value.items() if key in allowed}
            else:
                mode = str(
                    value.get("export_mode")
                    or self.get_in_session(session, session_id, section)["effective"].get("export_mode")
                    or "media"
                ).lower()
                if output_context["has_source_video"] and mode == "media":
                    value.pop("format", None)
                    value.pop("bitrate", None)
                else:
                    for key in (
                        "subtitle_mode",
                        "video_transcode",
                        "video_tail_extension_policy",
                        "burn_video_encoder",
                        "burn_video_resolution",
                        "burn_video_quality",
                        "burn_video_speed",
                        "burn_audio_codec",
                        "burn_audio_bitrate",
                    ):
                        value.pop(key, None)
                if not output_context["has_source_audio"]:
                    value.pop("audio_mode", None)
                    for key in (
                        "mix_source_gain_db",
                        "mix_voice_gain_db",
                        "mix_voice_lufs",
                        "mix_ducking",
                        "mix_attack_ms",
                        "mix_release_ms",
                        "mix_audio_bitrate",
                    ):
                        value.pop(key, None)
        record = session.get(
            SessionSetting,
            (session_id, section),
        )
        if record is None:
            if expected_revision != 0:
                raise RevisionConflict(
                    "Session settings were created in another client."
                )
            record = SessionSetting(
                session_id=session_id,
                section=section,
                value_json=value,
                revision=1,
            )
            session.add(record)
        else:
            if expected_revision != record.revision:
                raise RevisionConflict("Session settings changed in another client.")
            session.add(
                SessionSettingHistory(
                    session_id=session_id,
                    section=section,
                    value_json=record.value_json,
                    revision=record.revision,
                )
            )
            record.value_json = value
            record.revision += 1
            record.updated_at = utcnow()
        session.flush()
        return {
            "section": section,
            "override": deepcopy(value),
            "revision": record.revision,
        }

    def resolve(
        self,
        session_id: str,
        sections: list[str] | None = None,
        run_override: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        with self.database.snapshot_session() as session:
            return self.resolve_in_session(session, session_id, sections, run_override)

    def resolve_in_session(
        self,
        session: Session,
        session_id: str,
        sections: list[str] | None = None,
        run_override: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Resolve sections and service connections from the caller's snapshot."""
        requested = sections or list(SETTING_SECTIONS)
        override = run_override or {}
        snapshots = {
            section: self.get_in_session(session, session_id, section)
            for section in requested
        }
        resolved = {
            section: _merge(
                snapshots[section]["effective"],
                normalize_subtitle_limit_override(override.get(section, {}))
                if section == "subtitles" else override.get(section, {}),
            )
            for section in requested
        }
        if "tts" in resolved:
            connections = session.get(AppSetting, "services.tts")
            connection_value = (
                connections.value_json
                if connections and isinstance(connections.value_json, dict)
                else {}
            )
            from pandrator.logic import tts_handler

            snapshot = snapshots["tts"]
            selection_seed = _merge(
                snapshot["builtin"],
                snapshot["global"],
                connection_value,
                snapshot.get("session_context", {}),
                snapshot["override"],
                override.get("tts", {}),
            )
            selected = tts_handler.get_service_config(
                selection_seed, str(selection_seed.get("service") or "XTTS")
            )
            provider_defaults = selected.get("settings") if selected else None
            if not isinstance(provider_defaults, dict):
                provider_defaults = {}
            resolved["tts"] = _merge(
                snapshot["builtin"],
                snapshot["global"],
                connection_value,
                provider_defaults,
                snapshot.get("session_context", {}),
                snapshot["override"],
                override.get("tts", {}),
            )
            if selected:
                if str(selected.get("id") or "").lower() == "kobold_qwen":
                    model = tts_handler.resolve_kobold_qwen_model(
                        resolved["tts"],
                        fallback=str(selected.get("default_model") or ""),
                    )
                else:
                    model = str(
                        resolved["tts"].get("model")
                        or selected.get("default_model")
                        or ""
                    )
                default_voices = selected.get("default_voices")
                if not isinstance(default_voices, dict):
                    default_voices = {}
                language_defaults = selected.get("default_voices_by_language")
                if not isinstance(language_defaults, dict):
                    language_defaults = {}
                model_language_defaults = language_defaults.get(model)
                if not isinstance(model_language_defaults, dict):
                    model_language_defaults = {}
                language = (
                    str(
                        resolved["tts"].get("language")
                        or resolved["tts"].get("target_language")
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if str(selected.get("id") or "").lower() == "kokoro":
                    language = tts_handler.normalize_kokoro_language_code(language)
                voice = str(
                    resolved["tts"].get("voice")
                    or model_language_defaults.get(language)
                    or default_voices.get(model)
                    or selected.get("default_voice")
                    or ""
                )
                if model:
                    resolved["tts"]["model"] = model
                if voice:
                    resolved["tts"]["voice"] = voice
        if "stt" in resolved:
            connections = session.get(AppSetting, "services.stt")
            connection_value = (
                connections.value_json
                if connections and isinstance(connections.value_json, dict)
                else {}
            )
            snapshot = snapshots["stt"]
            selection_seed = _merge(
                snapshot["builtin"],
                snapshot["global"],
                connection_value,
                snapshot.get("session_context", {}),
                snapshot["override"],
                override.get("stt", {}),
            )
            selected_id = (
                str(
                    selection_seed.get("stt_engine")
                    or selection_seed.get("stt_backend")
                    or "whisper"
                )
                .strip()
                .lower()
                .replace("-", "_")
            )
            selected = next(
                (
                    item
                    for item in connection_value.get("provider_configs", [])
                    if isinstance(item, dict)
                    and str(item.get("id") or "").strip().lower().replace("-", "_")
                    == selected_id
                ),
                None,
            )
            provider_defaults = selected.get("settings") if selected else None
            if not isinstance(provider_defaults, dict):
                provider_defaults = {}
            resolved["stt"] = _merge(
                snapshot["builtin"],
                snapshot["global"],
                connection_value,
                provider_defaults,
                snapshot.get("session_context", {}),
                snapshot["override"],
                override.get("stt", {}),
            )
        safe = _secret_free(resolved)
        return safe, stable_hash(safe)
