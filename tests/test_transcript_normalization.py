import json
import tempfile
import unicodedata
import unittest
from pathlib import Path

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.dubbing.subtitle_finalization import compose_from_transcript_json
from pandrator.logic.dubbing.transcript_normalization import (
    NormalizedTranscript,
    TimedSegment,
    normalize_transcript,
    register_transcript_adapter,
    split_glued_digits,
    unregister_transcript_adapter,
)


class TranscriptNormalizationTests(unittest.TestCase):
    def test_crispasr_preserves_only_whitelisted_provenance_with_detached_copies(self):
        payload = {
            "crispasr": {"backend": "whisper", "language": "pl"},
            "transcription": [],
            "metadata": {
                "stt_routing": {"resolved_language": "pl", "requirements": ["runtime"]},
                "vocal_isolation": {"revision": "pin", "sha256": "hash"},
                "requested_language": "auto", "unrelated": "private", "engine": "override",
            },
        }
        normalized = normalize_transcript(payload)
        self.assertEqual("whisper", normalized.metadata["engine"])
        self.assertNotIn("unrelated", normalized.metadata)
        self.assertEqual("auto", normalized.metadata["requested_language"])
        payload["metadata"]["stt_routing"]["requirements"].append("changed")
        payload["metadata"]["vocal_isolation"]["revision"] = "changed"
        self.assertEqual(["runtime"], normalized.metadata["stt_routing"]["requirements"])
        self.assertEqual("pin", normalized.metadata["vocal_isolation"]["revision"])
        canonical = normalize_transcript(normalized.to_dict())
        self.assertEqual(normalized.metadata, canonical.metadata)

    def test_qwen_zero_duration_words_survive_native_and_canonical_metadata(self):
        examples = (
            ("en", "Hello for world.", [("Hello", 100, 400), ("for", 410, 410), ("world.", 450, 950)]),
            ("zh", "你好，世界！", [("你好", 100, 400), ("世", 410, 410), ("界！", 450, 950)]),
        )

        def lexical(text):
            return "".join(
                char for char in unicodedata.normalize("NFKC", text).casefold()
                if char.isalnum()
            )

        for language, text, entries in examples:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temp_dir:
                payload = {
                    "crispasr": {"backend": "qwen3", "language": language},
                    "transcription": [{
                        "text": text,
                        "offsets": {"from": 100, "to": 1000},
                        "words": [
                            {"text": surface, "offsets": {"from": start, "to": end}}
                            for surface, start, end in entries
                        ],
                    }],
                }
                native = normalize_transcript(payload)
                self.assertEqual(len(native.words), 3)
                self.assertEqual(native.words[1].start_ms, native.words[1].end_ms)
                canonical = normalize_transcript(native.to_dict())
                self.assertEqual(len(canonical.words), 3)
                self.assertEqual(canonical.words[1].start_ms, canonical.words[1].end_ms)
                self.assertEqual(lexical("".join(word.text for word in canonical.words)), lexical(text))
                path = Path(temp_dir) / "native.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                cues = parse_srt(compose_from_transcript_json(path))
                self.assertEqual(lexical("".join(cue.text for cue in cues)), lexical(text))

    def test_non_qwen_zero_duration_word_remains_invalid(self):
        payload = {
            "crispasr": {"backend": "whisper", "language": "en"},
            "transcription": [{
                "text": "Hello world", "offsets": {"from": 100, "to": 1000},
                "words": [
                    {"text": "Hello", "offsets": {"from": 100, "to": 100}},
                    {"text": "world", "offsets": {"from": 200, "to": 600}},
                ],
            }],
        }
        self.assertEqual([word.text for word in normalize_transcript(payload).words], ["world"])

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

    def test_glued_digits_split_only_where_a_word_meets_a_number(self):
        cases = {
            "for16 years": "for 16 years",
            "more than27 years": "more than 27 years",
            "in1900": "in 1900",
            "the19th century": "the 19th century",
            "in1,900": "in 1,900",
            "at10:30": "at 10:30",
            "(in1900),": "(in 1900),",
            "für1990er": "für1990er",
            "MP3": "MP3",
            "mp3": "mp3",
            "COVID19": "COVID19",
            "iPhone15": "iPhone15",
            "abc123def": "abc123def",
            "16": "16",
            "16th": "16th",
            "know,70 years": "know, 70 years",
            "No,10": "No, 10",
            "about 1,900": "about 1,900",
            "": "",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, split_glued_digits(source))

    def test_crispasr_parakeet_splits_glued_digits_without_retiming_words(self):
        payload = {
            "crispasr": {"backend": "parakeet", "language_detected": "en"},
            "transcription": [
                {
                    "offsets": {"from": 0, "to": 4000},
                    "text": "We worked for16 years and in1900 it was the19th century.",
                    "words": [
                        {"text": "We", "offsets": {"from": 0, "to": 200}},
                        {"text": "worked", "offsets": {"from": 220, "to": 500}},
                        {"text": "for16", "offsets": {"from": 520, "to": 900}},
                        {"text": "years", "offsets": {"from": 920, "to": 1200}},
                        {"text": "and", "offsets": {"from": 1220, "to": 1400}},
                        {"text": "in1900", "offsets": {"from": 1420, "to": 1800}},
                        {"text": "it", "offsets": {"from": 1820, "to": 1900}},
                        {"text": "was", "offsets": {"from": 1920, "to": 2100}},
                        {"text": "the19th", "offsets": {"from": 2120, "to": 2500}},
                        {"text": "century.", "offsets": {"from": 2520, "to": 3000}},
                    ],
                }
            ],
        }

        transcript = normalize_transcript(payload)
        segment = transcript.segments[0]

        self.assertEqual(
            [
                "We", "worked", "for 16", "years", "and",
                "in 1900", "it", "was", "the 19th", "century.",
            ],
            [word.text for word in segment.words],
        )
        self.assertEqual(
            [(word.start_ms, word.end_ms) for word in segment.words],
            [
                (0, 200), (220, 500), (520, 900), (920, 1200), (1220, 1400),
                (1420, 1800), (1820, 1900), (1920, 2100), (2120, 2500),
                (2520, 3000),
            ],
        )
        self.assertEqual(
            "We worked for 16 years and in 1900 it was the 19th century.",
            segment.text,
        )

    def test_crispasr_output_other_than_parakeet_is_not_split(self):
        # Unlabelled output keeps its text too: only Parakeet is known to glue.
        for header in (
            {"backend": "whisper", "language_detected": "en"},
            {"backend": "qwen3", "language_detected": "en"},
            {"backend": "moss-diarize", "language_detected": "en"},
            {"language_detected": "en"},
        ):
            with self.subTest(header=header):
                transcript = normalize_transcript(
                    {
                        "crispasr": header,
                        "transcription": [
                            {
                                "offsets": {"from": 0, "to": 1800},
                                "text": "We worked for16 years.",
                                "words": [
                                    {"text": "We", "offsets": {"from": 0, "to": 200}},
                                    {"text": "worked", "offsets": {"from": 220, "to": 500}},
                                    {"text": "for16", "offsets": {"from": 520, "to": 900}},
                                    {"text": "years.", "offsets": {"from": 920, "to": 1200}},
                                ],
                            }
                        ],
                    }
                )

                self.assertEqual(
                    ["We", "worked", "for16", "years."],
                    [word.text for word in transcript.segments[0].words],
                )
                self.assertEqual(
                    "We worked for16 years.", transcript.segments[0].text
                )

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


class SparseTranscriptContractTests(unittest.TestCase):
    def test_absent_headers_and_word_arrays_preserve_canonical_round_trip(self):
        for header in (None, {}, {"backend": "qwen3", "language_detected": "ja"}):
            for words in (None, [], [{"text": "日本語です。", "start": 0.1, "end": 0.9}]):
                with self.subTest(header=header, words=words):
                    transcript = normalize_transcript({
                        "crispasr": header,
                        "transcription": [{"text": "日本語です。", "start": 0.1,
                                           "end": 0.9, "words": words}],
                    })
                    self.assertEqual(1, len(transcript.segments))
                    segment = transcript.segments[0]
                    self.assertEqual(("日本語です。", 100, 900),
                                     (segment.text, segment.start_ms, segment.end_ms))
                    self.assertEqual(1 if words else 0, len(segment.words))
                    self.assertEqual(header or {}, transcript.metadata["header"])
                    self.assertEqual("ja" if header else "", transcript.language)
                    canonical = transcript.to_dict()
                    self.assertEqual(canonical, normalize_transcript(canonical).to_dict())


if __name__ == "__main__":
    unittest.main()
