import threading
import time
import unittest
from collections.abc import Generator
from unittest.mock import Mock, patch

from pandrator.logic import tts_handler
from pandrator.web.tts_providers import (
    AudioCppAdapter,
    TtsBatchItem,
    TtsCapabilities,
    TtsProviderError,
    TtsProviderRegistry,
)
from pandrator.web.workflow_handlers import WorkflowHandlers


class ParallelTtsGenerationTests(unittest.TestCase):
    @staticmethod
    def _items(count=6, *, service="openai"):
        return [
            TtsBatchItem(str(index), f"text-{index}", {"service": service})
            for index in range(count)
        ]

    def test_parallel_batches_bound_overlap_and_preserve_input_order(self):
        registry = TtsProviderRegistry()
        active = 0
        maximum = 0
        lock = threading.Lock()

        def synthesize(text, _settings, **_options):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return text

        with patch.object(registry, "synthesize", side_effect=synthesize):
            results = list(registry.synthesize_batch(self._items(), batch_size=3))

        self.assertEqual(
            [str(index) for index in range(6)], [item.id for item in results]
        )
        self.assertEqual(
            [f"text-{index}" for index in range(6)], [item.audio for item in results]
        )
        self.assertEqual(3, maximum)

    def test_parallel_batches_keep_order_when_completions_are_out_of_order(self):
        registry = TtsProviderRegistry()
        delays = {"text-0": 0.03, "text-1": 0.0, "text-2": 0.02, "text-3": 0.0}

        def synthesize(text, _settings, **_options):
            time.sleep(delays[text])
            return text

        with patch.object(registry, "synthesize", side_effect=synthesize):
            results = list(registry.synthesize_batch(self._items(4), batch_size=2))

        self.assertEqual(["0", "1", "2", "3"], [item.id for item in results])

    def test_parallel_preserves_execution_context_at_native_http_boundary(self):
        import requests

        from pandrator.logic.model_acquisition import (
            model_acquisition_scope,
            model_inference_uses_prepared_endpoint,
        )
        from pandrator.logic.native_speech_http import NativeSpeechPostOptions, post_native_speech
        from pandrator.logic.tts_io_policy import (
            tts_environment_credentials_allowed,
            tts_host_adc_allowed,
            tts_io_scope,
        )
        from pandrator.web.accounts.execution_actor import (
            HostActor,
            current_execution_actor,
            execution_actor_scope,
        )
        from pandrator.web.job_execution import JobLease, current_job_lease

        registry = TtsProviderRegistry()
        actor = HostActor("pandrator-cli")
        lease: JobLease = {"job_id": "synthetic-parallel-job", "owner_id": "worker", "lease_generation": 1}
        barrier = threading.Barrier(2)
        calls = []

        def proof():
            self.assertEqual(actor, current_execution_actor.get())
            self.assertEqual(lease, current_job_lease.get())
            self.assertFalse(tts_environment_credentials_allowed())
            self.assertFalse(tts_host_adc_allowed())
            self.assertTrue(model_inference_uses_prepared_endpoint())

        def post(url, **options):
            proof()
            self.assertEqual("https://parallel.invalid/speech", url)
            self.assertIs(options["allow_redirects"], False)
            barrier.wait(timeout=3)
            calls.append(options["json"]["input"])
            response = requests.Response()
            response.status_code = 200
            return response

        def synthesize(text, _settings, **_options):
            def options() -> NativeSpeechPostOptions:
                return {"headers": {}, "timeout": 3, "json": {"input": text}}

            response = post_native_speech(
                "https://parallel.invalid/speech", request_label="Parallel fixture",
                request_options=options,
            )
            self.assertEqual(200, response.status_code)
            return text

        token = current_job_lease.set(lease)
        try:
            with (
                execution_actor_scope(actor),
                model_acquisition_scope(None, inference_authorizer=lambda: False),
                tts_io_scope(proof, allow_environment_credentials=False, allow_host_adc=False),
                patch.object(registry, "synthesize", side_effect=synthesize),
                patch.object(requests, "post", side_effect=post),
            ):
                results = list(registry.synthesize_batch(self._items(4), batch_size=2))
        finally:
            current_job_lease.reset(token)
        self.assertEqual([f"text-{i}" for i in range(4)], [item.audio for item in results])
        self.assertCountEqual([f"text-{i}" for i in range(4)], calls)
        self.assertIsNone(current_execution_actor.get())

    def test_parallel_denies_revoked_io_before_native_send(self):
        import requests

        from pandrator.logic.native_speech_http import NativeSpeechPostOptions, post_native_speech
        from pandrator.logic.tts_io_policy import tts_io_scope

        registry = TtsProviderRegistry()

        def denied():
            raise PermissionError("Synthetic authority was revoked")

        def synthesize(text, _settings, **_options):
            def options() -> NativeSpeechPostOptions:
                return {"headers": {}, "timeout": 3, "json": {"input": text}}

            post_native_speech(
                "https://parallel.invalid/speech", request_label="Parallel denial",
                request_options=options,
            )
            return text

        with (
            tts_io_scope(denied, allow_environment_credentials=False, allow_host_adc=False),
            patch.object(registry, "synthesize", side_effect=synthesize),
            patch.object(requests, "post", return_value=requests.Response()) as send,
        ):
            results = list(registry.synthesize_batch(self._items(2), batch_size=2))
        send.assert_not_called()
        self.assertTrue(all(item.audio is None and item.error is not None for item in results))

    def test_parallel_batches_project_one_item_failure_without_losing_other_results(
        self,
    ):
        registry = TtsProviderRegistry()

        def synthesize(text, _settings, **_options):
            if text == "text-1":
                raise ValueError("bad voice")
            return text

        with patch.object(registry, "synthesize", side_effect=synthesize):
            results = list(registry.synthesize_batch(self._items(3), batch_size=3))

        self.assertEqual(["0", "1", "2"], [item.id for item in results])
        self.assertEqual("text-0", results[0].audio)
        self.assertIsNone(results[1].audio)
        self.assertIsInstance(results[1].error, TtsProviderError)
        assert results[1].error is not None
        self.assertEqual("synthesize", results[1].error.operation)
        self.assertEqual("text-2", results[2].audio)

    def test_cancellation_prevents_submission_of_a_later_wave(self):
        registry = TtsProviderRegistry()
        cancel_event = threading.Event()
        calls = []
        lock = threading.Lock()

        def synthesize(text, _settings, **_options):
            with lock:
                calls.append(text)
            if text == "text-1":
                cancel_event.set()
            return text

        with patch.object(registry, "synthesize", side_effect=synthesize):
            results = list(
                registry.synthesize_batch(
                    self._items(4),
                    batch_size=2,
                    cancel_event=cancel_event,
                )
            )

        self.assertEqual({"text-0", "text-1"}, set(calls))
        self.assertEqual({"0", "1"}, {item.id for item in results})

    def test_closing_after_first_result_does_not_start_a_future_wave(self):
        registry = TtsProviderRegistry()
        calls = []

        def synthesize(text, _settings, **_options):
            calls.append(text)
            return text

        with patch.object(registry, "synthesize", side_effect=synthesize):
            batch = registry.synthesize_batch(self._items(4), batch_size=2)
            self.assertEqual("0", next(batch).id)
            assert isinstance(batch, Generator)
            batch.close()

        self.assertEqual({"text-0", "text-1"}, set(calls))

    def test_local_and_streaming_adapters_do_not_gain_parallel_capability(self):
        registry = TtsProviderRegistry()

        for service in ("audio_cpp", "xtts", "kobold_qwen"):
            with self.subTest(service=service):
                self.assertFalse(
                    registry.synthesis_capabilities(
                        {"service": service}
                    ).parallel_synthesis
                )
        audio_cpp = registry.synthesis_capabilities({"service": "audio_cpp"})
        self.assertFalse(audio_cpp.batch_synthesis)
        self.assertFalse(audio_cpp.streaming_batch)
        self.assertEqual(1, audio_cpp.default_batch_size)
        self.assertEqual(1, audio_cpp.max_batch_size)
        self.assertTrue(
            registry.synthesis_capabilities({"service": "openai"}).parallel_synthesis
        )
        self.assertTrue(
            registry.synthesis_capabilities(
                {"service": "elevenlabs"}
            ).parallel_synthesis
        )
        self.assertIsInstance(
            registry.synthesis_capabilities({"service": "openai"}),
            TtsCapabilities,
        )

    def test_kobold_qwen_true_batch_capability_remains_supported(self):
        registry = TtsProviderRegistry()
        advertised = {
            "supported": True,
            "streaming": True,
            "protocol": "ndjson-v1",
            "default_batch_size": 10,
            "max_batch_size": 32,
        }

        with patch.object(
            tts_handler,
            "get_kobold_qwen_batch_capabilities",
            return_value=advertised,
        ):
            capabilities = registry.synthesis_capabilities(
                {"service": "kobold_qwen"}
            )

        self.assertTrue(capabilities.batch_synthesis)
        self.assertTrue(capabilities.streaming_batch)
        self.assertEqual(10, capabilities.default_batch_size)
        self.assertEqual(32, capabilities.max_batch_size)

    def test_audio_cpp_batch_compatibility_path_sends_one_serial_request_per_item(
        self,
    ):
        adapter = AudioCppAdapter("audio_cpp")

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

        class FakeRequestSession:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse()

        session = FakeRequestSession()
        settings = {
            "service": "audio_cpp",
            "audio_cpp_base_url": "http://audio.cpp.test:8060",
            "model": "qwen3_tts_1_7b_base_q8_0",
            "language": "en",
            "speaker": "reader",
            "audio_cpp_reference_text": "Reference transcript.",
            "audio_cpp_voice_ref": {"id": "reference-1", "data": "encoded"},
        }
        items = [
            TtsBatchItem(str(index), f"text-{index}", settings)
            for index in range(3)
        ]

        with (
            patch.object(adapter, "_session_for", return_value=session),
            patch.object(tts_handler, "_decode_audio_response", return_value=b"wav"),
        ):
            results = list(adapter.synthesize_batch(items, batch_size=3))

        self.assertEqual(["0", "1", "2"], [item.id for item in results])
        self.assertTrue(all(item.audio == b"wav" for item in results))
        self.assertEqual(3, len(session.calls))
        for index, (url, request) in enumerate(session.calls):
            with self.subTest(index=index):
                self.assertEqual(
                    "http://audio.cpp.test:8060/v1/audio/speech",
                    url,
                )
                self.assertEqual(f"text-{index}", request["json"]["input"])
                self.assertNotIn("items", request["json"])
                self.assertNotIn("stream", request["json"])
                self.assertEqual(
                    "Reference transcript.", request["json"]["reference_text"]
                )
                self.assertEqual(
                    {"id": "reference-1", "data": "encoded"},
                    request["json"]["voice_ref"],
                )

    def test_custom_transport_controls_parallel_eligibility(self):
        registry = TtsProviderRegistry()
        for adapter, expected in (
            ("generic_json", False),
            ("azure_speech", True),
            ("openai_compatible", True),
        ):
            provider = {
                "id": "configured",
                "api_base": "https://example.openai.azure.com",
                "adapter": adapter,
            }
            for selection in ("Custom", "configured"):
                with self.subTest(adapter=adapter, selection=selection):
                    settings = {
                        "service": selection,
                        "openai_audio_endpoint": "configured",
                        "provider_configs": [provider],
                    }
                    self.assertEqual(
                        expected,
                        registry.synthesis_capabilities(settings).parallel_synthesis,
                    )
                    self.assertEqual(
                        expected, registry.capabilities(provider).parallel_synthesis
                    )

    def test_local_openai_compatible_profiles_stay_serial(self):
        registry = TtsProviderRegistry()
        for base in (
            "http://127.0.0.1:5000",
            "http://192.168.1.20:8880",
            "https://my-local-engine.example",
        ):
            provider = {
                "id": "piper-kamil-krawiec",
                "adapter": "openai_compatible",
                "api_base": base,
            }
            settings = {
                "service": "Custom",
                "openai_audio_endpoint": provider["id"],
                "provider_configs": [provider],
                "tts_concurrent_requests": 8,
            }
            self.assertFalse(registry.capabilities(provider).parallel_synthesis)
            self.assertFalse(
                registry.synthesis_capabilities(settings).parallel_synthesis
            )

    def test_workflow_negotiates_parallel_requests_independently_of_batch_size(self):
        handlers = object.__new__(WorkflowHandlers)
        handlers.tts_providers = Mock()
        handlers.tts_providers.synthesis_capabilities.return_value = TtsCapabilities(
            parallel_synthesis=True,
        )

        self.assertEqual(
            8,
            handlers._negotiated_tts_batch_size(
                {
                    "tts_batch_size": 1,
                    "tts_concurrent_requests": 99,
                },
                {},
            ),
        )
        self.assertEqual(
            1,
            handlers._negotiated_tts_batch_size(
                {
                    "tts_batch_size": 32,
                    "tts_concurrent_requests": "invalid",
                },
                {},
            ),
        )


if __name__ == "__main__":
    unittest.main()
