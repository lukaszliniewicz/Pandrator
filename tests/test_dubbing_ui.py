import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from pandrator.app_state import DubbingSettings
from pandrator.gui.dialogs.dubbing_advanced_dialog import DubbingAdvancedDialog
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

        section.set_stt_backend("whisperx")
        self.assertFalse(section.dub_whisper_model_label.isHidden())
        self.assertFalse(section.dub_whisper_model_combo.isHidden())
        section.close()

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
