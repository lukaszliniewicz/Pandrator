"""Provider refusals stop retrying, and every retry and failure says why."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

import pytest

from pandrator.logic import tts_handler
from pandrator.logic.google_tts_audio import TtsNoAudioError, decode_google_wav_response
from pandrator.logic.tts_retry import (
    NO_AUDIO_MAX_ATTEMPTS,
    retry_reason_suffix,
    tts_failure_reason,
)
from pandrator.web.models import GenerationRun, GenerationSegment
from pandrator.web.tts_providers import TtsCapabilities
from tests import test_generation_missing_only as missing_only
from tests.test_google_tts_audio import ENDPOINT, response
from tests.test_tts_retry import audio_response, provider_error, silero_settings


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"candidates": [{"finishReason": "SAFETY"}]}, "SAFETY"),
        ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "PROHIBITED_CONTENT"),
    ],
)
def test_google_refusal_names_its_reason(payload, reason):
    with pytest.raises(TtsNoAudioError) as raised:
        decode_google_wav_response(
            response(payload), ENDPOINT, "Vertex AI", interactions=False
        )
    assert raised.value.refusal_reason == reason
    assert str(raised.value) == f"Vertex AI declined to synthesise this text ({reason})."
    assert tts_failure_reason(raised.value) == "content_refused"


def test_google_response_without_audio_or_reason_is_no_audio():
    with pytest.raises(TtsNoAudioError) as raised:
        decode_google_wav_response(
            response({"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}),
            ENDPOINT,
            "Vertex AI",
            interactions=False,
        )
    assert raised.value.refusal_reason == ""
    assert tts_failure_reason(raised.value) == "no_audio"


def _synthesize(error, *, retry_callback, max_attempts=10):
    post = Mock(return_value=audio_response())
    wait = Mock(return_value=True)
    with (
        patch.object(tts_handler.requests, "post", post),
        patch.object(tts_handler, "wait_for_retry", wait),
        patch.object(tts_handler, "_decode_audio_response", side_effect=error),
        pytest.raises(tts_handler.TtsGenerationError) as raised,
    ):
        tts_handler.text_to_audio(
            "A line the provider will not read",
            silero_settings(),
            max_attempts=max_attempts,
            silero_base_url="https://silero.invalid",
            retry_callback=retry_callback,
        )
    return post, raised.value


def test_stated_refusal_is_not_retried():
    retry_callback = Mock()
    post, error = _synthesize(
        TtsNoAudioError("declined", refusal_reason="SAFETY"),
        retry_callback=retry_callback,
    )

    assert post.call_count == 1
    retry_callback.assert_not_called()
    assert (error.reason, error.retryable) == ("content_refused", False)


def test_empty_audio_is_retried_a_few_times_with_its_reason():
    reasons = []

    def retry(attempt, total, delay, reason=""):
        reasons.append(reason)

    post, error = _synthesize(TtsNoAudioError("empty"), retry_callback=retry)

    assert post.call_count == NO_AUDIO_MAX_ATTEMPTS
    assert reasons == ["no_audio"] * (NO_AUDIO_MAX_ATTEMPTS - 1)
    assert (error.reason, error.retryable) == ("no_audio", False)
    assert f"after {NO_AUDIO_MAX_ATTEMPTS} attempt(s)" in str(error)


def test_three_argument_retry_callbacks_still_work():
    calls = []

    def retry(attempt, total, delay):
        calls.append((attempt, total))

    _post, error = _synthesize(TtsNoAudioError("empty"), retry_callback=retry)

    assert calls == [(2, 10), (3, 10)]
    assert error.reason == "no_audio"


def test_retry_reasons_have_short_labels():
    rate_limited = provider_error({})
    rate_limited.response.status_code = 429  # type: ignore[attr-defined]

    assert tts_failure_reason(rate_limited) == "rate_limited"
    assert retry_reason_suffix("rate_limited") == ": rate limited"
    assert retry_reason_suffix("") == ""


class RefusedSegmentRunTests(unittest.TestCase):
    """A declined segment fails alone; the run finishes partial."""

    # Borrow the generation fixture without collecting its tests again.
    setUp = missing_only.GenerationMissingOnlyTests.setUp
    _override = staticmethod(missing_only.GenerationMissingOnlyTests._override)
    _create_case = missing_only.GenerationMissingOnlyTests._create_case
    _job_payload = missing_only.GenerationMissingOnlyTests._job_payload

    def _run(self, texts, synthesize):
        session_id, _revision, segment_ids = self._create_case(texts=texts)
        started = self.generation.start(session_id, run_override=self._override())
        payload = self._job_payload(started["job_id"])
        progress_details = []
        with (
            patch.object(self.handlers.tts_providers, "synthesize", side_effect=synthesize),
            patch.object(
                self.handlers.tts_providers,
                "synthesis_capabilities",
                return_value=TtsCapabilities(batch_synthesis=False, streaming_batch=False),
            ),
        ):
            finished = self.handlers.run_generation(
                {
                    "generation_run_id": started["id"],
                    "segment_ids": payload["segment_ids"],
                    "operation": payload["operation"],
                },
                lambda _value, detail=None: progress_details.append(detail),
                threading.Event(),
            )
        return started["id"], segment_ids, finished, progress_details

    def test_refused_segment_is_skipped_and_reported(self):
        from pydub import AudioSegment

        calls = []

        def synthesize(text, _settings, **_kwargs):
            calls.append(text)
            if text == "Bravo":
                raise tts_handler.TtsGenerationError(
                    "Vertex AI declined to synthesise this text (SAFETY).",
                    retryable=False,
                    reason="content_refused",
                )
            return AudioSegment.silent(duration=20)

        run_id, segment_ids, finished, details = self._run(
            ["Alpha", "Bravo", "Charlie"], synthesize
        )

        self.assertEqual(["Alpha", "Bravo", "Charlie"], calls)
        self.assertEqual("partial", finished["status"])
        self.assertEqual(1, finished["remaining"])
        self.assertEqual(
            [(segment_ids[1], "content_refused")],
            [(item["segment_id"], item["reason"]) for item in finished["failed_segments"]],
        )
        self.assertIn("SAFETY", finished["failed_segments"][0]["message"])
        self.assertIn(
            "Skipped segment 2 of 3: the provider declined the text", details
        )
        with self.database.session() as session:
            self.assertEqual("partial", session.get(GenerationRun, run_id).status)
            statuses = [
                session.get(GenerationSegment, segment_id).status
                for segment_id in segment_ids
            ]
        self.assertEqual(["completed", "failed", "completed"], statuses)

    def test_run_fails_when_several_segments_in_a_row_return_no_audio(self):
        def synthesize(_text, _settings, **_kwargs):
            raise tts_handler.TtsGenerationError(
                "Vertex AI returned no decodable WAV audio payload.",
                retryable=False,
                reason="no_audio",
            )

        with self.assertRaises(tts_handler.TtsGenerationError):
            self._run(["One", "Two", "Three", "Four"], synthesize)
