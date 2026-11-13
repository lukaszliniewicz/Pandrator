"""Freeze casting intent and resolve it into requests within one logical segment."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Mapping
from copy import deepcopy
from typing import Any, cast

from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash, resolve_capabilities
from pandrator.logic.tts_service_identity import normalize_service_id

from . import models as m
from .abandoned_requests import raise_if_abandoned
from .access.contracts import AccessDenied, WorkspaceNotFound
from .access.voice_policy import require_current_voice_library_read
from .generation_control_schemas import VoiceBinding
from .generation_controls import get_generation_controls
from .project_speech_references import StoredProjectSpeechReference
from .projects.identity import session_project
from .speech_input_contracts import (
    BASE_VOICE_SETTING_KEYS,
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
    MissingSpeechSelection,
    PendingSpeechProof,
    PreparedSpeechSelection,
    RootSpeechBinding,
    canonical_json,
    retained_speech_selection,
    validate_speech_source_reference,
)
from .tts_providers import TtsProviderRegistry
from .voice_identity import voice_creation_workspace, voice_workspace

_VOICE_IDENTITY_KEYS = (
    "voice_id",
    "voice_description",
    "voice_name",
    "elevenlabs_voice_id",
    "_cast_voice_id",
    "audio_cpp_voice_ref",
    "audio_cpp_voice_ref_hash",
    "audio_cpp_reference_text",
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
)


def filter_segment_tts_override(
    base: Mapping[str, Any] | None, override: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Drop selected-take voice controls when the base mode is strict single.

    Alternate-take overrides remain useful for provider/model and language
    changes. Voice identity and mode flags are session-level in strict single
    mode, so a per-segment override cannot replace them.
    """
    result = deepcopy(dict(override or {}))
    from .generation_rendering import is_strict_single_voice

    if is_strict_single_voice(base if isinstance(base, Mapping) else {}):
        for key in (
            "voice",
            "speaker",
            *_VOICE_IDENTITY_KEYS,
            "casting_enabled",
            "voice_mode_version",
        ):
            result.pop(key, None)
    return result


def remap_markup(xml: str, segment_id: str, text: str, characters: list[dict]) -> str:
    # Validate before ElementTree touches the input (DTD/entity guards and bounds).
    import re

    match = re.search(r'<segment\s[^>]*\bid\s*=\s*["\']([^"\']+)', xml)
    if not match:
        raise ValueError("Speech XML must have a segment ID.")
    parsed = parse_speech_markup(
        xml,
        expected_segment_id=match.group(1),
        expected_text=text,
        characters=characters,
    )
    root = ET.fromstring(parsed.xml)
    root.set("id", segment_id)
    return parse_speech_markup(
        ET.tostring(root, encoding="unicode"),
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    ).xml


def _binding_key(binding: dict) -> str:
    return content_hash(VoiceBinding.model_validate(binding).model_dump(mode="json"))


