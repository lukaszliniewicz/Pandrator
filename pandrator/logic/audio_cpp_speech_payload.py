"""Own audio.cpp speech request preparation and model language metadata."""

from collections.abc import Callable, Mapping
from typing import Any, Protocol, TypeAlias

from .audio_cpp_catalogue import package_metadata as _package_metadata
from .audio_cpp_model_metadata import ModelCatalog as ModelCatalog
from .audio_cpp_model_metadata import _audio_cpp_model_metadata as _audio_cpp_model_metadata
from .audio_cpp_parameters import (
    FISH_AUDIO_TTS_TEXT_CHUNK_SIZE_DEFAULT,
    request_parameters_for_family,
    request_parameters_for_model,
)

ModelMetadataReader: TypeAlias = Callable[[str, dict], dict[str, object]]
ModelLanguageReader: TypeAlias = Callable[[str, object, dict | None], str]
ModelOptionsReader: TypeAlias = Callable[[dict[str, Any], str, str], dict[str, Any] | None]
ModelOptionsValidator: TypeAlias = Callable[[str, Mapping[str, Any]], dict[str, Any]]

_TTS_FORBIDDEN_LEGACY_OPTIONS = {
    "lfm2_audio": {
        "audio_chunk_mode",
        "audio_chunk_seconds",
        "text_temperature",
        "text_top_k",
        "stream_frames_per_event",
    },
    "dots_tts": {"use_xvector", "vocoder_merge_steps"},
    "higgs_audio_tts": {"repetition_penalty"},
}
# These runtimes reject even a neutral global speed as a style condition.
# Keep this scoped to verified contracts; other families can consume speed.
_UNSUPPORTED_SPEED_FAMILIES = {
    "breeze_tts": "Breeze",
    "voxcpm2": "VoxCPM2",
    "pocket_tts": "PocketTTS",
}
_MANAGED_REFERENCE_PATH_OPTIONS = {
    "voice_samples",
    "speaker_embedding_file",
    "reference_codes_file",
}
_LINKED_REFERENCE_TRANSCRIPT_FAMILIES = {
    "f5_tts": "F5-TTS",
    "glm_tts": "GLM-TTS",
    "zipvoice": "ZipVoice",
    "outetts": "OuteTTS",
    "kitten_tts2": "KittenTTS2",
}
_AUK_GENERATOR_MODEL_IDS = frozenset(
    {
        "auk_base_f16",
        "auk_base_f32",
        "auk_base_q8_0",
        "auk_flash_f16",
        "auk_flash_f32",
        "auk_flash_q8_0",
    }
)


def _reject_known_unsupported_options(
    family: str, model_id: str, options: Mapping[str, Any]
) -> None:
    normalized = str(family or "").strip().lower()
    forbidden = _MANAGED_REFERENCE_PATH_OPTIONS | _TTS_FORBIDDEN_LEGACY_OPTIONS.get(
        normalized, set()
    )
    family_parameters = request_parameters_for_family(normalized)
    model_parameters = request_parameters_for_model(normalized, model_id)
    forbidden |= family_parameters.keys() - model_parameters.keys()
    for key in sorted(forbidden.intersection(options)):
        raise ValueError(f"Unknown audio.cpp option '{key}' for family '{family}'.")


def _has_managed_wav_reference(reference: Any) -> bool:
    if not isinstance(reference, dict) or not reference:
        return False
    data = reference.get("data")
    return (
        str(reference.get("type") or "").strip().casefold() == "base64"
        and isinstance(data, str)
        and data.casefold().startswith("data:audio/wav;base64,")
    )


