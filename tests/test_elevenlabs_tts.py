import unittest
from unittest.mock import Mock, patch

import pytest
import requests

from pandrator.logic import tts_handler, tts_provider_profiles
from pandrator.logic.speech_performance import compile_performance
from pandrator.web.credentials import TTS_SERVICE_ENVS, redact_inline_secrets
from pandrator.web.tts_providers import (
    ElevenLabsAdapter,
    TtsCatalogueService,
    TtsHealth,
    TtsProviderError,
    TtsProviderRegistry,
)


def _response(payload, *, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.text = str(payload)
    response.raise_for_status.side_effect = (
        RuntimeError(f"HTTP {status_code}") if status_code >= 400 else None
    )
    return response


class ElevenLabsRequestTests(unittest.TestCase):
    def test_elevenlabs_is_a_first_class_native_service_with_required_credential(self):
        service = tts_handler.get_service_config({}, "ElevenLabs")

        self.assertIsNotNone(service)
        self.assertEqual("elevenlabs", service["id"])
        self.assertEqual("https://api.elevenlabs.io", service["api_base"])
        self.assertEqual("elevenlabs_native", service["adapter"])
        self.assertTrue(service["credential_required"])
        self.assertEqual("ELEVENLABS_API_KEY", service["api_key_env"])

    def _settings(self):
        return {
            "service": tts_handler.ELEVENLABS_SERVICE,
            "xtts_model": "eleven_multilingual_v2",
            "speaker": "voice/with spaces?x",
            "provider_configs": [
                {
                    "id": "elevenlabs",
                    "api_key": "secret-key",
                    "api_base": tts_handler.ELEVENLABS_API_BASE_URL,
                }
            ],
        }

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_native_request_encodes_voice_and_uses_elevenlabs_contract(self, post):
        response = _response({}, status_code=200)
        post.return_value = response

        returned = tts_handler._request_elevenlabs_audio("Hello", self._settings())

        self.assertIs(returned, response)
        self.assertEqual(
            "https://api.elevenlabs.io/v1/text-to-speech/voice%2Fwith%20spaces%3Fx",
            post.call_args.args[0],
        )
        self.assertEqual("secret-key", post.call_args.kwargs["headers"]["xi-api-key"])
        self.assertEqual(
            {"text": "Hello", "model_id": "eleven_multilingual_v2"},
            post.call_args.kwargs["json"],
        )
        self.assertEqual(
            {"output_format": "mp3_44100_128"}, post.call_args.kwargs["params"]
        )
        self.assertEqual(300, post.call_args.kwargs["timeout"])

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_v3_request_compiles_inline_controls_and_matches_preview(self, post):
        post.return_value = _response({}, status_code=200)
        settings = self._settings()
        settings.update({
            "xtts_model": "eleven_multilingual_v2",
            "elevenlabs_model": "eleven_v3",
            "generation_prompt": "Restrained",
            "performance_allow_vocalizations": True,
            "tts_context_mode": "both",
            "_semantic_context": {"before": "Before.", "after": "After."},
            "_performance": {
                "decision": "steer",
                "spans": [{"anchor": {"quote": "there"}, "delivery": {"emphasis": "strong"}}],
                "events": [{"kind": "sigh", "position": "before"}],
            },
            "elevenlabs_voice_settings": {"stability": 0.5},
        })
        endpoint = {
            "id": "my-eleven-endpoint", "adapter": "elevenlabs_native",
            "api_base": "https://custom.example.test", "api_key": "custom-key",
        }
        preview = compile_performance("Hello there.", settings, endpoint)
        tts_handler._request_elevenlabs_audio("Hello there.", settings, endpoint=endpoint)
        payload = post.call_args.kwargs["json"]
        self.assertEqual("eleven_v3", payload["model_id"])
        self.assertEqual(preview.input, payload["text"])
        self.assertIn("[sighs]", payload["text"])
        self.assertIn("[strong emphasis]there", payload["text"])
        self.assertEqual({"stability": 0.5}, payload["voice_settings"])
        self.assertNotIn("instructions", payload)
        self.assertNotIn("previous_text", payload)
        self.assertNotIn("next_text", payload)
        self.assertEqual("https://custom.example.test/v1/text-to-speech/voice%2Fwith%20spaces%3Fx", post.call_args.args[0])
        self.assertEqual("custom-key", post.call_args.kwargs["headers"]["xi-api-key"])

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_older_model_request_sends_only_selected_context(self, post):
        post.return_value = _response({}, status_code=200)
        settings = self._settings()
        settings.update({
            "tts_context_mode": "before",
            "_semantic_context": {"before": "Previous.", "after": "Next."},
            "generation_prompt": "Unsupported instruction.",
            "elevenlabs_voice_settings": {"stability": 0.4, "similarity_boost": 0.8,
                                           "style": 0.2, "speed": 1.1,
                                           "use_speaker_boost": False},
        })
        tts_handler._request_elevenlabs_audio("Current.", settings)
        payload = post.call_args.kwargs["json"]
        self.assertEqual("Current.", payload["text"])
        self.assertEqual("Previous.", payload["previous_text"])
        self.assertNotIn("next_text", payload)
        self.assertNotIn("instructions", payload)
        self.assertEqual(settings["elevenlabs_voice_settings"], payload["voice_settings"])
        settings["tts_context_mode"] = "both"
        tts_handler._request_elevenlabs_audio("Current.", settings)
        self.assertEqual("Next.", post.call_args.kwargs["json"]["next_text"])
        settings["tts_context_mode"] = "off"
        tts_handler._request_elevenlabs_audio("Current.", settings)
        self.assertNotIn("previous_text", post.call_args.kwargs["json"])
        self.assertNotIn("next_text", post.call_args.kwargs["json"])

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_unknown_model_discards_unsupported_controls(self, post):
        post.return_value = _response({}, status_code=200)
        settings = self._settings()
        settings.update({"elevenlabs_model": "future_model", "generation_prompt": "Do not speak.",
                         "tts_context_mode": "both", "_semantic_context": {"before": "Earlier."}})
        tts_handler._request_elevenlabs_audio("Current.", settings)
        self.assertEqual({"text": "Current.", "model_id": "future_model"}, post.call_args.kwargs["json"])

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_empty_voice_settings_are_omitted(self, post):
        post.return_value = _response({}, status_code=200)
        settings = self._settings()
        settings["elevenlabs_voice_settings"] = {}
        tts_handler._request_elevenlabs_audio("Current.", settings)
        self.assertNotIn("voice_settings", post.call_args.kwargs["json"])

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_native_request_normalizes_language_for_supporting_model(self, post):
        post.return_value = _response({}, status_code=200)
        settings = self._settings()
        settings["xtts_model"] = "eleven_turbo_v2_5"

        for language, expected_code in (("en-US", "en"), ("zh-cn", "zh")):
            with self.subTest(language=language):
                settings["language"] = language
                tts_handler._request_elevenlabs_audio("Hello", settings)

                self.assertEqual(
                    {
                        "text": "Hello",
                        "model_id": "eleven_turbo_v2_5",
                        "language_code": expected_code,
                    },
                    post.call_args.kwargs["json"],
                )

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_native_request_omits_language_for_multilingual_v2(self, post):
        post.return_value = _response({}, status_code=200)
        settings = self._settings()
        settings["language"] = "pl-PL"

        tts_handler._request_elevenlabs_audio("Cześć", settings)

        self.assertEqual(
            {"text": "Cześć", "model_id": "eleven_multilingual_v2"},
            post.call_args.kwargs["json"],
        )

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_native_request_omits_automatic_and_invalid_language_values(self, post):
        post.return_value = _response({}, status_code=200)
        settings = self._settings()
        settings["xtts_model"] = "eleven_turbo_v2_5"

        for language in ("", "auto", "und", "English", "en-US-extra", None):
            settings["language"] = language
            tts_handler._request_elevenlabs_audio("Hello", settings)
            self.assertNotIn("language_code", post.call_args.kwargs["json"])

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_native_request_can_use_environment_key_without_persisting_it(self, post):
        post.return_value = _response({}, status_code=200)
        settings = {
            "service": tts_handler.ELEVENLABS_SERVICE,
            "xtts_model": "eleven_turbo_v2_5",
            "speaker": "voice-id",
            "provider_configs": [
                {
                    "id": "elevenlabs",
                    "api_key_env": "TEST_ELEVENLABS_KEY",
                }
            ],
        }
        with patch.dict("os.environ", {"TEST_ELEVENLABS_KEY": "env-secret"}):
            tts_handler._request_elevenlabs_audio("Hello", settings)

        self.assertEqual(
            "env-secret", post.call_args.kwargs["headers"]["xi-api-key"]
        )
        self.assertEqual(
            "ELEVENLABS_API_KEY", TTS_SERVICE_ENVS["elevenlabs"]
        )
        self.assertNotIn(
            "env-secret",
            str(redact_inline_secrets({"api_key": "env-secret", **settings})),
        )

    def test_native_request_requires_key_and_voice(self):
        no_key = {
            "service": tts_handler.ELEVENLABS_SERVICE,
            "speaker": "voice-id",
            "provider_configs": [{"id": "elevenlabs"}],
        }
        with self.assertRaisesRegex(ValueError, "API key"):
            tts_handler._request_elevenlabs_audio("Hello", no_key)

        no_voice = {
            "service": tts_handler.ELEVENLABS_SERVICE,
            "provider_configs": [{"id": "elevenlabs", "api_key": "key"}],
        }
        with self.assertRaisesRegex(ValueError, "voice"):
            tts_handler._request_elevenlabs_audio("Hello", no_voice)

    @patch("pandrator.logic.tts_handler.requests.post")
    def test_native_request_normalizes_transport_failures(self, post):
        import requests

        post.side_effect = requests.exceptions.Timeout()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            tts_handler._request_elevenlabs_audio("Hello", self._settings())


@pytest.mark.parametrize("value", [
    {"stability": True}, {"stability": float("nan")},
    {"style": float("inf")}, {"speed": 0.24}, {"speed": 4.01},
    {"similarity_boost": 1.01}, {"use_speaker_boost": 1},
    {"unknown": 0.5}, ["stability"],
])
def test_invalid_elevenlabs_voice_settings_fail_before_http(value):
    settings = ElevenLabsRequestTests()._settings()
    settings["elevenlabs_voice_settings"] = value
    with patch("pandrator.logic.tts_handler.requests.post") as post:
        with pytest.raises(ValueError, match="ElevenLabs"):
            tts_handler._request_elevenlabs_audio("Text.", settings)
        post.assert_not_called()


@pytest.mark.parametrize("value", [
    {"stability": 0.3},
    {"stability": 0.3, "similarity_boost": 0.5, "style": 0.5,
     "speed": 4.0, "use_speaker_boost": True},
    {"speed": 0.25},
])
def test_v3_accepts_valid_generic_voice_settings_with_qualified_report(value):
    settings = ElevenLabsRequestTests()._settings()
    settings["elevenlabs_model"] = "eleven_v3"
    settings["elevenlabs_voice_settings"] = value
    compiled = compile_performance("Text.", settings)
    assert compiled.request_options["voice_settings"] == value
    approximated = [
        item for item in compiled.report
        if item["control"] == "voice_settings" and item["status"] == "approximated"
    ]
    assert bool(approximated) is any(key != "stability" for key in value)
    with patch("pandrator.logic.tts_handler.requests.post") as post:
        post.return_value = _response({}, status_code=200)
        tts_handler._request_elevenlabs_audio("Text.", settings)
        assert post.call_args.kwargs["json"]["voice_settings"] == value


def test_blank_legacy_model_is_rejected_by_preview_and_runtime_before_http():
    settings = ElevenLabsRequestTests()._settings()
    settings["elevenlabs_model"] = "  "
    with pytest.raises(ValueError, match="Select an ElevenLabs model"):
        compile_performance("Text.", settings)
    with patch("pandrator.logic.tts_handler.requests.post") as post:
        with pytest.raises(ValueError, match="Select an ElevenLabs model"):
            tts_handler._request_elevenlabs_audio("Text.", settings)
        post.assert_not_called()


class ElevenLabsCatalogueTests(unittest.TestCase):
    @patch("pandrator.logic.tts_handler.requests.get")
    def test_strict_model_catalogue_surfaces_auth_failure_without_body(self, get):
        response = Mock(status_code=401)
        response.raise_for_status.side_effect = requests.HTTPError(
            "secret-looking body", response=response
        )
        get.return_value = response

        with self.assertRaisesRegex(
            tts_handler.ElevenLabsCatalogError, "API key was rejected"
        ) as raised:
            tts_handler.get_elevenlabs_model_catalog(api_key="secret", strict=True)

        self.assertEqual(401, raised.exception.status_code)
        self.assertNotIn("secret-looking body", str(raised.exception))

    @patch("pandrator.logic.tts_handler.requests.get")
    def test_strict_voice_catalogue_surfaces_transport_failure(self, get):
        get.side_effect = requests.ConnectionError("network details")

        with self.assertRaisesRegex(
            tts_handler.ElevenLabsCatalogError, "Could not reach ElevenLabs"
        ) as raised:
            tts_handler.get_elevenlabs_voice_catalog(api_key="secret", strict=True)

        self.assertEqual(0, raised.exception.status_code)
        self.assertNotIn("network details", str(raised.exception))

    @patch("pandrator.logic.tts_handler.requests.get")
    def test_models_parse_only_tts_models_and_authoritative_languages(self, get):
        get.return_value = _response(
            [
                {
                    "model_id": "eleven_multilingual_v2",
                    "name": "Multilingual v2",
                    "can_do_text_to_speech": True,
                    "languages": [{"language_id": "pl", "name": "Polish"}],
                },
                {"model_id": "voice_conversion_only", "can_do_text_to_speech": False},
            ]
        )

        models = tts_handler.get_elevenlabs_model_catalog(api_key="secret")

        self.assertEqual("eleven_multilingual_v2", models[0]["id"])
        self.assertEqual(
            [{"language_id": "pl", "name": "Polish"}], models[0]["languages"]
        )
        self.assertEqual(
            {"xi-api-key": "secret", "Accept": "application/json"},
            get.call_args.kwargs["headers"],
        )
        self.assertEqual("https://api.elevenlabs.io/v1/models", get.call_args.args[0])

    @patch("pandrator.logic.tts_handler.requests.get")
    def test_voices_parse_v2_schema_and_follow_pagination(self, get):
        get.side_effect = [
            _response(
                {
                    "voices": [{"voice_id": "voice-1", "name": "One"}],
                    "next_page_token": "next",
                }
            ),
            _response(
                {
                    "voices": [{"voice_id": "voice-2", "name": "Two"}],
                }
            ),
        ]

        voices = tts_handler.get_elevenlabs_voice_catalog(api_key="secret")

        self.assertEqual(["voice-1", "voice-2"], [item["voice_id"] for item in voices])
        self.assertEqual("next", get.call_args_list[1].kwargs["params"]["page_token"])
        self.assertEqual(
            "https://api.elevenlabs.io/v2/voices", get.call_args.args[0]
        )

    @patch("pandrator.logic.tts_handler.get_elevenlabs_voice_catalog")
    @patch("pandrator.logic.tts_handler.get_elevenlabs_model_catalog")
    def test_registry_adapter_projects_catalogues_for_selectors(self, get_models, get_voices):
        get_models.return_value = [
            {"id": "eleven_multilingual_v2", "languages": [{"language_id": "en"}]}
        ]
        get_voices.return_value = [{"voice_id": "voice-1", "name": "One"}]
        adapter = ElevenLabsAdapter("elevenlabs")

        catalog = adapter.enrich_catalog(
            {
                "id": "elevenlabs",
                "api_base": tts_handler.ELEVENLABS_API_BASE_URL,
                "default_model": "eleven_multilingual_v2",
            },
            api_key="secret",
        )

        self.assertEqual(["eleven_multilingual_v2"], catalog["models"])
        self.assertEqual(["voice-1"], catalog["voices"])
        self.assertEqual(["voice-1"], catalog["voice_catalogues"]["eleven_multilingual_v2"])
        self.assertEqual("voice-1", catalog["default_voice"])
        self.assertEqual([{"language_id": "en"}], catalog["model_catalog"][0]["languages"])
        registry = TtsProviderRegistry()
        self.assertIn("elevenlabs", registry.service_ids())

    @patch("pandrator.logic.tts_handler.get_elevenlabs_voice_catalog")
    @patch("pandrator.logic.tts_handler.get_elevenlabs_model_catalog")
    def test_adapter_projects_auth_failure_as_unavailable_catalogue_error(
        self, get_models, get_voices
    ):
        get_models.side_effect = tts_handler.ElevenLabsCatalogError("models", 403)
        adapter = ElevenLabsAdapter("elevenlabs")

        with self.assertRaisesRegex(TtsProviderError, "API key was rejected") as raised:
            adapter.enrich_catalog(
                {
                    "id": "elevenlabs",
                    "api_base": tts_handler.ELEVENLABS_API_BASE_URL,
                },
                api_key="invalid",
            )

        self.assertFalse(raised.exception.retryable)
        get_voices.assert_not_called()

    @patch("pandrator.logic.tts_handler.get_elevenlabs_voice_catalog")
    @patch("pandrator.logic.tts_handler.get_elevenlabs_model_catalog")
    def test_adapter_projects_transport_failure_as_retryable_catalogue_error(
        self, get_models, get_voices
    ):
        get_models.side_effect = tts_handler.ElevenLabsCatalogError("models")
        adapter = ElevenLabsAdapter("elevenlabs")

        with self.assertRaises(TtsProviderError) as raised:
            adapter.enrich_catalog(
                {
                    "id": "elevenlabs",
                    "api_base": tts_handler.ELEVENLABS_API_BASE_URL,
                },
                api_key="key",
            )

        self.assertTrue(raised.exception.retryable)
        get_voices.assert_not_called()

    def test_catalogue_refresh_marks_provider_unavailable_after_auth_failure(self):
        providers = Mock()
        providers.health.return_value = TtsHealth(online=True, available=True)
        providers.enrich_catalog.side_effect = TtsProviderError(
            "elevenlabs",
            "catalog",
            "ElevenLabs API key was rejected while listing models.",
            retryable=False,
        )
        catalogue = TtsCatalogueService(Mock(), Mock(), providers)
        catalogue._resolved_api_key = Mock(return_value="invalid")
        services = [
            {
                "id": "elevenlabs",
                "kind": "commercial",
                "api_base": tts_handler.ELEVENLABS_API_BASE_URL,
            }
        ]

        catalogue._refresh(services)

        self.assertFalse(services[0]["available"])
        self.assertEqual(
            "ElevenLabs API key was rejected while listing models.",
            services[0]["availability_reason"],
        )


class ElevenLabsProfileTests(unittest.TestCase):
    def test_native_profile_is_not_openai_compatible(self):
        profile = tts_provider_profiles.get_tts_provider_profile("elevenlabs")
        self.assertIsNotNone(profile)
        self.assertEqual("elevenlabs_native", profile["adapter"])
        self.assertEqual("xi-api-key", profile["auth_mode"])
        self.assertIn("not an OpenAI-compatible", profile["description"])
        self.assertTrue(profile["credential_required"])

    def test_native_profile_merges_into_first_class_service(self):
        profile = tts_provider_profiles.get_tts_provider_profile("elevenlabs")
        services = tts_handler.get_service_configs({"provider_configs": [profile]})
        service = next(item for item in services if item["id"] == "elevenlabs")

        self.assertFalse(service["is_custom"])
        self.assertEqual("elevenlabs_native", service["adapter"])
        self.assertEqual("elevenlabs-native", service["profile_id"])


if __name__ == "__main__":
    unittest.main()
