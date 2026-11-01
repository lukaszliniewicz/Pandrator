"""Revisioned session settings and coherent effective-settings resolution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.preset_voice_metadata import language_default_voice
from pandrator.logic.tts_provider_switch import (
    normalize_tts_voice_aliases,
    prepare_tts_provider_switch,
)

from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .database import Database
from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SessionSettingHistory,
    WorkspaceSetting,
    WorkspaceSettingHistory,
    utcnow,
)
from .project_settings_policy import validate_project_speech_preferences
from .settings_identity import settings_workspace_id, workspace_setting_in_workspace
from .settings_policy import (
    BUILTIN_DEFAULTS,
    RUNTIME_SETTING_ALIASES,
    SETTING_SECTIONS,
    RevisionConflict,
    _merge,
    _secret_free,
    normalize_stt_engine_aliases,
    normalize_subtitle_limit_override,
    split_legacy_stt_settings,
    stable_hash,
    validate_audio_pause_settings,
    validate_dialogue_flow_setting,
    validate_output_settings,
    validate_stt_replacement,
    validate_stt_settings,
    validate_tts_retry_settings,
    validate_voiceover_repair_settings,
)
from .source_narration_settings import validate_source_narration_settings
from .source_resolution import classify_source, resolve_media_source


def _session_workspace_id(
    session: Session, session_id: str, *, workspace_id: str | None = None
) -> str:
    try:
        return settings_workspace_id(
            session, session_id=session_id, workspace_id=workspace_id
        )
    except ValueError as error:
        if session.scalar(
            select(SessionRecord.id).where(SessionRecord.id == session_id)
        ) is None:
            raise KeyError(session_id) from error
        raise


def migrate_legacy_subtitle_settings(
    session: Session,
    legacy: dict[str, Any],
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Move recognized flat fields atomically; canonical section values win."""
    if not legacy:
        return
    try:
        actual_workspace_id = (
            _session_workspace_id(session, session_id, workspace_id=workspace_id)
            if session_id is not None
            else settings_workspace_id(
                session, workspace_id=workspace_id
            )
        )
    except Exception:
        session.rollback()
        raise
    record = (
        session.get(SessionSetting, (session_id, "subtitles"))
        if session_id is not None
        else workspace_setting_in_workspace(
            session, actual_workspace_id, "defaults.subtitles"
        )
    )
    stored = dict(record.value_json or {}) if record else {}
    migrated = {**legacy, **stored}
    if migrated == stored:
        return
    if record is None:
        record = (
            SessionSetting(session_id=session_id, section="subtitles", value_json=migrated, revision=1)
            if session_id is not None
            else WorkspaceSetting(
                workspace_id=actual_workspace_id,
                key="defaults.subtitles",
                value_json=migrated,
                revision=1,
            )
        )
        session.add(record)
    else:
        history = (
            SessionSettingHistory(session_id=session_id, section="subtitles", value_json=stored, revision=record.revision)
            if session_id is not None
            else WorkspaceSettingHistory(
                workspace_id=actual_workspace_id,
                key="defaults.subtitles",
                value_json=stored,
                revision=record.revision,
            )
        )
        session.add(history)
        record.value_json = migrated
        record.revision += 1
        record.updated_at = utcnow()
    session.flush()


