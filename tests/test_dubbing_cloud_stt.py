import io
import json
import struct
import tempfile
import unittest
import wave
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pandrator.logic.dubbing import cloud_stt, stt_backends, stt_provider_profiles


def _write_wav(path: Path, samples: list[int], *, sample_rate: int = 1000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _timed_payload(start_ms: int = 100) -> dict:
    return {
        "phrases": [
            {
                "offsetMilliseconds": start_ms,
                "durationMilliseconds": 100,
                "text": "word",
                "words": [
                    {
                        "word": "word",
                        "offsetMilliseconds": start_ms,
                        "durationMilliseconds": 100,
                    }
                ],
            }
        ]
    }


class CloudSTTTests(unittest.TestCase):
    def test_azure_endpoint_accepts_regional_origin_and_canonicalizes(self):
        self.assertEqual(
            cloud_stt.normalize_azure_speech_api_base(
                " HTTPS://EastUS.api.Cognitive.Microsoft.COM/ "
            ),
            "https://eastus.api.cognitive.microsoft.com",
        )

    def test_azure_endpoint_rejects_deceptive_regional_lookalikes(self):
        invalid_endpoints = (
            "https://nested.westus.api.cognitive.microsoft.com",
            "https://westus.api.cognitive.microsoft.com.evil.example",
            "https://westus.api.cognitiveservices.microsoft.com",
            "https://api.cognitive.microsoft.com",
        )
        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(
                cloud_stt.CloudSTTConfigurationError
            ):
                cloud_stt.normalize_azure_speech_api_base(endpoint)

    def test_endpoint_and_environment_overrides_cannot_redirect_a_process_secret(self):
        with self.assertRaisesRegex(
            cloud_stt.CloudSTTConfigurationError,
            "cognitiveservices[.]azure[.]com",
        ):
            cloud_stt.normalize_azure_speech_api_base("https://attacker.example")

        requests_seen = []

        def fake_request(url, **kwargs):
            requests_seen.append((url, kwargs))
            return SimpleNamespace(status_code=200, json=lambda: _timed_payload())

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ",
            {
                "AZURE_SPEECH_KEY": "expected-azure-key",
                "PROCESS_SECRET": "must-not-be-forwarded",
            },
            clear=False,
        ):
            audio = Path(directory) / "source.wav"
            _write_wav(audio, [0] * 1000)
            cloud_stt.transcribe(
                audio,
                session_dir=directory,
                output_name="source",
                settings={
                    "stt_engine": "azure_mai_transcribe_1_5",
                    "provider_configs": [
                        {
                            "id": "azure_mai_transcribe_1_5",
                            "api_base": "https://example.cognitiveservices.azure.com",
                            "api_key_env": "PROCESS_SECRET",
                        }
                    ],
                },
                request_func=fake_request,
            )

        self.assertEqual(len(requests_seen), 1)
        self.assertEqual(
            requests_seen[0][1]["headers"],
            {"Ocp-Apim-Subscription-Key": "expected-azure-key"},
        )

    def test_short_wav_uses_one_open_stream_request(self):
        requests_seen = []

        def fake_request(url, **kwargs):
            requests_seen.append((url, kwargs))
            stream = kwargs["files"]["audio"][1]
            self.assertFalse(isinstance(stream, bytes))
            self.assertEqual(stream.read(4), b"RIFF")
            return SimpleNamespace(status_code=200, json=lambda: _timed_payload())

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "short.wav"
            _write_wav(audio, [0] * 1000)
            cloud_stt.transcribe(
                audio,
                session_dir=directory,
                output_name="short",
                settings={
                    "stt_engine": "azure_mai_transcribe_1_5",
                    "stt_api_base": "https://example.cognitiveservices.azure.com",
                    "stt_api_key": "secret",
                },
                request_func=fake_request,
            )

        self.assertEqual(len(requests_seen), 1)

    def test_azure_profile_is_deep_copied_and_describes_remote_capabilities(self):
        profiles = stt_provider_profiles.list_stt_provider_profiles()
        self.assertEqual(
            [item["id"] for item in profiles],
            ["azure_mai_transcribe_2", "azure_mai_transcribe_1_5"],
        )
        mai_2 = profiles[0]
        legacy = profiles[1]
        self.assertEqual(mai_2["name"], "Azure Speech · MAI-Transcribe-2")
        self.assertEqual(mai_2["model"], "MAI-Transcribe-2")
        self.assertEqual(mai_2["engine"], "azure_mai_transcribe_2")
        self.assertEqual(mai_2["adapter"], "azure_speech_fast_transcription")
        self.assertTrue(mai_2["word_timestamps"])
        self.assertFalse(mai_2["diarization"])
        self.assertEqual(mai_2["pricing"]["amount_usd"], 0.10)
        self.assertEqual(mai_2["pricing"]["unit"], "audio_hour")
        self.assertTrue(mai_2["pricing"]["estimate_only"])
        self.assertEqual(mai_2["pricing"]["price_effective_until"], "2026-12-31")
        self.assertEqual(
            mai_2["pricing"]["source_url"],
            "https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/mai-transcribe-2-highest-quality-transcription-at-the-fastest-speed-and-lowest-c/4550972",
        )
        self.assertEqual(mai_2["upload_limit"], legacy["upload_limit"])
        mai_2["supported_locales"].append("mutated")
        self.assertNotIn(
            "mutated",
            stt_provider_profiles.list_stt_provider_profiles()[0]["supported_locales"],
        )

    def test_azure_definition_maps_language_hotwords_and_style(self):
        auto = cloud_stt.build_azure_definition(
            {
                "stt_language": "auto",
                "stt_hotwords": "Pandrator, CrispASR\nMAI",
                "stt_transcribe_style": "readability",
            }
        )
        self.assertNotIn("locales", auto)
        self.assertNotIn("transcribeStyle", auto["enhancedMode"])
        self.assertEqual(
            auto["enhancedMode"],
            {"enabled": True, "model": "mai-transcribe-1.5"},
        )
        self.assertEqual(
            auto["phraseList"], {"phrases": ["Pandrator", "CrispASR", "MAI"]}
        )

        explicit = cloud_stt.build_azure_definition(
            {
                "stt_language": "Polish",
                "stt_transcribe_style": "verbatim",
            }
        )
        self.assertEqual(explicit["locales"], ["pl"])
        self.assertEqual(explicit["enhancedMode"]["transcribeStyle"], "verbatim")

        mai_2 = cloud_stt.build_azure_definition(
            {
                "stt_engine": "azure_mai_transcribe_2",
                "stt_hotwords": "Pascal, IARF",
                "stt_transcribe_style": "readability",
            }
        )
        self.assertEqual(
            mai_2["enhancedMode"],
            {
                "enabled": True,
                "model": "MAI-Transcribe-2",
                "modelOptions": {
                    "timestamps": "word",
                    "transcribeStyle": "clean",
                },
            },
        )
        self.assertEqual(mai_2["phraseList"], {"phrases": ["Pascal", "IARF"]})
        self.assertEqual(
            cloud_stt.build_azure_definition(
                {
                    "stt_engine": "azure_mai_transcribe_2",
                    "stt_language": "sw",
                }
            )["locales"],
            ["sw"],
        )
        self.assertEqual(
            cloud_stt.build_azure_definition(
                {
                    "stt_engine": "azure_mai_transcribe_2",
                    "stt_transcribe_style": "verbatim",
                }
            )["enhancedMode"]["modelOptions"]["transcribeStyle"],
            "verbatim",
        )
        with self.assertRaisesRegex(
            cloud_stt.CloudSTTConfigurationError, "readability.*clean.*verbatim"
        ):
            cloud_stt.build_azure_definition(
                {
                    "stt_engine": "azure_mai_transcribe_2",
                    "stt_transcribe_style": "unsupported",
                }
            )

    def test_azure_request_and_response_conversion_write_canonical_files(self):
        payload = {
            "phrases": [
                {
                    "offsetMilliseconds": 0,
                    "durationMilliseconds": 900,
                    "text": "Hello friend.",
                    "words": [
                        {
                            "word": "Hello",
                            "offsetMilliseconds": 0,
                            "durationMilliseconds": 400,
                        },
                        {
                            "word": "friend.",
                            "offsetMilliseconds": 450,
                            "durationMilliseconds": 450,
                        },
                    ],
                }
            ],
            "locale": "en-US",
        }
        request = {}

        def fake_request(url, **kwargs):
            request.update(url=url, **kwargs)
            return SimpleNamespace(status_code=200, json=lambda: payload)

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "source.wav"
            _write_wav(audio, [0] * 1000)
            result = cloud_stt.transcribe(
                audio,
                session_dir=directory,
                output_name="source",
                settings={
                    "stt_engine": "azure_mai_transcribe_1_5",
                    "stt_api_base": "https://example.cognitiveservices.azure.com",
                    "stt_api_key": "secret",
                    "stt_language": "English",
                },
                request_func=fake_request,
            )

            canonical = json.loads(
                Path(result.word_timestamps_path).read_text(encoding="utf-8")
            )
            self.assertEqual(result.engine, "azure_mai_transcribe_1_5")
            self.assertEqual(result.compute_backend, "remote")
            self.assertEqual(canonical["schema"], "pandrator.transcript.v1")
            self.assertEqual(canonical["segments"][0]["words"][1]["start_ms"], 450)
            self.assertTrue(Path(result.srt_path).is_file())
            self.assertEqual(Path(result.srt_path).read_text(encoding="utf-8"), "")

        self.assertEqual(
            request["url"],
            "https://example.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
        )
        self.assertEqual(request["headers"], {"Ocp-Apim-Subscription-Key": "secret"})
        definition = json.loads(request["files"]["definition"][1])
        self.assertEqual(definition["locales"], ["en"])
        self.assertEqual(request["files"]["audio"][0], "source.wav")

    def test_materialized_provider_record_supplies_endpoint_and_key(self):
        payload = {
            "phrases": [
                {
                    "text": "Hello.",
                    "words": [
                        {
                            "text": "Hello.",
                            "offsetMilliseconds": 0,
                            "durationMilliseconds": 400,
                        }
                    ],
                }
            ]
        }
        request = {}

        def fake_request(url, **kwargs):
            request.update(url=url, **kwargs)
            return SimpleNamespace(status_code=200, json=lambda: payload)

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "source.wav"
            _write_wav(audio, [0] * 1000)
            cloud_stt.transcribe(
                audio,
                session_dir=directory,
                output_name="source",
                settings={
                    "stt_engine": "azure_mai_transcribe_1_5",
                    "provider_configs": [
                        {
                            "id": "azure_mai_transcribe_1_5",
                            "api_base": "https://configured.cognitiveservices.azure.com",
                            "api_key": "stored-secret",
                        }
                    ],
                },
                request_func=fake_request,
            )

        self.assertTrue(
            request["url"].startswith("https://configured.cognitiveservices.azure.com/")
        )
        self.assertEqual(
            request["headers"],
            {"Ocp-Apim-Subscription-Key": "stored-secret"},
        )

    def test_text_without_words_and_diarization_are_rejected_before_output(self):
        with self.assertRaisesRegex(cloud_stt.CloudSTTResponseError, "no timed words"):
            cloud_stt.parse_azure_response({"phrases": [{"text": "Missing timing"}]})
        with self.assertRaisesRegex(
            cloud_stt.CloudSTTConfigurationError, "does not support diarization"
        ):
            cloud_stt.build_azure_definition({"diarization_enabled": True})
        with self.assertRaisesRegex(
            cloud_stt.CloudSTTConfigurationError,
            "current Pandrator adapter has not implemented the documented diarization request contract yet",
        ):
            cloud_stt.build_azure_definition(
                {
                    "stt_engine": "azure_mai_transcribe_2",
                    "diarization_enabled": True,
                }
            )

    def test_parser_metadata_uses_selected_mai_2_engine_and_model(self):
        transcript = cloud_stt.parse_azure_response(
            _timed_payload(), engine="azure_mai_transcribe_2"
        )
        self.assertEqual(transcript.metadata["engine"], "azure_mai_transcribe_2")
        self.assertEqual(transcript.metadata["model"], "MAI-Transcribe-2")

    def test_mai_2_usage_estimate_uses_exact_pcm_duration_and_published_price(self):
        request = {}

        def fake_request(url, **kwargs):
            request.update(url=url, **kwargs)
            return SimpleNamespace(status_code=200, json=lambda: _timed_payload())

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "source.wav"
            _write_wav(audio, [0] * 1001)
            result = cloud_stt.transcribe(
                audio,
                session_dir=directory,
                output_name="source",
                settings={
                    "stt_engine": "azure_mai_transcribe_2",
                    "stt_api_base": "https://example.cognitiveservices.azure.com",
                    "stt_api_key": "secret",
                },
                request_func=fake_request,
            )
            canonical = json.loads(
                Path(result.word_timestamps_path).read_text(encoding="utf-8")
            )

        usage = canonical["metadata"]["usage"]
        self.assertEqual(usage["submitted_audio_seconds"], 1.001)
        self.assertEqual(usage["billing_increment_seconds"], 1)
        self.assertEqual(usage["billable_audio_seconds"], 2)
        self.assertAlmostEqual(usage["estimated_cost_usd"], 0.10 * 2 / 3600)
        self.assertEqual(usage["currency"], "USD")
        self.assertEqual(usage["cost_source"], "published_list_price_estimate")
        self.assertFalse(usage["usage_reported_by_provider"])
        self.assertEqual(usage["price_effective_until"], "2026-12-31")
        self.assertNotIn("phraseList", json.loads(request["files"]["definition"][1]))

    def test_missing_endpoint_and_credential_are_typed_configuration_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "source.wav"
            _write_wav(audio, [0] * 1000)
            with self.assertRaisesRegex(
                cloud_stt.CloudSTTConfigurationError, "resource endpoint"
            ):
                cloud_stt.transcribe(
                    audio,
                    session_dir=directory,
                    output_name="source",
                    settings={
                        "stt_engine": "azure_mai_transcribe_1_5",
                        "stt_api_key": "secret",
                    },
                    request_func=lambda *_args, **_kwargs: None,
                )

            with self.assertRaisesRegex(
                cloud_stt.CloudSTTConfigurationError, "AZURE_SPEECH_KEY"
            ):
                cloud_stt.transcribe(
                    audio,
                    session_dir=directory,
                    output_name="source",
                    settings={
                        "stt_engine": "azure_mai_transcribe_1_5",
                        "stt_api_base": "https://example.cognitiveservices.azure.com",
                    },
                    request_func=lambda *_args, **_kwargs: None,
                )

    def test_chunk_planner_prefers_quiet_boundary_over_noisy_audio(self):
        sample_rate = 100
        samples = [20000] * (30 * sample_rate)
        samples[8 * sample_rate : 10 * sample_rate] = [0] * (2 * sample_rate)
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "quiet.wav"
            _write_wav(audio, samples, sample_rate=sample_rate)
            chunks = cloud_stt.plan_cloud_stt_chunks(
                audio,
                {
                    "stt_cloud_max_chunk_seconds": 10,
                    "stt_cloud_min_chunk_seconds": 5,
                    "stt_cloud_boundary_search_window_seconds": 5,
                },
            )
        self.assertGreaterEqual(chunks[0].end_frame, 850)
        self.assertLessEqual(chunks[0].end_frame, 950)

    def test_chunk_planner_falls_back_to_lowest_energy_window(self):
        sample_rate = 100
        samples = [20000] * (30 * sample_rate)
        samples[8 * sample_rate : 9 * sample_rate] = [1000] * sample_rate
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "energy.wav"
            _write_wav(audio, samples, sample_rate=sample_rate)
            chunks = cloud_stt.plan_cloud_stt_chunks(
                audio,
                {
                    "stt_cloud_max_chunk_seconds": 10,
                    "stt_cloud_min_chunk_seconds": 5,
                    "stt_cloud_boundary_search_window_seconds": 5,
                },
            )
        self.assertGreaterEqual(chunks[0].end_frame, 825)
        self.assertLessEqual(chunks[0].end_frame, 925)

    def test_chunk_planner_covers_every_source_frame_once(self):
        sample_rate = 10
        source_frames = 31 * sample_rate
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "long.wav"
            _write_wav(audio, [10000] * source_frames, sample_rate=sample_rate)
            chunks = cloud_stt.plan_cloud_stt_chunks(
                audio,
                {
                    "stt_cloud_max_chunk_seconds": 10,
                    "stt_cloud_min_chunk_seconds": 5,
                    "stt_cloud_boundary_search_window_seconds": 5,
                },
            )
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0].start_frame, 0)
        self.assertEqual(chunks[-1].end_frame, source_frames)
        self.assertEqual(
            sum(chunk.frame_count for chunk in chunks),
            source_frames,
        )
        for previous, current in pairwise(chunks):
            self.assertEqual(previous.end_frame, current.start_frame)
            self.assertEqual(previous.end_ms, current.start_ms)

    def test_long_wav_submits_sequential_chunks_and_rebases_word_times(self):
        sample_rate = 10
        source_frames = 31 * sample_rate
        source_samples = [10000] * source_frames
        requests_seen = []

        def fake_request(url, **kwargs):
            stream = kwargs["files"]["audio"][1]
            raw = stream.read()
            requests_seen.append((kwargs["files"]["audio"][0], raw))
            return SimpleNamespace(status_code=200, json=lambda: _timed_payload())

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "long.wav"
            _write_wav(audio, source_samples, sample_rate=sample_rate)
            result = cloud_stt.transcribe(
                audio,
                session_dir=directory,
                output_name="long",
                settings={
                    "stt_engine": "azure_mai_transcribe_1_5",
                    "stt_api_base": "https://example.cognitiveservices.azure.com",
                    "stt_api_key": "secret",
                    "stt_cloud_max_chunk_seconds": 10,
                    "stt_cloud_min_chunk_seconds": 5,
                    "stt_cloud_boundary_search_window_seconds": 5,
                },
                request_func=fake_request,
            )
            canonical = json.loads(
                Path(result.word_timestamps_path).read_text(encoding="utf-8")
            )
            boundaries = canonical["metadata"]["chunk_boundaries"]
            words = [
                word for segment in canonical["segments"] for word in segment["words"]
            ]

        self.assertGreater(len(requests_seen), 1)
        self.assertEqual(
            [name for name, _raw in requests_seen],
            [f"chunk-{index:04d}.wav" for index in range(1, len(requests_seen) + 1)],
        )
        self.assertEqual(len(words), len(requests_seen))
        self.assertEqual(
            [word["start_ms"] for word in words],
            [boundary["start_ms"] + 100 for boundary in boundaries],
        )
        self.assertEqual(
            [word["start_ms"] for word in words],
            sorted(word["start_ms"] for word in words),
        )
        chunk_frames = []
        for _name, raw in requests_seen:
            with wave.open(io.BytesIO(raw), "rb") as chunk:
                self.assertEqual(chunk.getframerate(), sample_rate)
                chunk_frames.append(chunk.readframes(chunk.getnframes()))
        self.assertEqual(
            b"".join(chunk_frames),
            struct.pack(f"<{len(source_samples)}h", *source_samples),
        )

    def test_chunk_maximum_is_below_azure_limit(self):
        with self.assertRaisesRegex(
            cloud_stt.CloudSTTConfigurationError,
            "below Azure Speech's 2-hour limit",
        ):
            cloud_stt._chunk_config({"stt_cloud_max_chunk_seconds": 7200})

    def test_backend_normalization_preserves_azure_without_local_install_claim(self):
        self.assertEqual(
            stt_backends.normalize_stt_backend("azure_mai_transcribe_1_5"),
            "azure_mai_transcribe_1_5",
        )
        statuses = stt_backends.detect_stt_backend_statuses(
            environ={"CRISPASR_EXECUTABLE": ""},
            path_exists=lambda _path: False,
            run_func=lambda *_args, **_kwargs: None,
        )
        status = statuses["azure_mai_transcribe_1_5"]
        self.assertTrue(status.remote)
        self.assertEqual(status.installation_status, "not_applicable")
        self.assertEqual(status.word_timing, "native")


if __name__ == "__main__":
    unittest.main()
