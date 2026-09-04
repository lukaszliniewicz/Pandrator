import inspect
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.domain_blueprints import DOMAIN_ORDER, route_domain
from pandrator.web.job_registry import JobHandlerRegistry, JobPayloadContract
from pandrator.web.tts_providers import (
    TtsBatchItem,
    TtsCapabilities,
    TtsHealth,
    TtsProviderAdapter,
    TtsProviderConfigurationError,
    TtsProviderRegistry,
    TtsRetryPolicy,
    _audio_cpp_static_model_catalog,
)


class BackendArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.app = create_app(
            data_root=cls.temporary.name,
            testing=True,
            bootstrap_tokens=BootstrapTokenStore(),
        )

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["pandrator"]["database"].dispose()
        cls.temporary.cleanup()

    def test_application_factory_is_composition_only(self):
        source = inspect.getsource(create_app)
        self.assertLess(len(source.splitlines()), 100)
        self.assertNotIn("@app.", source)
        self.assertIn("ApplicationServices.build", source)
        self.assertIn("register_routes", source)

    def test_route_contract_is_partitioned_without_losing_rules(self):
        rules = list(self.app.url_map.iter_rules())
        self.assertEqual(200, len(rules))
        self.assertEqual(
            193,
            sum(rule.rule.startswith("/api/") for rule in rules),
        )
        self.assertEqual(set(DOMAIN_ORDER), set(self.app.blueprints))
        for rule in rules:
            if rule.endpoint == "static":
                continue
            expected_domain = route_domain(rule.rule)
            self.assertEqual(
                expected_domain,
                rule.endpoint.split(".", 1)[0],
                rule.rule,
            )

    def test_runtime_routes_cover_every_openapi_operation(self):
        runtime_operations = {
            (re.sub(r"<[^>]+>", "{}", rule.rule), method.lower())
            for rule in self.app.url_map.iter_rules()
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        document = self.app.test_client().get("/api/v1/openapi.json").get_json()
        for path, operations in document["paths"].items():
            for method in operations:
                normalized_path = re.sub(r"{[^}]+}", "{}", path)
                self.assertIn(
                    (normalized_path, method.lower()),
                    runtime_operations,
                )

    def test_extension_mapping_uses_the_composed_service_instances(self):
        extension = self.app.extensions["pandrator"]
        services = extension["services"]
        self.assertIs(services.database, extension["database"])
        self.assertIs(services.workflow_handlers, extension["workflow_handlers"])
        self.assertIs(services.tts_providers, extension["tts_providers"])
        self.assertIs(
            services.tts_providers,
            services.workflow_handlers.tts_providers,
        )

    def test_workflow_job_registry_has_domain_ownership_and_late_binding(self):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        registry = handlers.handler_registry
        self.assertEqual(29, len(registry))
        self.assertEqual(
            {
                "delivery",
                "generation",
                "source",
                "text",
                "voice",
                "workflow",
            },
            {item.domain for item in registry.registrations()},
        )

        original = handlers.translate
        calls = []

        def replacement(payload, _progress, _cancel_event):
            calls.append(payload)
            return {"replacement": True}

        try:
            handlers.translate = replacement
            result = registry["dubbing.translate"](
                {
                    "session_id": "example",
                    "source_artifact_id": "source",
                },
                lambda *_: None,
                threading.Event(),
            )
        finally:
            handlers.translate = original
        self.assertEqual({"replacement": True}, result)
        self.assertEqual(
            [
                {
                    "session_id": "example",
                    "source_artifact_id": "source",
                }
            ],
            calls,
        )

    def test_job_registry_rejects_duplicate_or_unowned_handlers(self):
        registry = JobHandlerRegistry()

        def handler(_payload, _progress, _cancel_event):
            return {}

        registry.register("example", handler, domain="test")
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register("example", handler, domain="other")
        with self.assertRaisesRegex(ValueError, "needs a domain"):
            JobHandlerRegistry().register("example", handler, domain="")
        validated = JobHandlerRegistry()
        validated.register(
            "validated",
            handler,
            domain="test",
            payload_contract=JobPayloadContract(("session_id",)),
        )
        with self.assertRaisesRegex(ValueError, "session_id"):
            validated["validated"]({}, lambda *_: None, threading.Event())

    def test_tts_registry_exposes_and_dispatches_the_provider_protocol(self):
        class RecordingAdapter:
            service_id = "xtts"

            def __init__(self):
                self.calls = []

            def capabilities(self, service):
                return TtsCapabilities(dynamic_catalog=True)

            def health(self, service):
                return TtsHealth(True, True)

            def enrich_catalog(self, service, *, api_key=""):
                return {"models": ["recorded"]}

            def synthesize(self, text, settings, **options):
                self.calls.append((text, settings, options))
                return "audio"

            def upload_voice(
                self,
                wav_file_path,
                *,
                base_url,
                service,
                prompt_text=None,
                mode=None,
                voice_id=None,
                api_key="",
            ):
                return "voice-id"

            def delete_voice(
                self,
                voice_id,
                *,
                base_url,
                service,
                api_key="",
            ):
                return True

        registry = TtsProviderRegistry()
        adapter = RecordingAdapter()
        self.assertIsInstance(adapter, TtsProviderAdapter)
        registry.replace(adapter)
        result = registry.synthesize(
            "Hello",
            {"service": "XTTS"},
            max_attempts=2,
        )
        self.assertEqual("audio", result)
        self.assertEqual("Hello", adapter.calls[0][0])
        self.assertEqual(2, adapter.calls[0][2]["max_attempts"])
        policy = TtsRetryPolicy.from_settings(
            {"max_attempts": 100, "retry_max_delay_seconds": 0}
        )
        self.assertEqual(20, policy.max_attempts)
        self.assertEqual(90.0, policy.maximum_delay_seconds)

        def invalid_synthesis(_text, _settings, **_options):
            raise ValueError("invalid voice")

        adapter.synthesize = invalid_synthesis
        with self.assertRaises(TtsProviderConfigurationError) as raised:
            registry.synthesize("Hello", {"service": "XTTS"})
        self.assertFalse(raised.exception.retryable)

    def test_audio_cpp_adapter_is_selected_and_reuses_one_batch_session(self):
        registry = TtsProviderRegistry()
        base_settings = {
            "service": "Custom",
            "openai_audio_endpoint": "audio-cpp-experimental",
            "model": "fish-audio-s2-pro",
            "provider_configs": [
                {
                    "id": "audio-cpp-experimental",
                    "name": "audio.cpp",
                    "provider": "openai",
                    "api_base": "http://127.0.0.1:8080",
                    "adapter": "audio_cpp",
                    "speech_path": "/v1/audio/speech",
                }
            ],
        }

        self.assertEqual("audio_cpp", registry.service_id_for_settings(base_settings))
        self.assertEqual(
            "xtts",
            registry.service_id_for_settings(
                {**base_settings, "preview_service_id": "xtts"}
            ),
        )
        capabilities = registry.synthesis_capabilities(base_settings)
        self.assertTrue(capabilities.batch_synthesis)
        self.assertTrue(capabilities.streaming_batch)
        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            side_effect=["one", "two"],
        ) as synthesize:
            results = list(
                registry.synthesize_batch(
                    [
                        TtsBatchItem("1", "First", dict(base_settings)),
                        TtsBatchItem("2", "Second", dict(base_settings)),
                    ],
                    batch_size=10,
                )
            )

        self.assertEqual(["one", "two"], [item.audio for item in results])
        first_session = synthesize.call_args_list[0].kwargs["request_session"]
        second_session = synthesize.call_args_list[1].kwargs["request_session"]
        self.assertIs(first_session, second_session)

        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            side_effect=["three", "four"],
        ) as synthesize_again:
            registry.synthesize("Third", dict(base_settings))
            registry.synthesize("Fourth", dict(base_settings))
        self.assertIs(
            first_session,
            synthesize_again.call_args_list[0].kwargs["request_session"],
        )
        self.assertIs(
            first_session,
            synthesize_again.call_args_list[1].kwargs["request_session"],
        )

        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            return_value=None,
        ):
            failed = list(
                registry.synthesize_batch(
                    [TtsBatchItem("failed", "No audio", dict(base_settings))],
                    batch_size=1,
                )
            )
        self.assertIsNone(failed[0].audio)
        self.assertIsNotNone(failed[0].error)
        self.assertTrue(failed[0].error.retryable)

    def test_audio_cpp_catalogue_uses_configured_models_and_model_voices(self):
        registry = TtsProviderRegistry()
        service = {
            "id": "audio-cpp-experimental",
            "adapter": "audio_cpp",
            "api_base": "http://127.0.0.1:8088",
            "models_path": "/v1/models",
            "voices_path": "/v1/audio/voices",
            "default_model": "fish",
            "default_voice": "sofia",
            "voices": ["manual"],
        }
        with (
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_model_catalog",
                return_value=[
                    {"id": "fish", "family": "fish_audio", "task": "tts"},
                    {"id": "breeze", "family": "breeze_tts", "task": "clon"},
                    {"id": "omnivoice", "family": "omnivoice", "task": "clon"},
                ],
            ),
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_voice_catalog",
                side_effect=[["narrator"], ["narrator", "reader"]],
            ),
        ):
            catalogue = registry.enrich_catalog(service)

        self.assertEqual(["fish", "omnivoice"], catalogue["models"])
        self.assertEqual(
            {
                "fish": ["sofia", "manual", "narrator"],
                "omnivoice": ["manual", "narrator", "reader"],
            },
            catalogue["voice_catalogues"],
        )
        self.assertEqual(["sofia", "manual", "narrator", "reader"], catalogue["voices"])
        self.assertEqual("fish", catalogue["default_model"])

    def test_audio_cpp_live_catalogue_excludes_models_not_configured_in_server(self):
        registry = TtsProviderRegistry()
        service = {
            "id": "audio_cpp",
            "adapter": "audio_cpp",
            "api_base": "http://127.0.0.1:8080",
            "default_model": "not-installed",
            "model_catalog": [
                {"id": "installed", "family": "fish_audio"},
                {"id": "not-installed", "family": "qwen3_tts"},
            ],
        }
        with (
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_model_catalog",
                return_value=[{"id": "installed", "task": "tts"}],
            ),
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_voice_catalog",
                return_value=[],
            ),
        ):
            catalogue = registry.enrich_catalog(service)

        self.assertEqual(["installed"], catalogue["models"])
        self.assertEqual("installed", catalogue["default_model"])

    def test_audio_cpp_live_qwen_catalogue_keeps_static_segment_recommendation(self):
        registry = TtsProviderRegistry()
        service = {
            "id": "audio_cpp",
            "adapter": "audio_cpp",
            "api_base": "http://127.0.0.1:8060",
            "model_catalog": [{"id": "qwen3_tts_1_7b_base_q8_0"}],
        }
        with (
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_model_catalog",
                return_value=[
                    {
                        "id": "qwen3_tts_1_7b_base_q8_0",
                        "task": "tts",
                    }
                ],
            ),
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_voice_catalog",
                return_value=[],
            ),
        ):
            catalogue = registry.enrich_catalog(service)

        self.assertEqual(
            300,
            catalogue["model_catalog"][0]["recommended_chunk_characters"],
        )

    def test_audio_cpp_static_catalogue_refreshes_metadata_without_network(self):
        catalogue = _audio_cpp_static_model_catalog(
            {
                "models": ["qwen3_tts_1_7b_base_q8_0"],
                "model_catalog": [{"id": "qwen3_tts_1_7b_base_q8_0"}],
            }
        )

        self.assertEqual(300, catalogue[0]["recommended_chunk_characters"])


if __name__ == "__main__":
    unittest.main()