def cast_snapshot_version(frozen: Mapping[str, Any]) -> int:
    """Return the supported frozen-cast schema version (legacy defaults to v1)."""
    if not isinstance(frozen, Mapping):
        raise ValueError("Invalid generation-control snapshot.")
    version = frozen.get("schema_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("Unsupported generation-control snapshot.")
    return version


def cast_resolution_key(
    binding: dict,
    slot_key: str | None = None,
    *,
    schema_version: int = 1,
) -> str:
    """Build the stable binding key used by one frozen-cast schema."""
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError("Unsupported generation-control snapshot.")
    normalized = VoiceBinding.model_validate(binding).model_dump(mode="json")
    if schema_version == 1:
        return _binding_key(normalized)
    canonical_slot = None
    if slot_key is not None:
        from .project_speech_references import normalize_speech_reference_slot

        canonical_slot = normalize_speech_reference_slot(slot_key)
    return content_hash({"binding": normalized, "slot_key": canonical_slot})


def lookup_cast_resolution(
    frozen: Mapping[str, Any], binding: dict, slot_key: str | None = None
) -> dict[str, Any] | None:
    """Return a defensive copy of the exact frozen resolution, with no fallback."""
    version = cast_snapshot_version(frozen)
    key = cast_resolution_key(binding, slot_key, schema_version=version)
    resolutions = frozen.get("resolved_bindings", {})
    if not isinstance(resolutions, Mapping):
        raise ValueError("Invalid frozen cast resolution map.")
    value = resolutions.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Invalid frozen cast resolution.")
    return deepcopy(dict(value))


def segment_cast_snapshot(
    frozen: Mapping[str, Any], segment_id: str
) -> dict[str, Any]:
    """Copy snapshot metadata and select this segment's independent key map."""
    result = dict(frozen)
    segments = frozen.get("segments", {})
    if not isinstance(segments, Mapping):
        raise ValueError("Invalid frozen cast segment map.")
    entry = segments.get(segment_id)
    if entry is not None and not isinstance(entry, Mapping):
        raise ValueError("Invalid frozen cast segment entry.")
    if isinstance(entry, Mapping) and "resolved_bindings" in entry:
        resolutions = entry["resolved_bindings"]
    else:
        resolutions = frozen.get("resolved_bindings", {})
    if not isinstance(resolutions, Mapping):
        raise ValueError("Invalid frozen cast resolution map.")
    result["resolved_bindings"] = dict(resolutions)
    return result


def _normalized_voice_binding(binding: dict) -> dict[str, Any]:
    return VoiceBinding.model_validate(binding).model_dump(mode="json")


def _retained_binding_markers(resolved: Mapping[str, Any]) -> bool:
    markers = (
        PROJECT_SPEECH_INPUT_KEY,
        PROJECT_SPEECH_BINDING_KEY,
        PROJECT_SPEECH_SELECTOR_KEY,
    )
    present = [key in resolved for key in markers]
    if any(present) and not all(present):
        raise ValueError("Incomplete retained cast resolution.")
    return all(present)


def validate_retained_cast_resolution(
    session,
    session_id: str,
    binding: dict,
    settings: dict,
    resolved: Mapping[str, Any],
    _service_config_cache=None,
    *, _pending_proof: PendingSpeechProof | None = None,
    _pending_revision_id: str | None = None,
    _proofs: set[tuple[Any, ...]] | None = None,
) -> dict[str, Any]:
    """Revalidate a frozen retained cast pin against current session ownership.

    ``_proofs`` holds the access and pin checks that already passed in one
    identity context or one cast freeze, so a hit skips only those database
    proofs; the selector and settings checks still run on every call. A failing
    check is never recorded, so it runs again and raises as before.
    """
    from .access.contracts import ProjectAction
    from .access.current_session_policy import require_current_session_access

    access_key = ("access", session_id)
    if _proofs is None or access_key not in _proofs:
        require_current_session_access(session, session_id, ProjectAction.READ)
        if _proofs is not None:
            _proofs.add(access_key)

    if not isinstance(resolved, Mapping):
        raise ValueError("Invalid retained cast resolution.")
    normalized = _normalized_voice_binding(binding)
    result = deepcopy(dict(resolved))
    if not _retained_binding_markers(result):
        raise ValueError("Retained cast resolution is missing its markers.")
    if result.get(PROJECT_SPEECH_SELECTOR_KEY) != _binding_key(normalized):
        raise ValueError("Retained cast resolution belongs to another cast selector.")

    from .project_speech_references import normalize_speech_reference_binding
    from .projects.identity import session_project
    from .speech_input_contracts import SpeechInputOwner, validate_speech_input_pin
    from .speech_inputs import check_speech_input, speech_input_adapter

    pin = validate_speech_input_pin(result[PROJECT_SPEECH_INPUT_KEY])
    retained_binding = normalize_speech_reference_binding(
        result[PROJECT_SPEECH_BINDING_KEY]
    )
    for name in ("voice_id", "service", "model", "voice_description"):
        if result.get(name) != normalized.get(name):
            raise ValueError("Retained cast resolution changed its source selector.")
    alias = result.get("voice")
    if type(alias) is not str or not alias or alias != retained_binding["voice"]:
        raise ValueError("Retained cast resolution disagrees with its saved voice.")
    if not normalized.get("voice_id") and alias != normalized["voice"]:
        raise ValueError("Retained cast resolution changed its selected voice.")

    adapter, _registrations = speech_input_adapter(settings)
    capability = resolve_capabilities(settings, None, _service_config_cache)
    selected_model = str(
        capability.get("model")
        or settings.get("xtts_model")
        or settings.get("model")
        or ""
    )
    if (
        normalized["voice_description"]
        or capability.get("voice_mode") in {"prebuilt", "design"}
        or adapter != "audio_cpp"
        or adapter != retained_binding["adapter"]
        or selected_model != retained_binding["model"]
    ):
        raise ValueError("Retained cast resolution is incompatible with this service or model.")

    # Validate the service/model/alias using only the pure, unmanaged resolver
    # branch. A retained pin never authorizes a source-library read.
    pure_binding = {
        **normalized,
        "voice": alias,
        "voice_id": None,
        "model": retained_binding["model"],
        "voice_description": "",
    }
    checked = resolve_binding(
        session, pure_binding, settings, _service_config_cache
    )
    if checked.get("voice") != alias:
        raise ValueError("Retained cast voice is incompatible with this selection.")

    input_key: tuple[Any, ...] | None = None
    if _proofs is not None:
        input_key = (
            "input", session_id, _pending_revision_id,
            canonical_json(pin), canonical_json(retained_binding),
        )
    if _proofs is None or input_key not in _proofs:
        project = session_project(session, session_id)
        owner = SpeechInputOwner(project.workspace_id, project.id, session_id)
        if _pending_proof is None or not _pending_proof(session, owner, _pending_revision_id, pin, retained_binding):
            check_speech_input(session, pin, owner=owner)
        if _proofs is not None and input_key is not None:
            _proofs.add(input_key)
    result[PROJECT_SPEECH_INPUT_KEY] = pin
    result[PROJECT_SPEECH_BINDING_KEY] = retained_binding
    result[PROJECT_SPEECH_SELECTOR_KEY] = _binding_key(normalized)
    result["_recast_reference_fingerprint"] = (
        [pin["content_hash"]] if pin is not None else []
    )
    return result


def resolve_project_cast_binding(
    session,
    session_id: str,
    binding: dict,
    settings: dict,
    slot_key: str | None,
    _service_config_cache=None,
    *,
    _saved_references: Mapping[str, StoredProjectSpeechReference] | None = None,
) -> dict[str, Any] | None:
    """Resolve one current-session saved cast choice without reading its source."""
    from .access.contracts import ProjectAction
    from .access.current_session_policy import require_current_session_access
    from .project_speech_references import (
        get_project_speech_reference,
        normalize_speech_reference_binding,
        normalize_speech_reference_slot,
    )
    from .speech_inputs import speech_input_adapter

    if slot_key is None:
        return None
    registry = TtsProviderRegistry()
    service = registry.service_id_for_settings(settings)
    adapter, linked_services = speech_input_adapter(settings)
    capability = resolve_capabilities(settings, None, _service_config_cache)
    if (
        service != "audio_cpp"
        or adapter != "audio_cpp"
        or capability.get("voice_mode") in {"prebuilt", "design"}
    ):
        return None

    normalized = _normalized_voice_binding(binding)
    if normalized["voice_description"]:
        return None
    actual_service = str(
        settings.get("service") or settings.get("tts_service") or service
    )
    registration_services = {
        normalize_service_id(service),
        normalize_service_id(actual_service),
    }
    registration_services.update(linked_services)
    requested_service = normalized.get("service")
    if requested_service and requested_service.casefold() not in {
        service.casefold(),
        actual_service.casefold(),
    } and normalize_service_id(requested_service) not in registration_services:
        return None
    normalized_slot = normalize_speech_reference_slot(slot_key)
    if not (normalized_slot.startswith("cast:") or normalized_slot.startswith("segment:")):
        raise ValueError("A cast snapshot needs a canonical cast or segment slot.")

    require_current_session_access(session, session_id, ProjectAction.READ)

    def guard(current_session) -> None:
        require_current_session_access(current_session, session_id, ProjectAction.READ)

    if _saved_references is None:
        stored = get_project_speech_reference(
            session, session_id, normalized_slot, authorization_guard=guard,
        )
    else:
        from .speech_input_contracts import SpeechInputOwner

        stored = _saved_references.get(normalized_slot)
        if stored is not None:
            project = session_project(session, session_id)
            if (
                stored.slot_key != normalized_slot
                or stored.owner != SpeechInputOwner(project.workspace_id, project.id, session_id)
            ):
                raise ValueError("The saved segment voice does not belong to this project.")
    if stored is None:
        return None
    selector_hash = _binding_key(normalized)
    if stored.selector_hash != selector_hash or stored.selector != normalized:
        return None
    retained_binding = normalize_speech_reference_binding(stored.binding)
    selected_model = str(
        capability.get("model")
        or settings.get("xtts_model")
        or settings.get("model")
        or ""
    )
    if (
        retained_binding["adapter"] != adapter
        or retained_binding["model"] != selected_model
        or (normalized["model"] is not None and normalized["model"] != selected_model)
        or (not normalized["voice_id"] and retained_binding["voice"] != normalized["voice"])
    ):
        return None

    resolution = {
        **normalized,
        "voice": retained_binding["voice"],
        PROJECT_SPEECH_INPUT_KEY: stored.pin,
        PROJECT_SPEECH_BINDING_KEY: retained_binding,
        PROJECT_SPEECH_SELECTOR_KEY: selector_hash,
        "_recast_reference_fingerprint": (
            [stored.pin["content_hash"]] if stored.pin is not None else []
        ),
    }
    return validate_retained_cast_resolution(
        session,
        session_id,
        normalized,
        settings,
        resolution,
        _service_config_cache,
    )


def resolve_binding(
    session, binding: dict, settings: dict, _service_config_cache=None, _registry=None,
    *, workspace_id: str | None = None,
) -> dict:
    """Resolve a managed reference once; never treat a character name as a voice."""
    binding = VoiceBinding.model_validate(binding).model_dump(mode="json")
    registry = _registry if _registry is not None else TtsProviderRegistry()
    service = registry.service_id_for_settings(settings)
    requested_service = binding.get("service")
    actual_service = str(
        settings.get("service") or settings.get("tts_service") or service
    )
    registration_services = {normalize_service_id(service), normalize_service_id(actual_service)}
    if service == "audio_cpp":
        from .speech_inputs import speech_input_adapter

        _adapter, linked_services = speech_input_adapter(settings)
        registration_services.update(linked_services)
    if requested_service and requested_service.casefold() not in {
        service.casefold(),
        actual_service.casefold(),
    } and normalize_service_id(requested_service) not in registration_services:
        raise ValueError(
            f"Cast voice is assigned to {requested_service}; this run uses {actual_service}. Reassign the cast for the selected service."
        )
    capability = resolve_capabilities(settings, None, _service_config_cache)
    model = str(
        capability.get("model")
        or settings.get("xtts_model")
        or settings.get("model")
        or ""
    )
    if binding.get("model") and model and binding["model"] != model:
        raise ValueError(
            f"Cast voice is assigned to model {binding['model']}; this run uses {model}."
        )
    voice_name = binding.get("voice") or ""
    managed_id = binding.get("voice_id")
    if (
        service == "audio_cpp"
        and capability.get("voice_mode") in {"prebuilt", "design"}
        and managed_id
    ):
        raise ValueError(
            "This model cannot use a cloned cast reference. Choose its built-in speakers or a cloning-capable model."
        )
    if service == "audio_cpp" and capability.get("voice_mode") == "prebuilt" and voice_name:
        from pandrator.logic.tts_handler import get_service_config
        config = get_service_config(settings, service, _service_config_cache) or {}
        catalogues = cast(Mapping[str, list[str]], config.get("voice_catalogues") or {})
        allowed = catalogues.get(model) or []
        if allowed and voice_name not in allowed:
            raise ValueError(f"Cast voice '{voice_name}' is not a built-in speaker for {model}.")
    if managed_id:
        workspace = voice_creation_workspace(session, workspace_id=workspace_id)
        require_current_voice_library_read(session, workspace.id)
        voice = session.scalar(select(m.Voice).where(
            m.Voice.id == managed_id, m.Voice.workspace_id == workspace.id,
        ))
        if voice is None:
            raise ValueError(f"Managed cast voice no longer exists: {managed_id}")
        voice_workspace(session, voice, workspace_id=workspace.id)
        providers = (voice.metadata_json or {}).get("providers") or {}
        registrations = [
            (key, value)
            for key, value in providers.items()
            if isinstance(value, dict)
            and normalize_service_id(key) in registration_services
        ]
        ready = next(
            (value for _, value in registrations if value.get("status") == "ready"),
            None,
        )
        if ready is None:
            raise ValueError(
                f"Publish or link '{voice.name}' to {actual_service} before casting it."
            )
        if service in {"gemini", "vertex_ai"} and ready.get(
            "resource_kind"
        ) == "designed_voice":
            from pandrator.logic import google_tts_audio, tts_handler
            from pandrator.logic.google_voice_scope import google_voice_scope

            normalized_model = tts_handler._normalize_model_for_provider(
                model, service
            )
            if normalized_model not in google_tts_audio.STRUCTURED_TTS_MODELS:
                raise ValueError(
                    "Designed Google voices require Gemini 3.8 TTS."
                )
            service_config = tts_handler.get_service_config(
                settings, service, _service_config_cache
            )
            if not service_config:
                raise ValueError(
                    "Google voice configuration is unavailable for this designed voice."
                )
            credential = tts_handler._resolve_openai_audio_api_key(service_config)
            if credential == tts_handler.XTTS_OPENAI_PLACEHOLDER_API_KEY:
                credential = ""
            try:
                scope = google_voice_scope(service, service_config, credential)
            except Exception:
                raise ValueError(
                    "Google voice credentials or project are unavailable for this designed voice."
                ) from None
            saved_scope = str(ready.get("scope_fingerprint") or "").strip()
            if not saved_scope:
                raise ValueError(
                    "Designed Google voice registration is missing its saved scope."
                )
            if scope["fingerprint"] != saved_scope:
                raise ValueError(
                    "Google voice credentials or project changed since this voice was designed."
                )
        voice_name = str(
            ready.get("provider_voice_id") or ready.get("voice_id") or voice.name
        )
    # Voice-design models have a stable voice-description channel; other models
    # must not turn a voice identity description into a local emotion.
    if binding.get("voice_description") and not capability.get(
        "voice_design"
    ):
        raise ValueError(
            "A cast voice description requires a voice-design model. Choose a provider voice or managed reference for this model."
        )
    return {**binding, "voice": voice_name}


def apply_resolved_binding(
    binding: dict | None,
    settings: dict,
    resolutions: dict[str, dict],
    *,
    slot_key: str | None = None,
    schema_version: int = 1,
    _service_config_cache=None,
) -> dict:
    result = dict(settings)
    if not binding:
        return result
    key = cast_resolution_key(
        binding, slot_key, schema_version=schema_version
    )
    if not isinstance(resolutions, Mapping):
        raise ValueError("Invalid frozen cast resolution map.")
    resolved_value = resolutions.get(key)
    if resolved_value is not None and not isinstance(resolved_value, Mapping):
        raise ValueError("Invalid frozen cast resolution.")
    resolved = dict(resolved_value) if isinstance(resolved_value, Mapping) else None
    if resolved is None:
        raise ValueError(
            "The cast assignment is absent from this run snapshot. Start a new generation run."
        )
    normalized = _normalized_voice_binding(binding)
    retained = _retained_binding_markers(resolved)
    retained_binding = None
    if retained:
        from .project_speech_references import normalize_speech_reference_binding
        from .speech_input_contracts import validate_speech_input_pin

        if resolved.get(PROJECT_SPEECH_SELECTOR_KEY) != _binding_key(normalized):
            raise ValueError("Retained cast resolution belongs to another cast selector.")
        pin = validate_speech_input_pin(resolved[PROJECT_SPEECH_INPUT_KEY])
        retained_binding = normalize_speech_reference_binding(
            resolved[PROJECT_SPEECH_BINDING_KEY]
        )
        capability = resolve_capabilities(settings, None, _service_config_cache)
        selected_model = str(
            capability.get("model")
            or settings.get("xtts_model")
            or settings.get("model")
            or ""
        )
        if selected_model != retained_binding["model"]:
            raise ValueError("Retained cast resolution is incompatible with this model.")
        if pin is None:
            resolved[PROJECT_SPEECH_INPUT_KEY] = None
        else:
            resolved[PROJECT_SPEECH_INPUT_KEY] = pin

    # Do not reuse a narrator reference waveform for a newly selected character.
    for key in (
        "_cast_voice_id",
        "voice_id",
        "voice_description",
        "audio_cpp_voice_ref",
        "audio_cpp_voice_ref_hash",
        "audio_cpp_reference_text",
        PROJECT_SPEECH_INPUT_KEY,
        PROJECT_SPEECH_BINDING_KEY,
        PROJECT_SPEECH_SELECTOR_KEY,
        PROJECT_SPEECH_SOURCE_KEY,
    ):
        result.pop(key, None)
    if retained_binding is not None:
        result["model"] = retained_binding["model"]
        result["xtts_model"] = retained_binding["model"]
        result[PROJECT_SPEECH_INPUT_KEY] = resolved[PROJECT_SPEECH_INPUT_KEY]
        result[PROJECT_SPEECH_BINDING_KEY] = retained_binding
        result[PROJECT_SPEECH_SELECTOR_KEY] = resolved[PROJECT_SPEECH_SELECTOR_KEY]
        if validate_speech_source_reference(
            resolved.get(PROJECT_SPEECH_SOURCE_KEY), resolved[PROJECT_SPEECH_INPUT_KEY], retained_binding,
        ) is not None:
            result[PROJECT_SPEECH_SOURCE_KEY] = deepcopy(resolved[PROJECT_SPEECH_SOURCE_KEY])
    if resolved.get("voice"):
        result["voice"] = result["speaker"] = resolved["voice"]
        # The native ElevenLabs adapter reads this legacy key before voice.
        if "elevenlabs_voice_id" in result:
            result["elevenlabs_voice_id"] = resolved["voice"]
    if resolved.get("voice_id"):
        result["_cast_voice_id"] = resolved["voice_id"]
    if resolved.get("voice_description"):
        general = str(
            result.get("generation_prompt")
            or result.get("openai_audio_instructions")
            or ""
        ).strip()
        description = resolved["voice_description"]
        combined = "\n".join(
            dict.fromkeys(part for part in (description, general) if part)
        )
        result["generation_prompt"] = combined
        result["openai_audio_instructions"] = combined
    if retained_binding is not None:
        from .generation_speech_inputs import retained_generation_input

        if retained_generation_input(result) is None:
            raise ValueError("Retained cast resolution has no selected speech input.")
    return result


def segment_voice_binding(segment: m.GenerationSegment, snapshot: dict | None = None) -> dict | None:
    """An alternate take's voice wins over the persistent block assignment."""
    alternate = filter_segment_tts_override(
        (snapshot or {}).get("tts"), ((snapshot or {}).get("selected_segment_override") or {}).get("tts"),
    )
    managed_id = str(alternate.get("voice_id") or "").strip()
    voice = str(alternate.get("voice") or "").strip() or str(alternate.get("speaker") or "").strip()
    if managed_id or voice:
        return VoiceBinding.model_validate({"voice_id": managed_id or None, "voice": voice}).model_dump(
            mode="json", exclude_defaults=True, exclude_none=True)
    if segment.voice_id:
        return VoiceBinding.model_validate({"voice_id": segment.voice_id}).model_dump(
            mode="json", exclude_defaults=True, exclude_none=True)
    if segment.voice:
        return VoiceBinding.model_validate({"voice": segment.voice}).model_dump(
            mode="json", exclude_defaults=True, exclude_none=True)
    return None


def apply_segment_voice(
    settings: dict, snapshot: dict, segment_id: str, *, _service_config_cache=None,
) -> dict:
    from .generation_rendering import is_strict_single_voice

    base_tts = snapshot.get("tts")
    base_tts = base_tts if isinstance(base_tts, dict) else settings
    if is_strict_single_voice(base_tts):
        # Alternate takes and frozen block assignments remain stored, but a
        # strict single-voice run uses the session's selected voice. Keep the
        # alternate's unrelated settings, such as language and performance.
        result = dict(settings)
        for key in ("voice", "speaker", *_VOICE_IDENTITY_KEYS):
            if key in base_tts:
                result[key] = deepcopy(base_tts[key])
            else:
                result.pop(key, None)
        for key in ("casting_enabled", "voice_mode_version"):
            if key in base_tts:
                result[key] = deepcopy(base_tts[key])
        return result

    frozen = snapshot.get("generation_control_snapshot") or {}
    segments = frozen.get("segments") or {}
    entry = segments.get(segment_id) or {}
    selected = segment_cast_snapshot(frozen, segment_id)
    version = cast_snapshot_version(frozen)
    from .generation_rendering import _canonical_segment_slot

    return apply_resolved_binding(
        entry.get("voice_binding"),
        settings,
        selected.get("resolved_bindings") or {},
        slot_key=_canonical_segment_slot(segment_id),
        schema_version=version,
        _service_config_cache=_service_config_cache,
    )


def freeze_cast_snapshot(
    session, revision_id: str, snapshot: dict, settings: dict,
    _service_config_cache=None,
    *,
    _settings_copy=None,
    controls_by_segment: Mapping[str, dict] | None = None,
    validate_segment_ids: Collection[str] | None = None,
    _voice_reference_index: dict[str, list[dict[str, Any]]] | None = None,
    _segment_references: Mapping[str, StoredProjectSpeechReference] | None = None,
    _missing_selection: MissingSpeechSelection | None = None,
    _root_binding: RootSpeechBinding | None = None,
    _carried_snapshot: Mapping[str, Any] | None = None,
    _freeze_segment_ids: Collection[str] | None = None,
) -> None:
    """Freeze cast entries, resolving voices only for requested validation IDs.

    By default every non-removed segment of the revision is frozen.
    ``_freeze_segment_ids`` limits the freeze itself to those segments (a page
    listing); ``validate_segment_ids`` only limits which entries must resolve.
    """
    from .generation_rendering import build_render_parts

    if _service_config_cache is None:
        _service_config_cache = {}
    # Retained pins proven once for this freeze; never kept past this call.
    proofs: set[tuple[Any, ...]] = set()
    revision = session.get(m.GenerationPlanRevision, revision_id)
    plan = session.get(m.GenerationPlan, revision.plan_id) if revision else None
    if plan is None:
        raise ValueError("The speech plan is unavailable.")
    project = session_project(session, plan.session_id)
    workspace = voice_creation_workspace(session, workspace_id=project.workspace_id)
    controls = get_generation_controls(session, plan.session_id)
    from .project_segment_speech import (
        SegmentSpeechReferenceIndex,
        has_selected_voice_override,
        uses_segment_speech_references,
    )

    segment_references = _segment_references or {}
    if _segment_references is None and uses_segment_speech_references(settings):
        segment_references = SegmentSpeechReferenceIndex(session).for_revision(plan.session_id, revision_id)
    entries, resolutions = {}, {}
    current_resolutions: dict[str, dict[str, Any]] = {}
    live_resolutions: dict[str, dict[str, Any]] = {}
    performance = (snapshot.get("performance_snapshot") or {}).get("annotations") or {}
    reference_index = _voice_reference_index
    reference_index_denied = False
    validate_ids = set(validate_segment_ids) if validate_segment_ids is not None else None
    # One private copy shared by every segment without its own controls. Only
    # top-level keys are set on a segment's scoped controls, so each segment
    # takes a shallow copy of it; every stored copy is still a deep copy.
    shared_controls = deepcopy(controls)
    # Validated characters and cast of the controls shared by those segments.
    validated_controls: dict[str, tuple[object, Any]] = {}

    segment_query = select(m.GenerationSegment).where(
        m.GenerationSegment.plan_revision_id == revision_id,
        m.GenerationSegment.removed.is_(False),
    )
    if _freeze_segment_ids is not None:
        segment_query = segment_query.where(m.GenerationSegment.id.in_(list(_freeze_segment_ids)))
    for segment in session.scalars(segment_query):
        raise_if_abandoned()
        carried_entry = None
        if _carried_snapshot is not None:
            if (cast_snapshot_version(_carried_snapshot) != 2
                    or _carried_snapshot.get("plan_revision_id") != revision_id):
                raise ValueError("Carried cast snapshot belongs to another revision.")
            carried_entry = (_carried_snapshot.get("segments") or {}).get(segment.id)
            if carried_entry is None or carried_entry.get("text_hash") != content_hash(segment.optimized_text or segment.text):
                raise ValueError("Carried cast text changed after freezing.")
        scoped_controls: Any
        if carried_entry is not None:
            assert _carried_snapshot is not None
            scoped_controls = deepcopy(carried_entry.get("controls", _carried_snapshot["controls"]))
            scoped_controls["_retained_cast_resolutions"] = segment_cast_snapshot(
                _carried_snapshot, segment.id,
            )["resolved_bindings"]
        elif controls_by_segment is not None and segment.id in controls_by_segment:
            scoped_controls = deepcopy(controls_by_segment[segment.id])
        else:
            scoped_controls = dict(shared_controls)
        text = segment.optimized_text or segment.text
        record = (performance.get(segment.id) or {}).get("annotation") or {}
        xml = record.get("_speech_xml") or (segment.speech_plan_json or {}).get(
            "speech_xml"
        )
        if carried_entry is not None:
            xml = carried_entry.get("speech_xml")
        if xml:
            xml = remap_markup(xml, segment.id, text, scoped_controls["characters"])
        voice_binding = segment_voice_binding(segment, snapshot)
        if carried_entry is not None:
            voice_binding = deepcopy(carried_entry.get("voice_binding"))
        saved_segment = segment_references.get(f"segment:{segment.id}")
        if saved_segment is not None and carried_entry is None and not has_selected_voice_override(snapshot):
            if (
                saved_segment.slot_key != f"segment:{segment.id}"
                or saved_segment.owner.session_id != plan.session_id
                or saved_segment.owner.project_id != project.id
                or saved_segment.owner.workspace_id != workspace.id
            ):
                raise ValueError("The saved segment voice does not belong to this project.")
            voice_binding = deepcopy(saved_segment.selector)
        entry = {
            "text_hash": content_hash(text),
            "speech_xml": xml,
            "source_speaker": carried_entry.get("source_speaker") if carried_entry is not None else segment.speaker,
            "subtitle_timed": carried_entry.get("subtitle_timed", False) if carried_entry is not None else segment.node_kind == "subtitle_cue",
            "voice_binding": voice_binding,
        }
        entries[segment.id] = entry
        if carried_entry is not None or (controls_by_segment is not None and segment.id in controls_by_segment):
            stored_controls = deepcopy(scoped_controls)
            stored_controls.pop("_retained_cast_resolutions", None)
            entry["controls"] = stored_controls
        requested = validate_ids is None or segment.id in validate_ids
        audible_preparation = _missing_selection is not None or _root_binding is not None
        if not requested and not audible_preparation:
            continue
        base = dict(settings)
        base.update(deepcopy(scoped_controls.get("_base_voice_settings") or {}))
        if segment.voice and carried_entry is None:
            base["voice"] = base["speaker"] = segment.voice
        if segment.language:
            base["language"] = segment.language

        scoped_resolutions: dict[str, dict[str, Any]] = {}
        scoped_resolution_conflict = [False]
        carries_retained_resolutions = "_retained_cast_resolutions" in scoped_controls
        carried_resolutions = scoped_controls.get("_retained_cast_resolutions")
        if carries_retained_resolutions and not isinstance(carried_resolutions, Mapping):
            raise ValueError("Invalid retained cast resolution map.")

        def apply_at_slot(
            binding,
            part_settings,
            slot_key,
            *,
            _scoped_resolutions=scoped_resolutions,
            _carries_retained_resolutions=carries_retained_resolutions,
            _carried_resolutions=carried_resolutions,
            _scoped_resolution_conflict=scoped_resolution_conflict,
            _scoped_controls=scoped_controls, _entry=entry, _requested=requested,
        ):
            nonlocal reference_index, reference_index_denied
            if not binding and _root_binding is not None:
                bound = _root_binding(session, part_settings, _requested)
                fallback = {key: deepcopy(bound.get(key)) for key in BASE_VOICE_SETTING_KEYS}
                _scoped_controls["_base_voice_settings"] = fallback
                stored_controls = deepcopy(_scoped_controls)
                stored_controls.pop("_retained_cast_resolutions", None)
                _entry["controls"] = stored_controls
                return bound
            if not binding:
                return apply_resolved_binding(
                    binding,
                    part_settings,
                    _scoped_resolutions,
                    slot_key=slot_key,
                    schema_version=2,
                    _service_config_cache=_service_config_cache,
                )
            key = cast_resolution_key(binding, slot_key, schema_version=2)
            if key not in _scoped_resolutions:
                resolved = None
                use_carried = False
                if _carries_retained_resolutions and key in _carried_resolutions:
                    carried = _carried_resolutions[key]
                    if not isinstance(carried, Mapping):
                        raise ValueError("Invalid retained cast resolution.")
                    if _retained_binding_markers(carried):
                        resolved = validate_retained_cast_resolution(
                            session,
                            plan.session_id,
                            binding,
                            part_settings,
                            carried,
                            _service_config_cache,
                            _proofs=proofs,
                        )
                        use_carried = True
                if not use_carried:
                    current = current_resolutions.get(key)
                    metadata_only = (
                        current is not None and _requested and _missing_selection is not None
                        and not _retained_binding_markers(current)
                    )
                    if current is not None and not metadata_only:
                        resolved = deepcopy(current)
                    else:
                        if slot_key is not None:
                            resolved = resolve_project_cast_binding(
                                session,
                                plan.session_id,
                                binding,
                                part_settings,
                                slot_key,
                                _service_config_cache,
                                _saved_references=(
                                    segment_references if slot_key.startswith("segment:") else None
                                ),
                            )
                        if resolved is None and _missing_selection is not None and _requested:
                            from .project_speech_references import (
                                normalize_speech_reference_binding,
                            )
                            from .speech_input_contracts import validate_speech_input_pin

                            normalized = _normalized_voice_binding(binding)
                            selected = dict(part_settings)
                            for name in _VOICE_IDENTITY_KEYS:
                                selected.pop(name, None)
                            selected.update(voice=normalized["voice"], speaker=normalized["voice"],
                                            voice_id=normalized["voice_id"])
                            from .speech_inputs import speech_input_adapter

                            eligible = retained_speech_selection(selected,
                                speech_input_adapter(selected)[0],
                                resolve_capabilities(selected, None, _service_config_cache))
                            fresh = (_missing_selection(session, normalized, eligible[1])
                                     if eligible is not None and not normalized["voice_description"] else None)
                            if fresh is not None:
                                assert eligible is not None
                                if not isinstance(fresh, PreparedSpeechSelection):
                                    raise ValueError("Invalid prepared cast selection.")
                                retained_binding = normalize_speech_reference_binding(fresh.binding)
                                pin = validate_speech_input_pin(fresh.pin)
                                if (fresh.selector_json != canonical_json(normalized)
                                        or retained_binding["adapter"] != "audio_cpp"
                                        or retained_binding["model"] != eligible[1]["model"]
                                        or (not normalized["voice_id"] and retained_binding["voice"] != normalized["voice"])):
                                    raise ValueError("Prepared cast selection disagrees with its selector.")
                                # This new pin has no DB graph until final publication.
                                resolve_binding(session, {**normalized, "voice_id": None,
                                    "voice": retained_binding["voice"]}, selected, _service_config_cache)
                                resolved = {**normalized, "voice": retained_binding["voice"],
                                    PROJECT_SPEECH_INPUT_KEY: pin,
                                    PROJECT_SPEECH_BINDING_KEY: retained_binding,
                                    PROJECT_SPEECH_SELECTOR_KEY: _binding_key(normalized),
                                    "_recast_reference_fingerprint": [pin["content_hash"]] if pin else []}
                                if fresh.source is not None:
                                    resolved[PROJECT_SPEECH_SOURCE_KEY] = fresh.source
                        if resolved is None:
                            binding_key = _binding_key(binding)
                            cached = live_resolutions.get(binding_key)
                            if cached is None:
                                resolved = resolve_binding(
                                    session,
                                    binding,
                                    part_settings,
                                    _service_config_cache,
                                    workspace_id=workspace.id,
                                )
                                live_resolutions[binding_key] = deepcopy(resolved)
                            else:
                                resolved = deepcopy(cached)

                            # Persist material evidence only for current live
                            # resolution; retained pins carry their own hash.
                            if reference_index is None and not reference_index_denied:
                                from .generation_voice_references import voice_reference_index

                                try:
                                    reference_index = voice_reference_index(
                                        session, workspace_id=workspace.id
                                    )
                                except (AccessDenied, WorkspaceNotFound):
                                    if binding.get("voice_id"):
                                        raise
                                    reference_index_denied = True
                            if reference_index is not None:
                                names = [resolved.get("voice"), resolved.get("voice_id")]
                                references = [
                                    reference
                                    for name in names
                                    if name
                                    for reference in reference_index.get(
                                        str(name).strip().casefold().removesuffix(".wav"), []
                                    )
                                ]
                                resolved["_recast_reference_fingerprint"] = sorted(
                                    {content_hash(reference) for reference in references}
                                )
                        current_resolutions[key] = deepcopy(resolved)

                if resolved is None:
                    raise ValueError("Cast voice resolution is unavailable.")
                _scoped_resolutions[key] = deepcopy(resolved)
                existing = resolutions.get(key)
                if existing is None:
                    resolutions[key] = deepcopy(resolved)
                elif existing != resolved:
                    if (not _retained_binding_markers(existing)
                            and _retained_binding_markers(resolved)):
                        # A requested choice upgrades earlier ordinary metadata
                        # for this exact shared selector to its one retained graph.
                        resolutions[key] = deepcopy(resolved)
                        for previous_entry in entries.values():
                            local = previous_entry.get("resolved_bindings")
                            if local is not None and local.get(key) == existing:
                                local[key] = deepcopy(resolved)
                    else:
                        # Distinct saved/historical pins keep independent maps.
                        _scoped_resolution_conflict[0] = True
            return apply_resolved_binding(
                binding,
                part_settings,
                _scoped_resolutions,
                slot_key=slot_key,
                schema_version=2,
                _service_config_cache=_service_config_cache,
            )

        try:
            build_render_parts(
                text,
                base,
                speech_xml=xml,
                segment_id=segment.id,
                controls=scoped_controls,
                source_speaker=entry["source_speaker"],
                voice_binding=entry["voice_binding"],
                apply_binding_at_slot=apply_at_slot,
                _settings_copy=_settings_copy,
                _bind_base=_root_binding is not None,
                _read_only_controls=True,
                _validated_controls=validated_controls,
            )
        except (ValueError, AccessDenied, WorkspaceNotFound):
            if requested:
                raise
            # Whole-plan identities still need ordinary metadata for untouched
            # takes. An unavailable unrequested choice stays fail-closed.

        if scoped_resolutions and (
            carries_retained_resolutions or scoped_resolution_conflict[0]
        ):
            # An entry map is complete for this segment's selectors and takes
            # precedence over the shared outer map when reading the snapshot.
            entry["resolved_bindings"] = deepcopy(scoped_resolutions)
    snapshot["generation_control_snapshot"] = {
        "schema_version": 2,
        "plan_revision_id": revision_id,
        "controls": deepcopy(controls),
        "segments": entries,
        "resolved_bindings": resolutions,
    }


def segment_render_parts(
    settings: dict, snapshot: dict, segment_id: str, text: str,
    *, _settings_copy=None,
    _validate_binding: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], None] | None = None,
    _service_config_cache=None,
    _read_only_controls: bool = False,
    _validated_controls: dict[str, tuple[object, Any]] | None = None,
) -> list[dict[str, Any]]:
    from .generation_rendering import build_render_parts

    frozen = snapshot.get("generation_control_snapshot") or {}
    if not frozen:
        if settings.get("casting_enabled"):
            raise ValueError(
                "Casting requires a frozen character/cast snapshot. Start a new run."
            )
        return build_render_parts(text, settings, _settings_copy=_settings_copy)
    version = cast_snapshot_version(frozen)
    segments = frozen.get("segments") or {}
    if not isinstance(segments, Mapping):
        raise ValueError("Invalid frozen cast segment map.")
    entry = segments.get(segment_id)
    if not entry or entry.get("text_hash") != content_hash(text):
        raise ValueError(
            "Spoken text changed after the cast was frozen. Review a new speech plan."
        )
    selected = segment_cast_snapshot(frozen, segment_id)
    settings = dict(settings)
    controls = entry.get("controls", frozen["controls"])
    # A scoped recast retains recorded narrator fallback identity while using
    # the current run's provider, model, language and delivery settings.
    for key, value in (controls.get("_base_voice_settings") or {}).items():
        if value is None and not (key == PROJECT_SPEECH_INPUT_KEY and (controls.get("_base_voice_settings") or {}).get(PROJECT_SPEECH_BINDING_KEY)):
            settings.pop(key, None)
        else:
            settings[key] = deepcopy(value)
    audio = snapshot.get("audio") or {}
    settings["in_segment_voice_change_silence_ms"] = int(
        audio.get("in_segment_voice_change_silence_ms", audio.get("voice_change_silence_ms", 0)) or 0
    )
    settings["_subtitle_timed"] = bool(entry.get("subtitle_timed"))
    if version == 1:
        return build_render_parts(
            text,
            settings,
            speech_xml=entry.get("speech_xml"),
            segment_id=segment_id,
            controls=controls,
            source_speaker=entry.get("source_speaker"),
            voice_binding=entry.get("voice_binding"),
            apply_binding=lambda binding, base: apply_resolved_binding(
                binding,
                base,
                selected["resolved_bindings"],
                schema_version=1,
                _service_config_cache=_service_config_cache,
            ),
            _settings_copy=_settings_copy,
            _read_only_controls=_read_only_controls,
            _validated_controls=_validated_controls,
        )
    def apply_at_slot(binding, base, slot_key):
        if binding is not None and _validate_binding is not None:
            resolved = lookup_cast_resolution(selected, binding, slot_key)
            if resolved is None:
                raise ValueError("The cast assignment is absent from this run snapshot.")
            _validate_binding(binding, base, resolved)
        return apply_resolved_binding(binding, base, selected["resolved_bindings"],
            slot_key=slot_key, schema_version=2,
            _service_config_cache=_service_config_cache)

    return build_render_parts(
        text,
        settings,
        speech_xml=entry.get("speech_xml"),
        segment_id=segment_id,
        controls=controls,
        source_speaker=entry.get("source_speaker"),
        voice_binding=entry.get("voice_binding"),
        apply_binding_at_slot=apply_at_slot,
        _settings_copy=_settings_copy,
        _bind_base=True,
        _read_only_controls=_read_only_controls,
        _validated_controls=_validated_controls,
    )


