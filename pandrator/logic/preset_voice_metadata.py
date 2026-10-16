"""Descriptive preset identities, kept separate from synthesis language support.

Voxtral locale groups are preferences, not language gates. Qwen's speaker
profiles likewise describe native voices without restricting target languages.
Provider IDs and explicit saved names/defaults remain unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..constants import (
    AUDIO_CPP_KOKORO_VOICES,
    KOKORO_PREFIX_LANGUAGE_CODES,
    KOKORO_VOICE_LANGUAGE_GROUPS,
)
from .audio_cpp_inventory_data import family_metadata, inventory

_AUDIO_CPP_SOURCE = (
    "https://github.com/0xShug0/audio.cpp/blob/"
    "0097d365a00b52a6991ad7618f0a04e8d2935e47/docs/models/"
)
_LANGUAGES = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "hi": "Hindi",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "nl": "Dutch",
    "pt": "Portuguese",
    "zh": "Chinese",
    "vi": "Vietnamese",
    "ru": "Russian",
}
_QWEN = {
    "aiden": ("Aiden", "male", "en", "American"),
    "dylan": ("Dylan", "male", "zh", "Beijing"),
    "eric": ("Eric", "male", "zh", "Sichuan"),
    "ono_anna": ("Ono Anna", "female", "ja", ""),
    "ryan": ("Ryan", "male", "en", ""),
    "serena": ("Serena", "female", "zh", ""),
    "sohee": ("Sohee", "female", "ko", ""),
    "uncle_fu": ("Uncle Fu", "male", "zh", ""),
    "vivian": ("Vivian", "female", "zh", ""),
}
_MAGPIE = {
    "EN-US": "en",
    "ES-US": "es",
    "FR-FR": "fr",
    "DE-DE": "de",
    "VI-VN": "vi",
    "IT-IT": "it",
    "ZH-CN": "zh",
    "HI-IN": "hi",
    "JA-JP": "ja",
}
_KITTEN_PRESETS = {
    str(voice).lower()
    for option in family_metadata("kitten_tts2").get("options", {}).get("request", [])
    if option.get("name") == "voice_id"
    for voice in option.get("values", [])
}
_POCKET_ENGLISH_PRESETS = {
    name.rsplit("/", 1)[-1].removesuffix(".safetensors")
    for package in inventory()["packages"]
    if package["id"] == "pocket_tts_english_safetensors"
    for name in package.get("files", [])
    if name.startswith("languages/english/embeddings/") and name.endswith(".safetensors")
}
_NEUTTS_PRESETS = {"dave", "emily", "greta", "jo", "juliette", "mateo", "paul", "sophie", "steven"}
CLASSIC_KITTEN_PRESETS = {
    "expr-voice-2-f": "Bella",
    "expr-voice-2-m": "Jasper",
    "expr-voice-3-f": "Luna",
    "expr-voice-3-m": "Bruno",
    "expr-voice-4-f": "Rosie",
    "expr-voice-4-m": "Hugo",
    "expr-voice-5-f": "Kiki",
    "expr-voice-5-m": "Leo",
}
_NAMED_COMPATIBLE_PRESETS = {
    "alloy",
    "ash",
    "ballad",
    "cedar",
    "coral",
    "echo",
    "fable",
    "marin",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
}


def language_default_voice(defaults: Mapping[str, Any], language: str) -> str | None:
    """Prefer an exact locale, then its base language, preserving saved IDs."""
    normalized = language.strip().replace("_", "-").lower()
    values = {str(key).replace("_", "-").lower(): value for key, value in defaults.items()}
    for key in (normalized, normalized.split("-", 1)[0]):
        if key in values and isinstance(values[key], str):
            return values[key]
    if normalized and "-" not in normalized:
        for key, value in values.items():
            if key.split("-", 1)[0] == normalized and isinstance(value, str):
                return value
    return None


def kokoro_voice_language(voice: str) -> str | None:
    """Native Kokoro phonemizer language, including a blend's shared accent."""
    codes = {
        "a": "en-us",
        "b": "en-gb",
        "e": "es",
        "f": "fr",
        "h": "hi",
        "i": "it",
        "j": "ja",
        "p": "pt",
        "z": "zh",
    }
    found = set()
    for part in voice.split("+"):
        match = re.fullmatch(
            r"([abefhijpz])[fm]_[a-z0-9_]+(?:\(\s*\d+(?:\.\d+)?\s*\))?", part.strip(), re.IGNORECASE
        )
        if not match:
            return None
        found.add(codes[match[1].lower()])
    return next(iter(found)) if len(found) == 1 else None


