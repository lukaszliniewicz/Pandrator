import unittest

from pandrator.logic import llm_handler
from pandrator.logic.dubbing import llm_config, settings


class DubbingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.providers = llm_handler.get_provider_configs(None)

    def test_legacy_provider_model_and_language_migrate_to_native_fields(self):
        migrated = settings.migrate_dubbing_payload(
            {
                "whisper_language": "Polish",
                "translation_provider": "anthropic",
                "translation_model": "claude-sonnet-4-6",
                "custom_translation_model": "unused",
                "custom_api_base": "https://legacy.invalid/v1",
            },
            self.providers,
        )

        self.assertEqual(migrated["stt_language"], "Polish")
        self.assertEqual(migrated["correction_model"], "anthropic/claude-sonnet-4-6")
        self.assertEqual(migrated["translation_backend"], "llm")
        self.assertEqual(migrated["translation_model"], "anthropic/claude-sonnet-4-6")
        self.assertNotIn("whisper_language", migrated)
        self.assertNotIn("translation_provider", migrated)
        self.assertNotIn("custom_api_base", migrated)

    def test_default_payload_contains_only_native_dubbing_fields(self):
        payload = settings.migrate_dubbing_payload({}, self.providers)

        self.assertEqual(payload["stt_language"], "English")
        self.assertEqual(payload["correction_model"], "default")
        self.assertEqual(payload["translation_backend"], "llm")
        self.assertEqual(payload["translation_model"], "default")
        self.assertNotIn("whisper_language", payload)
        self.assertNotIn("translation_provider", payload)
        self.assertNotIn("custom_translation_model", payload)

    def test_legacy_custom_provider_migrates_to_native_custom_model_id(self):
        providers = self.providers + [
            {
                "id": "local-openai",
                "name": "Local OpenAI",
                "provider": "openai",
                "api_base": "http://127.0.0.1:8000/v1",
                "api_key": "",
                "api_key_env": "",
                "is_custom": True,
                "models": ["local-model"],
            }
        ]
        migrated = settings.migrate_dubbing_payload(
            {
                "translation_provider": "local-openai",
                "translation_model": "local-model",
            },
            providers,
        )

        self.assertEqual(migrated["translation_model"], "custom:local-openai/local-model")
        self.assertEqual(migrated["correction_model"], "custom:local-openai/local-model")

    def test_deepl_translation_keeps_correction_on_global_default(self):
        migrated = settings.migrate_dubbing_payload(
            {
                "translation_provider": "deepl",
                "translation_model": "",
            },
            self.providers,
        )

        self.assertEqual(migrated["translation_backend"], "deepl")
        self.assertEqual(migrated["translation_model"], "default")
        self.assertEqual(migrated["correction_model"], "default")

    def test_legacy_merge_threshold_migrates_to_independent_speech_blocks(self):
        migrated = settings.migrate_dubbing_payload(
            {"subtitle_merge_threshold": 475},
            self.providers,
        )

        self.assertEqual(migrated["speech_block_merge_threshold"], 475)
        self.assertEqual(migrated["speech_block_min_chars"], 10)
        self.assertEqual(migrated["speech_block_max_chars"], 220)

    def test_legacy_parakeet_int8_migrates_to_crispasr_q8(self):
        migrated = settings.migrate_dubbing_payload(
            {"stt_engine": "parakeet_onnx", "parakeet_quantization": "int8"},
            self.providers,
        )

        self.assertEqual(migrated["stt_engine"], "parakeet")
        self.assertEqual(migrated["stt_model_quantization"], "q8_0")
        self.assertNotIn("parakeet_quantization", migrated)

    def test_stage_resolver_uses_independent_native_models_and_global_default(self):
        base = {
            "correction_model": "default",
            "translation_backend": "llm",
            "translation_model": "gemini/gemini-3-flash-preview",
            "llm_default_model": "openai/gpt-5.4-mini",
            "llm_provider_configs": self.providers,
        }

        correction = llm_config.resolve_dubbing_llm_settings(base, stage="correction")
        translation = llm_config.resolve_dubbing_llm_settings(base, stage="translation")

        self.assertEqual(correction.model_name, "default")
        self.assertEqual(correction.llm_settings["default_model"], "openai/gpt-5.4-mini")
        self.assertEqual(translation.model_name, "gemini/gemini-3-flash-preview")


if __name__ == "__main__":
    unittest.main()