def preview_render_parts(
    session,
    session_id: str,
    text: str,
    segment_id: str,
    settings: dict,
    xml: str | None,
    source_speaker: str | None = None,
    *,
    controls: dict[str, Any] | None = None,
) -> list[dict]:
    from .access.contracts import ProjectAction
    from .access.current_session_policy import require_current_session_access
    from .generation_rendering import build_render_parts

    require_current_session_access(session, session_id, ProjectAction.READ)
    project = session_project(session, session_id)
    workspace = voice_creation_workspace(session, workspace_id=project.workspace_id)
    if controls is None:
        controls = get_generation_controls(session, session_id)
    resolutions: dict[str, dict[str, Any]] = {}
    live_resolutions: dict[str, dict[str, Any]] = {}
    service_config_cache: dict[str, Any] = {}

    def apply_at_slot(binding, base, slot_key):
        if not binding:
            return apply_resolved_binding(
                binding, base, resolutions, slot_key=slot_key, schema_version=2,
                _service_config_cache=service_config_cache,
            )
        key = cast_resolution_key(binding, slot_key, schema_version=2)
        if key not in resolutions:
            resolved = None
            if slot_key is not None:
                resolved = resolve_project_cast_binding(
                    session,
                    session_id,
                    binding,
                    base,
                    slot_key,
                    service_config_cache,
                    _saved_references=(
                        segment_references if slot_key.startswith("segment:") else None
                    ),
                )
            if resolved is None:
                binding_key = _binding_key(binding)
                resolved = live_resolutions.get(binding_key)
                if resolved is None:
                    resolved = resolve_binding(
                        session,
                        binding,
                        base,
                        service_config_cache,
                        workspace_id=workspace.id,
                    )
                    live_resolutions[binding_key] = deepcopy(resolved)
            resolutions[key] = deepcopy(resolved)
        return apply_resolved_binding(
            binding,
            base,
            resolutions,
            slot_key=slot_key,
            schema_version=2,
            _service_config_cache=service_config_cache,
        )

    segment = session.scalar(
        select(m.GenerationSegment)
        .join(m.GenerationPlanRevision, m.GenerationPlanRevision.id == m.GenerationSegment.plan_revision_id)
        .join(m.GenerationPlan, m.GenerationPlan.id == m.GenerationPlanRevision.plan_id)
        .where(m.GenerationSegment.id == segment_id, m.GenerationPlan.session_id == session_id)
    )
    settings = dict(settings)
    settings["_subtitle_timed"] = bool(segment and segment.node_kind == "subtitle_cue")
    voice_binding = segment_voice_binding(segment) if segment is not None else None
    segment_references = None
    from .project_segment_speech import uses_segment_speech_references

    if segment is not None and uses_segment_speech_references(settings):
        from .project_speech_references import get_project_speech_reference

        saved = get_project_speech_reference(
            session, session_id, f"segment:{segment_id}",
            authorization_guard=lambda current: require_current_session_access(current, session_id),
        )
        segment_references = {saved.slot_key: saved} if saved is not None else {}
        if saved is not None:
            voice_binding = deepcopy(saved.selector)
    return build_render_parts(
        text,
        settings,
        speech_xml=xml,
        segment_id=segment_id,
        controls=controls,
        source_speaker=source_speaker,
        voice_binding=voice_binding,
        apply_binding_at_slot=apply_at_slot,
    )


