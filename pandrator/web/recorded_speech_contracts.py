"""Pure closed evidence for copied retained speech; never an authorization grant."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash

from .generation_cast_runtime import cast_resolution_key, remap_markup
from .generation_control_schemas import CastSettings, CharacterEntry
from .generation_rendering import build_render_parts, is_strict_single_voice
from .project_speech_references import (
    normalize_speech_reference_binding,
    speech_reference_selector,
    speech_reference_selector_hash,
)
from .speech_input_contracts import (
    BASE_VOICE_SETTING_KEYS,
    JSON_LIMIT,
    PROJECT_SPEECH_BINDING_KEY,
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
    SHA256,
    canonical_json,
    validate_speech_input_pin,
    validate_speech_source_reference,
)

PROVENANCE_KEY = "copied_speech_provenance"
_MARKERS = {PROJECT_SPEECH_INPUT_KEY, PROJECT_SPEECH_BINDING_KEY, PROJECT_SPEECH_SELECTOR_KEY}
_BINDING_FIELDS = {"voice", "voice_id", "service", "model", "voice_description"}
_RESOLUTION_FIELDS = _BINDING_FIELDS | _MARKERS | {"_recast_reference_fingerprint"}
_TTS_FIELDS = {"service", "tts_service", *BASE_VOICE_SETTING_KEYS,
               "casting_enabled", "performance_enabled", "voice_mode_version"}
_EMPTY_BASE_PLACEHOLDERS = {"voice_description", "voice_name", "elevenlabs_voice_id",
    "audio_cpp_voice_ref", "audio_cpp_voice_ref_hash", "audio_cpp_reference_text"}
_ENTRY_FIELDS = {"text_hash", "speech_xml", "source_speaker", "subtitle_timed", "voice_binding"}
_OUTER_FIELDS = {"schema_version", "plan_revision_id", "controls", "segments", "resolved_bindings"}


def _invalid() -> ValueError:
    return ValueError("Invalid copied recorded speech evidence.")


def finite_json(value: Any) -> None:
    def check(item: Any) -> None:
        if item is None or type(item) in {str, bool, int, float}:
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise _invalid()
    check(value)
    try:
        canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise _invalid() from error


def _uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _source(value: Any, pin: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    if validate_speech_source_reference(value, pin, binding) is not None:
        return deepcopy(value)
    if (type(value) is dict and set(value) == {"version", "pin", "binding", "source_voice_reference_hash"}
            and type(value["version"]) is int and value["version"] == 1
            and value["pin"] == pin and value["binding"] == binding
            and type(value["source_voice_reference_hash"]) is str
            and SHA256.fullmatch(value["source_voice_reference_hash"])):
        return deepcopy(value)
    raise _invalid()


def _marker_shapes(value: dict[str, Any], *, allow_saved_selections: bool = False
                   ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not _MARKERS <= value.keys():
        raise _invalid()
    pin = validate_speech_input_pin(value[PROJECT_SPEECH_INPUT_KEY])
    binding = normalize_speech_reference_binding(value[PROJECT_SPEECH_BINDING_KEY])
    if (pin is None and not allow_saved_selections) or binding != value[PROJECT_SPEECH_BINDING_KEY]:
        raise _invalid()
    if (type(value[PROJECT_SPEECH_SELECTOR_KEY]) is not str
            or not SHA256.fullmatch(value[PROJECT_SPEECH_SELECTOR_KEY])):
        raise _invalid()
    if PROJECT_SPEECH_SOURCE_KEY in value:
        if pin is None:
            raise _invalid()
        _source(value[PROJECT_SPEECH_SOURCE_KEY], pin, binding)
    return pin, binding


def _retained(value: dict[str, Any], *, selector: dict[str, Any] | None = None,
              allow_saved_selections: bool = False) -> dict[str, Any] | None:
    pin, binding = _marker_shapes(value, allow_saved_selections=allow_saved_selections)
    model = value.get("model") or value.get("xtts_model")
    voice = value.get("voice") or value.get("speaker")
    if binding["model"] != model or binding["voice"] != voice:
        raise _invalid()
    if selector is None:
        selector = speech_reference_selector({
            "voice": "" if value.get("voice_id") or value.get("_cast_voice_id") else voice,
            "voice_id": value.get("voice_id") or value.get("_cast_voice_id"), "model": model,
            "service": value.get("service") or value.get("tts_service") or "audio_cpp",
        })
    if value[PROJECT_SPEECH_SELECTOR_KEY] != speech_reference_selector_hash(selector):
        raise _invalid()
    return pin


def _tts(value: Any, *, base: bool = False, allow_saved_selections: bool = False) -> dict[str, Any]:
    allowed = set(BASE_VOICE_SETTING_KEYS) if base else _TTS_FIELDS
    if type(value) is not dict or not set(value) <= allowed:
        raise _invalid()
    result = deepcopy(value)
    for key, item in result.items():
        if key in _MARKERS | {PROJECT_SPEECH_SOURCE_KEY}:
            continue
        if key in {"casting_enabled", "performance_enabled"}:
            if type(item) is not bool or key == "performance_enabled" and item:
                raise _invalid()
        elif key == "voice_mode_version":
            if type(item) is not int or item != 1:
                raise _invalid()
        elif item is not None and (type(item) is not str or len(item) > 512):
            raise _invalid()
    if (_MARKERS | {PROJECT_SPEECH_SOURCE_KEY}) & result.keys():
        if base:
            _marker_shapes(result, allow_saved_selections=allow_saved_selections)
        else:
            _retained(result, allow_saved_selections=allow_saved_selections)
    return result


def _controls(value: Any, *, project: bool, allow_saved_selections: bool = False) -> dict[str, Any]:
    if type(value) is not dict:
        raise _invalid()
    allowed = {"characters", "cast", "_base_voice_settings"}
    if set(value) - allowed - ({"id", "session_id", "revision"} if project else set()):
        raise _invalid()
    if not {"characters", "cast"} <= value.keys():
        raise _invalid()
    raw = value["characters"]
    if type(raw) is not list or len(raw) > 2000:
        raise _invalid()
    characters = [CharacterEntry.model_validate(item).model_dump(mode="json") for item in raw]
    ids = [item["id"] for item in characters]
    if any(not identifier for identifier in ids) or len(ids) != len(set(ids)):
        raise _invalid()
    cast = CastSettings.model_validate(value["cast"])
    if set(cast.characters) - set(ids):
        raise _invalid()
    result = {"characters": characters, "cast": cast.model_dump(mode="json")}
    if "_base_voice_settings" in value:
        if type(value["_base_voice_settings"]) is not dict:
            raise _invalid()
        base = {key: deepcopy(item) for key, item in value["_base_voice_settings"].items()
                if key != PROJECT_SPEECH_SOURCE_KEY or item is not None}
        result["_base_voice_settings"] = _tts(base, base=True, allow_saved_selections=allow_saved_selections)
    if not project and result != value:
        raise _invalid()
    return result



def _pruned_controls(controls: dict[str, Any], entry: dict[str, Any], segment_id: str, text: str) -> dict[str, Any]:
    wanted = set()
    if entry["speech_xml"] is not None:
        parsed = parse_speech_markup(entry["speech_xml"], expected_segment_id=segment_id,
                                     expected_text=text, characters=controls["characters"])
        wanted = {span.speaker_id for span in parsed.spans if span.speaker_id is not None}
    result = deepcopy(controls)
    result["characters"] = [item for item in controls["characters"] if item["id"] in wanted]
    result["cast"]["characters"] = {key: value for key, value in controls["cast"]["characters"].items() if key in wanted}
    result["cast"]["source_speakers"] = {key: value for key, value in controls["cast"]["source_speakers"].items()
                                           if key == entry["source_speaker"]}
    if entry["voice_binding"] is not None:
        result["cast"] = CastSettings().model_dump(mode="json")
    return result


def _resolution(value: Any, selector: dict[str, Any], model: object, *,
                allow_saved_selections: bool = False) -> dict[str, Any]:
    if type(model) is not str or not model:
        raise _invalid()
    if type(value) is not dict or set(value) not in (_RESOLUTION_FIELDS, _RESOLUTION_FIELDS | {PROJECT_SPEECH_SOURCE_KEY}):
        raise _invalid()
    for key in _BINDING_FIELDS - {"voice"}:
        if value[key] != selector[key]:
            raise _invalid()
    if type(value["voice"]) is not str or not value["voice"] or not selector["voice_id"] and value["voice"] != selector["voice"]:
        raise _invalid()
    pin = _retained({**value, "model": model}, selector=selector,
                    allow_saved_selections=allow_saved_selections)
    if value["_recast_reference_fingerprint"] != ([] if pin is None else [pin["content_hash"]]):
        raise _invalid()
    return deepcopy(value)


def _audible(tts: dict[str, Any], controls: dict[str, Any], entry: dict[str, Any], segment_id: str,
             text: str, *, allow_saved_selections: bool = False
             ) -> tuple[dict[str, tuple[dict[str, Any], str]], bool]:
    required: dict[str, tuple[dict[str, Any], str]] = {}
    fallback = False
    base = {**tts, **controls.get("_base_voice_settings", {})}
    def apply(binding, settings, slot):
        nonlocal fallback
        if binding is None:
            fallback = True
        else:
            selector = speech_reference_selector(binding)
            if slot is None or not (slot.startswith("cast:") or
                    allow_saved_selections and slot == f"segment:{segment_id}"):
                raise _invalid()
            required[cast_resolution_key(selector, slot, schema_version=2)] = (selector, slot)
        return deepcopy(settings)
    build_render_parts(text, base, speech_xml=entry["speech_xml"], segment_id=segment_id,
        controls=controls, source_speaker=entry["source_speaker"], voice_binding=entry["voice_binding"],
        apply_binding_at_slot=apply,
        _bind_base=True)
    return required, fallback


def _entry_binding(entry: dict[str, Any], *, allow_saved_selections: bool) -> None:
    binding = entry["voice_binding"]
    if binding is not None and (not allow_saved_selections or speech_reference_selector(binding) != binding):
        raise _invalid()


def _mapped_resolution_key(selector: dict[str, Any], slot: str, source_id: str,
                           target_id: str) -> str:
    if slot == f"segment:{source_id}":
        slot = f"segment:{target_id}"
    return cast_resolution_key(selector, slot, schema_version=2)


def project_recorded_snapshot(snapshot: Any, *, entry_id: str, segment_id: str,
                              revision_id: str, text: str,
                              allow_saved_selections: bool = False) -> dict[str, Any]:
    """Project only finite audible evidence from an actual executor snapshot."""
    if type(snapshot) is not dict or type(snapshot.get("tts")) is not dict:
        raise _invalid()
    if any(snapshot.get(key) for key in ("performance_snapshot", "semantic_context_snapshot", "performance_context_snapshot")):
        raise _invalid()
    raw_tts = snapshot["tts"]
    tts = {key: deepcopy(value) for key, value in raw_tts.items() if key in _TTS_FIELDS
           and (key != PROJECT_SPEECH_SOURCE_KEY or value is not None)}
    tts.update(performance_enabled=False, voice_mode_version=1)
    frozen = snapshot.get("generation_control_snapshot")
    if frozen is None:
        _retained(tts, allow_saved_selections=allow_saved_selections)
        result = {"tts": tts, "generation_control_snapshot": None}
    else:
        if (type(frozen) is not dict or set(frozen) != _OUTER_FIELDS or type(frozen["schema_version"]) is not int or frozen["schema_version"] != 2
                or type(frozen["segments"]) is not dict or type(frozen["resolved_bindings"]) is not dict):
            raise _invalid()
        raw_entry = frozen["segments"].get(entry_id)
        if type(raw_entry) is not dict or not _ENTRY_FIELDS <= raw_entry.keys() or set(raw_entry) - _ENTRY_FIELDS - {"controls", "resolved_bindings"}:
            raise _invalid()
        source_controls = raw_entry.get("controls", frozen["controls"])
        if type(source_controls) is not dict:
            raise _invalid()
        controls = _controls({key: value for key, value in source_controls.items()
                              if key != "_base_voice_settings"}, project=True,
                              allow_saved_selections=allow_saved_selections)
        entry = {key: deepcopy(raw_entry[key]) for key in _ENTRY_FIELDS}
        _entry_binding(entry, allow_saved_selections=allow_saved_selections)
        if entry["text_hash"] != content_hash(text) or (entry["voice_binding"] is not None and is_strict_single_voice(raw_tts)):
            raise _invalid()
        if entry["speech_xml"] is not None:
            parse_speech_markup(entry["speech_xml"], expected_segment_id=entry_id, expected_text=text, characters=controls["characters"])
        controls = _pruned_controls(controls, entry, entry_id, text)
        tts["casting_enabled"] = entry["voice_binding"] is not None or bool(raw_tts.get("casting_enabled", True))
        required, fallback = _audible(tts, controls, entry, entry_id, text,
                                      allow_saved_selections=allow_saved_selections)
        raw_resolutions = raw_entry.get("resolved_bindings", frozen["resolved_bindings"])
        if type(raw_resolutions) is not dict:
            raise _invalid()
        resolutions = {}
        for key, (selector, slot) in required.items():
            raw_resolution = raw_resolutions.get(key)
            if (allow_saved_selections and type(raw_resolution) is dict
                    and raw_resolution.get(PROJECT_SPEECH_INPUT_KEY) is None
                    and raw_resolution.get(PROJECT_SPEECH_SOURCE_KEY) is None):
                raw_resolution = {field: item for field, item in raw_resolution.items() if field != PROJECT_SPEECH_SOURCE_KEY}
            resolutions[_mapped_resolution_key(selector, slot, entry_id, segment_id)] = _resolution(
                raw_resolution, selector, tts.get("model") or tts.get("xtts_model"),
                allow_saved_selections=allow_saved_selections)
        if entry["speech_xml"] is not None:
            entry["speech_xml"] = remap_markup(entry["speech_xml"], segment_id, text, controls["characters"])
        if fallback:
            if "_base_voice_settings" in source_controls:
                base = source_controls["_base_voice_settings"]
                if type(base) is not dict or any(key in base and base[key] not in (None, "")
                                                for key in _EMPTY_BASE_PLACEHOLDERS):
                    raise _invalid()
                controls = _controls({**controls, "_base_voice_settings": {
                    key: value for key, value in base.items() if key not in _EMPTY_BASE_PLACEHOLDERS
                }}, project=True, allow_saved_selections=allow_saved_selections)
            _retained({**tts, **controls.get("_base_voice_settings", {})}, allow_saved_selections=allow_saved_selections)
        else:
            for key in _MARKERS | {PROJECT_SPEECH_SOURCE_KEY}:
                tts.pop(key, None)
            controls.pop("_base_voice_settings", None)
        result = {"tts": tts, "generation_control_snapshot": {"schema_version": 2,
            "plan_revision_id": revision_id, "controls": controls,
            "segments": {segment_id: entry}, "resolved_bindings": resolutions}}
    return validate_recorded_snapshot(result, segment_id=segment_id, revision_id=revision_id, text=text,
                                      allow_saved_selections=allow_saved_selections)


def validate_recorded_snapshot(value: Any, *, segment_id: str, revision_id: str,
                               text: str, allow_saved_selections: bool = False) -> dict[str, Any]:
    finite_json(value)
    if (not _uuid(segment_id) or not _uuid(revision_id) or type(text) is not str
            or type(value) is not dict or set(value) != {"tts", "generation_control_snapshot"}
            or len(canonical_json(value).encode("utf-8")) > JSON_LIMIT):
        raise _invalid()
    tts = _tts(value["tts"], allow_saved_selections=allow_saved_selections)
    frozen = value["generation_control_snapshot"]
    if frozen is None:
        _retained(tts, allow_saved_selections=allow_saved_selections)
        return deepcopy(value)
    if (type(frozen) is not dict or set(frozen) != _OUTER_FIELDS
            or type(frozen["schema_version"]) is not int or frozen["schema_version"] != 2
            or frozen["plan_revision_id"] != revision_id
            or type(frozen["segments"]) is not dict or set(frozen["segments"]) != {segment_id}
            or type(frozen["resolved_bindings"]) is not dict):
        raise _invalid()
    controls = _controls(frozen["controls"], project=False, allow_saved_selections=allow_saved_selections)
    entry = frozen["segments"][segment_id]
    if (type(entry) is not dict or set(entry) != _ENTRY_FIELDS
            or type(entry["text_hash"]) is not str or entry["text_hash"] != content_hash(text)
            or type(entry["subtitle_timed"]) is not bool
            or entry["source_speaker"] is not None and (type(entry["source_speaker"]) is not str or len(entry["source_speaker"]) > 160)):
        raise _invalid()
    _entry_binding(entry, allow_saved_selections=allow_saved_selections)
    if entry["voice_binding"] is not None and tts.get("casting_enabled") is not True:
        raise _invalid()
    if entry["speech_xml"] is not None:
        if type(entry["speech_xml"]) is not str:
            raise _invalid()
        parse_speech_markup(entry["speech_xml"], expected_segment_id=segment_id, expected_text=text, characters=controls["characters"])
    if _pruned_controls(controls, entry, segment_id, text) != controls:
        raise _invalid()
    required, fallback = _audible(tts, controls, entry, segment_id, text,
                                  allow_saved_selections=allow_saved_selections)
    if set(frozen["resolved_bindings"]) != set(required):
        raise _invalid()
    for key, (selector, _slot) in required.items():
        _resolution(frozen["resolved_bindings"][key], selector, tts.get("model") or tts.get("xtts_model"),
                    allow_saved_selections=allow_saved_selections)
    if fallback:
        _retained({**tts, **controls.get("_base_voice_settings", {})}, allow_saved_selections=allow_saved_selections)
    elif "_base_voice_settings" in controls or (_MARKERS | {PROJECT_SPEECH_SOURCE_KEY}) & tts.keys():
        raise _invalid()
    return deepcopy(value)


def recorded_input_pins(value: dict[str, Any], *, allow_saved_selections: bool = False
                        ) -> list[dict[str, Any]]:
    tts = value["tts"]
    values = [tts]
    frozen = value["generation_control_snapshot"]
    if frozen is not None:
        selectors = {}
        for entry in frozen["segments"].values():
            _entry_binding(entry, allow_saved_selections=allow_saved_selections)
            if entry["voice_binding"] is not None:
                selector = entry["voice_binding"]
                selectors[speech_reference_selector_hash(selector)] = selector
        cast = CastSettings.model_validate(frozen["controls"]["cast"]).model_dump(mode="json")
        choices = [cast["narrator"]]
        for bucket in ("categories", "characters", "source_speakers"):
            choices += list(cast[bucket].values())
        for choice in choices:
            if choice is not None:
                selector = speech_reference_selector(choice)
                selectors[speech_reference_selector_hash(selector)] = selector
        values += [frozen["controls"].get("_base_voice_settings", {})]
        for resolution in frozen["resolved_bindings"].values():
            selector = selectors.get(resolution.get(PROJECT_SPEECH_SELECTOR_KEY))
            if selector is None:
                raise _invalid()
            _resolution(resolution, selector, tts.get("model") or tts.get("xtts_model"),
                        allow_saved_selections=allow_saved_selections)
            values.append(resolution)
    base = {} if frozen is None else frozen["controls"].get("_base_voice_settings", {})
    if (_MARKERS | {PROJECT_SPEECH_SOURCE_KEY}) & (tts.keys() | base.keys()):
        _retained({**tts, **base}, allow_saved_selections=allow_saved_selections)
    pins = {}
    for item in values:
        if (_MARKERS | {PROJECT_SPEECH_SOURCE_KEY}) & item.keys():
            pin, _binding = _marker_shapes(item, allow_saved_selections=allow_saved_selections)
            if pin is None:
                continue
            previous = pins.get(pin["artifact_id"])
            if previous is not None and previous != pin:
                raise _invalid()
            pins[pin["artifact_id"]] = pin
    return list(pins.values())


def remap_recorded_snapshot(value: dict[str, Any], identities: dict[str, str], *, segment_id: str,
                             revision_id: str, text: str,
                             allow_saved_selections: bool = False) -> dict[str, Any]:
    frozen_source = value.get("generation_control_snapshot")
    source_id, source_revision_id = segment_id, revision_id
    if frozen_source is not None:
        if (type(frozen_source) is not dict or type(frozen_source.get("segments")) is not dict
                or len(frozen_source["segments"]) != 1
                or type(frozen_source.get("plan_revision_id")) is not str):
            raise _invalid()
        source_id = next(iter(frozen_source["segments"]))
        source_revision_id = frozen_source["plan_revision_id"]
    validate_recorded_snapshot(value, segment_id=source_id, revision_id=source_revision_id, text=text,
                               allow_saved_selections=allow_saved_selections)
    result = deepcopy(value)
    def pin_ids(item):
        if item.get(PROJECT_SPEECH_INPUT_KEY) is not None:
            try:
                item[PROJECT_SPEECH_INPUT_KEY]["artifact_id"] = identities[item[PROJECT_SPEECH_INPUT_KEY]["artifact_id"]]
            except KeyError as error:
                raise _invalid() from error
        source = item.get(PROJECT_SPEECH_SOURCE_KEY)
        if source is not None:
            try:
                source["pin"]["artifact_id"] = identities[source["pin"]["artifact_id"]]
            except KeyError as error:
                raise _invalid() from error
    pin_ids(result["tts"])
    frozen = result["generation_control_snapshot"]
    if frozen is not None:
        entry = next(iter(frozen["segments"].values()))
        required, _fallback = _audible(result["tts"], frozen["controls"], entry, source_id, text,
                                       allow_saved_selections=allow_saved_selections)
        frozen["resolved_bindings"] = {
            _mapped_resolution_key(selector, slot, source_id, segment_id): frozen["resolved_bindings"][key]
            for key, (selector, slot) in required.items()
        }
        frozen["plan_revision_id"] = revision_id
        frozen["segments"] = {segment_id: entry}
        if entry["speech_xml"] is not None:
            entry["speech_xml"] = remap_markup(entry["speech_xml"], segment_id, text, frozen["controls"]["characters"])
        pin_ids(frozen["controls"].get("_base_voice_settings", {}))
        for resolution in frozen["resolved_bindings"].values():
            pin_ids(resolution)
    return validate_recorded_snapshot(result, segment_id=segment_id, revision_id=revision_id, text=text,
                                      allow_saved_selections=allow_saved_selections)


def recorded_provenance_version(snapshot: dict[str, Any]) -> int:
    """Choose the envelope for an already canonical, validated finite snapshot."""
    values = [snapshot["tts"]]
    frozen = snapshot["generation_control_snapshot"]
    if frozen is not None:
        if any(entry["voice_binding"] is not None for entry in frozen["segments"].values()):
            return 2
        values += [frozen["controls"].get("_base_voice_settings", {})]
        values += list(frozen["resolved_bindings"].values())
    return 2 if any(_MARKERS <= item.keys() and item[PROJECT_SPEECH_INPUT_KEY] is None for item in values) else 1


def validate_speech_provenance(value: Any, *, allow_saved_selections: bool = False) -> dict[str, Any]:
    finite_json(value)
    if (type(value) is not dict or set(value) != {"version", "segments"}
            or type(value["version"]) is not int or value["version"] not in ({1, 2} if allow_saved_selections else {1})
            or type(value["segments"]) is not dict or not value["segments"]
            or any(not _uuid(key) for key in value["segments"])):
        raise _invalid()
    # Segment text/revision validation is deliberately supplied by the owned caller.
    for snapshot in value["segments"].values():
        if type(snapshot) is not dict or set(snapshot) != {"tts", "generation_control_snapshot"}:
            raise _invalid()
    return deepcopy(value)
