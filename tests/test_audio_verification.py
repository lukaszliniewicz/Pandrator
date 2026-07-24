import unittest

from pydub import AudioSegment
from pydub.generators import Sine

from pandrator.web.audio_verification import (
    add_run_rms_warning,
    analyze_audio,
    run_rms_outliers,
    verify_audio,
)


class AudioVerificationTests(unittest.TestCase):
    @staticmethod
    def clean_take(gain_db: float = -18.0) -> AudioSegment:
        return (
            Sine(220)
            .to_audio_segment(duration=950)
            .apply_gain(gain_db)
            + AudioSegment.silent(duration=50, frame_rate=44100)
        )

    def test_verification_is_opt_in(self):
        self.assertIsNone(verify_audio(self.clean_take(), "Hello world.", {}))

    def test_clean_take_passes_and_records_raw_metrics(self):
        result = verify_audio(
            self.clean_take(),
            "Hello world.",
            {"audio_verification_mode": "signal"},
        )

        self.assertEqual("passed", result["status"])
        self.assertEqual("raw_signal", result["scope"])
        self.assertLess(result["metrics"]["rms_dbfs"], -20)
        self.assertEqual([], result["issues"])

    def test_silence_is_a_failed_take(self):
        result = analyze_audio(AudioSegment.silent(duration=1000), "Hello world.")

        self.assertEqual("failed", result["status"])
        self.assertIn("near_silence", {item["code"] for item in result["issues"]})

    def test_run_relative_screen_finds_only_conspicuous_loud_outlier(self):
        values = [-24.8, -24.7, -24.9, -24.6, -24.8, -24.7, -24.9, -18.2]

        outliers = run_rms_outliers(values)

        self.assertEqual({7}, set(outliers))
        updated = add_run_rms_warning(
            {"status": "passed", "issues": [], "metrics": {"rms_dbfs": values[7]}},
            outliers[7],
        )
        self.assertEqual("warning", updated["status"])
        self.assertEqual("run_rms_outlier", updated["issues"][0]["code"])


if __name__ == "__main__":
    unittest.main()
