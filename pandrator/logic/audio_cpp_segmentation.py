"""Local narration policies and one-segment audio.cpp request chunking.

Character targets are application policies, not model context limits. Native
chunk sizes and output-token budgets have different units and stay separate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .audio_cpp_inventory_data import family_metadata

REVIEWED_RUNTIME = "0.9.2"
NATIVE_SPLIT_ONLY_FAMILIES = frozenset({"f5_tts", "outetts"})
NATIVE_SPLIT_NOTE = (
    "audio.cpp may split each Pandrator segment again inside this model. "
    "Native sub-splits are not reported, so exact internal boundaries and pauses "
    "are not guaranteed. Pandrator's saved segments and recordings stay separate."
)
SINGLE_SEGMENT_SESSION_CHARACTERS = 65536

# These families' text_chunk_size defaults are Unicode character budgets.
# Confucius uses text tokens in its default mode; Pocket uses a separate
# tokenizer budget unless an explicit character-size override is supplied.
CHARACTER_CHUNK_FAMILIES = frozenset({
    "audio8_tts", "breeze_tts", "chatterbox", "chatterbox_turbo", "cosyvoice3",
    "dots_tts", "echo_tts", "fireredtts3", "fish_audio", "higgs_audio_tts",
    "inflect_v2", "kitten_tts", "kitten_tts2", "kokoro_tts", "kugelaudio",
    "lfm2_audio", "magpie_tts", "maya1", "moss_tts_local", "moss_tts_nano",
    "moss_tts_v15", "moss_voicegen", "neutts", "outetts", "piper_tts",
    "qwen3_tts", "sanotts", "soprano_tts", "voxcpm1", "voxcpm2", "voxtral_tts", "zipvoice",
})


def _family(family: str) -> str:
    family = family.strip().lower()
    return {"fish_audio_s2": "fish_audio", "kokoro": "kokoro_tts"}.get(family, family)


def native_character_chunk_default(family: str) -> int | None:
    family = _family(family)
    if family not in CHARACTER_CHUNK_FAMILIES:
        return None
    metadata = family_metadata(family).get("options", {})
    for option in [*metadata.get("request", []), *metadata.get("session", [])]:
        value = option.get("default")
        if option.get("name") == "text_chunk_size" and type(value) is int and value > 0:
            return value
    return None


def narration_character_policy(family: str, language: str = "en") -> tuple[int, str]:
    """Return the reviewed policy ceiling before the shared 10% reserve."""
    family = _family(family)
    cjk = language.lower().replace("_", "-").split("-", 1)[0] in {"zh", "ja", "ko"}
    explicit = {
        "qwen3_tts": 500 if cjk else 2000,
        "voxcpm1": 2000,
        "voxcpm2": 2000,
        "fish_audio": 300,
        # 128 is a native splitting default, not a model input limit. These
        # ordinary narration policies stay above 160 after the shared reserve.
        "chatterbox": 200,
        "chatterbox_turbo": 200,
        "zipvoice": 200,
        "supertonic": 120 if language.lower().startswith(("ja", "ko")) else 300,
        "pocket_tts": 200,
        "confucius4_tts": 80 if cjk else 240,
        "irodori_tts": 80,
        # Mistral recommends fewer than 300 words. The additional word budget
        # is enforced by Pandrator preparation, independently of characters.
        # Q8 Vulkan generation was checked progressively through ~1280 chars
        # on an 8 GiB GPU. The shared reserve keeps the default below that run.
        "voxtral_tts": 1280,
    }
    if cjk:
        # Output frames are not text tokens. Short frame budgets need smaller
        # CJK segments because each written character represents more speech.
        cjk_limits = {
            "voxcpm1": 500, "voxcpm2": 500, "breeze_tts": 360,
            "higgs_audio_tts": 300, "moss_tts_local": 500, "moss_tts_nano": 80,
            # Korean Chatterbox has 384 speech tokens at 25 Hz: about 15 s.
            # A Latin-script character target cannot carry over to Hangul.
            "chatterbox": 70,
        }
        if family in cjk_limits:
            return cjk_limits[family], f"audio.cpp {family} CJK output-budget policy"
    if family in explicit:
        return explicit[family], f"audio.cpp {family} application policy"
    native = native_character_chunk_default(family)
    if native is not None:
        return native, f"audio.cpp {family} reviewed native character budget"
    return 300, f"audio.cpp {family} conservative policy; no verified character ceiling"


def irodori_tokenizer_byte_bound(model: str, text: str) -> int:
    """Worst-case byte-fallback token count, including normalization and BOS."""
    normalized = text.replace(" ", "▁")
    prefix = 0 if "_v4_" in model else 3
    return len(normalized.encode("utf-8")) + prefix + 1


def validate_segment_text_window(model: str, text: str) -> None:
    if model.startswith("irodori_tts_") and irodori_tokenizer_byte_bound(model, text) > 256:
        raise ValueError(
            "Irodori speech text may exceed its 256-token text window after normalization; "
            "split this segment in Pandrator before generation."
        )


def single_segment_options(
    family: str, text: str, options: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep a Pandrator speech segment whole where the native runtime allows it.

    This does not expand model context or output budgets. Model-specific hard
    limits remain in force. Native endline mode always splits at punctuation;
    Confucius' default mode and Pocket's omitted character size use token or
    sentence splitting. Explicit character budgets avoid those second passes.
    F5's reference-dependent splitting and OuteTTS's context/output retries
    cannot be disabled; they remain usable with the documented limitation.
    """
    family = _family(family)
    result = dict(options)
    if family == "soprano_tts" and len(text) > SINGLE_SEGMENT_SESSION_CHARACTERS:
        raise ValueError("Resegment this Soprano request in Pandrator before generation.")
    if family == "dramabox":
        # This controls entry to native long-form splitting, not output length.
        # The byte bound exceeds the speech estimator and all its nonverbal
        # regex contributions in the reviewed runtime. Keep explicit duration.
        scale = float(result.get("duration_scale") or 1.1)
        duration = float(result.get("duration_sec") or 0)
        threshold = max(86400.0, len(text.encode("utf-8")) * 100.0) * max(1.0, scale)
        threshold = max(threshold, duration)
        if not math.isfinite(threshold) or threshold > 3e38:
            raise ValueError("DramaBox duration settings exceed the single-segment request range.")
        result["audio_chunk_threshold_sec"] = threshold
    native = {
        item["name"]: item
        for item in family_metadata(family).get("options", {}).get("request", [])
    }
    if "text_chunk_size" not in native:
        return result
    supplied = result.get("text_chunk_size")
    if supplied is not None and (
        isinstance(supplied, bool) or not isinstance(supplied, int) or supplied <= 0
    ):
        raise ValueError("audio.cpp text_chunk_size must be a positive integer.")
    base = supplied if supplied is not None else native_character_chunk_default(family) or 0
    minimum = native["text_chunk_size"].get("min", 1)
    size = max(len(text), base, minimum, 1)
    maximum = native["text_chunk_size"].get("max")
    if maximum is not None and size > maximum:
        raise ValueError(
            f"The {family} speech segment exceeds its {maximum}-character chunk limit; "
            "resegment it in Pandrator before generation."
        )
    result["text_chunk_size"] = size
    mode = result.get("text_chunk_mode", next(
        (item.get("default", "default") for item in native.values()
         if item.get("name") == "text_chunk_mode"), "default"
    ))
    if "text_chunk_mode" in native and (
        mode == "endline" or family == "confucius4_tts"
    ):
        result["text_chunk_mode"] = "tag_aware"
    return result
