"""Persisted audio.cpp voice maps retain overrides and refresh old seeds."""

from __future__ import annotations

import copy
import unittest
from typing import Any, cast

from pandrator.constants import magpie_voice_catalog
from pandrator.logic.audio_cpp_voice_catalogue_compat import (
    merge_audio_cpp_voice_catalogues,
)
from pandrator.logic.tts_provider_profiles import (
    AUDIO_CPP_MODEL_DEFAULT_VOICES,
    AUDIO_CPP_MODEL_PRESETS,
)
from pandrator.logic.tts_service_catalogue import (
    _default_service_configs,
    _merge_service_config,
    get_service_config,
)


def _legacy_generated_maps() -> tuple[dict[str, list[str]], dict[str, str]]:
    return (
        {
            "qwen3_tts_1_7b_customvoice_q8_0": [
                "Aiden",
                "Dylan",
                "Eric",
                "Ono_Anna",
                "Ryan",
                "Serena",
                "Sohee",
                "Uncle_Fu",
                "Vivian",
            ],
            "magpie_tts_q8_0": magpie_voice_catalog(
                include_legacy_emotions=True
            ),
            "pocket_tts_english_q8_0": ["alba"],
        },
        {
            "qwen3_tts_1_7b_customvoice_q8_0": "Aiden",
            "magpie_tts_q8_0": "Magpie-Multilingual.EN-US.Sofia",
            "pocket_tts_english_q8_0": "alba",
        },
    )


class AudioCppVoiceCatalogueCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builtin = next(
            service
            for service in _default_service_configs()
            if service["id"] == "audio_cpp"
        )

    def test_exact_historical_maps_regenerate_current_per_model_maps(self) -> None:
        legacy_catalogues, legacy_defaults = _legacy_generated_maps()
        self.assertEqual(75, len(legacy_catalogues["magpie_tts_q8_0"]))

        merged = _merge_service_config(
            self.builtin,
            {
                "voice_catalogues": legacy_catalogues,
                "default_voices": legacy_defaults,
            },
        )

        catalogues = cast(dict[str, Any], merged["voice_catalogues"])
        defaults = cast(dict[str, Any], merged["default_voices"])
        self.assertEqual(AUDIO_CPP_MODEL_PRESETS, catalogues)
        self.assertEqual(
            AUDIO_CPP_MODEL_DEFAULT_VOICES,
            defaults,
        )
        self.assertEqual(54, len(catalogues["kokoro_82m_q8_0"]))
        self.assertEqual("af_heart", defaults["kokoro_82m_q8_0"])
        self.assertEqual(["alba"], catalogues["pocket_tts_english_bf16"])
        self.assertEqual("alba", defaults["pocket_tts_english_bf16"])
        self.assertEqual(45, len(catalogues["magpie_tts_q8_0"]))
        self.assertNotIn("chatterbox_turbo_q8_0", catalogues)
        self.assertNotIn("chatterbox_turbo_q8_0", defaults)

    def test_partial_and_empty_explicit_overrides_are_preserved(self) -> None:
        raw_catalogues = {
            "kokoro_82m_q8_0": [],
            "pocket_tts_english_bf16": ["cached-user-voice"],
        }
        raw_defaults = {"pocket_tts_english_bf16": ""}
        raw_catalogues_before = copy.deepcopy(raw_catalogues)
        raw_defaults_before = copy.deepcopy(raw_defaults)

        catalogues, defaults = merge_audio_cpp_voice_catalogues(
            AUDIO_CPP_MODEL_PRESETS,
            AUDIO_CPP_MODEL_DEFAULT_VOICES,
            raw_catalogues,
            raw_defaults,
        )

        self.assertEqual([], catalogues["kokoro_82m_q8_0"])
        self.assertNotIn("kokoro_82m_q8_0", defaults)
        self.assertEqual(
            ["cached-user-voice"], catalogues["pocket_tts_english_bf16"]
        )
        self.assertEqual("", defaults["pocket_tts_english_bf16"])
        self.assertIn("magpie_tts_q8_0", catalogues)
        self.assertEqual(raw_catalogues_before, raw_catalogues)
        self.assertEqual(raw_defaults_before, raw_defaults)

        catalogues["pocket_tts_english_bf16"].append("mutated-copy")
        self.assertEqual(
            ["cached-user-voice"], raw_catalogues["pocket_tts_english_bf16"]
        )
        self.assertNotIn("mutated-copy", AUDIO_CPP_MODEL_PRESETS["pocket_tts_english_bf16"])

    def test_custom_voice_list_prevents_legacy_whole_map_refresh(self) -> None:
        legacy_catalogues, legacy_defaults = _legacy_generated_maps()
        legacy_catalogues["qwen3_tts_1_7b_customvoice_q8_0"].append("Custom")

        merged = _merge_service_config(
            self.builtin,
            {
                "voice_catalogues": legacy_catalogues,
                "default_voices": legacy_defaults,
            },
        )

        catalogues = cast(dict[str, Any], merged["voice_catalogues"])
        self.assertEqual(
            legacy_catalogues["qwen3_tts_1_7b_customvoice_q8_0"],
            catalogues["qwen3_tts_1_7b_customvoice_q8_0"],
        )
        self.assertEqual(75, len(catalogues["magpie_tts_q8_0"]))

    def test_provider_then_service_config_precedence_is_preserved(self) -> None:
        legacy_catalogues, legacy_defaults = _legacy_generated_maps()
        service = get_service_config(
            {
                "provider_configs": [
                    {
                        "id": "audio_cpp",
                        "voice_catalogues": legacy_catalogues,
                        "default_voices": legacy_defaults,
                    }
                ],
                "service_configs": [
                    {
                        "id": "audio_cpp",
                        "voice_catalogues": {
                            "kokoro_82m_q8_0": ["service-config-voice"]
                        },
                        "default_voices": {"kokoro_82m_q8_0": ""},
                    }
                ],
            },
            "audio_cpp",
        )

        self.assertIsNotNone(service)
        assert service is not None
        catalogues = cast(dict[str, Any], service["voice_catalogues"])
        defaults = cast(dict[str, Any], service["default_voices"])
        self.assertEqual(
            ["service-config-voice"],
            catalogues["kokoro_82m_q8_0"],
        )
        self.assertEqual("", defaults["kokoro_82m_q8_0"])
        self.assertEqual(45, len(catalogues["magpie_tts_q8_0"]))

    def test_non_audio_cpp_maps_keep_existing_replacement_behavior(self) -> None:
        merged = _merge_service_config(
            {
                "id": "generic-service",
                "voice_catalogues": {"base-model": ["base-voice"]},
                "default_voices": {"base-model": "base-voice"},
            },
            {
                "voice_catalogues": {"custom-model": ["custom-voice"]},
                "default_voices": {"custom-model": "custom-voice"},
            },
        )

        self.assertEqual(
            {"custom-model": ["custom-voice"]}, merged["voice_catalogues"]
        )
        self.assertEqual(
            {"custom-model": "custom-voice"}, merged["default_voices"]
        )
