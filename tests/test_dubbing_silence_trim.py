"""Trim the silence around a speech block before it is fitted to its timing.

A short cue such as "Ja." can arrive as a take that is mostly padding: the
provider adds silence before and after the spoken words, and fitting the whole
take pushes later speech late. These tests cover the edge measurement, the
guards, both assembly backends and the fit that a trimmed block now makes.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

from pandrator.logic.dubbing import audio_sync

SAMPLE_RATE = 24_000
TAKE_LEAD_MS = 300
TAKE_TONE_MS = 270
TAKE_TAIL_MS = 400
TAKE_MS = TAKE_LEAD_MS + TAKE_TONE_MS + TAKE_TAIL_MS


def _silence_bytes(milliseconds: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * milliseconds // 1000)


def _tone_bytes(milliseconds: int) -> bytes:
    return Sine(440, sample_rate=SAMPLE_RATE).to_audio_segment(
        duration=milliseconds
    ).raw_data


def _padded_take(path: Path) -> None:
    take = (
        AudioSegment.silent(duration=TAKE_LEAD_MS, frame_rate=SAMPLE_RATE)
        + Sine(440, sample_rate=SAMPLE_RATE).to_audio_segment(duration=TAKE_TONE_MS)
        + AudioSegment.silent(duration=TAKE_TAIL_MS, frame_rate=SAMPLE_RATE)
    )
    take.export(path, format="wav").close()


class SpeechEdgesTests(unittest.TestCase):
    def test_silence_runs_are_measured_within_one_frame(self):
        raw = (
            _silence_bytes(TAKE_LEAD_MS)
            + _tone_bytes(TAKE_TONE_MS)
            + _silence_bytes(TAKE_TAIL_MS)
        )

        lead_ms, tail_ms = audio_sync.speech_edges_ms(
            raw, sample_rate=SAMPLE_RATE, channels=1, sample_width=2
        )

        frame_ms = audio_sync.SILENCE_TRIM_FRAME_MS
        self.assertLessEqual(abs(lead_ms - TAKE_LEAD_MS), frame_ms)
        self.assertLessEqual(abs(tail_ms - TAKE_TAIL_MS), frame_ms)
        self.assertEqual(0, lead_ms % frame_ms)
        self.assertEqual(0, tail_ms % frame_ms)

    def test_audio_that_is_silent_throughout_is_never_trimmed(self):
        self.assertEqual(
            (0, 0),
            audio_sync.speech_edges_ms(
                _silence_bytes(500), sample_rate=SAMPLE_RATE, channels=1, sample_width=2
            ),
        )

    def test_audio_without_silence_returns_no_edges(self):
        self.assertEqual(
            (0, 0),
            audio_sync.speech_edges_ms(
                _tone_bytes(500), sample_rate=SAMPLE_RATE, channels=1, sample_width=2
            ),
        )

    def test_trim_keeps_the_configured_margins(self):
        raw = (
            _silence_bytes(TAKE_LEAD_MS)
            + _tone_bytes(TAKE_TONE_MS)
            + _silence_bytes(TAKE_TAIL_MS)
        )

        trimmed, lead_cut_ms, tail_cut_ms = audio_sync._trim_pcm_bytes(
            raw, sample_rate=SAMPLE_RATE, channels=1, sample_width=2
        )

        self.assertEqual(TAKE_LEAD_MS - audio_sync.SILENCE_TRIM_LEAD_GUARD_MS, lead_cut_ms)
        self.assertEqual(TAKE_TAIL_MS - audio_sync.SILENCE_TRIM_TAIL_GUARD_MS, tail_cut_ms)
        self.assertEqual(
            len(raw) - (lead_cut_ms + tail_cut_ms) * 2 * SAMPLE_RATE // 1000,
            len(trimmed),
        )


class SpeechEdgesSampleRateTests(unittest.TestCase):
    """A frame is a whole sample count, so its length in ms is not exact.

    ``frame_ms`` only describes the requested frame: at 22,050 Hz a 10 ms frame
    is 220 samples, which is 9.98 ms. Reporting ``frames * frame_ms`` slightly
    overstated the measured silence, and trimming could cut into speech.
    """

    def _edges(self, sample_rate: int, silent_samples: int) -> tuple[int, int]:
        tone = Sine(440, sample_rate=sample_rate).to_audio_segment(
            duration=270
        ).raw_data
        raw = (
            b"\x00\x00" * silent_samples
            + tone
            + b"\x00\x00" * (sample_rate // 2)
        )
        return audio_sync.speech_edges_ms(
            raw, sample_rate=sample_rate, channels=1, sample_width=2
        )

    def test_22050_hz_edges_never_exceed_the_measured_silence(self):
        sample_rate = 22_050
        # Thirty whole 220-sample frames plus a few samples: rounding the frame
        # to 10 ms would claim three samples more silence than the block has.
        silent_samples = 6_610

        lead_ms, tail_ms = self._edges(sample_rate, silent_samples)

        self.assertEqual(299, lead_ms)
        self.assertEqual(498, tail_ms)
        self.assertLessEqual(lead_ms * sample_rate // 1000, silent_samples)
        self.assertLessEqual(tail_ms * sample_rate // 1000, sample_rate // 2)

    def test_24000_hz_edges_are_unchanged(self):
        sample_rate = 24_000

        lead_ms, tail_ms = self._edges(sample_rate, sample_rate * 3 // 10)

        self.assertEqual(300, lead_ms)
        self.assertEqual(500, tail_ms)


class TrimSettingTests(unittest.TestCase):
    def test_missing_setting_trims_and_explicit_false_does_not(self):
        self.assertTrue(audio_sync.trim_silence_enabled_from_settings({}))
        self.assertTrue(
            audio_sync.trim_silence_enabled_from_settings(
                {"synchronization_trim_silence": None}
            )
        )
        self.assertTrue(
            audio_sync.trim_silence_enabled_from_settings(
                {"synchronization_trim_silence": True}
            )
        )
        self.assertFalse(
            audio_sync.trim_silence_enabled_from_settings(
                {"synchronization_trim_silence": False}
            )
        )
        self.assertFalse(
            audio_sync.trim_silence_enabled_from_settings(
                {"synchronization_trim_silence": "off"}
            )
        )


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg qualification requires ffmpeg")
class SilenceTrimAssemblyTests(unittest.TestCase):
    def _align(self, temp_dir: str, backend: str, **options):
        source = Path(temp_dir, f"padded-take-{backend}.wav")
        _padded_take(source)
        diagnostics: dict = {}
        output = audio_sync.align_audio_blocks(
            [audio_sync.AudioAlignmentBlock("1", "Ja.", 0, 700, [source], [1])],
            temp_dir,
            delay_start_ms=0,
            speed_up_percent=115,
            sentence_gap_ms=0,
            min_block_gap_ms=0,
            output_path=Path(temp_dir, f"aligned-{backend}-{len(options)}.wav"),
            diagnostics=diagnostics,
            backend=backend,
            allow_slowdown=False,
            **options,
        )
        return output, diagnostics["blocks"][0]

    def test_both_backends_trim_a_padded_block_to_the_guards(self):
        trimmed_by_backend = {}
        for backend in ("pydub", "streaming"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temp_dir:
                _output, detail = self._align(
                    temp_dir, backend, trim_silence=True
                )

                self.assertEqual(
                    TAKE_LEAD_MS - audio_sync.SILENCE_TRIM_LEAD_GUARD_MS,
                    detail["trimmed_lead_ms"],
                )
                self.assertEqual(
                    TAKE_TAIL_MS - audio_sync.SILENCE_TRIM_TAIL_GUARD_MS,
                    detail["trimmed_tail_ms"],
                )
                self.assertEqual(
                    TAKE_MS - detail["trimmed_lead_ms"] - detail["trimmed_tail_ms"],
                    detail["original_audio_ms"],
                )
                self.assertEqual(
                    detail["original_audio_ms"], detail["processed_audio_ms"]
                )
                trimmed_by_backend[backend] = detail["original_audio_ms"]

        self.assertLessEqual(
            abs(trimmed_by_backend["pydub"] - trimmed_by_backend["streaming"]),
            audio_sync.SILENCE_TRIM_FRAME_MS,
        )

    def test_padded_block_fitting_its_cue_no_longer_needs_speed_up(self):
        for backend in ("pydub", "streaming"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temp_dir:
                _untrimmed_output, untrimmed = self._align(
                    temp_dir, backend, trim_silence=False
                )
                _trimmed_output, trimmed = self._align(
                    temp_dir, backend, trim_silence=True
                )

                # The whole take overruns the 700 ms cue, so it is sped up.
                self.assertEqual(TAKE_MS, untrimmed["original_audio_ms"])
                self.assertGreater(untrimmed["requested_speed_factor"], 1.0)
                self.assertTrue(untrimmed["speed_adjusted"])
                # Without the padding the same speech fits as generated.
                self.assertEqual(1.0, trimmed["requested_speed_factor"])
                self.assertFalse(trimmed["speed_adjusted"])
                self.assertLess(
                    trimmed["processed_audio_ms"], untrimmed["processed_audio_ms"]
                )
                self.assertLessEqual(trimmed["processed_audio_ms"], 700)

    def test_disabled_trim_matches_the_unchanged_default(self):
        for backend in ("pydub", "streaming"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temp_dir:
                default_output, default_detail = self._align(temp_dir, backend)
                disabled_output, disabled_detail = self._align(
                    temp_dir, backend, trim_silence=False
                )

                self.assertEqual(0, default_detail["trimmed_lead_ms"])
                self.assertEqual(0, default_detail["trimmed_tail_ms"])
                self.assertEqual(TAKE_MS, default_detail["original_audio_ms"])
                self.assertEqual(default_detail, disabled_detail)
                self.assertEqual(
                    AudioSegment.from_wav(default_output).raw_data,
                    AudioSegment.from_wav(disabled_output).raw_data,
                )

    def test_summary_reports_the_trimmed_total(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "padded-take-summary.wav")
            _padded_take(source)
            diagnostics: dict = {}
            audio_sync.align_audio_blocks(
                [
                    audio_sync.AudioAlignmentBlock("1", "Ja.", 0, 700, [source], [1]),
                    audio_sync.AudioAlignmentBlock("2", "Dag.", 2000, 2600, [source], [2]),
                ],
                temp_dir,
                delay_start_ms=0,
                speed_up_percent=115,
                sentence_gap_ms=0,
                min_block_gap_ms=0,
                output_path=Path(temp_dir, "aligned-summary.wav"),
                diagnostics=diagnostics,
                backend="pydub",
                allow_slowdown=False,
                trim_silence=True,
            )

            per_block = sum(
                detail["trimmed_lead_ms"] + detail["trimmed_tail_ms"]
                for detail in diagnostics["blocks"]
            )
            self.assertEqual(2, diagnostics["silence_trimmed_block_count"])
            self.assertEqual(per_block, diagnostics["silence_trimmed_ms"])
            self.assertEqual(
                2
                * (
                    TAKE_LEAD_MS
                    - audio_sync.SILENCE_TRIM_LEAD_GUARD_MS
                    + TAKE_TAIL_MS
                    - audio_sync.SILENCE_TRIM_TAIL_GUARD_MS
                ),
                diagnostics["silence_trimmed_ms"],
            )


if __name__ == "__main__":
    unittest.main()