def _require_auk_duration_without_reference(
    tts_settings: Mapping[str, Any],
    model: str,
    family: str,
    linked_reference: bool,
    validate_options: ModelOptionsValidator,
) -> None:
    if (
        family != "auk"
        or model.casefold() not in _AUK_GENERATOR_MODEL_IDS
        or linked_reference
    ):
        return

    model_settings = tts_settings.get("audio_cpp_model_settings")
    selected = (
        model_settings.get(model)
        if isinstance(model_settings, dict)
        else None
    )
    if isinstance(selected, dict):
        duration_value = selected.get("duration_sec")
    else:
        raw_options = tts_settings.get("audio_cpp_options")
        if raw_options is None:
            raw_options = tts_settings.get("options")
        duration_value = (
            raw_options.get("duration_sec")
            if isinstance(raw_options, dict)
            else None
        )

    message = (
        "AuK generation without linked reference audio requires a finite positive "
        "options.duration_sec."
    )
    try:
        validated = validate_options(family, {"duration_sec": duration_value})
        duration = validated["duration_sec"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if duration <= 0:
        raise ValueError(message)


class CompiledPerformanceProjection(Protocol):
    @property
    def input(self) -> str: ...

    @property
    def instructions(self) -> str: ...

    @property
    def request_options(self) -> Mapping[str, object]: ...


PerformanceCompiler: TypeAlias = Callable[[str, dict, dict], CompiledPerformanceProjection]
PerformanceCompilerLoader: TypeAlias = Callable[[], PerformanceCompiler]


_AUDIO_CPP_QWEN_LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
}
# Native request tags in audio.cpp v0.7.2's FireRedTTS3 tokenizer_text.cpp.
_AUDIO_CPP_FIRERED_LANGUAGE_NAMES = {
    **_AUDIO_CPP_QWEN_LANGUAGE_NAMES,
    "yue": "Cantonese",
    "ar": "Arabic",
    "tr": "Turkish",
    "id": "Indonesian",
    "nl": "Dutch",
    "vi": "Vietnamese",
    "uk": "Ukrainian",
    "th": "Thai",
    "pl": "Polish",
    "ro": "Romanian",
    "el": "Greek",
    "cs": "Czech",
    "fi": "Finnish",
    "hi": "Hindi",
}
_AUDIO_CPP_FIRERED_DIALECTS = (
    "ZH_Anhui",
    "ZH_Fujian",
    "ZH_Gansu",
    "ZH_Guizhou",
    "ZH_Hebei",
    "ZH_Henan",
    "ZH_Hubei",
    "ZH_Hunan",
    "ZH_Jiangxi",
    "ZH_Liaoning",
    "ZH_Minnan",
    "ZH_Ningxia",
    "ZH_Shaanxi",
    "ZH_Shandong",
    "ZH_Shanghai",
    "ZH_Shanxi",
    "ZH_Sichuan",
    "ZH_Tianjin",
    "ZH_Wenzhou",
    "ZH_Wu",
    "ZH_Yunnan",
)




def _audio_cpp_language(
    model: str,
    language: object,
    endpoint: dict | None = None,
    *,
    model_metadata: ModelMetadataReader,
) -> str:
    from .tts_language_preflight import validate_tts_language

    raw_language = str(language or "").strip()
    normalized = raw_language.lower().replace("_", "-")
    metadata = model_metadata(model, endpoint or {})
    family = str(metadata.get("family") or "").strip().lower()
    validation_endpoint = dict(endpoint or {})
    validation_endpoint.setdefault("id", "audio_cpp")
    validation_endpoint.setdefault("adapter", "audio_cpp")
    try:
        validated = validate_tts_language(
            {"service": "audio.cpp", "xtts_model": model, "language": language},
            endpoint=validation_endpoint,
        )
    except ValueError as exc:
        supported = metadata.get("supported_languages")
        if family == "pocket_tts" and supported == ["en"]:
            raise ValueError(
                "The selected PocketTTS package is English-only. "
                "Select a model package matching the requested language."
            ) from exc
        raise ValueError(
            f"audio.cpp model '{model}' does not support language '{language}'."
        ) from exc
    canonical = validated["language"]
    native_language = validated["native_language"]
    native_dialect = next(
        (tag for tag in _AUDIO_CPP_FIRERED_DIALECTS if tag.lower().replace("_", "-") == normalized),
        None,
    )
    if not canonical or canonical == "auto":
        return ""
    iso = str(native_language).split("-", 1)[0]
    if family in {"fish_audio_s2", "fish_audio", "voxcpm2", "breeze_tts", "cosyvoice3"}:
        # These v0.7.2 sessions infer language from text; a hint is not consumed.
        return ""
    if family == "moss_voicegen":
        names = {"en": "English", "zh": "Chinese"}
        return names.get(iso, str(native_language))
    if family == "pocket_tts":
        # Pocket's language belongs to the loaded model package, not a request.
        return ""
    if family == "magpie_tts":
        # Keep supported regional Arabic tokenizers instead of collapsing to ar.
        regional = {
            "ar-ae": "ar-AE",
            "ar-sa": "ar-SA",
            "ar-msa": "ar-MSA",
            "pt-br": "pt-BR",
            "pt-brasil": "pt-BR",
        }
        return regional.get(canonical, iso)
    if family in {"qwen3_tts", "fireredtts3"}:
        if family == "fireredtts3":
            if native_dialect:
                return native_dialect
            names = _AUDIO_CPP_FIRERED_LANGUAGE_NAMES
        else:
            names = _AUDIO_CPP_QWEN_LANGUAGE_NAMES
        named = next((name for name in names.values() if name.lower() == canonical), None)
        result = named or names.get(iso)
        if result:
            return result
        return str(native_language)
    return iso


