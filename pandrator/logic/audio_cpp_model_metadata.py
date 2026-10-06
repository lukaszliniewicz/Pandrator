"""Resolve audio.cpp model metadata independently of speech request preparation."""

from collections.abc import Mapping, Sequence
from typing import TypeAlias

ModelCatalog: TypeAlias = Sequence[Mapping[str, object]]


def _audio_cpp_model_metadata(
    model: str, endpoint: dict, *, model_catalog: ModelCatalog
) -> dict[str, object]:
    normalized = str(model or "").strip().casefold()
    configured_modes = endpoint.get("model_voice_modes")
    mode = ""
    if isinstance(configured_modes, dict):
        mode = next(
            (
                str(value or "").strip().lower()
                for key, value in configured_modes.items()
                if str(key or "").strip().casefold() == normalized
            ),
            "",
        )
    live_item: dict[str, object] = {}
    configured_catalog = endpoint.get("model_catalog")
    if isinstance(configured_catalog, list):
        live_item = next(
            (
                dict(item)
                for item in configured_catalog
                if isinstance(item, dict)
                and str(item.get("id") or "").strip().casefold() == normalized
            ),
            {},
        )
    for item in model_catalog:
        if str(item.get("id") or "").casefold() == normalized:
            result = {**item}
            for key in ("family", "voice_mode", "experimental"):
                if live_item.get(key) is not None:
                    result[key] = live_item[key]
            if live_item.get("mode") and not result.get("voice_mode"):
                result["voice_mode"] = live_item["mode"]
            if mode:
                result["voice_mode"] = mode
            return result
    if "voicedesign" in normalized:
        inferred_mode = "design"
    elif "customvoice" in normalized or "magpie" in normalized:
        inferred_mode = "prebuilt"
    elif "pocket" in normalized:
        inferred_mode = "hybrid"
    elif "breeze" in normalized:
        inferred_mode = "optional_cloning"
    elif normalized in {"chatterbox", "chatterbox_q8_0"}:
        inferred_mode = "cloning"
    else:
        # Do not send reference recordings to an unfamiliar model on a guess.
        inferred_mode = "unknown"
    if "qwen" in normalized:
        family = "qwen3_tts"
    elif "fish" in normalized:
        family = "fish_audio_s2"
    elif "voxcpm" in normalized:
        family = "voxcpm2"
    elif "chatterbox" in normalized:
        family = "chatterbox"
    elif "omni" in normalized:
        family = "omnivoice"
    elif "pocket" in normalized:
        family = "pocket_tts"
    elif "firered" in normalized:
        family = "fireredtts3"
    elif "magpie" in normalized:
        family = "magpie_tts"
    elif "breeze" in normalized:
        family = "breeze_tts"
    else:
        family = ""
    if live_item:
        result = {"id": model, **live_item}
        if mode:
            result["voice_mode"] = mode
        elif not result.get("voice_mode"):
            live_family = str(result.get("family") or family).strip().lower()
            result["voice_mode"] = (
                "optional_cloning" if live_family == "breeze_tts" else inferred_mode
            )
        if not result.get("family") and family:
            result["family"] = family
        if result.get("voice_mode") == "unknown":
            live_family = str(result.get("family") or "")
            task = str(result.get("task") or "")
            if task in {"clon", "clone"}:
                result["voice_mode"] = "cloning"
            elif task in {"vdes", "design"}:
                result["voice_mode"] = "design"
            else:
                modes = {
                    entry["voice_mode"]
                    for entry in model_catalog
                    if entry.get("family") == live_family
                }
                if len(modes) == 1:
                    result["voice_mode"] = modes.pop()
        return result
    if mode:
        return {"id": model, "family": family, "voice_mode": mode}
    return {"id": model, "family": family, "voice_mode": inferred_mode}