def _info(
    name: str,
    gender: str = "",
    language: str = "",
    *,
    multilingual: bool = False,
    label: str = "",
    accent: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "display_name": name,
        "gender": gender,
        "language_code": language,
        "multilingual": multilingual,
        "pandrator_preset": True,
        "language_label": label or _LANGUAGES.get(language, language) or "Multilingual",
    }
    if accent:
        result["accent"] = accent
    if language and not multilingual:
        result["supported_languages"] = [language]
    return result


def preset_voice_info(family: str, model: str, voice: str) -> dict[str, Any]:
    """Return sourced preset metadata, or nothing for an unknown identity."""
    key = voice.lower()
    if family == "voxtral_tts":
        if key in {
            "casual_female",
            "casual_male",
            "cheerful_female",
            "neutral_female",
            "neutral_male",
        }:
            style, gender = key.split("_")
            return _info(f"{style.title()} · {gender.title()}", gender, multilingual=True)
        match = re.fullmatch(r"(ar|de|es|fr|hi|it|nl|pt)_(female|male)", key)
        if match and key != "ar_female":
            language, gender = match.groups()
            return _info(
                f"{_LANGUAGES[language]} · {gender.title()}",
                gender,
                language,
                multilingual=True,
                label=f"{_LANGUAGES[language]} preset · Multilingual",
            )
    if family == "qwen3_tts" and key in _QWEN:
        name, gender, language, accent = _QWEN[key]
        label = f"Native language: {_LANGUAGES[language]}"
        if accent:
            label = f"Native accent: {accent} {_LANGUAGES[language]}"
        return _info(name, gender, language, multilingual=True, label=label, accent=accent)
    if family == "kokoro_tts":
        match = re.fullmatch(r"([abdefhijpz])([fm])_(.+)", key)
        if match and key in AUDIO_CPP_KOKORO_VOICES:
            prefix, gender, name = match.groups()
            return _info(
                name.replace("_", " ").title(),
                "female" if gender == "f" else "male",
                "en-us" if prefix == "a" else KOKORO_PREFIX_LANGUAGE_CODES[prefix],
                label={"p": "Brazilian Portuguese", "z": "Mandarin Chinese"}.get(
                    prefix, KOKORO_VOICE_LANGUAGE_GROUPS[prefix]
                ),
                accent={"a": "American", "b": "British", "p": "Brazilian"}.get(prefix, ""),
            )
    if family == "pocket_tts":
        if "english_safetensors" in model and key in _POCKET_ENGLISH_PRESETS:
            return _info(voice.replace("_", " ").title(), language="en")
        if key == "alba" and "english" in model:
            return _info("Alba", language="en")
        if key == "cosette" and "french" in model:
            return _info("Cosette", language="fr")
    if family == "kitten_tts" and model == "kitten_tts_mini_0_8_orig":
        name = CLASSIC_KITTEN_PRESETS.get(key) or next(
            (name for name in CLASSIC_KITTEN_PRESETS.values() if name.lower() == key),
            None,
        )
        if name:
            return _info(name, language="en")
    if family == "kitten_tts2" and key in _KITTEN_PRESETS:
        language_presets = {
            "arabic": "ar",
            "chinese": "zh",
            "french": "fr",
            "german": "de",
            "hindi": "hi",
            "italian": "it",
            "portuguese": "pt",
            "russian": "ru",
            "spanish": "es",
        }
        if key == "preparedbruno":
            return _info("Prepared Bruno", label="Language not documented")
        return _info(voice, language=language_presets.get(key, "en"))
    if family == "kugelaudio":
        if key in {"default", "clear"}:
            return _info(
                "German · Female" + (" · Clear" if key == "clear" else ""),
                "female",
                "de",
                multilingual=True,
                label="Native language: German",
            )
        if key in {"english_female", "english_male"}:
            gender = key.split("_")[1]
            return _info(
                f"British English · {gender.title()}",
                gender,
                "en-gb",
                multilingual=True,
                label="Native accent: British English",
                accent="British",
            )
    if family == "neutts" and key in _NEUTTS_PRESETS:
        return _info(voice.title(), label="Voice language not documented")
    if family == "supertonic" and re.fullmatch(r"[fm][1-5]", key):
        gender = "female" if key[0] == "f" else "male"
        return _info(f"{gender.title()} {key[1]}", gender, multilingual=True)
    if family == "lfm2_audio" and key in {"us_male", "us_female", "uk_male", "uk_female"}:
        region, gender = key.split("_")
        label = "American English" if region == "us" else "British English"
        return _info(
            f"{label} · {gender.title()}", gender, "en" if region == "us" else "en-gb", label=label
        )
    if family == "magpie_tts":
        parts = voice.split(".")
        if len(parts) >= 3 and parts[0] == "Magpie-Multilingual" and parts[1] in _MAGPIE:
            speaker = parts[2]
            if speaker in {"Sofia", "Aria", "Jason", "Leo", "John Van Stan"}:
                gender = "female" if speaker in {"Sofia", "Aria"} else "male"
                result = _info(
                    speaker + (f" · {parts[3]}" if len(parts) > 3 else ""),
                    gender,
                    _MAGPIE[parts[1]],
                )
                result["locale"] = parts[1].lower()
                return result
    return {}


