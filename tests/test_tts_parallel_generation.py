import threading
import time
import unittest
from unittest.mock import Mock, patch

from pandrator.web.tts_providers import (
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
