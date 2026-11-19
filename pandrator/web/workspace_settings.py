"""Revisioned session settings and coherent effective-settings resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def settings_provenance(
    snapshot: dict[str, Any],
    keys: Iterable[str],
    run_override: dict[str, Any] | None = None,
    effective: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Report each key's effective value and the highest layer that supplied it.

    ``snapshot`` is a layered section snapshot as returned by ``get_in_session``.
    The origin is the last layer that supplies the key, from the built-in
    defaults up to the run override: ``builtin``, ``global``, ``service``,
    ``provider``, ``session_context``, ``session_override``, ``run_override``.
    Only ``tts`` and ``stt`` have the service-connection and provider-default
    layers; every other section reports them as empty dicts. A value the
    resolver changed afterwards, such as the voiceover clamps, reports
    ``clamped`` so a caller never mistakes a rewritten value for the one
    somebody chose. A key no layer supplies but the effective section carries,
    such as the automatic subtitle profile, reports ``derived``.

    Pass ``effective`` when the caller already holds the resolved section, so the
    reported value is the one the run really uses. Without it the helper merges
    the run override over the snapshot itself, which is only right when the
    resolver had nothing further to say about the value.
    """

    layers = (
        ("builtin", snapshot.get("builtin")),
        ("global", snapshot.get("global")),
        ("service", snapshot.get("service")),
        ("provider", snapshot.get("provider")),
        ("session_context", snapshot.get("session_context")),
        ("session_override", snapshot.get("override")),
        ("run_override", run_override),
    )
    resolved_effective: dict[str, Any]
    if effective is None:
        # resolve_in_session merges the run override over the resolved section,
        # so the effective value here must include it to describe the planned run.
        resolved_effective = deepcopy(snapshot.get("effective") or {})
        for key, value in (run_override or {}).items():
            resolved_effective[key] = deepcopy(value)
    else:
        resolved_effective = dict(effective)
    fields: dict[str, dict[str, Any]] = {}
    for key in keys:
        origin: str | None = None
        supplied: Any = None
        for name, values in layers:
            if isinstance(values, dict) and key in values:
                origin, supplied = name, values[key]
        value = resolved_effective.get(key)
        if origin is None:
            # A layer-less key can only come from the resolver itself, which
            # adds it to the effective section after the layers are merged.
            normalized = "derived" if key in resolved_effective else "builtin"
        else:
            normalized = "clamped" if value != supplied else origin
        fields[key] = {
            "effective": value,
            "origin": normalized,
        }
    return fields


# The service connection and the provider defaults are the two layers the run
# resolver merges between ``global`` and ``session_context``, for ``tts`` and
# ``stt`` only. ``provider_configs`` is the connection catalogue rather than a
# setting, so the exposed layer leaves it out and the read stays small.
_HIDDEN_LAYER_CATALOGUE_KEYS = frozenset({"provider_configs"})

# The keys that decide which voice route a run takes, reported with their origin
# by the tts settings read, branch readiness and plans.
TTS_ROUTE_KEYS = ("service", "model", "voice", "language")
# Origins that mean "nobody chose this for the session".
INHERITED_SETTING_ORIGINS = frozenset({"builtin", "global", "service", "provider"})


def inherited_tts_route_warning(
    route: Mapping[str, Any],
    origins: Mapping[str, str],
    *,
    language_support: str | None,
    language: str = "",
) -> str | None:
    """Name an inherited voice route that is not verified for its language.

    Warnings never block; an ``unsupported`` decision still stops generation.
    """
    if language_support != "unverified":
        return None
    origin = next(
        (
            str(origins.get(key) or "")
            for key in ("service", "voice")
            if origins.get(key) in INHERITED_SETTING_ORIGINS
        ),
        "",
    )
    if not origin:
        return None
    names = "/".join(str(route.get(key) or "") for key in ("service", "model", "voice"))
    source = "built-in defaults" if origin == "builtin" else f"{origin} settings"
    return (
        f"The speech route {names} was inherited from the {source} and is "
        f"not verified for {language or 'this language'}. Choose a voice for this "
        "language, or confirm this one."
    )


