import unittest
import os
from unittest.mock import patch

from pandrator.logic import llm_handler


class LlmHandlerTests(unittest.TestCase):
    def test_chat_completion_with_metadata_forwards_max_tokens(self):
        captured_payload = {}

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {
                "id": "response-1",
                "model": kwargs["model"],
                "choices": [
                    {
                        "message": {
                            "content": "Corrected text",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                },
            }

        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            result = llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "Fix this"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={
                    "request_timeout_seconds": 30,
                    "reasoning_effort": "medium",
                },
                max_tokens=1234,
                temperature=0.1,
            )

        self.assertEqual(result.content, "Corrected text")
        self.assertEqual(captured_payload["max_tokens"], 1234)
        self.assertEqual(captured_payload["temperature"], 0.1)
        self.assertEqual(captured_payload["timeout"], 30)
        self.assertEqual(captured_payload["reasoning_effort"], "medium")

    def test_chat_completion_with_metadata_uses_model_defaults_when_unset(self):
        captured_payload = {}

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}}]}

        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "Fix this"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={"request_timeout_seconds": 600},
            )

        self.assertNotIn("max_tokens", captured_payload)
        self.assertNotIn("temperature", captured_payload)
        self.assertEqual(captured_payload["timeout"], 600)

    def test_chat_completion_uses_explicit_builtin_provider_api_key(self):
        captured_payload = {}
        provider_configs = llm_handler.get_provider_configs(None)
        for provider in provider_configs:
            if provider["id"] == "openai":
                provider["api_key"] = "explicit-openai-key"

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {
                "model": kwargs["model"],
                "choices": [{"message": {"content": "ok"}}],
            }

        with patch.dict(os.environ, {}, clear=True), patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "Fix this"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={"provider_configs": provider_configs},
            )

        self.assertEqual(captured_payload["api_key"], "explicit-openai-key")

    def test_validate_model_credentials_requires_builtin_provider_key(self):
        with patch.dict(os.environ, {}, clear=True):
            status = llm_handler.validate_model_credentials(
                "claude-sonnet-4-6",
                {"provider_configs": llm_handler.get_provider_configs(None)},
            )

        self.assertFalse(status.ok)
        self.assertTrue(status.needs_api_key)
        self.assertEqual(status.api_key_env, "ANTHROPIC_API_KEY")
        self.assertIn("Anthropic requires an API key", status.message)

    def test_validate_model_credentials_accepts_builtin_env_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=True):
            status = llm_handler.validate_model_credentials(
                "claude-sonnet-4-6",
                {"provider_configs": llm_handler.get_provider_configs(None)},
            )

        self.assertTrue(status.ok)
        self.assertTrue(status.needs_api_key)
        self.assertEqual(status.provider, "anthropic")

    def test_validate_model_credentials_allows_keyless_custom_openai_endpoint(self):
        provider_configs = llm_handler.get_provider_configs(None)
        provider_configs.append(
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
        )

        with patch.dict(os.environ, {}, clear=True):
            status = llm_handler.validate_model_credentials(
                "custom:local-openai/local-model",
                {"provider_configs": provider_configs},
            )

        self.assertTrue(status.ok)
        self.assertFalse(status.needs_api_key)
        self.assertEqual(status.provider_id, "local-openai")

    def test_validate_model_credentials_requires_openrouter_env_for_prefixed_model(self):
        with patch.dict(os.environ, {}, clear=True):
            status = llm_handler.validate_model_credentials(
                "openrouter/deepseek/deepseek-r1",
                {"provider_configs": llm_handler.get_provider_configs(None)},
            )

        self.assertFalse(status.ok)
        self.assertTrue(status.needs_api_key)
        self.assertEqual(status.provider, "openrouter")
        self.assertEqual(status.api_key_env, "OPENROUTER_API_KEY")


if __name__ == "__main__":
    unittest.main()
