import unittest

from pandrator.logic import tts_handler


class TTSHandlerTests(unittest.TestCase):
    def test_first_class_services_are_separate_from_custom_providers(self):
        settings = {
            "provider_configs": [
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "api_base": "https://openai.example/v1",
                },
                {
                    "id": "magpie",
                    "name": "Magpie",
                    "api_base": "http://127.0.0.1:9999",
                },
                {
                    "id": "my-server",
                    "name": "My Server",
                    "provider": "openai",
                    "api_base": "http://127.0.0.1:9000/v1",
                },
            ],
            "service_configs": [],
        }

        custom_providers = tts_handler.get_provider_configs(settings)
        self.assertEqual([provider["id"] for provider in custom_providers], ["my-server"])

        services = {
            service["id"]: service
            for service in tts_handler.get_service_configs(settings)
        }
        self.assertEqual(services["openai"]["api_base"], "https://openai.example/v1")
        self.assertEqual(services["magpie"]["api_base"], "http://127.0.0.1:9999")

    def test_first_class_cloud_service_and_custom_endpoint_resolve_separately(self):
        settings = {
            "service": tts_handler.GEMINI_SERVICE,
            "service_configs": [
                {
                    "id": "gemini",
                    "name": "Google Gemini",
                    "kind": "commercial",
                    "provider": "gemini",
                    "api_base": "https://gemini.example/openai",
                },
            ],
            "provider_configs": [],
            "openai_audio_endpoint": "",
        }

        endpoint, error = tts_handler.resolve_openai_audio_endpoint(settings)
        self.assertEqual(error, "")
        self.assertEqual(endpoint["name"], "gemini")
        self.assertEqual(endpoint["base_url"], "https://gemini.example/openai")

        settings["service"] = tts_handler.OPENAI_COMPAT_SERVICE
        settings["openai_audio_endpoint"] = "gemini"
        endpoint, error = tts_handler.resolve_openai_audio_endpoint(settings)
        self.assertIsNone(endpoint)
        self.assertIn("No custom", error)

    def test_custom_provider_cannot_use_first_class_service_name(self):
        success, providers, provider_id, message = tts_handler.save_provider(
            {},
            provider_name="Magpie",
            provider_type="openai",
            api_base="http://127.0.0.1:9999",
        )

        self.assertFalse(success)
        self.assertEqual(providers, [])
        self.assertEqual(provider_id, "")
        self.assertIn("first-class", message)

    def test_service_base_url_uses_saved_default_and_active_session_override(self):
        settings = {
            "service": "Kokoro",
            "use_external_server": False,
            "external_server_url": "http://session.example:9999",
            "service_configs": [
                {
                    "id": "kokoro",
                    "name": "Kokoro",
                    "kind": "local",
                    "api_base": "http://saved.example:8880",
                },
            ],
            "provider_configs": [],
        }

        self.assertEqual(
            tts_handler.resolve_service_base_url(settings, "Kokoro"),
            "http://saved.example:8880",
        )
        settings["use_external_server"] = True
        self.assertEqual(
            tts_handler.resolve_service_base_url(settings, "Kokoro"),
            "http://session.example:9999",
        )

    def test_kokoro_voice_language_inference(self):
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("af_heart"), "en")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("bf_alice"), "en-gb")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("jf_alpha"), "ja")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("martin"), "de")
        self.assertEqual(tts_handler.infer_kokoro_voice_language_code("alloy"), "en")
        self.assertEqual(
            tts_handler.infer_kokoro_voice_language_code("af_heart(0.7)+am_echo(0.3)"),
            "en",
        )
        self.assertEqual(
            tts_handler.infer_kokoro_voice_language_code("af_heart+jf_alpha"),
            "",
        )

    def test_fishs2_payload_includes_advanced_settings(self):
        payload = tts_handler._build_fishs2_payload(
            "Hello",
            {
                "xtts_model": "fishs2",
                "speaker": "demo-voice",
                "speed": 1.25,
                "fishs2_temperature": 0.8,
                "fishs2_top_p": 0.65,
                "fishs2_chunk_length": 240,
                "fishs2_latency": "normal",
                "fishs2_normalize": False,
                "fishs2_prosody_volume": 3.5,
                "fishs2_normalize_loudness": False,
            },
        )

        self.assertEqual(payload["model"], "fishaudio/s2-pro")
        self.assertEqual(payload["voice"], "demo-voice")
        self.assertEqual(payload["temperature"], 0.8)
        self.assertEqual(payload["top_p"], 0.65)
        self.assertEqual(payload["chunk_length"], 240)
        self.assertEqual(payload["latency"], "normal")
        self.assertFalse(payload["normalize"])
        self.assertEqual(payload["speed"], 1.25)
        self.assertEqual(payload["prosody"]["speed"], 1.25)
        self.assertEqual(payload["prosody"]["volume"], 3.5)
        self.assertFalse(payload["prosody"]["normalize_loudness"])

    def test_fishs2_payload_clamps_advanced_settings(self):
        payload = tts_handler._build_fishs2_payload(
            "Hello",
            {
                "speed": 5.0,
                "fishs2_temperature": 2.0,
                "fishs2_top_p": -1.0,
                "fishs2_chunk_length": 999,
                "fishs2_latency": "fastest",
                "fishs2_prosody_volume": -100.0,
            },
        )

        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["top_p"], 0.0)
        self.assertEqual(payload["chunk_length"], 300)
        self.assertEqual(payload["latency"], "balanced")
        self.assertEqual(payload["speed"], 2.0)
        self.assertEqual(payload["prosody"]["volume"], -20.0)

    def test_chatterbox_payload_construction(self):
        from unittest.mock import patch
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.content = b"fake audio content"
            
            tts_handler._request_chatterbox_audio(
                "Hello world",
                {
                    "xtts_model": "turbo",
                    "speaker": "test-voice",
                    "speed": 1.2,
                    "language": "en",
                    "chatterbox_temperature": 0.9,
                    "chatterbox_repetition_penalty": 1.5,
                    "chatterbox_min_p": 0.08,
                    "chatterbox_top_p": 0.8,
                    "chatterbox_top_k": 800,
                    "chatterbox_exaggeration": 0.4,
                    "chatterbox_cfg_weight": 0.7,
                    "chatterbox_norm_loudness": False,
                },
                "http://localhost:8040"
            )
            
            mock_post.assert_called_once()
            called_args, called_kwargs = mock_post.call_args
            payload = called_kwargs["json"]
            self.assertEqual(payload["model"], "chatterbox-turbo")
            self.assertEqual(payload["input"], "Hello world")
            self.assertEqual(payload["voice"], "test-voice")
            self.assertEqual(payload["speed"], 1.2)
            self.assertEqual(payload["language"], "en")
            self.assertEqual(payload["temperature"], 0.9)
            self.assertEqual(payload["repetition_penalty"], 1.5)
            self.assertEqual(payload["min_p"], 0.08)
            self.assertEqual(payload["top_p"], 0.8)
            self.assertEqual(payload["top_k"], 800)
            self.assertEqual(payload["exaggeration"], 0.4)
            self.assertEqual(payload["cfg_weight"], 0.7)
            self.assertFalse(payload["norm_loudness"])

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            
            tts_handler._request_chatterbox_audio(
                "Bonjour",
                {
                    "xtts_model": "chatterbox-multilingual",
                    "language": "fr",
                },
                "http://localhost:8040"
            )
            
            mock_post.assert_called_once()
            called_args, called_kwargs = mock_post.call_args
            payload = called_kwargs["json"]
            self.assertEqual(payload["model"], "chatterbox-multilingual")
            self.assertEqual(payload["input"], "Bonjour")
            self.assertIsNone(payload["voice"])
            self.assertEqual(payload["language"], "fr")
            # Fallbacks
            self.assertEqual(payload["temperature"], 0.8)
            self.assertEqual(payload["repetition_penalty"], 1.2)
            self.assertEqual(payload["min_p"], 0.05)
            self.assertEqual(payload["top_p"], 0.95)
            self.assertEqual(payload["top_k"], 1000)
            self.assertTrue(payload["norm_loudness"])


if __name__ == "__main__":
    unittest.main()