def _setting_layer(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy one hidden layer without its provider catalogue."""
    return {
        key: deepcopy(item)
        for key, item in (value or {}).items()
        if str(key) not in _HIDDEN_LAYER_CATALOGUE_KEYS
    }


def _stt_provider_record(
    connection_value: Mapping[str, Any], engine: str
) -> dict[str, Any] | None:
    selected = next(
        (
            item
            for item in connection_value.get("provider_configs") or []
            if isinstance(item, Mapping)
            and str(item.get("id") or "").strip().lower().replace("-", "_") == engine
        ),
        None,
    )
    return dict(selected) if isinstance(selected, Mapping) else None


def _selected_service(
    section: str,
    seed: Mapping[str, Any],
    connection_value: Mapping[str, Any],
    *,
    cache: dict | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return the route's service record and the defaults that record supplies.

    This is the one place that reads the service connection layers for ``tts``
    and ``stt``. ``resolve_in_session`` merges the returned defaults as the
    ``provider`` layer and the read exposes the ``service`` layer beside it, so
    the two can never choose different providers. Pass the caller's
    request-local ``cache`` so one request builds the provider catalogue once.
    """
    selected: dict[str, Any] | None
    if section == "tts":
        from pandrator.logic import tts_handler

        selected = tts_handler.get_service_config(
            dict(seed), str(seed.get("service") or "XTTS"), _cache=cache
        )
    elif section == "stt":
        engine = (
            str(seed.get("stt_engine") or seed.get("stt_backend") or "whisper")
            .strip()
            .lower()
            .replace("-", "_")
        )
        selected = _stt_provider_record(connection_value, engine)
    else:
        return None, {}
    defaults = selected.get("settings") if isinstance(selected, Mapping) else None
    return (
        dict(selected) if isinstance(selected, Mapping) else None,
        dict(defaults) if isinstance(defaults, Mapping) else {},
    )


def _tts_service_defaults(
    effective: Mapping[str, Any], selected: Mapping[str, Any] | None
) -> dict[str, str]:
    """Return the model and voice a selected TTS service supplies by default.

    ``resolve_in_session`` fills these after the layers are merged, so a caller
    that only merges the layers reports no model or voice at all. The resolver
    logic lives here so the resolved section and the reported route cannot
    drift apart. A value the layers already supply is returned unchanged; the
    default only answers a key nobody set.
    """
    if not isinstance(selected, Mapping):
        return {}
    from pandrator.logic import tts_handler

    selected_id = str(selected.get("id") or "").lower()
    if selected_id == "kobold_qwen":
        model = tts_handler.resolve_kobold_qwen_model(
            dict(effective), fallback=str(selected.get("default_model") or "")
        )
    else:
        model = str(effective.get("model") or selected.get("default_model") or "")
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
        str(effective.get("language") or effective.get("target_language") or "")
        .strip()
        .lower()
    )
    if selected_id == "kokoro":
        language = tts_handler.normalize_kokoro_language_code(language)
    voice = str(
        effective.get("voice")
        or language_default_voice(model_language_defaults, language)
        or default_voices.get(model)
        or selected.get("default_voice")
        or ""
    )
    return {"model": model, "voice": voice}


@dataclass(slots=True)
class _SettingsReadContext:
    session_id: str
    workspace_id: str
    session_record: SessionRecord
    workspace_defaults: dict[str, WorkspaceSetting]
    session_overrides: dict[str, SessionSetting]
    outcome_value: dict[str, Any]
    generation_input: str | None = None