def _family(service: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    declared = str(model.get("family") or "")
    if declared:
        return declared
    identifier = str(service.get("profile_id") or service.get("id") or "").lower()
    if identifier in {
        "voxtral",
        "pandrator-voxtral-fastapi",
        "voxtral-second-state",
        "voxtral-vllm-omni",
    }:
        return "voxtral_tts"
    if identifier in {"kokoro", "kokoro-remsky"}:
        return "kokoro_tts"
    if identifier in {"kobold_qwen", "qwen3-second-state", "pandrator-kobold-qwen-fastapi"}:
        return "qwen3_tts"
    if identifier == "magpie":
        return "magpie_tts"
    return ""


def _preferred(family: str) -> dict[str, str]:
    if family == "voxtral_tts":
        return {
            "en": "casual_male",
            **{lang: f"{lang}_male" for lang in ("ar", "de", "es", "fr", "hi", "it", "nl", "pt")},
        }
    if family == "qwen3_tts":
        return {"en": "Aiden", "zh": "Serena", "ja": "Ono_Anna", "ko": "Sohee"}
    if family == "kokoro_tts":
        return {
            "en": "af_heart",
            "en-gb": "bf_emma",
            "es": "ef_dora",
            "fr": "ff_siwis",
            "hi": "hf_alpha",
            "it": "if_sara",
            "ja": "jf_alpha",
            "pt": "pf_dora",
            "zh": "zf_xiaobei",
        }
    if family == "lfm2_audio":
        return {"en": "us_male", "en-us": "us_male", "en-gb": "uk_male"}
    if family == "magpie_tts":
        return {
            language: f"Magpie-Multilingual.{locale}.Sofia" for locale, language in _MAGPIE.items()
        }
    if family == "kitten_tts2":
        return {
            "en": "Bruno",
            **{
                code: name.title()
                for code, name in _LANGUAGES.items()
                if code in {"ar", "zh", "fr", "de", "hi", "it", "pt", "ru", "es"}
            },
        }
    if family == "kugelaudio":
        return {"de": "default", "en": "english_female", "en-gb": "english_female"}
    return {}


def preferred_preset_voice(family: str, language: str) -> str | None:
    return language_default_voice(_preferred(family), language)


def decorate_preset_voices(service: dict[str, Any], *, include_defaults: bool = True) -> None:
    """Enrich only advertised presets; retain saved metadata and chosen defaults."""
    model_rows = service.get("model_catalog") or []
    rows = {str(row["id"]): row for row in model_rows if isinstance(row, dict) and row.get("id")}
    catalogues = service.get("voice_catalogues") or {}
    metadata = deepcopy(service.get("voice_metadata") or {})
    defaults = deepcopy(service.get("default_voices_by_language") or {})
    model_ids = list(
        dict.fromkeys(str(model) for model in [*(service.get("models") or []), *catalogues, *rows])
    )
    for model in model_ids:
        row = rows.get(model, {"id": model})
        mode = row.get("voice_mode") or (service.get("model_voice_modes") or {}).get(model)
        if (
            mode in {"cloning", "design", "optional_cloning"}
            or str(model).lower() == "voice cloning"
        ):
            continue
        family = _family(service, row)
        voices = catalogues.get(model)
        if voices is None:
            voices = (
                service.get("voices") or []
                if not catalogues or model == service.get("default_model")
                else []
            )
        if not isinstance(voices, list):
            continue
        available = {str(voice).lower(): str(voice) for voice in voices if isinstance(voice, str)}
        for voice in available.values():
            info: dict[str, Any] = preset_voice_info(family, model, voice)
            if not info and voice.lower() in _NAMED_COMPATIBLE_PRESETS:
                # Formatting an ID establishes no language or gender facts.
                info = {"display_name": voice.title()}
            if not info:
                continue
            source = _AUDIO_CPP_SOURCE + family + ".md"
            info.update(id=voice, model=model)
            if info.get("pandrator_preset"):
                info["source_url"] = source
            extra_sources = {
                "kokoro_tts": "https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md",
                "qwen3_tts": "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                "supertonic": "https://supertone-inc.github.io/supertonic-py/voices/",
                "kitten_tts": "https://huggingface.co/KittenML/kitten-tts-mini-0.8/blob/main/config.json",
            }
            if info.get("pandrator_preset"):
                info["sources"] = [
                    source,
                    *([extra_sources[family]] if family in extra_sources else []),
                ]
            old = metadata.get(f"{model}:{voice}") or metadata.get(voice) or {}
            old = old if isinstance(old, dict) else {}
            merged = {
                **info,
                **{key: value for key, value in old.items() if value is not None and value != ""},
            }
            if not old.get("display_name") or old.get("display_name") == voice:
                merged["display_name"] = (
                    old.get("name")
                    if old.get("name") and old["name"] != voice
                    else info["display_name"]
                )
            metadata[f"{model}:{voice}"] = merged
        preferred = {
            language: available[voice.lower()]
            for language, voice in _preferred(family).items()
            if voice.lower() in available
        }
        standard = {
            "voxtral_tts": "casual_male",
            "qwen3_tts": "Aiden",
            "kokoro_tts": "af_heart",
            "lfm2_audio": "us_male",
            "magpie_tts": "Magpie-Multilingual.EN-US.Sofia",
            "kitten_tts2": "Bruno",
            "kugelaudio": "default",
        }.get(family, "")
        selected = str((service.get("default_voices") or {}).get(model) or "")
        if not selected and model == service.get("default_model"):
            selected = str(service.get("default_voice") or "")
        if selected and selected.lower() != standard.lower():
            preferred = {}
        if include_defaults and preferred:
            defaults[model] = {**preferred, **(defaults.get(model) or {})}
    if metadata:
        service["voice_metadata"] = metadata
    if defaults:
        service["default_voices_by_language"] = defaults
