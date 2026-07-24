import json
import tempfile
import unittest
from pathlib import Path

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.dubbing.subtitle_finalization import compose_from_transcript_json
from pandrator.logic.dubbing.transcript_normalization import (
    NormalizedTranscript,
    TimedSegment,
    normalize_transcript,
    register_transcript_adapter,
    unregister_transcript_adapter,
)


class TranscriptNormalizationTests(unittest.TestCase):
    def test_crispasr_whisper_dtw_and_parakeet_share_one_adapter(self):
        for backend in ("whisper", "parakeet"):
            with self.subTest(backend=backend):
                transcript = normalize_transcript(
                    {
                        "crispasr": {"backend": backend, "language_detected": "en"},
                        "transcription": [
                            {
                                "offsets": {"from": 1000, "to": 1800},
                                "speaker": "(speaker 2)",
                                "text": "Hello there.",
                                "words": [
                                    {
                                        "text": "Hello",
                                        "offsets": {"from": 1000, "to": 1300},
                                        "probability": 0.0,
                                    },
                                    {"text": "there.", "t0": 132, "t1": 180},
                                ],
                            }
                        ],
                    }
                )

                self.assertEqual(transcript.source_format, "crispasr")
                self.assertEqual(transcript.language, "en")
                self.assertEqual([word.start_ms for word in transcript.words], [1000, 1320])
                self.assertEqual([word.speaker for word in transcript.words], ["speaker 2", "speaker 2"])
                self.assertEqual(transcript.words[0].confidence, 0.0)

    def test_whisperx_accepts_pyannote_speakers_or_no_diarization(self):
        diarized = normalize_transcript(
            {
                "language": "pl",
                "segments": [
                    {
                        "id": 0,
                        "start": 0.5,
                        "end": 1.5,
                        "text": "Dzień dobry.",
                        "speaker": "SPEAKER_00",
                        "words": [
                            {"word": "Dzień", "start": 0.5, "end": 0.9, "score": 0.91},
                            {
                                "word": "dobry.",
                                "start": 0.95,
                                "end": 1.5,
                                "speaker": "SPEAKER_00",
                            },
                        ],
                    }
                ],
            }
        )
        plain = normalize_transcript(
            {
                "segments": [{"start": 0.0, "end": 1.0, "text": "Hello."}],
                "word_segments": [{"word": "Hello.", "start": 0.0, "end": 1.0}],
            }
        )

        self.assertEqual(diarized.source_format, "whisperx")
        self.assertEqual(diarized.words[0].speaker, "SPEAKER_00")
        self.assertEqual(diarized.words[0].start_ms, 500)
        self.assertEqual(diarized.metadata["diarization"], "pyannote-or-supplied")
        self.assertEqual(len(plain.words), 1)
        self.assertEqual(plain.words[0].speaker, "")
        self.assertEqual(plain.metadata["diarization"], "none")

    def test_moss_ctc_words_are_grouped_without_losing_speakers(self):
        transcript = normalize_transcript(
            [
                {
                    "start": 1.2,
                    "end": 1.5,
                    "text": "Hello",
                    "speaker": "S01",
                    "moss_segment_id": "seg_1",
                },
                {
                    "start": 1.6,
                    "end": 2.0,
                    "text": "there.",
                    "speaker": "S01",
                    "moss_segment_id": "seg_1",
                },
                {
                    "start": 2.2,
                    "end": 2.7,
                    "text": "Welcome.",
                    "speaker": "S02",
                    "moss_segment_id": "seg_2",
                },
            ]
        )

        self.assertEqual(transcript.source_format, "moss-ctc-words")
        self.assertEqual([segment.identifier for segment in transcript.segments], ["seg_1", "seg_2"])
        self.assertEqual([segment.speaker for segment in transcript.segments], ["S01", "S02"])
        self.assertEqual([word.start_ms for word in transcript.words], [1200, 1600, 2200])

    def test_standalone_moss_segments_compose_speaker_safe_cues(self):
        payload = [
            {"id": "seg_1", "start": 0.2, "end": 1.0, "speaker": "S01", "text": "Hello."},
            {"id": "seg_2", "start": 1.1, "end": 2.0, "speaker": "S02", "text": "Welcome."},
        ]
        transcript = normalize_transcript(payload)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "moss.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            cues = parse_srt(compose_from_transcript_json(path))

        self.assertEqual([segment.speaker for segment in transcript.segments], ["S01", "S02"])
        self.assertEqual([cue.text for cue in cues], ["Hello.", "Welcome."])
        self.assertEqual([cue.speaker for cue in cues], ["", ""])

    def test_canonical_round_trip_and_custom_adapter_extension(self):
        original = NormalizedTranscript(
            segments=(TimedSegment("Hello.", 0, 500),),
            source_format="future-engine",
            language="en",
        )
        round_trip = normalize_transcript(original.to_dict())
        self.assertEqual(round_trip.segments, original.segments)

        register_transcript_adapter(
            "future",
            lambda payload: isinstance(payload, dict) and payload.get("future") is True,
            lambda _payload: original,
        )
        try:
            self.assertIs(normalize_transcript({"future": True}), original)
        finally:
            unregister_transcript_adapter("future")


if __name__ == "__main__":
    unittest.main()