def combine_source_markup(
    units: list[tuple[str, str | None]],
    segment_id: str,
    text: str,
    characters: list[dict],
) -> str:
    """Reconcile whole source units with an accepted block, never fabricate offsets."""
    from pandrator.logic.speech_markup import plain_speech_markup

    root = ET.Element("segment", {"id": segment_id})
    cursor = 0
    for index, (source_text, xml) in enumerate(units):
        spoken = source_text.strip()
        position = text.find(spoken, cursor)
        if position < 0 or text[cursor:position].strip():
            raise ValueError(
                "Speech-block wording no longer matches its dialogue annotation. Keep annotated units integral or reannotate the accepted text."
            )
        separator = text[cursor:position]
        if len(root):
            root[-1].tail = (root[-1].tail or "") + separator
        else:
            root.text = (root.text or "") + separator
        canonical = (
            remap_markup(xml, str(index + 1), spoken, characters)
            if xml
            else plain_speech_markup(str(index + 1), spoken)
        )
        child = ET.fromstring(canonical)
        boundary = child.attrib.get("boundary_after")
        child.tag = "span"
        child.attrib.clear()
        root.append(child)
        if boundary:
            root.set("boundary_after", boundary)
        cursor = position + len(spoken)
    if text[cursor:].strip():
        raise ValueError(
            "Speech-block text contains words absent from its source dialogue annotations."
        )
    if len(root):
        root[-1].tail = (root[-1].tail or "") + text[cursor:]
    return parse_speech_markup(
        ET.tostring(root, encoding="unicode"),
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    ).xml
