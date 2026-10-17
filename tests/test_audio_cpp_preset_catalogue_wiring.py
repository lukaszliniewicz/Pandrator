"""audio.cpp presets stay model-scoped across builtin and external profiles."""

from __future__ import annotations

import copy
import unittest
from typing import Any, cast
from unittest.mock import patch

from pandrator.constants import AUDIO_CPP_KOKORO_VOICES, magpie_voice_catalog
from pandrator.logic import tts_handler
from pandrator.logic.audio_cpp_catalogue import family_metadata
from pandrator.logic.tts_provider_profiles import (
    AUDIO_CPP_MODEL_CATALOG,
    AUDIO_CPP_MODEL_DEFAULT_VOICES,
    AUDIO_CPP_MODEL_PRESETS,
    AUDIO_CPP_PREBUILT_VOICES,
    get_tts_provider_profile,
)
from pandrator.logic.tts_service_catalogue import (
    get_service_config,
    resolve_openai_audio_endpoint,
)
from pandrator.web.tts_providers import AudioCppAdapter


def _voice_id_values(family: str) -> list[str]:
    options = family_metadata(family).get("options", {}).get("request", [])
    return next(
        (
            list(option.get("values", []))
            for option in options
            if option.get("name") == "voice_id"
        ),
        [],
    )


