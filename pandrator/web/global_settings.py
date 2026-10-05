"""Global settings reads and revisioned replacement within one writer."""

from __future__ import annotations

from typing import Any

from pandrator.logic.tts_provider_switch import (
    normalize_tts_voice_aliases,
    prepare_tts_provider_switch,
)
from pandrator.runtime import DataPaths

from .credentials import (
    contains_inline_secret,
    prepare_stt_settings_for_storage,
    prepare_tts_settings_for_storage,
    redact_inline_secrets,
)
from .database import Database
from .models import AppSetting, AppSettingHistory, utcnow
from .settings_policy import (
    BUILTIN_DEFAULTS,
    SETTING_SECTIONS,
    split_legacy_stt_settings,
    validate_stt_replacement,
    validate_voiceover_repair_settings,
)
from .workspace_settings import migrate_legacy_subtitle_settings


class SettingPreconditionError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class GlobalSettingsService:
    def __init__(self, database: Database, paths: DataPaths):
        self.database = database
        self.paths = paths

    def defaults(self, section: str) -> dict[str, Any]:
        if section not in SETTING_SECTIONS:
            raise KeyError(section)
        with self.database.session() as session:
            record = session.get(AppSetting, f"defaults.{section}")
            value = (
                dict(record.value_json or {})
                if record and isinstance(record.value_json, dict)
                else {}
            )
            revision = record.revision if record else 0
            if section == "stt":
                value, _ = split_legacy_stt_settings(value, reject_conflicts=False)
            elif section == "subtitles":
                legacy = session.get(AppSetting, "defaults.stt")
                _, legacy_subtitles = split_legacy_stt_settings(
                    dict(legacy.value_json or {}) if legacy else {},
                    reject_conflicts=False,
                )
                value = {**legacy_subtitles, **value}
        return redact_inline_secrets(
            {
                "section": section,
                "builtin": BUILTIN_DEFAULTS[section],
                "value": value,
                "effective": {**BUILTIN_DEFAULTS[section], **value},
                "revision": revision,
            }
        )

    def get(self, setting_key: str) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(AppSetting, setting_key)
            if record is None:
                raise KeyError(setting_key)
            return self._payload(record)

    @staticmethod
    def _payload(record: AppSetting) -> dict[str, Any]:
        return {
            "key": record.key,
            "value": redact_inline_secrets(record.value_json),
            "revision": record.revision,
            "updated_at": record.updated_at.isoformat(),
        }

    def replace(
        self,
        setting_key: str,
        value: Any,
        revision_token: str,
    ) -> dict[str, Any]:
        """Preserve create tokens and optimistic replacement under the writer."""
        if not setting_key or len(setting_key) > 120:
            raise ValueError("Invalid setting key.")
        with self.database.immediate_session() as session:
            record = session.get(AppSetting, setting_key)
            if record is None:
                if revision_token not in {"", "0", "*"}:
                    raise SettingPreconditionError(
                        "revision_conflict",
                        "The setting does not exist at that revision.",
                        409,
                    )
            else:
                try:
                    expected = int(revision_token)
                except ValueError as error:
                    raise SettingPreconditionError(
                        "precondition_required",
                        "If-Match must contain the current setting revision.",
                        428,
                    ) from error
                if expected != record.revision:
                    raise SettingPreconditionError(
                        "revision_conflict",
                        "The setting changed in another client.",
                        409,
                    )
            prepared_value = (
                prepare_tts_settings_for_storage(
                    session,
                    self.database,
                    self.paths,
                    value,
                    record.value_json if record is not None else {},
                )
                if setting_key == "services.tts"
                else prepare_stt_settings_for_storage(
                    session,
                    self.database,
                    self.paths,
                    value,
                    record.value_json if record is not None else {},
                )
                if setting_key == "services.stt"
                else value
            )
            if setting_key == "defaults.stt":
                prepared_value, incoming_subtitles = split_legacy_stt_settings(prepared_value)
                previous_stt, previous_subtitles = split_legacy_stt_settings(
                    dict(record.value_json or {}) if record else {},
                    reject_conflicts=False,
                )
                validate_stt_replacement(prepared_value, previous_stt)
                migrate_legacy_subtitle_settings(
                    session, {**previous_subtitles, **incoming_subtitles}
                )
            if setting_key == "defaults.tts":
                validate_voiceover_repair_settings(prepared_value)
                previous = {
                    **BUILTIN_DEFAULTS["tts"],
                    **(record.value_json if record is not None else {}),
                }
                prepared_value = normalize_tts_voice_aliases(
                    prepare_tts_provider_switch(previous, prepared_value)
                )
            if setting_key == "defaults.source_passages":
                from pandrator.logic.dubbing.source_passage_settings import (
                    SOURCE_PASSAGE_DEFAULTS,
                    normalize_source_passage_settings,
                )

                if not isinstance(prepared_value, dict):
                    raise ValueError("source_passages defaults must be an object.")
                unknown = set(prepared_value) - set(SOURCE_PASSAGE_DEFAULTS)
                if unknown:
                    raise ValueError(f"Unknown source_passages keys: {sorted(unknown)}")
                # PUT replaces sparse defaults; {} restores built-in values.
                try:
                    normalize_source_passage_settings(prepared_value)
                except TypeError as error:
                    raise ValueError(str(error)) from error
            if setting_key not in {"services.tts", "services.stt"} and contains_inline_secret(
                prepared_value
            ):
                raise ValueError(
                    "API keys and other credentials must be saved in provider settings."
                )
            if record is None:
                record = AppSetting(key=setting_key, value_json=prepared_value, revision=1)
                session.add(record)
            else:
                session.add(
                    AppSettingHistory(
                        key=record.key,
                        value_json=record.value_json,
                        revision=record.revision,
                    )
                )
                record.value_json = prepared_value
                record.revision += 1
                record.updated_at = utcnow()
            session.flush()
            result = self._payload(record)
        return result
