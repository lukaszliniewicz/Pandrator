import os
import unittest
from unittest.mock import patch

from pandrator.logic import llm_handler
from pandrator.logic.dubbing import credentials


class DubbingCredentialTests(unittest.TestCase):
    def _settings(self, **overrides):
        settings = {
            "correction_model": "anthropic/claude-sonnet-4-6",
            "translation_backend": "llm",
            "translation_model": "anthropic/claude-sonnet-4-6",
            "llm_provider_configs": llm_handler.get_provider_configs(None),
            "llm_default_model": "openai/gpt-5.4-mini",
        }
        settings.update(overrides)
        return settings

    def test_settings_use_deepl_accepts_current_and_legacy_state(self):
        self.assertTrue(credentials.settings_use_deepl(self._settings(translation_backend="deepl")))
        self.assertTrue(credentials.settings_use_deepl({"translation_provider": "DeepL"}))
        self.assertFalse(credentials.settings_use_deepl(self._settings()))

    def test_deepl_translation_requires_deepl_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = credentials.validate_translation_credentials(
                self._settings(translation_backend="deepl")
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.step_key, "translate")
        self.assertIn("DEEPL_API_KEY", result.message)

    def test_deepl_translation_accepts_environment_key(self):
        with patch.dict(os.environ, {"DEEPL_API_KEY": "deepl-key"}, clear=True):
            result = credentials.validate_translation_credentials(
                self._settings(translation_backend="deepl")
            )

        self.assertTrue(result.ok)

    def test_correction_is_independent_from_deepl_translation(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=True):
            result = credentials.validate_correction_credentials(
                self._settings(translation_backend="deepl")
            )

        self.assertTrue(result.ok)

    def test_transcription_diarization_requires_hf_token(self):
        with patch.dict(os.environ, {}, clear=True):
            result = credentials.validate_transcription_credentials(
                self._settings(diarization_enabled=True)
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.step_key, "transcribe")
        self.assertIn("HF_TOKEN", result.message)

    def test_transcription_diarization_accepts_settings_token(self):
        with patch.dict(os.environ, {}, clear=True):
            result = credentials.validate_transcription_credentials(
                self._settings(diarization_enabled=True, hf_token="hf-token")
            )

        self.assertTrue(result.ok)

    def test_parakeet_transcription_does_not_require_whisperx_diarization_token(self):
        with patch.dict(os.environ, {}, clear=True):
            result = credentials.validate_transcription_credentials(
                self._settings(stt_backend="parakeet_onnx", diarization_enabled=True)
            )

        self.assertTrue(result.ok)

    def test_transcription_correction_reuses_llm_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            result = credentials.validate_transcription_credentials(
                self._settings(correction_enabled=True)
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.step_key, "transcribe")
        self.assertIn("Subtitle correction cannot run", result.message)

    def test_generate_audio_without_srt_validates_transcription_requirements(self):
        with patch.dict(os.environ, {}, clear=True):
            result = credentials.validate_task_credentials(
                "generate_audio",
                self._settings(diarization_enabled=True),
                current_srt_exists=False,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.step_key, "transcribe")

    def test_generate_audio_with_translation_validates_translation_requirements(self):
        with patch.dict(os.environ, {}, clear=True):
            result = credentials.validate_task_credentials(
                "generate_audio",
                self._settings(translation_enabled=True, translation_backend="deepl"),
                current_srt_exists=True,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.step_key, "translate")
        self.assertIn("DEEPL_API_KEY", result.message)


if __name__ == "__main__":
    unittest.main()
