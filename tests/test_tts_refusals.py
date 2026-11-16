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
from pandrator.web.tts_provider_contracts import TtsBatchResult, TtsProviderError
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


def test_google_empty_audio_data_is_no_audio_in_both_decoders():
    empty_wav = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/wav", "data": ""}}
    ]}}]}
    with pytest.raises(TtsNoAudioError) as raised:
        decode_google_wav_response(response(empty_wav), ENDPOINT, "Vertex AI", interactions=False)
    assert tts_failure_reason(raised.value) == "no_audio"

    empty_pcm = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/L16;rate=24000", "data": ""}}
    ]}}]}
    with pytest.raises(TtsNoAudioError) as raised:
        tts_handler._google_tts_pcm_response(response(empty_pcm), ENDPOINT, "Gemini")
    assert tts_failure_reason(raised.value) == "no_audio"

    malformed = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/wav", "data": 7}}
    ]}}]}
    with pytest.raises(RuntimeError):
        decode_google_wav_response(response(malformed), ENDPOINT, "Vertex AI", interactions=False)


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
        finished, progress_details = self._execute(started, synthesize)
        return started["id"], segment_ids, finished, progress_details

    def _execute(self, started, synthesize, synthesize_batch=None):
        payload = self._job_payload(started["job_id"])
        progress_details = []
        capabilities = (
            TtsCapabilities(
                batch_synthesis=True, streaming_batch=True, default_batch_size=2, max_batch_size=2
            )
            if synthesize_batch is not None
            else TtsCapabilities(batch_synthesis=False, streaming_batch=False)
        )
        with (
            patch.object(self.handlers.tts_providers, "synthesize", side_effect=synthesize),
            patch.object(
                self.handlers.tts_providers, "synthesize_batch", side_effect=synthesize_batch
            ),
            patch.object(
                self.handlers.tts_providers, "synthesis_capabilities", return_value=capabilities
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
        return finished, progress_details

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

    def test_declined_regeneration_keeps_the_earlier_take(self):
        from pydub import AudioSegment

        def succeed(_text, _settings, **_kwargs):
            return AudioSegment.silent(duration=20)

        def decline(_text, _settings, **_kwargs):
            raise tts_handler.TtsGenerationError(
                "Gemini declined to synthesise this text (SAFETY).",
                retryable=False,
                reason="content_refused",
            )

        run_id, segment_ids, first, _details = self._run(["Alpha", "Bravo", "Charlie"], succeed)
        self.assertEqual("completed", first["status"])
        with self.database.session() as session:
            session_id = session.get(GenerationRun, run_id).session_id
        regenerated = self.generation.start(
            session_id,
            run_override=self._override(),
            segment_ids=segment_ids[1:2],
            generation_run_id=run_id,
            operation="regenerate",
        )
        finished, _details = self._execute(regenerated, decline)

        self.assertEqual(
            [(segment_ids[1], "content_refused", True)],
            [
                (item["segment_id"], item["reason"], item["kept_previous_take"])
                for item in finished["failed_segments"]
            ],
        )
        self.assertEqual("partial", finished["status"])
        with self.database.session() as session:
            self.assertEqual("completed", session.get(GenerationSegment, segment_ids[1]).status)
            self.assertEqual("partial", session.get(GenerationRun, regenerated["id"]).status)

    def test_retryable_grouped_batch_error_is_retried_alone_but_never_skipped(self):
        from pydub import AudioSegment

        batched = []

        def batch(items, **_kwargs):
            for item in items:
                batched.append(item.id)
                yield TtsBatchResult(
                    id=item.id,
                    error=TtsProviderError("gemini", "synthesize", "busy", retryable=True),
                )

        def recover(_text, _settings, **_kwargs):
            return AudioSegment.silent(duration=20)

        session_id, _revision, _ids = self._create_case(texts=["One", "Two"])
        started = self.generation.start(session_id, run_override=self._override(tts_batch_size=2))
        finished, _details = self._execute(started, recover, batch)
        self.assertEqual(("completed", 2), (finished["status"], finished["generated"]))
        self.assertEqual(2, len(batched))

        def still_busy(_text, _settings, **_kwargs):
            raise tts_handler.TtsGenerationError(
                "budget expired", retryable=True, reason="no_audio"
            )

        session_id, _revision, _ids = self._create_case(texts=["Three", "Four"])
        started = self.generation.start(session_id, run_override=self._override(tts_batch_size=2))
        with self.assertRaises(tts_handler.TtsGenerationError):
            self._execute(started, still_busy, batch)
        with self.database.session() as session:
            self.assertEqual("failed", session.get(GenerationRun, started["id"]).status)

    def test_lost_ownership_while_skipping_writes_nothing(self):
        from pandrator.web import workflow_generation_execution as execution
        from pandrator.web.access.contracts import AccessDenied

        revoked = threading.Event()
        original = execution.require_generation_execution

        def require(*args, **kwargs):
            if revoked.is_set():
                raise AccessDenied()
            return original(*args, **kwargs)

        def batch(items, **_kwargs):
            refusal = TtsProviderError("gemini", "synthesize", "declined", retryable=False)
            refusal.__cause__ = TtsNoAudioError("declined", refusal_reason="SAFETY")
            for item in items:
                revoked.set()  # another worker takes over before the skip is saved
                yield TtsBatchResult(id=item.id, error=refusal)

        def unused(_text, _settings, **_kwargs):
            raise AssertionError("a refused grouped segment is not retried alone")

        # The grouped path skips inside the segment's try block, so a denial
        # there reaches the generic failure handler.
        session_id, _revision, segment_ids = self._create_case(texts=["One", "Two"])
        started = self.generation.start(
            session_id, run_override=self._override(tts_batch_size=2)
        )
        with (
            patch.object(execution, "require_generation_execution", side_effect=require),
            self.assertRaises(AccessDenied),
        ):
            self._execute(started, unused, batch)
        with self.database.session() as session:
            self.assertNotEqual("failed", session.get(GenerationRun, started["id"]).status)
            self.assertNotEqual("failed", session.get(GenerationSegment, segment_ids[0]).status)

    def test_a_refusal_breaks_a_run_of_empty_responses(self):
        sequence = iter(["no_audio", "content_refused", "no_audio", "no_audio"])

        def synthesize(_text, _settings, **_kwargs):
            raise tts_handler.TtsGenerationError(
                "declined or empty", retryable=False, reason=next(sequence)
            )

        _run_id, _segment_ids, finished, _details = self._run(
            ["One", "Two", "Three", "Four"], synthesize
        )

        self.assertEqual("partial", finished["status"])
        self.assertEqual(4, len(finished["failed_segments"]))

    def test_retryable_empty_audio_still_fails_the_run(self):
        def synthesize(_text, _settings, **_kwargs):
            raise tts_handler.TtsGenerationError(
                "budget expired", retryable=True, reason="no_audio"
            )

        with self.assertRaises(tts_handler.TtsGenerationError):
            self._run(["One", "Two"], synthesize)

    def test_run_fails_when_several_segments_in_a_row_return_no_audio(self):
        def synthesize(_text, _settings, **_kwargs):
            raise tts_handler.TtsGenerationError(
                "Vertex AI returned no decodable WAV audio payload.",
                retryable=False,
                reason="no_audio",
            )

        with self.assertRaises(tts_handler.TtsGenerationError):
            self._run(["One", "Two", "Three", "Four"], synthesize)


def test_exhausted_retry_budget_is_not_mistaken_for_a_refusal():
    from pandrator.logic.tts_retry import TtsRetryBudgetExceeded

    exhausted = TtsRetryBudgetExceeded("The TTS retry budget expired.")
    exhausted.__cause__ = TtsNoAudioError("empty")

    assert tts_failure_reason(exhausted) == "timeout"


def test_batch_boundary_keeps_the_failure_reason():
    from pandrator.web.tts_providers import _synthesize_serial_batch

    adapter = Mock(service_id="openai")
    adapter.synthesize.side_effect = tts_handler.TtsGenerationError(
        "declined", retryable=False, reason="content_refused"
    )
    item = Mock(id="one", text="Line", settings={})

    [result] = list(_synthesize_serial_batch(adapter, [item]))

    assert result.error.retryable is False
    assert tts_failure_reason(result.error) == "content_refused"
