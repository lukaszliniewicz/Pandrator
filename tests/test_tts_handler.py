import unittest
import base64
from unittest.mock import Mock, patch

from pandrator.logic import tts_handler


class TTSHandlerTests(unittest.TestCase):
    def test_commercial_tts_cost_estimate_uses_model_units_and_actual_duration(self):
        usage = tts_handler.estimate_tts_usage(
            "A" * 400,
            2_000,
            {"service": "OpenAI", "model": "gpt-4o-mini-tts"},
        )

        self.assertTrue(usage["commercial"])
        self.assertTrue(usage["estimated"])
        self.assertEqual(100, usage["input_tokens"])
        self.assertEqual(42, usage["output_audio_tokens"])
        self.assertAlmostEqual(0.000564, usage["cost_usd"])

    def test_local_tts_does_not_create_a_commercial_cost_estimate(self):
        self.assertIsNone(
            tts_handler.estimate_tts_usage(
                "Local speech",
                1_000,
                {"service": "Kokoro", "model": "gpt-4o-mini-tts"},
            )
        )

    def test_azure_openai_profile_uses_api_key_header_and_manual_deployment_directly(self):
        settings = {
            "service": tts_handler.OPENAI_COMPAT_SERVICE,
            "openai_audio_endpoint": "azure-openai-v1",
            "xtts_model": "my-tts-deployment",
            "speaker": "alloy",
            "provider_configs": [
                {
                    "id": "azure-openai-v1",
                    "name": "Azure OpenAI (v1 TTS)",
                    "provider": "openai",
                    "api_base": "https://example-resource.openai.azure.com",
                    "adapter": "openai_compatible",
                    "speech_path": "/openai/v1/audio/speech?api-version=preview",
                    "auth_mode": "api-key",
                    "direct_http": True,
                    "api_key": "azure-secret",
                    "models": ["my-tts-deployment"],
                    "default_model": "my-tts-deployment",
                }
            ],
        }
        with patch("pandrator.logic.tts_handler._request_litellm_audio") as litellm, patch(
            "pandrator.logic.tts_handler.requests.post"
        ) as post:
            post.return_value.status_code = 200
            tts_handler._request_openai_compatible_audio("Hello", settings)

        litellm.assert_not_called()
        self.assertEqual(
            "https://example-resource.openai.azure.com/openai/v1/audio/speech?api-version=preview",
            post.call_args.args[0],
        )
        self.assertEqual({"api-key": "azure-secret"}, post.call_args.kwargs["headers"])
        self.assertEqual("my-tts-deployment", post.call_args.kwargs["json"]["model"])

    def test_vertex_tts_wraps_raw_pcm_as_wav(self):
        pcm = b"\x00\x00\x01\x00"
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode()}}]}}]
        }
        settings = {
            "service": tts_handler.VERTEX_SERVICE,
            "model": "gemini-2.5-flash-tts",
            "voice": "Kore",
            "generation_prompt": "Speak brightly, with a quick conversational rhythm.",
            "provider_configs": [{"id": "vertex_ai", "vertex_location": "us-central1"}],
        }
        with patch("pandrator.logic.tts_handler._vertex_access_token", return_value=("token", "project")), patch(
            "pandrator.logic.tts_handler.requests.post", return_value=response
        ) as post:
            audio_response = tts_handler._request_vertex_ai_audio("Hello", settings)

        self.assertTrue(audio_response.content.startswith(b"RIFF"))
        self.assertIn("/projects/project/locations/us-central1/", post.call_args.args[0])
        self.assertEqual("Bearer token", post.call_args.kwargs["headers"]["Authorization"])
        self.assertEqual("AUDIO", post.call_args.kwargs["json"]["generationConfig"]["responseModalities"][0])
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Speaking directions:", prompt)
        self.assertIn("Speak brightly", prompt)
        self.assertIn("Transcript:\nHello", prompt)

    def test_generation_prompt_capabilities_are_model_specific(self):
        services = {item["id"]: item for item in tts_handler.get_service_configs({})}
        self.assertEqual(
            ["gpt-4o-mini-tts"],
            services["openai"][tts_handler.GENERATION_PROMPT_MODELS_FIELD],
        )
        self.assertEqual(
            tts_handler.GEMINI_TTS_MODELS,
            services["gemini"][tts_handler.GENERATION_PROMPT_MODELS_FIELD],
        )
        self.assertEqual(
            tts_handler.GEMINI_TTS_MODELS,
            services["vertex_ai"][tts_handler.GENERATION_PROMPT_MODELS_FIELD],
        )
        self.assertIn(
            "Prebuilt Voices",
            services["kobold_qwen"][tts_handler.GENERATION_PROMPT_MODELS_FIELD],
        )
        self.assertNotIn(
            "Voice Cloning",
            services["kobold_qwen"][tts_handler.GENERATION_PROMPT_MODELS_FIELD],
        )

    def test_openai_generation_prompt_uses_instructions_only_on_capable_model(self):
        endpoint = {
            "name": "OpenAI",
            "provider": "openai",
            "default_voice": "alloy",
        }
        settings = {
            "xtts_model": "gpt-4o-mini-tts",
            "speaker": "alloy",
            "generation_prompt": "Speak like a patient documentary narrator.",
        }
        payload = tts_handler._build_openai_compatible_audio_payload(
            "A short transcript.", settings, endpoint
        )
        self.assertEqual(
            "Speak like a patient documentary narrator.", payload["instructions"]
        )
        self.assertEqual("A short transcript.", payload["input"])

        settings["xtts_model"] = "tts-1-hd"
        classic_payload = tts_handler._build_openai_compatible_audio_payload(
            "A short transcript.", settings, endpoint
        )
        self.assertNotIn("instructions", classic_payload)

    def test_gemini_generation_prompt_is_combined_with_labeled_transcript(self):
        payload = tts_handler._build_openai_compatible_audio_payload(
            "The transcript must remain unchanged.",
            {
                "xtts_model": "gemini-2.5-flash-tts",
                "speaker": "Kore",
                "generation_prompt": "Warm and reassuring, with deliberate pauses.",
            },
            {"name": "Google Gemini", "provider": "gemini"},
        )
        self.assertNotIn("instructions", payload)
        self.assertIn("Speaking directions:\nWarm and reassuring", payload["input"])
        self.assertIn("Transcript:\nThe transcript must remain unchanged.", payload["input"])
        self.assertNotEqual("The transcript must remain unchanged.", payload["input"])

    def test_json_speech_response_reports_provider_error_instead_of_audio_decode_noise(self):
        response = Mock()
        response.headers = {"Content-Type": "application/json"}
        response.content = b'{"detail":"voice is unavailable"}'
        response.json.return_value = {"detail": "voice is unavailable"}
        with self.assertRaisesRegex(RuntimeError, "voice is unavailable"):
            tts_handler._decode_audio_response(response)

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

    def test_generic_provider_adapter_round_trip_has_no_openai_catalog_defaults(self):
        success, providers, provider_id, message = tts_handler.save_provider(
            {"provider_configs": []},
            provider_name="StyleTTS Local",
            provider_type="openai",
            api_base="http://127.0.0.1:8000",
            adapter_config={
                "adapter": "generic_json",
                "speech_path": "/generate",
                "request_fields": {
                    "text": "text",
                    "model": "",
                    "voice": "speaker",
                    "speed": "",
                    "format": "",
                },
            },
        )

        self.assertTrue(success, message)
        self.assertEqual(provider_id, "styletts-local")
        self.assertEqual(providers[0]["adapter"], "generic_json")
        self.assertEqual(providers[0]["speech_path"], "/generate")
        self.assertEqual(providers[0]["models"], [])
        self.assertEqual(providers[0]["voices"], [])

    def test_generic_provider_generation_uses_discovered_mapping(self):
        settings = {
            "service": tts_handler.OPENAI_COMPAT_SERVICE,
            "openai_audio_endpoint": "styletts-local",
            "xtts_model": "",
            "speaker": "alice",
            "speed": 1.2,
            "provider_configs": [
                {
                    "id": "styletts-local",
                    "name": "StyleTTS Local",
                    "provider": "openai",
                    "api_base": "http://127.0.0.1:8000",
                    "adapter": "generic_json",
                    "speech_path": "/generate",
                    "request_fields": {
                        "text": "text",
                        "model": "",
                        "voice": "speaker",
                        "speed": "rate",
                        "format": "",
                    },
                    "request_defaults": {"language": "en"},
                }
            ],
        }

        with patch("pandrator.logic.tts_handler.requests.post") as mock_post:
            tts_handler._request_openai_compatible_audio("Hello", settings)

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(mock_post.call_args.args[0], "http://127.0.0.1:8000/generate")
        self.assertEqual(kwargs["headers"], {})
        self.assertEqual(
            kwargs["json"],
            {"language": "en", "text": "Hello", "speaker": "alice", "rate": 1.2},
        )

    def test_generic_provider_connection_uses_safe_get_on_configured_route(self):
        settings = {
            "service": tts_handler.OPENAI_COMPAT_SERVICE,
            "openai_audio_endpoint": "styletts-local",
            "provider_configs": [
                {
                    "id": "styletts-local",
                    "name": "StyleTTS Local",
                    "provider": "openai",
                    "api_base": "http://127.0.0.1:8000",
                    "adapter": "generic_json",
                    "speech_path": "/generate",
                    "request_fields": {"text": "text"},
                }
            ],
        }

        with patch("pandrator.logic.tts_handler.requests.get") as mock_get:
            mock_get.return_value.status_code = 405
            connected, message = tts_handler.check_openai_audio_connection(settings)

        self.assertTrue(connected, message)
        mock_get.assert_called_once_with(
            "http://127.0.0.1:8000/generate",
            headers={},
            timeout=8,
        )

    def test_openai_provider_generation_prioritizes_profile_speech_path(self):
        settings = {
            "service": tts_handler.OPENAI_COMPAT_SERVICE,
            "openai_audio_endpoint": "profile-server",
            "xtts_model": "tts-local",
            "speaker": "voice-local",
            "provider_configs": [
                {
                    "id": "profile-server",
                    "name": "Profile Server",
                    "provider": "openai",
                    "api_base": "http://127.0.0.1:9000",
                    "adapter": "openai_compatible",
                    "speech_path": "/custom/speech",
                    "models": ["tts-local"],
                    "voices": ["voice-local"],
                }
            ],
        }

        with patch(
            "pandrator.logic.tts_handler._request_litellm_audio",
            side_effect=RuntimeError("use direct HTTP"),
        ), patch("pandrator.logic.tts_handler.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            tts_handler._request_openai_compatible_audio("Hello", settings)

        self.assertEqual(mock_post.call_args.args[0], "http://127.0.0.1:9000/custom/speech")

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

    def test_local_service_preserves_discovered_catalogue_and_request_schema(self):
        settings = {
            "provider_configs": [
                {
                    "id": "kokoro",
                    "name": "Kokoro",
                    "api_base": "http://kokoro.example:8880",
                    "models": ["kokoro-v1"],
                    "voices": ["af_heart", "bf_alice"],
                    "default_model": "kokoro-v1",
                    "default_voice": "bf_alice",
                    "adapter": "generic_json",
                    "speech_path": "/generate",
                    "request_fields": {"text": "text", "voice": "voice"},
                    "request_defaults": {"format": "wav"},
                }
            ]
        }
        service = next(item for item in tts_handler.get_service_configs(settings) if item["id"] == "kokoro")
        self.assertEqual(["kokoro-v1"], service["models"])
        self.assertEqual(["af_heart", "bf_alice"], service["voices"])
        self.assertEqual("bf_alice", service["default_voice"])
        self.assertEqual("/generate", service["speech_path"])
        self.assertEqual({"format": "wav"}, service["request_defaults"])

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
                    "language": "pt-BR",
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
            self.assertEqual(payload["language"], "pt")
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

    def test_chatterbox_language_normalization_preserves_only_supported_base_codes(self):
        self.assertEqual(tts_handler.normalize_chatterbox_language_code("pt_br"), "pt")
        self.assertEqual(tts_handler.normalize_chatterbox_language_code("en-US"), "en")
        self.assertEqual(tts_handler.normalize_chatterbox_language_code("zh-CN"), "zh")
        self.assertEqual(tts_handler.normalize_chatterbox_language_code("vi-VN"), "vi-vn")

    def test_kobold_qwen_payload_construction(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.content = b"fake audio content"

            tts_handler._request_kobold_qwen_audio(
                "Hello world",
                {
                    "xtts_model": "qwen3-tts",
                    "speaker": "reader-voice",
                    "speed": 0.9,
                },
                "http://localhost:8042",
            )

        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.args[0], "http://localhost:8042/v1/audio/speech")
        called_kwargs = mock_post.call_args.kwargs
        self.assertEqual(
            called_kwargs["headers"],
            {"Authorization": f"Bearer {tts_handler.XTTS_OPENAI_PLACEHOLDER_API_KEY}"},
        )
        self.assertEqual(
            called_kwargs["json"],
            {
                "model": "qwen3-tts",
                "input": "Hello world",
                "voice": "reader-voice",
                "speed": 0.9,
                "response_format": "wav",
            },
        )
        self.assertEqual(
            called_kwargs["timeout"],
            tts_handler.KOBOLD_QWEN_MODEL_PREPARATION_TIMEOUT_SECONDS,
        )

    def test_kobold_qwen_forwards_generation_prompt_only_to_custom_voice(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            tts_handler._request_kobold_qwen_audio(
                "Hello world",
                {
                    "model": "Prebuilt Voices",
                    "voice": "Ryan",
                    "generation_prompt": "Sound excited but controlled.",
                },
                "http://localhost:8042",
            )
        self.assertEqual(
            "Sound excited but controlled.",
            mock_post.call_args.kwargs["json"]["instructions"],
        )

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            tts_handler._request_kobold_qwen_audio(
                "Hello world",
                {
                    "model": "Voice Cloning",
                    "voice": "my-cloned-voice",
                    "generation_prompt": "This model cannot use the prompt.",
                },
                "http://localhost:8042",
            )
        self.assertNotIn("instructions", mock_post.call_args.kwargs["json"])

    def test_kobold_qwen_rejects_a_voice_from_the_wrong_model_catalogue(self):
        with self.assertRaisesRegex(ValueError, "pre-built"):
            tts_handler._request_kobold_qwen_audio(
                "Hello world",
                {"model": "Voice Cloning", "voice": "Ryan"},
                "http://localhost:8042",
            )

        with self.assertRaisesRegex(ValueError, "cloning reference"):
            tts_handler._request_kobold_qwen_audio(
                "Hello world",
                {"model": "Prebuilt Voices", "voice": "my-uploaded-voice"},
                "http://localhost:8042",
            )

    def test_kobold_qwen_catalogue_seeds_kobo_as_a_cloning_voice(self):
        with patch(
            "requests.get",
            side_effect=tts_handler.requests.exceptions.ConnectionError("offline"),
        ):
            catalogue = tts_handler.get_kobold_qwen_voice_catalog("http://localhost:8042")

        kobo = next(item for item in catalogue if item["id"] == "kobo")
        self.assertEqual(kobo["type"], "cloned")
        self.assertEqual(kobo["model"], "Voice Cloning")

    def test_silero_catalog_filters_installed_models_and_voices(self):
        models_response = Mock()
        models_response.json.return_value = {
            "data": [
                {"id": "v5_cis_base_nostress", "status": {"installed": True}},
                {"id": "v5_cis_ext", "status": {"installed": False}},
            ]
        }
        models_response.raise_for_status.return_value = None
        voices_response = Mock()
        voices_response.json.return_value = {
            "data": [{"id": "ukr_igor", "language": "ukr", "available": True}]
        }
        voices_response.raise_for_status.return_value = None

        with patch("pandrator.logic.tts_handler.requests.get", side_effect=[models_response, voices_response]) as get:
            self.assertEqual(
                tts_handler.get_silero_models("http://silero", installed_only=True),
                ["v5_cis_base_nostress"],
            )
            self.assertEqual(
                tts_handler.get_silero_speakers(
                    "http://silero",
                    model="v5_cis_base_nostress",
                    language="uk",
                ),
                ["ukr_igor"],
            )

        self.assertEqual(get.call_args_list[1].kwargs["params"]["language"], "ukr")

    def test_silero_generation_uses_stateless_speech_contract(self):
        response = Mock()
        response.raise_for_status.return_value = None
        decoded = object()
        with patch("pandrator.logic.tts_handler.requests.post", return_value=response) as post, patch(
            "pandrator.logic.tts_handler._decode_audio_response",
            return_value=decoded,
        ):
            result = tts_handler.text_to_audio(
                "Привіт, світе!",
                {
                    "service": "Silero",
                    "xtts_model": "v5_cis_base_nostress",
                    "speaker": "ukr_igor",
                    "language": "uk",
                    "speed": 1.1,
                    "silero_sample_rate": 24000,
                    "silero_stress_mode": "auto",
                },
                silero_base_url="http://silero",
                max_attempts=1,
            )

        self.assertIs(result, decoded)
        self.assertEqual(post.call_args.args[0], "http://silero/v1/audio/speech")
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "model": "v5_cis_base_nostress",
                "input": "Привіт, світе!",
                "voice": "ukr_igor",
                "language": "ukr",
                "response_format": "wav",
                "speed": 1.1,
                "sample_rate": 24000,
                "stress_mode": "auto",
            },
        )

    def test_tts_payload_collapses_visual_line_breaks_and_whitespace(self):
        response = Mock()
        response.raise_for_status.return_value = None
        with patch("pandrator.logic.tts_handler.requests.post", return_value=response) as post, patch(
            "pandrator.logic.tts_handler._decode_audio_response",
            return_value=object(),
        ):
            tts_handler.text_to_audio(
                "Visually wrapped\n  but spoken\tcontinuously.",
                {"service": "Silero", "speaker": "en_0", "language": "en"},
                silero_base_url="http://silero",
                max_attempts=1,
            )

        self.assertEqual(
            post.call_args.kwargs["json"]["input"],
            "Visually wrapped but spoken continuously.",
        )

    def test_tts_generation_retries_transient_http_failures_with_backoff(self):
        transient = Mock(status_code=503, headers={}, text="temporarily unavailable")
        transient.raise_for_status.side_effect = tts_handler.requests.exceptions.HTTPError(
            "503 unavailable", response=transient
        )
        success = Mock(status_code=200, headers={"Content-Type": "audio/wav"}, text="")
        success.raise_for_status.return_value = None
        cancel_event = Mock()
        cancel_event.is_set.return_value = False
        cancel_event.wait.return_value = False
        decoded = object()

        with patch("pandrator.logic.tts_handler.requests.post", side_effect=[transient, success]) as post, patch(
            "pandrator.logic.tts_handler._decode_audio_response", return_value=decoded
        ):
            result = tts_handler.text_to_audio(
                "Retry me",
                {"service": "Silero", "speaker": "en_0", "language": "en"},
                max_attempts=3,
                cancel_event=cancel_event,
            )

        self.assertIs(decoded, result)
        self.assertEqual(2, post.call_count)
        retry_delay = cancel_event.wait.call_args.args[0]
        self.assertGreaterEqual(retry_delay, 0.4)
        self.assertLessEqual(retry_delay, 0.6)

    def test_tts_generation_does_not_retry_invalid_http_requests(self):
        rejected = Mock(status_code=400, headers={}, text="invalid voice")
        rejected.raise_for_status.side_effect = tts_handler.requests.exceptions.HTTPError(
            "400 invalid", response=rejected
        )

        with patch("pandrator.logic.tts_handler.requests.post", return_value=rejected) as post:
            result = tts_handler.text_to_audio(
                "Do not retry",
                {"service": "Silero", "speaker": "missing", "language": "en"},
                max_attempts=5,
            )

        self.assertIsNone(result)
        self.assertEqual(1, post.call_count)


if __name__ == "__main__":
    unittest.main()