def subtitle_settings_provenance(
    snapshot: dict[str, Any], *, structured: dict[str, Any] | None = None,
    flat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trace only finalizer settings through the actual resolution layers."""
    aliases = RUNTIME_SETTING_ALIASES["subtitles"]
    allowed = set(aliases)
    fields: dict[str, dict[str, Any]] = {}
    for origin, values in (
        ("builtin", snapshot.get("builtin")),
        ("global", snapshot.get("global")),
        ("session_context", snapshot.get("session_context")),
        ("session_override", snapshot.get("override")),
    ):
        for key, value in (values or {}).items():
            if key in allowed:
                fields[key] = {"origin": origin, "supplied": value}
    explicit = {**(snapshot.get("global") or {}), **(snapshot.get("override") or {})}
    if "language_defaults" not in explicit:
        causes = [
            {"field": key, "origin": fields[key]["origin"]}
            for key in ("max_chars_per_line", "max_cps")
            if key in explicit and explicit[key] is not None
            and explicit[key] != BUILTIN_DEFAULTS["subtitles"][key]
        ]
        if causes:
            fields["language_defaults"] = {
                "origin": "derived_from_custom_limits", "supplied": False,
                "causes": causes,
            }
    for origin, values, runtime in (
        ("structured_run_override", structured or {}, False),
        ("flat_run_override", flat or {}, True),
    ):
        mapped = {key: aliases[key] if runtime else key for key in allowed}
        for key, alias in mapped.items():
            if alias in values:
                fields[key] = {"origin": origin, "supplied": values[alias]}
        flag = "subtitle_language_defaults" if runtime else "language_defaults"
        causes = [
            {"field": key, "origin": origin}
            for key in ("max_chars_per_line", "max_cps")
            if values.get((aliases[key] if runtime else key)) not in (None, "")
        ]
        if flag not in values and causes:
            fields["language_defaults"] = {
                "origin": "derived_from_custom_limits", "supplied": False,
                "causes": causes,
            }
    return {"fields": fields}


@dataclass(frozen=True, slots=True)
class _SubtitleSettingsReadContext:
    session_id: str
    workspace_id: str
    workspace_defaults: dict[str, WorkspaceSetting]
    session_overrides: dict[str, SessionSetting]
    outcome_value: dict[str, Any]


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
        subtitle_tracks: list[dict[str, Any]] = []
        subtitle_display_revision = None
        if workflow_kind != "audiobook" and (
            available_subtitle_roles or source_profile == "subtitles" or has_generated_voiceover
        ):
            from .export_subtitle_selection import capture_subtitle_track_snapshot

            subtitle_tracks = capture_subtitle_track_snapshot(session, session_record.id)["tracks"]
            if subtitle_tracks:
                display_revision = session.scalar(
                    select(GenerationPlanRevision)
                    .join(GenerationPlan, GenerationPlan.active_revision_id == GenerationPlanRevision.id)
                    .where(GenerationPlan.session_id == session_record.id)
                )
                if display_revision is not None:
                    display_id = str((display_revision.settings_json or {}).get("_source_artifact_id") or "")
                    pinned_ids = {item["artifact_id"] for item in subtitle_tracks}
                    if display_id not in pinned_ids and display_id:
                        speech_source = session.get(Artifact, display_id)
                        if speech_source is not None and speech_source.role == "tts_optimized":
                            display_id = str((speech_source.metadata_json or {}).get("source_artifact_id") or "")
                    if display_id in pinned_ids:
                        subtitle_display_revision = {
                            "revision_id": display_revision.id,
                            "artifact_id": display_id,
                        }
        return {
            "workflow_kind": workflow_kind,
            "source_profile": source_profile,
            "source_name": source_name,
            "source_kind": source_kind,
            "source_mime_type": source_mime_type,
            "source_artifact_id": source_artifact.id if source_artifact else None,
            "source_content_hash": source_artifact.content_hash if source_artifact else None,
            "source_revision_id": (source_artifact.metadata_json or {}).get("revision_id") if source_artifact else None,
            "source_resolution": source_resolution,
            "has_source_video": has_source_video,
            "has_source_audio": has_source_audio,
            "has_generated_voiceover": has_generated_voiceover,
            "subtitle_tracks": subtitle_tracks,
            "subtitle_display_revision": subtitle_display_revision,
            "subtitle_selection": subtitle_selection or None,
            "applicable_groups": applicable_groups,
        }

    def get(self, session_id: str, section: str) -> dict[str, Any]:
        section = self._validate_section(section)
        with self.database.snapshot_session() as session:
            return self.get_in_session(session, session_id, section)

    def get_in_session(
        self,
        session: Session,
        session_id: str,
        section: str,
    ) -> dict[str, Any]:
        """Return the complete settings representation from one transaction."""

        section = self._validate_section(section)
        require_current_session_access(session, session_id)
        actual_workspace_id = _session_workspace_id(session, session_id)
        return self._get_in_workspace(
            session, session_id, section, actual_workspace_id
        )

    def _get_in_workspace(
        self,
        session: Session,
        session_id: str,
        section: str,
        workspace_id: str,
        *,
        _read_context: _SubtitleSettingsReadContext | None = None,
    ) -> dict[str, Any]:
        """Resolve one section inside an already validated workspace."""
        section = self._validate_section(section)
        if _read_context is not None and (
            _read_context.session_id != session_id
            or _read_context.workspace_id != workspace_id
        ):
            raise ValueError(
                "Subtitle settings read context does not match the requested session and workspace."
            )
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        read_context = _read_context
        if section == "subtitles" and read_context is None:
            default_keys = (
                "defaults.subtitles",
                "defaults.stt",
                "defaults.translation",
            )
            override_sections = ("subtitles", "stt", "translation")
            with session.no_autoflush:
                default_records = session.scalars(
                    select(WorkspaceSetting)
                    .where(
                        WorkspaceSetting.workspace_id == workspace_id,
                        WorkspaceSetting.key.in_(default_keys),
                    )
                    .execution_options(populate_existing=True)
                ).all()
            override_records = session.scalars(
                select(SessionSetting).where(
                    SessionSetting.session_id == session_id,
                    SessionSetting.section.in_(override_sections),
                )
            ).all()
            outcome = session.get(OutcomePlan, session_id)
            outcome_value = (
                outcome.value_json
                if outcome and isinstance(outcome.value_json, dict)
                else {}
            )
            read_context = _SubtitleSettingsReadContext(
                session_id=session_id,
                workspace_id=workspace_id,
                workspace_defaults={
                    record.key.removeprefix("defaults."): record
                    for record in default_records
                },
                session_overrides={
                    record.section: record for record in override_records
                },
                outcome_value=outcome_value,
            )
        if read_context is None:
            global_record = workspace_setting_in_workspace(
                session, workspace_id, f"defaults.{section}"
            )
            override = session.get(SessionSetting, (session_id, section))
        else:
            global_record = read_context.workspace_defaults.get(section)
            override = read_context.session_overrides.get(section)
        global_value = (
            global_record.value_json
            if global_record and isinstance(global_record.value_json, dict)
            else {}
        )
        override_value = override.value_json if override else {}
        if section == "stt":
            global_value, _ = split_legacy_stt_settings(global_value, reject_conflicts=False)
            override_value, _ = split_legacy_stt_settings(override_value, reject_conflicts=False)
        elif section == "subtitles":
            if read_context is None:
                raise AssertionError("Subtitle read context was not prepared.")
            legacy_global = read_context.workspace_defaults.get("stt")
            legacy_override = read_context.session_overrides.get("stt")
            _, global_subtitles = split_legacy_stt_settings(
                dict(legacy_global.value_json or {}) if legacy_global else {}, reject_conflicts=False,
            )
            _, override_subtitles = split_legacy_stt_settings(
                dict(legacy_override.value_json or {}) if legacy_override else {}, reject_conflicts=False,
            )
            global_value = {**global_subtitles, **global_value}
            override_value = {**override_subtitles, **override_value}
        source_language = str(session_record.source_language or "auto")
        target_language = str(session_record.target_language or "")
        if read_context is None:
            outcome = session.get(OutcomePlan, session_id)
            outcome_value = (
                outcome.value_json
                if outcome and isinstance(outcome.value_json, dict)
                else {}
            )
        else:
            outcome_value = read_context.outcome_value
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
            # A captured named selector is binding even when it came from
            # workspace defaults. Synthesize a workflow choice only when
            # neither persisted layer supplies one.
            if "subtitle_selection" in global_value or "subtitle_selection" in override_value:
                session_context.pop("subtitle_selection", None)
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
        result = {
            "section": section,
            "workspace_id": workspace_id,
            "builtin": deepcopy(BUILTIN_DEFAULTS[section]),
            "global": deepcopy(global_value),
            "override": deepcopy(override_value),
            "session_context": session_context,
            "effective": effective,
            "context": output_context,
            "revision": override.revision if override else 0,
            "global_revision": global_record.revision if global_record else 0,
        }
        if section == "subtitles":
            from .export_subtitles import subtitle_profile
            from .settings_policy import adapt_runtime_settings

            # Translation settings may change the requested language without
            # renaming the session or its project branch. Preview the language
            # the translation workflow will actually use.
            translation = self._get_in_workspace(
                session,
                session_id,
                "translation",
                workspace_id,
                _read_context=read_context,
            )
            if target_language or any(
                translation[layer].get("target_language")
                for layer in ("global", "override")
            ):
                target_language = str(translation["effective"].get("target_language") or "")
            target_language_origin = "effective_translation_settings"
            provenance = subtitle_settings_provenance(result)
            runtime = adapt_runtime_settings("subtitles", effective)
            result["subtitle_settings_provenance"] = provenance
            result["subtitle_profiles"] = {
                "source": subtitle_profile(
                    runtime, provenance, language=source_language,
                    language_origin="session_source_language",
                ),
                "target": subtitle_profile(
                    runtime, provenance, language=target_language,
                    language_origin=target_language_origin,
                ) if target_language else None,
            }
            result["subtitle_profile_scope"] = "source_target_alternatives"
            # Resolve previews using the same finalizer as export, even while
            # the settings editor is switching an existing custom session to auto.
            automatic_runtime = {**runtime, "subtitle_language_defaults": True}
            result["subtitle_automatic_profiles"] = {
                "source": subtitle_profile(
                    automatic_runtime, provenance, language=source_language,
                    language_origin="session_source_language",
                ),
                "target": subtitle_profile(
                    automatic_runtime, provenance, language=target_language,
                    language_origin=target_language_origin,
                ) if target_language else None,
            }
        return result

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

    def tts_update_candidate_in_session(
        self,
        session: Session,
        session_id: str,
        expected_revision: int,
        value: dict[str, Any],
        *,
        patch: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve a prospective saved selection without writing or reading files."""
        require_current_session_access(session, session_id, ProjectAction.EDIT)
        validate_project_speech_preferences(session, session_id, value)
        current = self.get_in_session(session, session_id, "tts")
        if current["revision"] != expected_revision:
            raise RevisionConflict("Session settings changed in another client.")
        candidate = dict(value)
        if patch:
            candidate = {
                **dict(current["override"] or {}),
                **normalize_tts_voice_aliases(candidate),
            }
        candidate = self._prepare_tts_value(current["effective"], candidate)
        resolved, _digest = self.resolve_in_session(
            session, session_id, ["tts"], _session_tts_override=candidate
        )
        return candidate, resolved["tts"]

    @staticmethod
    def _prepare_tts_value(previous: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        validate_tts_retry_settings(value)
        validate_voiceover_repair_settings(value)
        value = dict(value)
        if "voice_mode_version" not in value and previous.get("voice_mode_version", 0):
            value["voice_mode_version"] = previous["voice_mode_version"]
        return normalize_tts_voice_aliases(prepare_tts_provider_switch(previous, value))

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
        require_current_session_access(session, session_id, ProjectAction.EDIT)
        if section == "tts":
            validate_project_speech_preferences(session, session_id, value)
        try:
            _workspace_id = _session_workspace_id(session, session_id)
        except Exception:
            session.rollback()
            raise
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
        elif section == "stt":
            # Canonicalize the submitted alias before merging, so it replaces
            # an older stt_engine value instead of being shadowed by it.
            value, submitted_subtitles = split_legacy_stt_settings(value)
            validate_stt_settings(value)
            # Keep recognized aliases until the write migrates them in the same
            # transaction. Unknown old fields remain in the merged override.
            value.update({RUNTIME_SETTING_ALIASES["subtitles"][key]: item for key, item in submitted_subtitles.items()})
            existing = normalize_stt_engine_aliases(
                existing if isinstance(existing, dict) else {},
                reject_conflicts=False,
            )
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
            _stt_fields_validated=section == "stt",
        )

    def update_in_session(
        self,
        session: Session,
        session_id: str,
        section: str,
        expected_revision: int,
        value: dict[str, Any],
        *,
        _stt_fields_validated: bool = False,
    ) -> dict[str, Any]:
        section = self._validate_section(section)
        require_current_session_access(session, session_id, ProjectAction.EDIT)
        if section == "tts":
            validate_project_speech_preferences(session, session_id, value)
        try:
            workspace_id = _session_workspace_id(session, session_id)
        except Exception:
            session.rollback()
            raise
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        value = dict(value)
        if session_record.status == "purging":
            raise RevisionConflict("Session deletion has started; its settings can no longer change.")
        if section == "audio":
            validate_audio_pause_settings(value)
        if section == "text":
            validate_dialogue_flow_setting(value)
            from pandrator.logic.audiobook_chunking import (
                validate_audiobook_chunking_settings,
            )

            validate_audiobook_chunking_settings(value)
        if section == "stt":
            value, incoming_subtitles = split_legacy_stt_settings(
                value,
                reject_conflicts=not _stt_fields_validated,
            )
            stored = session.get(SessionSetting, (session_id, section))
            previous_stt, previous_subtitles = split_legacy_stt_settings(
                dict(stored.value_json or {}) if stored else {}, reject_conflicts=False,
            )
            if not _stt_fields_validated:
                validate_stt_replacement(value, previous_stt)
            migrate_legacy_subtitle_settings(
                session,
                {**previous_subtitles, **incoming_subtitles},
                session_id=session_id,
                workspace_id=workspace_id,
            )
        if section == "tts":
            previous = self._get_in_workspace(
                session, session_id, section, workspace_id
            )["effective"]
            value = self._prepare_tts_value(previous, value)
        if section == "output":
            validate_output_settings(value)
        if section == "source_cleaning":
            validate_source_narration_settings(value)
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
            snapshot = self._get_in_workspace(
                session, session_id, section, workspace_id
            )
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
                    "subtitle_text_source",
                    "subtitle_selection",
                    "subtitle_format",
                    "language",
                }
                value = {key: item for key, item in value.items() if key in allowed}
            else:
                mode = str(
                    value.get("export_mode")
                    or self._get_in_workspace(
                        session, session_id, section, workspace_id
                    )["effective"].get("export_mode")
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
        try:
            resolved_workspace_id = _session_workspace_id(session, session_id)
            if resolved_workspace_id != workspace_id:
                raise ValueError("The session workspace changed during settings update.")
        except Exception:
            session.rollback()
            raise
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
        *,
        _session_tts_override: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Resolve sections and service connections from the caller's snapshot."""
        require_current_session_access(session, session_id)
        workspace_id = _session_workspace_id(session, session_id)
        requested = sections or list(SETTING_SECTIONS)
        override = run_override or {}
        snapshots = {
            section: self._get_in_workspace(
                session, session_id, section, workspace_id
            )
            for section in requested
        }
        if _session_tts_override is not None and "tts" in snapshots:
            candidate = normalize_tts_voice_aliases(_session_tts_override)
            current_tts = snapshots["tts"]
            current_tts["override"] = candidate
            current_tts["effective"] = _merge(
                current_tts["builtin"], current_tts["global"],
                current_tts.get("session_context", {}), candidate,
            )
        resolved = {
            section: _merge(
                snapshots[section]["effective"],
                normalize_subtitle_limit_override(override.get(section, {}))
                if section == "subtitles" else override.get(section, {}),
            )
            for section in requested
        }
        if "tts" in resolved:
            connections = workspace_setting_in_workspace(
                session, workspace_id, "services.tts"
            )
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
                    or language_default_voice(model_language_defaults, language)
                    or default_voices.get(model)
                    or selected.get("default_voice")
                    or ""
                )
                if model:
                    resolved["tts"]["model"] = model
                if voice:
                    resolved["tts"]["voice"] = voice
        if "stt" in resolved:
            connections = workspace_setting_in_workspace(
                session, workspace_id, "services.stt"
            )
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