class AudioCppPresetCatalogueWiringTests(unittest.TestCase):
    def test_catalogue_presets_cover_supported_family_package_variants(self) -> None:
        models_by_family: dict[str, list[str]] = {}
        model_families = {item["id"]: item["family"] for item in AUDIO_CPP_MODEL_CATALOG}
        for model in AUDIO_CPP_MODEL_CATALOG:
            models_by_family.setdefault(model["family"], []).append(model["id"])

        for model in models_by_family["kokoro_tts"]:
            self.assertEqual(list(AUDIO_CPP_KOKORO_VOICES), AUDIO_CPP_MODEL_PRESETS[model])
            self.assertEqual(54, len(AUDIO_CPP_MODEL_PRESETS[model]))
        qwen_customvoice = [
            model
            for model in models_by_family["qwen3_tts"]
            if "customvoice" in model
        ]
        self.assertTrue(qwen_customvoice)
        for model in qwen_customvoice:
            self.assertEqual(AUDIO_CPP_PREBUILT_VOICES[:9], AUDIO_CPP_MODEL_PRESETS[model])
            self.assertEqual(9, len(AUDIO_CPP_MODEL_PRESETS[model]))
        self.assertEqual(
            magpie_voice_catalog(),
            AUDIO_CPP_MODEL_PRESETS[models_by_family["magpie_tts"][0]],
        )
        self.assertEqual(45, len(AUDIO_CPP_MODEL_PRESETS[models_by_family["magpie_tts"][0]]))

        english_pocket = [
            model
            for model in models_by_family["pocket_tts"]
            if "pocket_tts_english_" in model
        ]
        self.assertTrue(english_pocket)
        for model in english_pocket:
            if model.endswith("_safetensors"):
                self.assertEqual(26, len(AUDIO_CPP_MODEL_PRESETS[model]))
                self.assertIn("cosette", AUDIO_CPP_MODEL_PRESETS[model])
                self.assertIn("peter_yearsley", AUDIO_CPP_MODEL_PRESETS[model])
            else:
                self.assertEqual(["alba"], AUDIO_CPP_MODEL_PRESETS[model])
        french_pocket = {
            model for model in models_by_family["pocket_tts"] if "pocket_tts_french_" in model
        }
        for model in french_pocket:
            self.assertEqual(["cosette"], AUDIO_CPP_MODEL_PRESETS[model])
        self.assertFalse(
            (set(models_by_family["pocket_tts"]) - set(english_pocket) - french_pocket)
            & set(AUDIO_CPP_MODEL_PRESETS)
        )

        expected_supertonic = [
            f"{gender}{number}" for gender in ("F", "M") for number in range(1, 6)
        ]
        for model in models_by_family["supertonic"]:
            self.assertEqual(expected_supertonic, AUDIO_CPP_MODEL_PRESETS[model])
            self.assertEqual(10, len(AUDIO_CPP_MODEL_PRESETS[model]))
        for model in models_by_family["neutts"]:
            self.assertEqual(_voice_id_values("neutts"), AUDIO_CPP_MODEL_PRESETS[model])

        for model in models_by_family["lfm2_audio"]:
            expected = (
                []
                if "_jp_" in model
                else ["us_male", "us_female", "uk_male", "uk_female"]
            )
            self.assertEqual(expected, AUDIO_CPP_MODEL_PRESETS[model])
        for family in ("kitten_tts2", "kugelaudio"):
            for model in models_by_family[family]:
                self.assertIn(model, AUDIO_CPP_MODEL_PRESETS)
                self.assertEqual(_voice_id_values(family), AUDIO_CPP_MODEL_PRESETS[model])
        for model, family in model_families.items():
            if family == "qwen3_tts" and "customvoice" not in model:
                self.assertNotIn(model, AUDIO_CPP_MODEL_PRESETS)
        for model in AUDIO_CPP_MODEL_CATALOG:
            if model.get("voice_mode") == "cloning":
                self.assertNotIn(model["id"], AUDIO_CPP_MODEL_PRESETS)
        self.assertNotIn("chatterbox_turbo_f16", AUDIO_CPP_MODEL_PRESETS)
        self.assertNotIn("chatterbox_turbo_q8_0", AUDIO_CPP_MODEL_PRESETS)

        expected_default_models = {
            model for model, presets in AUDIO_CPP_MODEL_PRESETS.items() if presets
        }
        self.assertEqual(expected_default_models, set(AUDIO_CPP_MODEL_DEFAULT_VOICES))
        for model, presets in AUDIO_CPP_MODEL_PRESETS.items():
            if not presets:
                self.assertNotIn(model, AUDIO_CPP_MODEL_DEFAULT_VOICES)
                continue
            family = model_families[model]
            expected = presets[0]
            if family == "kokoro_tts":
                expected = "af_heart"
            elif family == "voxtral_tts":
                expected = "casual_male"
            elif family == "supertonic":
                expected = "M1"
            elif family == "kitten_tts2":
                expected = "Bruno"
            elif family == "neutts":
                expected = "emily"
            elif family == "kitten_tts":
                expected = "expr-voice-5-m"
            elif family == "qwen3_tts" and "customvoice" in model:
                expected = "Aiden"
            self.assertEqual(expected, AUDIO_CPP_MODEL_DEFAULT_VOICES[model])

    def test_builtin_and_external_profiles_share_copied_preset_maps(self) -> None:
        builtin = get_service_config({}, "audio_cpp")
        profile = get_tts_provider_profile("audio-cpp-experimental")
        self.assertIsNotNone(builtin)
        self.assertIsNotNone(profile)
        assert builtin is not None
        assert profile is not None
        builtin = cast(dict[str, Any], builtin)
        profile = cast(dict[str, Any], profile)

        self.assertEqual(AUDIO_CPP_MODEL_PRESETS, builtin["voice_catalogues"])
        self.assertEqual(AUDIO_CPP_MODEL_PRESETS, profile["voice_catalogues"])
        self.assertIsNot(builtin["voice_catalogues"], AUDIO_CPP_MODEL_PRESETS)
        self.assertIsNot(profile["voice_catalogues"], AUDIO_CPP_MODEL_PRESETS)
        self.assertEqual(AUDIO_CPP_MODEL_DEFAULT_VOICES, builtin["default_voices"])
        self.assertEqual(AUDIO_CPP_MODEL_DEFAULT_VOICES, profile["default_voices"])
        self.assertIsNot(builtin["default_voices"], AUDIO_CPP_MODEL_DEFAULT_VOICES)
        self.assertIsNot(profile["default_voices"], AUDIO_CPP_MODEL_DEFAULT_VOICES)
        self.assertEqual("af_heart", AUDIO_CPP_MODEL_DEFAULT_VOICES["kokoro_82m_q8_0"])
        self.assertEqual(
            "Aiden",
            AUDIO_CPP_MODEL_DEFAULT_VOICES["qwen3_tts_1_7b_customvoice_q8_0"],
        )
        self.assertEqual(
            magpie_voice_catalog()[0],
            AUDIO_CPP_MODEL_DEFAULT_VOICES["magpie_tts_q8_0"],
        )
        self.assertEqual("alba", AUDIO_CPP_MODEL_DEFAULT_VOICES["pocket_tts_english_q8_0"])
        kokoro = get_service_config({}, "kokoro")
        self.assertIsNotNone(kokoro)
        assert kokoro is not None
        kokoro = cast(dict[str, Any], kokoro)
        self.assertEqual("af_heart", kokoro["default_voice"])

    def test_presets_have_names_and_package_specific_language_metadata(self) -> None:
        from pandrator.logic.preset_voice_metadata import preset_voice_info

        service = get_service_config({}, "audio_cpp")
        assert service is not None
        service = cast(dict[str, Any], service)
        model = "voxtral_4b_tts_2603_q8_0"
        metadata = service["voice_metadata"]
        french = metadata[f"{model}:fr_female"]
        self.assertEqual("French · Female", french["display_name"])
        self.assertEqual("fr", french["language_code"])
        self.assertTrue(french["multilingual"])
        self.assertEqual("fr_female", french["id"])
        self.assertEqual(20, sum(key.startswith(f"{model}:") for key in metadata))
        for voice in ("casual_female", "casual_male", "cheerful_female", "neutral_female", "neutral_male"):
            self.assertEqual("", metadata[f"{model}:{voice}"]["language_code"])
            self.assertTrue(metadata[f"{model}:{voice}"]["multilingual"])
        self.assertEqual("en", metadata["pocket_tts_english_safetensors:cosette"]["language_code"])
        self.assertEqual("fr", metadata["pocket_tts_french_q8_0:cosette"]["language_code"])
        self.assertEqual("Peter Yearsley", metadata["pocket_tts_english_safetensors:peter_yearsley"]["display_name"])
        self.assertEqual("Heart", metadata["kokoro_82m_q8_0:af_heart"]["display_name"])
        self.assertEqual("en-us", metadata["kokoro_82m_q8_0:af_heart"]["language_code"])
        self.assertEqual(8, len(service["voice_catalogues"]["kitten_tts_mini_0_8_orig"]))
        self.assertEqual("Leo", metadata["kitten_tts_mini_0_8_orig:expr-voice-5-m"]["display_name"])
        supertonic = next(item["id"] for item in AUDIO_CPP_MODEL_CATALOG if item["family"] == "supertonic")
        self.assertEqual("Female 1", metadata[f"{supertonic}:F1"]["display_name"])
        self.assertEqual({}, preset_voice_info("kitten_tts2", "kitten", "my-reference"))
        self.assertEqual({}, preset_voice_info("voxtral_tts", model, "ar_female"))
        unknown = preset_voice_info("neutts", "neutts", "juliette")
        self.assertEqual("", unknown["language_code"])
        self.assertEqual("", unknown["gender"])
        self.assertFalse(unknown["multilingual"])

    def test_language_defaults_preserve_explicit_names_selections_and_locale(self) -> None:
        from pandrator.logic.preset_voice_metadata import language_default_voice

        model = "voxtral_4b_tts_2603_q8_0"
        service = get_service_config({"service_configs": [{
            "id": "audio_cpp", "default_voices": {model: "neutral_female"},
            "default_voices_by_language": {model: {"fr": "fr_female", "fr-CA": "casual_female"}},
            "voice_metadata": {f"{model}:fr_female": {"display_name": "My French narrator"}},
        }]}, "audio_cpp")
        assert service is not None
        service = cast(dict[str, Any], service)
        defaults = service["default_voices_by_language"][model]
        self.assertEqual("fr_female", language_default_voice(defaults, "fr_FR"))
        self.assertEqual("casual_female", language_default_voice(defaults, "fr-CA"))
        self.assertIsNone(language_default_voice(defaults, "de-DE"))
        self.assertEqual("neutral_female", service["default_voices"][model])
        self.assertEqual("My French narrator", service["voice_metadata"][f"{model}:fr_female"]["display_name"])

    def test_resolved_audio_cpp_endpoint_keeps_language_defaults_for_speech_requests(self) -> None:
        model = "kokoro_82m_q8_0"
        stored_service = {
            "id": "audio_cpp",
            "api_base": "http://audio-cpp.fixture",
            "default_model": model,
            "default_voice": "af_heart",
            "default_voices": {model: "af_heart"},
            "default_voices_by_language": {
                model: {"fr": "ff_siwis", "en-GB": "bf_emma"}
            },
        }
        settings = {
            "service": "audio_cpp",
            "service_configs": [stored_service],
            "model": model,
        }
        endpoint, error = resolve_openai_audio_endpoint(settings)
        self.assertEqual("", error)
        self.assertIsNotNone(endpoint)
        assert endpoint is not None
        self.assertEqual("af_heart", endpoint["default_voices"][model])
        language_defaults = endpoint["default_voices_by_language"][model]
        self.assertEqual("ff_siwis", language_defaults["fr"])
        self.assertEqual("bf_emma", language_defaults["en-GB"])
        self.assertIsNot(endpoint["default_voices"], stored_service["default_voices"])
        self.assertIsNot(
            endpoint["default_voices_by_language"][model],
            stored_service["default_voices_by_language"][model],
        )

        endpoint["default_voices"][model] = "bf_alice"
        endpoint["default_voices_by_language"][model]["fr"] = "bf_alice"
        self.assertEqual("af_heart", stored_service["default_voices"][model])
        self.assertEqual(
            "ff_siwis",
            stored_service["default_voices_by_language"][model]["fr"],
        )

        response = object()
        requests = (
            ({"language": "fr-FR"}, "ff_siwis", "fr"),
            ({"language": "en-GB"}, "bf_emma", "en-gb"),
            ({"language": "en-GB", "speaker": "bf_alice"}, "bf_alice", "en-gb"),
        )
        for overrides, expected_voice, expected_language in requests:
            with self.subTest(overrides=overrides):
                request_settings = {**settings, **overrides}
                with patch(
                    "pandrator.logic.tts_handler.requests.post",
                    return_value=response,
                ) as post:
                    self.assertIs(
                        response,
                        tts_handler._request_openai_compatible_audio(
                            "A short passage.", request_settings
                        ),
                    )

                self.assertEqual(
                    "http://audio-cpp.fixture/v1/audio/speech",
                    post.call_args.args[0],
                )
                payload = post.call_args.kwargs["json"]
                self.assertEqual(model, payload["model"])
                self.assertEqual("A short passage.", payload["input"])
                self.assertEqual(expected_voice, payload["voice"])
                self.assertEqual(expected_language, payload["language"])
                self.assertNotIn("default_voices", payload)
                self.assertNotIn("default_voices_by_language", payload)

    def test_curated_metadata_never_changes_uploaded_reference_voices(self) -> None:
        from pandrator.logic.preset_voice_metadata import decorate_preset_voices

        service = {
            "id": "audio_cpp", "adapter": "audio_cpp", "models": ["base"],
            "model_catalog": [{"id": "base", "family": "qwen3_tts", "voice_mode": "cloning"}],
            "voice_catalogues": {"base": ["Aiden"]},
            "voice_metadata": {"base:Aiden": {"display_name": "My reference", "language_code": "fr"}},
        }
        before = copy.deepcopy(service)
        decorate_preset_voices(service)
        self.assertEqual(before, service)

    def test_compatible_preset_names_add_no_voice_capability_claims(self) -> None:
        from pandrator.logic.preset_voice_metadata import decorate_preset_voices

        service = {"id": "custom", "models": ["model"], "voices": ["alloy", "opaque-id-42"]}
        decorate_preset_voices(service)
        self.assertEqual({"display_name": "Alloy", "id": "alloy", "model": "model"}, service["voice_metadata"]["model:alloy"])
        self.assertNotIn("model:opaque-id-42", service["voice_metadata"])

    def test_builtin_adapter_does_not_spread_aggregate_or_global_voices(self) -> None:
        service = get_service_config({}, "audio_cpp")
        self.assertIsNotNone(service)
        assert service is not None
        service = cast(dict[str, Any], service)
        service = copy.deepcopy(service)
        kokoro_model = "kokoro_82m_q8_0"
        turbo_model = "chatterbox_turbo_q8_0"
        qwen_base = "qwen3_tts_1_7b_base_q8_0"
        qwen_design = "qwen3_tts_1_7b_voicedesign_q8_0"
        pocket_model = "pocket_tts_english_bf16"
        models = [kokoro_model, turbo_model, qwen_base, qwen_design, pocket_model]
        service["voices"] = ["alba"]
        service["default_model"] = kokoro_model
        service["default_voice"] = "alba"
        service["voice_catalogues"][qwen_base] = ["cached-reference"]
        service["default_voices"][qwen_base] = "cached-reference-default"
        native_voices = {qwen_base: ["native-reference", "cached-reference"]}

        adapter = AudioCppAdapter("audio_cpp")
        try:
            with (
                patch(
                    "pandrator.logic.tts_handler.get_audio_cpp_model_catalog",
                    return_value=[{"id": model} for model in models],
                ),
                patch(
                    "pandrator.logic.tts_handler.get_audio_cpp_voice_catalog",
                    side_effect=lambda _base, model, **_kwargs: native_voices.get(
                        model, []
                    ),
                ),
            ):
                catalogues_by_default_model: dict[str, dict[str, Any]] = {}
                for default_model in models:
                    service["default_model"] = default_model
                    catalogues_by_default_model[default_model] = adapter.enrich_catalog(
                        service
                    )
        finally:
            adapter.close()

        catalogue = catalogues_by_default_model[kokoro_model]
        voice_catalogues = catalogue["voice_catalogues"]
        expected_kokoro_voices = list(AUDIO_CPP_KOKORO_VOICES)
        expected_kokoro_voices.remove("af_heart")
        expected_kokoro_voices.insert(0, "af_heart")
        self.assertEqual(expected_kokoro_voices, voice_catalogues[kokoro_model])
        self.assertEqual(54, len(voice_catalogues[kokoro_model]))
        self.assertEqual([], voice_catalogues[turbo_model])
        self.assertEqual(
            ["cached-reference-default", "cached-reference", "native-reference"],
            voice_catalogues[qwen_base],
        )
        self.assertEqual([], voice_catalogues[qwen_design])
        self.assertEqual(["alba"], voice_catalogues[pocket_model])
        self.assertNotIn("alba", voice_catalogues[kokoro_model])
        self.assertNotIn("alba", voice_catalogues[turbo_model])
        self.assertNotIn("alba", voice_catalogues[qwen_base])
        self.assertNotIn("alba", voice_catalogues[qwen_design])
        for model in (kokoro_model, turbo_model, qwen_design, pocket_model):
            self.assertNotIn("native-reference", voice_catalogues[model])
        self.assertEqual("af_heart", catalogue["default_voice"])
        self.assertEqual(
            "cached-reference-default",
            catalogues_by_default_model[qwen_base]["default_voice"],
        )
        self.assertNotIn("default_voice", catalogues_by_default_model[turbo_model])
        self.assertNotIn("default_voice", catalogues_by_default_model[qwen_design])
        pocket_catalogue = catalogues_by_default_model[pocket_model]
        self.assertEqual("alba", pocket_catalogue["default_voice"])