def _has_current_translation(session: Session, session_id: str) -> bool:
    return (
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

    def _hidden_layers_in_session(
        self,
        session: Session,
        section: str,
        workspace_id: str,
        *,
        builtin: Mapping[str, Any],
        global_value: Mapping[str, Any],
        session_context: Mapping[str, Any],
        override_value: Mapping[str, Any],
        service_config_cache: dict | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        """Resolve the service connection and provider default layers.

        They sit between ``global`` and ``session_context`` and only ``tts`` and
        ``stt`` have them. The returned service and provider copies leave the
        provider catalogue out; the selected service record is returned whole so
        a caller can ask about this route without resolving it again.
        """
        if section not in {"tts", "stt"}:
            return {}, {}, None
        row = workspace_setting_in_workspace(
            session, workspace_id, f"services.{section}"
        )
        connection_value = (
            dict(row.value_json)
            if row is not None and isinstance(row.value_json, dict)
            else {}
        )
        seed = _merge(
            dict(builtin),
            dict(global_value),
            connection_value,
            dict(session_context),
            dict(override_value),
        )
        selected, provider_defaults = _selected_service(
            section, seed, connection_value, cache=service_config_cache
        )
        return (
            _setting_layer(connection_value),
            _setting_layer(provider_defaults),
            selected,
        )

    @staticmethod
    def _tts_route(
        *,
        global_value: dict[str, Any],
        service_layer: dict[str, Any],
        provider_layer: dict[str, Any],
        session_context: dict[str, Any],
        override_value: dict[str, Any],
        selected: Mapping[str, Any] | None,
        service_config_cache: dict | None = None,
    ) -> dict[str, Any]:
        """Report the voice route this session resolves, with each origin.

        This is the session's route: a run override can still change it, and the
        plan and receipt routes are read from the resolved section, which does
        include one.
        """
        from pandrator.logic.tts_language_preflight import (
            tts_language_support_decision,
        )

        route_effective = _merge(
            BUILTIN_DEFAULTS["tts"],
            normalize_tts_voice_aliases(global_value),
            service_layer,
            provider_layer,
            session_context,
            normalize_tts_voice_aliases(override_value),
        )
        # The resolver fills a model and voice the selected service supplies by
        # default after its layers are merged. Report those same defaults here,
        # so the route a caller reads is the route a run would use. Only a key
        # no layer supplies is filled, and it is reported as provider-supplied.
        service_defaults = {
            key: value
            for key, value in _tts_service_defaults(route_effective, selected).items()
            if value and not route_effective.get(key)
        }
        route_effective.update(service_defaults)
        origins = settings_provenance(
            {
                "builtin": BUILTIN_DEFAULTS["tts"],
                "global": normalize_tts_voice_aliases(global_value),
                "service": service_layer,
                "provider": _merge(provider_layer, service_defaults),
                "session_context": session_context,
                "override": normalize_tts_voice_aliases(override_value),
                "effective": route_effective,
            },
            TTS_ROUTE_KEYS,
        )
        support = tts_language_support_decision(
            route_effective,
            endpoint=dict(selected) if selected is not None else None,
            _service_config_cache=service_config_cache,
        )
        return {
            **{key: route_effective.get(key) for key in TTS_ROUTE_KEYS},
            "origin": {key: field["origin"] for key, field in origins.items()},
            "language_support": support["decision"],
            "language_support_language": support["language"],
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
            session,
            session_id,
            section,
            actual_workspace_id,
            _service_config_cache={},
        )

    def _prepare_read_context(
        self,
        session: Session,
        session_id: str,
        workspace_id: str,
        sections: list[str],
    ) -> _SettingsReadContext:
        """Read session-wide rows once for a multi-section resolution."""
        # The public resolve API accepts case/whitespace variants, while the
        # stored keys always use canonical section names. Normalize only the
        # bulk-read keys; preserve caller-facing response keys in resolve().
        canonical_sections = [self._validate_section(section) for section in sections]
        keys = {f"defaults.{section}" for section in canonical_sections}
        override_sections = set(canonical_sections)
        if "subtitles" in override_sections:
            keys.update({"defaults.stt", "defaults.translation"})
            override_sections.update({"stt", "translation"})
        with session.no_autoflush:
            default_records = session.scalars(
                select(WorkspaceSetting)
                .where(
                    WorkspaceSetting.workspace_id == workspace_id,
                    WorkspaceSetting.key.in_(keys),
                )
                .execution_options(populate_existing=True)
            ).all()
        override_records = session.scalars(
            select(SessionSetting).where(
                SessionSetting.session_id == session_id,
                SessionSetting.section.in_(override_sections),
            )
        ).all()
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        outcome = session.get(OutcomePlan, session_id)
        outcome_value = (
            outcome.value_json
            if outcome and isinstance(outcome.value_json, dict)
            else {}
        )
        return _SettingsReadContext(
            session_id=session_id,
            workspace_id=workspace_id,
            session_record=session_record,
            workspace_defaults={
                record.key.removeprefix("defaults."): record
                for record in default_records
            },
            session_overrides={
                record.section: record for record in override_records
            },
            outcome_value=outcome_value,
        )

    def _get_in_workspace(
        self,
        session: Session,
        session_id: str,
        section: str,
        workspace_id: str,
        *,
        _read_context: _SettingsReadContext | None = None,
        _service_config_cache: dict | None = None,
    ) -> dict[str, Any]:
        """Resolve one section inside an already validated workspace."""
        section = self._validate_section(section)
        if _read_context is not None and (
            _read_context.session_id != session_id
            or _read_context.workspace_id != workspace_id
        ):
            raise ValueError(
                "Settings read context does not match the requested session and workspace."
            )
        session_record = (
            _read_context.session_record
            if _read_context is not None
            else session.get(SessionRecord, session_id)
        )
        if session_record is None:
            raise KeyError(session_id)
        read_context = _read_context
        if section == "subtitles" and read_context is None:
            read_context = self._prepare_read_context(
                session, session_id, workspace_id, ["subtitles"]
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
            if read_context is not None:
                if read_context.generation_input is None:
                    read_context.generation_input = (
                        "translation"
                        if _has_current_translation(session, session_id)
                        else "source"
                    )
                generation_input = read_context.generation_input
            else:
                generation_input = (
                    "translation"
                    if _has_current_translation(session, session_id)
                    else "source"
                )
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
        service_layer, provider_layer, selected_service = self._hidden_layers_in_session(
            session,
            section,
            workspace_id,
            builtin=result["builtin"],
            global_value=global_value,
            session_context=session_context,
            override_value=override_value,
            service_config_cache=_service_config_cache,
        )
        result["service"] = service_layer
        result["provider"] = provider_layer
        # Origins name the highest layer that supplies each key of the effective
        # section this read returns, including the service and provider layers
        # for tts and stt. Where the session layers resolve a different value
        # than the layer supplies, the helper reports ``clamped``; the resolved
        # route with its exact origins is reported separately as ``tts_route``.
        result["origins"] = {
            key: field["origin"]
            for key, field in settings_provenance(result, list(effective)).items()
        }
        if section == "tts":
            result["tts_route"] = self._tts_route(
                global_value=global_value,
                service_layer=service_layer,
                provider_layer=provider_layer,
                session_context=session_context,
                override_value=override_value,
                selected=selected_service,
                service_config_cache=_service_config_cache,
            )
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
                session,
                session_id,
                section,
                workspace_id,
                _service_config_cache={},
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
        # One request-local catalogue cache: the read resolves the service
        # connection layers and the merge below picks the same provider, so the
        # provider catalogue is built once per request, not twice.
        service_config_cache: dict = {}
        read_context = (
            self._prepare_read_context(session, session_id, workspace_id, requested)
            if len(requested) > 1
            else None
        )
        snapshots = {
            section: self._get_in_workspace(
                session,
                session_id,
                section,
                workspace_id,
                _read_context=read_context,
                _service_config_cache=service_config_cache,
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
            # The layers no longer describe the candidate, so its origin report
            # is dropped rather than left naming the stored selection.
            current_tts.pop("origins", None)
            current_tts.pop("tts_route", None)
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
            snapshot = snapshots["tts"]
            selection_seed = _merge(
                snapshot["builtin"],
                snapshot["global"],
                connection_value,
                snapshot.get("session_context", {}),
                snapshot["override"],
                override.get("tts", {}),
            )
            selected, provider_defaults = _selected_service(
                "tts", selection_seed, connection_value, cache=service_config_cache
            )
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
                for key, value in _tts_service_defaults(
                    resolved["tts"], selected
                ).items():
                    if value:
                        resolved["tts"][key] = value
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
            _selected, provider_defaults = _selected_service(
                "stt", selection_seed, connection_value
            )
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