def _audio_cpp_selected_model_options(
    tts_settings: dict[str, Any],
    model: str,
    family: str,
    *,
    validate_options: ModelOptionsValidator,
) -> dict[str, Any] | None:
    """Return validated options only when the exact model has an option map."""

    model_settings = tts_settings.get("audio_cpp_model_settings")
    if not isinstance(model_settings, dict):
        return None
    selected = model_settings.get(model)
    if not isinstance(selected, dict):
        return None
    model_parameters = request_parameters_for_model(family, model)
    for key in selected:
        if key not in model_parameters:
            raise ValueError(f"Unknown audio.cpp option '{key}' for family '{family}'.")
    return validate_options(family, selected)


def _build_audio_cpp_audio_payload(
    text: str,
    tts_settings: dict,
    endpoint: dict,
    *,
    model_metadata: ModelMetadataReader,
    model_language: ModelLanguageReader,
    selected_model_options: ModelOptionsReader,
    validate_options: ModelOptionsValidator,
    get_performance_compiler: PerformanceCompilerLoader,
) -> dict:
    """Build audio.cpp's direct HTTP speech request."""

    model = str(
        tts_settings.get("xtts_model")
        or tts_settings.get("model")
        or endpoint.get("default_model")
        or ""
    ).strip()
    if not model:
        raise ValueError("Select one of the model IDs configured in the audio.cpp server.")

    metadata = model_metadata(model, endpoint)
    if metadata.get("entry_role") == "component" or _package_metadata(model).get("entry_role") == "component":
        raise ValueError(f"audio.cpp package '{model}' is a component, not a speech model.")
    voice_mode = str(metadata.get("voice_mode") or "cloning").lower()
    is_design = voice_mode == "design"
    is_prebuilt = voice_mode == "prebuilt"
    family = str(metadata.get("family") or "").lower()
    raw_voice_ref = tts_settings.get("audio_cpp_voice_ref")
    linked_reference = isinstance(raw_voice_ref, dict) and bool(raw_voice_ref)
    reference_text = str(tts_settings.get("audio_cpp_reference_text") or "").strip()
    if family == "moss_ttsd" and linked_reference and reference_text and not reference_text.startswith("[S1]"):
        reference_text = f"[S1] {reference_text}"
    if family == "index_tts2" and not _has_managed_wav_reference(raw_voice_ref):
        raise ValueError("IndexTTS2 requires a linked WAV reference audio file.")
    model_settings = tts_settings.get("audio_cpp_model_settings")
    has_selected_options = isinstance(model_settings, dict) and isinstance(
        model_settings.get(model), dict
    )
    raw_options_value = tts_settings.get("audio_cpp_options")
    if raw_options_value is None:
        raw_options_value = tts_settings.get("options")
    legacy_options = raw_options_value if isinstance(raw_options_value, dict) else {}
    legacy_reference_text = (
        str(legacy_options.get("reference_text") or "").strip()
        if not has_selected_options
        else ""
    )
    if (
        linked_reference
        and not reference_text
        and not is_design
        and not is_prebuilt
        and family in _LINKED_REFERENCE_TRANSCRIPT_FAMILIES
    ):
        if family == "kitten_tts2":
            raise ValueError(
                "KittenTTS2 voice cloning requires a matching reference transcript."
            )
        family_name = _LINKED_REFERENCE_TRANSCRIPT_FAMILIES[family]
        raise ValueError(
            f"{family_name} linked voice references require a matching reference transcript."
        )
    if family == "moss_tts_nano" and not linked_reference and (
        reference_text or legacy_reference_text
    ):
        raise ValueError(
            "MOSS-TTS-Nano reference text requires linked reference audio."
        )
    _require_auk_duration_without_reference(
        tts_settings,
        model,
        family,
        linked_reference,
        validate_options,
    )
    if family == "lfm2_audio" and "_jp_" in model.casefold():
        if any(
            str(tts_settings.get(key) or "").strip()
            for key in ("speaker", "voice")
        ):
            raise ValueError(
                "The Japanese LFM checkpoint uses its built-in voice; clear the selected voice."
            )
    if voice_mode in {"unknown", "none"}:
        raise ValueError(
            f"audio.cpp model '{model}' has no verified speech request contract. "
            "Refresh the catalogue or choose a documented speech model."
        )

    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": "wav",
    }
    voice = str(tts_settings.get("speaker") or tts_settings.get("voice") or "").strip()
    if voice and not is_design and family not in {"magpie_tts", "neutts"}:
        payload["voice"] = voice

    raw_language = str(
        tts_settings.get("language") or tts_settings.get("target_language") or ""
    ).strip()
    language = model_language(
        model,
        raw_language,
        endpoint,
    )
    if language:
        payload["language"] = language

    compile_for_provider = get_performance_compiler()

    compiled = compile_for_provider(
        text, {**tts_settings, "xtts_model": model}, {**endpoint, "adapter": "audio_cpp"}
    )
    payload["input"] = compiled.input
    controls = getattr(compiled, "capabilities", {})
    transport = str(controls.get("instruction_transport") or "")
    if controls.get("requires_instruction") and not compiled.instructions.strip():
        raise ValueError("This audio.cpp model requires a voice description before synthesis.")
    if (
        is_design
        and not transport
        and not str(
            tts_settings.get("generation_prompt")
            or tts_settings.get("openai_audio_instructions")
            or ""
        ).strip()
    ):
        raise ValueError(
            "audio.cpp VoiceDesign models require instructions: provide a stable voice description in the general direction field."
        )
    if compiled.instructions and not transport:
        payload["instructions"] = compiled.instructions

    if (
        reference_text
        and not is_prebuilt
        and not is_design
        and family not in {"pocket_tts", "moss_tts_local"}
    ):
        payload["reference_text"] = reference_text
    voice_ref = raw_voice_ref
    if not is_prebuilt and not is_design and isinstance(voice_ref, dict) and voice_ref:
        payload["voice_ref"] = dict(voice_ref)
        # A linked Pandrator ID identifies the local sample, not a voice cached
        # inside audio.cpp. VoxCPM rejects cached_voice_id even with audio present.
        payload.pop("voice", None)

    selected_options = selected_model_options(tts_settings, model, family)
    selected_options_authoritative = selected_options is not None
    if selected_options_authoritative:
        # A selected model map is the complete request-tuning source.  Legacy
        # scalar settings and raw option bags are intentionally ignored.
        options = selected_options
        # Voice auditions can request a fresh seed for one preview while the
        # persisted model map remains in the inherited settings snapshot.  The
        # preview boundary marks that explicit value with preview_service_id;
        # keep this one-shot request control authoritative for the audition.
        if (
            tts_settings.get("preview_service_id")
            and "audio_cpp_seed" in tts_settings
            and tts_settings.get("audio_cpp_seed") not in (None, "")
        ):
            preview_seed = tts_settings["audio_cpp_seed"]
            if isinstance(preview_seed, bool):
                raise ValueError("audio.cpp preview seed must be an integer.")
            try:
                parsed_preview_seed = int(preview_seed)
            except (TypeError, ValueError) as error:
                raise ValueError("audio.cpp preview seed must be an integer.") from error
            if isinstance(preview_seed, float) and not preview_seed.is_integer():
                raise ValueError("audio.cpp preview seed must be an integer.")
            if is_design:
                if not 0 <= parsed_preview_seed <= 0xFFFFFFFF:
                    raise ValueError("audio.cpp preview seed must be between 0 and 4294967295.")
                options["seed"] = parsed_preview_seed
            else:
                options["seed"] = validate_options(
                    family,
                    {"seed": parsed_preview_seed},
                )["seed"]
    else:
        raw_options = tts_settings.get("audio_cpp_options")
        if raw_options is None:
            raw_options = tts_settings.get("options")
        options = dict(raw_options) if isinstance(raw_options, dict) else {}
        _reject_known_unsupported_options(family, model, options)
        for key in (
            "speed",
            "seed",
            "temperature",
            "top_k",
            "top_p",
            "max_tokens",
            "max_steps",
            "repetition_penalty",
            "guidance_scale",
            "num_inference_steps",
        ):
            value = tts_settings.get(f"audio_cpp_{key}")
            if value in (None, "") and key == "seed":
                value = tts_settings.get("seed")
            if value in (None, "") and key == "speed":
                value = tts_settings.get("speed")
            if value not in (None, ""):
                if is_design and key == "seed":
                    if isinstance(value, bool):
                        raise ValueError("audio.cpp VoiceDesign seed must be an integer.")
                    try:
                        parsed_seed = int(value)
                    except (TypeError, ValueError) as error:
                        raise ValueError(
                            "audio.cpp VoiceDesign seed must be an integer."
                        ) from error
                    if isinstance(value, float) and not value.is_integer():
                        raise ValueError("audio.cpp VoiceDesign seed must be an integer.")
                    if not 0 <= parsed_seed <= 0xFFFFFFFF:
                        raise ValueError(
                            "audio.cpp VoiceDesign seed must be between 0 and 4294967295."
                        )
                    value = parsed_seed
                if family == "omnivoice" and key == "speed":
                    options[key] = value
                else:
                    payload[key] = (
                        str(value)
                        if key == "seed"
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 2**53
                        else value
                    )

        _reject_known_unsupported_options(family, model, payload)

    if is_design:
        cloning_only_options = {
            "audio_sample",
            "clone",
            "cloning",
            "prompt_text",
            "ref_audio",
            "reference_audio",
            "reference_text",
            "speaker_wav",
            "voice_ref",
            "x_vector_only_mode",
        }
        options = {
            key: value
            for key, value in options.items()
            if str(key).strip().casefold() not in cloning_only_options
        }
    if family == "moss_tts_local":
        # MOSS-TTS-Local consumes the linked audio prompt but has no
        # reference-transcript request field.
        options.pop("reference_text", None)
    if family in {"magpie_tts", "neutts"} and voice and not is_design:
        # Magpie consumes its selected preset as an option, not OpenAI's
        # top-level voice field.  A selected voice is authoritative.
        options["voice_id"] = voice
    if family == "pocket_tts" and reference_text and not is_design and not is_prebuilt:
        # Pocket's reviewed clone transcript is a request option.  It must win
        # over any stale value in a legacy raw option bag.
        options["voice_clone_text"] = reference_text
    if family in {"irodori_tts", "moss_voicegen"} and language:
        options["language"] = payload.pop("language")
    if family == "moss_voicegen":
        native_seed = options.get("seed", payload.pop("seed", None))
        if native_seed is not None:
            # The native parser uses stoi, then casts to uint32_t. Preserve
            # all 32 seed bits using its accepted signed decimal spelling.
            numeric_seed = int(native_seed)
            options["seed"] = numeric_seed if numeric_seed < 2**31 else numeric_seed - 2**32
    if family == "cosyvoice3":
        options["template_name"] = "instruct" if compiled.instructions else "zero_shot"
    if family == "fireredtts3" and "instruct" in model.casefold():
        if linked_reference:
            options["template_name"] = "instruct_tts"
        else:
            if not compiled.instructions:
                raise ValueError(
                    "FireRedTTS3 Instruct without reference audio requires a voice description."
                )
            options["template_name"] = "voice_design"
    if linked_reference and family == "omnivoice" and not reference_text and not is_design:
        raise ValueError("OmniVoice linked voice references require a reviewed transcript.")
    if linked_reference and family == "qwen3_tts" and not is_design:
        options["x_vector_only_mode"] = not bool(reference_text)
    # Compiler-owned transport safeguards take precedence over raw tuning.
    # In particular, Fish's generated bracket controls require tag-aware splits.
    options.update(compiled.request_options)
    if family in {"fish_audio_s2", "fish_audio"}:
        options.setdefault(
            "text_chunk_size", FISH_AUDIO_TTS_TEXT_CHUNK_SIZE_DEFAULT
        )
    if family == "moss_ttsd" and linked_reference:
        option_reference_text = options.get("reference_text")
        if isinstance(option_reference_text, str) and option_reference_text.strip():
            normalized_reference_text = option_reference_text.strip()
            options["reference_text"] = (
                normalized_reference_text
                if normalized_reference_text.startswith("[S1]")
                else f"[S1] {normalized_reference_text}"
            )
    if transport in {"options.instruct", "options.instruction"}:
        for alias in ("instruct", "instruction", "instructions", "caption"):
            options.pop(alias, None)
            payload.pop(alias, None)
        if compiled.instructions:
            options[transport.split(".", 1)[1]] = compiled.instructions
    if model.casefold() in {
        "irodori_tts_500m_v3_f16", "irodori_tts_500m_v3_q8_0",
        "irodori_tts_600m_v3_voicedesign_f16", "irodori_tts_600m_v3_voicedesign_q8_0",
        "irodori_tts_v4_small_f16", "irodori_tts_v4_small_q8_0",
    }:
        options["no_ref"] = not linked_reference
    if family in _UNSUPPORTED_SPEED_FAMILIES:
        # Ordinary sessions inherit speed=1 even without any delivery direction.
        # Omit that no-op; reject an intentional unsupported speed locally.
        for request_fields in (payload, options):
            speed = request_fields.pop("speed", None)
            if speed is not None and (
                isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed != 1
            ):
                raise ValueError(
                    f"{_UNSUPPORTED_SPEED_FAMILIES[family]} speed is unsupported; "
                    "only the neutral value 1 is allowed."
                )
    if options:
        payload["options"] = options
    return payload
