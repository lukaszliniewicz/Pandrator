import os
import shutil
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from pandrator.app_state import DubbingSettings
from pandrator.gui.dialogs.dubbing_advanced_dialog import DubbingAdvancedDialog
from pandrator.gui.dialogs.manual_timing_dialog import ManualTimingDialog
from pandrator.gui.widgets.session_sections import DubbingSection


class DubbingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_whisper_model_selector_is_hidden_for_parakeet(self):
        section = DubbingSection()
        section.show()

        section.set_stt_backend("parakeet_onnx")
        self.assertTrue(section.dub_whisper_model_label.isHidden())
        self.assertTrue(section.dub_whisper_model_combo.isHidden())
        self.assertFalse(section.fine_tune_timings_button.isHidden())

        section.set_stt_backend("whisperx")
        self.assertFalse(section.dub_whisper_model_label.isHidden())
        self.assertFalse(section.dub_whisper_model_combo.isHidden())
        section.close()

    def test_manual_timing_dialog_plays_cached_clip_through_app_playback(self):
        srt_content = """1
00:00:00,000 --> 00:00:02,000
Preview this segment.
"""
        play_audio = Mock(return_value=True)
        stop_audio = Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = Path(temp_dir) / "source.srt"
            audio_path = Path(temp_dir) / "reference.wav"
            preview_path = Path(temp_dir) / "preview.wav"
            srt_path.write_text(srt_content, encoding="utf-8")
            audio_path.write_bytes(b"reference")
            preview_path.write_bytes(b"preview")

            dialog = ManualTimingDialog(
                str(srt_path),
                str(audio_path),
                temp_dir,
                play_audio_callback=play_audio,
                stop_audio_callback=stop_audio,
            )
            dialog._cache_preview((0, 2150), str(preview_path))

            self.assertTrue(dialog.play_button.isEnabled())
            dialog.play_button.click()
            play_audio.assert_called_once_with(str(preview_path))
            self.assertEqual(dialog.playback_status_label.text(), "Playing selected segment.")

            dialog.stop_button.click()
            self.assertGreaterEqual(stop_audio.call_count, 2)
            dialog.close()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for preview extraction")
    def test_manual_timing_dialog_extracts_selected_clip_asynchronously(self):
        srt_content = """1
00:00:01,000 --> 00:00:02,000
Preview this segment.
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = Path(temp_dir) / "source.srt"
            audio_path = Path(temp_dir) / "reference.wav"
            srt_path.write_text(srt_content, encoding="utf-8")
            with wave.open(str(audio_path), "wb") as audio_file:
                audio_file.setnchannels(1)
                audio_file.setsampwidth(2)
                audio_file.setframerate(16000)
                audio_file.writeframes(b"\x00\x00" * 16000 * 3)

            played_paths: list[str] = []
            event_loop = QEventLoop()

            def play_audio(preview_path: str) -> bool:
                played_paths.append(preview_path)
                event_loop.quit()
                return True

            dialog = ManualTimingDialog(
                str(srt_path),
                str(audio_path),
                temp_dir,
                play_audio_callback=play_audio,
                stop_audio_callback=lambda: None,
            )

            started_at = time.perf_counter()
            with patch.object(QMessageBox, "warning") as warning:
                dialog.play_button.click()
                QTimer.singleShot(5000, event_loop.quit)
                event_loop.exec()

            self.assertFalse(warning.called)
            self.assertEqual(len(played_paths), 1)
            self.assertTrue(Path(played_paths[0]).is_file())
            self.assertGreater(Path(played_paths[0]).stat().st_size, 44)
            self.assertLess(time.perf_counter() - started_at, 5.0)
            dialog.close()

    def test_native_model_catalog_populates_both_dubbing_stages(self):
        section = DubbingSection()
        models = [
            "default",
            "openai/gpt-5.4-mini",
            "custom:local-openai/local-model",
        ]

        section.set_llm_model_options(
            models,
            "custom:local-openai/local-model",
            "openai/gpt-5.4-mini",
        )

        correction_options = [
            section.dub_correction_model_combo.itemText(index)
            for index in range(section.dub_correction_model_combo.count())
        ]
        translation_options = [
            section.dub_trans_model_combo.itemText(index)
            for index in range(section.dub_trans_model_combo.count())
        ]
        self.assertEqual(correction_options, models)
        self.assertEqual(translation_options, models)
        self.assertEqual(
            section.dub_correction_model_combo.currentText(),
            "custom:local-openai/local-model",
        )
        self.assertEqual(
            section.dub_trans_model_combo.currentText(),
            "openai/gpt-5.4-mini",
        )
        self.assertGreaterEqual(section.dub_translation_backend_combo.findData("llm"), 0)
        self.assertGreaterEqual(section.dub_translation_backend_combo.findData("deepl"), 0)

    def test_translation_options_are_hidden_until_enabled(self):
        section = DubbingSection()
        section.show()

        self.assertTrue(section.translation_frame.isHidden())
        section.dub_translate_check.setChecked(True)
        self.assertFalse(section.translation_frame.isHidden())
        section.dub_translate_check.setChecked(False)
        self.assertTrue(section.translation_frame.isHidden())
        section.close()

    def test_individual_dubbing_stages_are_grouped_in_one_menu(self):
        section = DubbingSection()

        self.assertIs(section.stage_actions_button.menu(), section.stage_actions_menu)
        self.assertEqual(section.only_transcribe_action.text(), "Transcribe Only")
        self.assertEqual(section.only_correct_action.text(), "Correct Only")
        self.assertEqual(section.only_translate_action.text(), "Translate Only")
        self.assertEqual(section.dub_advanced_button.text(), "Advanced Dubbing Settings…")
        self.assertEqual(section.fine_tune_timings_button.text(), "Preview Subtitles")

        actions_layout = section.buttons_frame.layout()
        self.assertEqual(
            actions_layout.indexOf(section.dub_advanced_button),
            actions_layout.indexOf(section.stage_actions_button) + 1,
        )
        self.assertEqual(section.transcription_frame.layout().indexOf(section.dub_advanced_button), -1)

    def test_parakeet_advanced_dialog_shows_vad_group_not_whisper_group(self):
        state = DubbingSettings(stt_backend="parakeet_onnx")
        dialog = DubbingAdvancedDialog(state)
        dialog.show()

        self.assertTrue(dialog.whisper_group.isHidden())
        self.assertFalse(dialog.parakeet_group.isHidden())
        self.assertTrue(dialog.parakeet_vad_check.isCheckable())
        self.assertTrue(dialog.parakeet_vad_check.isChecked())
        dialog.close()

    def test_disabling_vad_disables_its_parameter_controls(self):
        state = DubbingSettings(
            stt_backend="parakeet_onnx",
            parakeet_vad_enabled=False,
        )
        dialog = DubbingAdvancedDialog(state)
        dialog.show()

        self.assertFalse(dialog.parakeet_vad_check.isChecked())
        self.assertFalse(dialog.parakeet_vad_threshold_spin.isEnabled())
        self.assertFalse(dialog.parakeet_vad_min_silence_spin.isEnabled())
        dialog.close()

    def test_advanced_dialog_round_trips_valid_zero_values(self):
        state = DubbingSettings(
            stt_backend="parakeet_onnx",
            subtitle_merge_threshold=0,
            parakeet_vad_neg_threshold=0.0,
            parakeet_vad_min_silence_ms=0.0,
            parakeet_vad_min_speech_ms=0.0,
            parakeet_vad_speech_pad_ms=0.0,
            sync_delay_start_ms=0,
        )
        dialog = DubbingAdvancedDialog(state)

        self.assertEqual(dialog.merge_threshold_spin.value(), 0)
        self.assertEqual(dialog.parakeet_vad_min_silence_spin.value(), 0.0)
        self.assertEqual(dialog.parakeet_vad_min_speech_spin.value(), 0.0)
        self.assertEqual(dialog.parakeet_vad_speech_pad_spin.value(), 0.0)
        self.assertEqual(dialog.sync_delay_spin.value(), 0)

        dialog.apply_to_state(state)
        self.assertEqual(state.subtitle_merge_threshold, 0)
        self.assertEqual(state.parakeet_vad_min_silence_ms, 0.0)
        self.assertEqual(state.parakeet_vad_min_speech_ms, 0.0)
        self.assertEqual(state.parakeet_vad_speech_pad_ms, 0.0)
        self.assertEqual(state.sync_delay_start_ms, 0)


if __name__ == "__main__":
    unittest.main()
