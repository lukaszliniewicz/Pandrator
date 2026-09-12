import unittest

from pandrator.logic import tts_handler, tts_provider_profiles
from pandrator.logic.audio_cpp_parameters import validate_model_options
from pandrator.web.generation_audio_identity import _material_settings


class AudioCppParameterTests(unittest.TestCase):
    def test_every_static_model_has_scalar_request_parameters(self):
        self.assertEqual(11, len(tts_provider_profiles.AUDIO_CPP_MODEL_CATALOG))
        for model in tts_provider_profiles.AUDIO_CPP_MODEL_CATALOG:
            with self.subTest(model=model["id"]):
                parameters = model["request_parameters"]
                self.assertTrue(parameters)
                self.assertNotIn("reference_text", parameters)
                self.assertNotIn("voice_id", parameters)
                for descriptor in parameters.values():
                    self.assertIn(
                        descriptor["type"], {"integer", "number", "boolean", "string"}
                    )

    def test_selected_model_settings_are_authoritative_and_isolated(self):
        endpoint = {"model_catalog": tts_provider_profiles.AUDIO_CPP_MODEL_CATALOG}
        model = "qwen3_tts_1_7b_base_q8_0"
        payload = tts_handler._build_audio_cpp_audio_payload(
            "Hello",
            {
                "model": model,
                "audio_cpp_model_settings": {
                    model: {"temperature": 0.8, "max_tokens": None},
                    "voxcpm2_q8_0": {"temperature": 0.1},
                },
                "audio_cpp_options": {"top_p": 0.1},
                "audio_cpp_temperature": 0.2,
                "temperature": 0.3,
                "seed": 99,
                "speed": 1.4,
            },
            endpoint,
        )

        self.assertEqual({"temperature": 0.8}, payload["options"])
        self.assertNotIn("top_p", payload["options"])
        self.assertNotIn("seed", payload["options"])
        self.assertNotIn("speed", payload)

    def test_selected_request_representatives_build_for_each_catalog_model(self):
        representative = {
            "qwen3_tts": {"max_tokens": 1},
            "fish_audio_s2": {"text_chunk_mode": "word_budget"},
            "voxcpm2": {"min_tokens": 0},
            "magpie_tts": {"text_chunk_mode": "tag_aware"},
            "chatterbox": {"do_sample": True},
            "omnivoice": {"speed": 1},
            "pocket_tts": {"max_steps": 0},
            "fireredtts3": {"stop_threshold": 0.5},
            "breeze_tts": {"top_p": 1},
        }
        endpoint = {"model_catalog": tts_provider_profiles.AUDIO_CPP_MODEL_CATALOG}
        for model in tts_provider_profiles.AUDIO_CPP_MODEL_CATALOG:
            with self.subTest(model=model["id"]):
                settings = {
                    "model": model["id"],
                    "audio_cpp_model_settings": {
                        model["id"]: representative[model["family"]]
                    },
                }
                if model["voice_mode"] == "design":
                    settings["generation_prompt"] = "Warm and clear."
                payload = tts_handler._build_audio_cpp_audio_payload(
                    "Hello", settings, endpoint
                )
                self.assertEqual(representative[model["family"]], payload["options"])

    def test_selected_values_validate_types_bounds_and_unknown_keys(self):
        with self.assertRaisesRegex(ValueError, "boolean"):
            validate_model_options("qwen3_tts", {"do_sample": 1})
        with self.assertRaisesRegex(ValueError, "greater than"):
            validate_model_options("fish_audio_s2", {"temperature": 0})
        with self.assertRaisesRegex(ValueError, "Unknown"):
            validate_model_options("qwen3_tts", {"reference_text": "stale"})
        self.assertEqual(
            {"text_chunk_mode": "word_budget"},
            validate_model_options("fish_audio_s2", {"text_chunk_mode": "word_budget"}),
        )

    def test_numeric_wire_limits_reject_overflow_without_rounding(self):
        for value in (9007199254740993, 10**400):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "at most"):
                validate_model_options("qwen3_tts", {"max_tokens": value})
        self.assertEqual(
            {"max_tokens": 2**53 - 1},
            validate_model_options("qwen3_tts", {"max_tokens": 2**53 - 1}),
        )
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_model_options("qwen3_tts", {"temperature": 10**400})

    def test_legacy_wire_aliases_are_fixed(self):
        omni = tts_handler._build_audio_cpp_audio_payload(
            "Hello", {"model": "omnivoice_q8_0", "speed": 1.2}, {}
        )
        self.assertEqual(1.2, omni["options"]["speed"])
        self.assertNotIn("speed", omni)

        magpie = tts_handler._build_audio_cpp_audio_payload(
            "Hello", {"model": "magpie_tts_q8_0", "voice": "Aria"}, {}
        )
        self.assertEqual("Aria", magpie["options"]["voice_id"])
        self.assertNotIn("voice", magpie)

        pocket = tts_handler._build_audio_cpp_audio_payload(
            "Hello",
            {
                "model": "pocket_tts_english_q8_0",
                "audio_cpp_reference_text": "Reviewed words.",
            },
            {},
        )
        self.assertEqual("Reviewed words.", pocket["options"]["voice_clone_text"])
        self.assertNotIn("reference_text", pocket)

    def test_preview_seed_overrides_inherited_selected_model_map(self):
        model = "magpie_tts_q8_0"
        payload = tts_handler._build_audio_cpp_audio_payload(
            "Hello",
            {
                "model": model,
                "audio_cpp_model_settings": {model: {"seed": 3}},
                "preview_service_id": "audio_cpp",
                "audio_cpp_seed": 2**53 - 1,
            },
            {},
        )
        self.assertEqual(2**53 - 1, payload["options"]["seed"])

    def test_identity_keeps_only_selected_canonical_model_settings(self):
        model = "qwen3_tts_1_7b_base_q8_0"
        first = _material_settings(
            {
                "tts": {
                    "service": "audio_cpp",
                    "model": model,
                    "audio_cpp_model_settings": {
                        model: {"temperature": 0.8},
                        "voxcpm2_q8_0": {"temperature": 0.1},
                    },
                    "audio_cpp_temperature": 0.2,
                    "audio_cpp_options": {"top_p": 0.1},
                    "speed": 1.4,
                }
            }
        )
        second = _material_settings(
            {
                "tts": {
                    "service": "audio_cpp",
                    "model": model,
                    "audio_cpp_model_settings": {
                        model: {"temperature": 0.8},
                        "voxcpm2_q8_0": {"temperature": 0.9},
                    },
                    "audio_cpp_temperature": 0.7,
                    "audio_cpp_options": {"top_p": 0.9},
                    "speed": 1.8,
                }
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(
            {model: {"temperature": 0.8}},
            first["audio_cpp_model_settings"],
        )
