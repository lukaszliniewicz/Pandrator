import os
import unittest
from unittest.mock import Mock

from PyQt6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton

from pandrator_installer.backend_catalog import (
    CRISPASR_MODELS,
    PARAKEET_06B_V3_LANGUAGES,
    TTS_BACKENDS,
    TTS_BACKEND_ORDER,
    WHISPER_LARGE_V3_LANGUAGES,
    formatted_crispasr_languages,
    formatted_crispasr_model_licences,
)
from pandrator_installer.gui.backend_card import BackendOptionCard
from pandrator_installer.gui.actions import GuiActionsMixin


class TestInstallerBackendCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_every_engine_has_complete_presentation_metadata(self):
        self.assertEqual(tuple(TTS_BACKENDS), TTS_BACKEND_ORDER)
        for key, backend in TTS_BACKENDS.items():
            with self.subTest(key=key):
                self.assertTrue(backend.summary)
                self.assertTrue(backend.languages)
                self.assertEqual(len(backend.languages), len(set(backend.languages)))
                self.assertTrue(backend.note)
                self.assertTrue(backend.source_url.startswith("https://"))
                self.assertTrue(backend.models)
                for model in backend.models:
                    self.assertTrue(model.name)
                    self.assertTrue(model.licence)
                    self.assertTrue(model.licence_url.startswith("https://"))
                    self.assertTrue(model.usage)

    def test_every_offered_tts_model_variant_has_licence_metadata(self):
        expected_counts = {
            "kokoro": 1,
            "kobold_qwen": 3,
            "xtts": 1,
            "voxcpm": 1,
            "fishs2": 1,
            "voxtral": 1,
            "silero": 10,
            "chatterbox": 3,
            "magpie": 1,
        }
        self.assertEqual(
            {key: len(value.models) for key, value in TTS_BACKENDS.items()},
            expected_counts,
        )
        silero_names = {model.name for model in TTS_BACKENDS["silero"].models}
        self.assertEqual(
            silero_names,
            {
                "v5_cis_base",
                "v5_cis_base_nostress",
                "v5_cis_ext",
                "v5_5_ru",
                "v3_en",
                "v3_en_indic",
                "v3_de",
                "v3_es",
                "v3_fr",
                "v3_indic",
            },
        )

    def test_expected_language_catalogue_sizes(self):
        expected_sizes = {
            "kokoro": 9,
            "kobold_qwen": 10,
            "xtts": 16,
            "voxcpm": 30,
            "fishs2": 83,
            "voxtral": 9,
            "silero": 25,
            "chatterbox": 23,
            "magpie": 9,
        }
        self.assertEqual(
            {key: len(value.languages) for key, value in TTS_BACKENDS.items()},
            expected_sizes,
        )

    def test_crispasr_catalogues_transcription_and_alignment_models(self):
        self.assertEqual(len(WHISPER_LARGE_V3_LANGUAGES), 100)
        self.assertEqual(len(PARAKEET_06B_V3_LANGUAGES), 25)
        formatted = formatted_crispasr_languages()
        self.assertIn("Whisper large-v3 (100)", formatted)
        self.assertIn("Parakeet TDT 0.6B v3 (25)", formatted)
        self.assertIn("MOSS Transcribe-Diarize 0.9B", formatted)
        self.assertEqual(len(CRISPASR_MODELS), 4)
        licences = formatted_crispasr_model_licences()
        self.assertIn("Apache-2.0", licences)
        self.assertIn("CC BY 4.0", licences)
        self.assertIn("Canary CTC forced aligner", licences)

    def test_card_expands_without_a_large_details_button(self):
        card = BackendOptionCard(
            QCheckBox("Engine"),
            "Description",
            languages="English, Polish.",
            models=(
                '<b>Example model</b> — '
                '<a href="https://example.com/license">Example licence</a>'
            ),
            details="A note.",
            voice_cloning=True,
            prebuilt_voices=False,
        )
        self.assertFalse(card.is_expanded)
        self.assertEqual(card.minimumHeight(), card.COLLAPSED_HEIGHT)
        self.assertEqual(card.maximumHeight(), card.COLLAPSED_HEIGHT)
        self.assertEqual(
            {label.text() for label in card.findChildren(QLabel, "voiceCapabilityBadge")},
            {"Voice cloning", "Pre-built voices"},
        )

        card.toggle_expanded()

        self.assertTrue(card.is_expanded)
        self.assertTrue(card.details_panel.isVisibleTo(card))
        self.assertGreater(card.maximumHeight(), card.COLLAPSED_HEIGHT)
        model_label = card.findChild(QLabel, "optionCardModels")
        self.assertIsNotNone(model_label)
        self.assertTrue(model_label.openExternalLinks())
        self.assertFalse(
            any(button.text() == "More details" for button in card.findChildren(QPushButton))
        )

    def test_collapsed_card_keeps_the_summary_compact(self):
        card = BackendOptionCard(
            QCheckBox("Engine"),
            "A concise description that remains visible while the details are collapsed.",
            languages="English, Polish.",
            voice_cloning=True,
            prebuilt_voices=True,
        )
        card.show()
        self.app.processEvents()

        self.assertEqual(card.COLLAPSED_HEIGHT, 92)
        self.assertEqual(card.height(), 92)
        self.assertEqual(card.maximumHeight(), 92)

    def test_card_shows_runtime_status_and_individual_stop_control(self):
        card = BackendOptionCard(QCheckBox("Engine"), "Description")
        requested = []
        card.stop_requested.connect(lambda: requested.append(True))

        card.set_runtime_state(True)
        self.assertTrue(card.runtime_status_label.isVisibleTo(card))
        self.assertTrue(card.runtime_stop_button.isVisibleTo(card))
        self.assertEqual(card.runtime_status_label.text(), "Running")
        card.runtime_stop_button.click()
        self.assertEqual(requested, [True])

        card.set_runtime_state(False)
        self.assertFalse(card.runtime_status_label.isVisible())
        self.assertFalse(card.runtime_stop_button.isVisible())

    def test_runtime_refresh_marks_supervised_backend_running_and_disables_launch(self):
        class RuntimeUi(GuiActionsMixin):
            worker = None

            def _collect_running_backends(self):
                return []

            def _collect_supervised_backends(self):
                return [("kokoro", "Kokoro", Mock(pid=4321))]

            def get_installed_components(self):
                return {"kokoro": True}

        ui = RuntimeUi()
        control = QCheckBox("Launch Kokoro")
        control.setChecked(True)
        card = BackendOptionCard(control, "Description")
        ui.active_backend_value_label = QLabel()
        ui.stop_backend_button = QPushButton()
        ui.refresh_backend_status_button = QPushButton()
        ui.launch_backend_cards = {"kokoro": card}
        ui.launch_backend_controls = {"kokoro": control}

        ui.update_backend_runtime_controls()

        self.assertIn("Kokoro (PID 4321, managed with Pandrator)", ui.active_backend_value_label.text())
        self.assertFalse(control.isChecked())
        self.assertFalse(control.isEnabled())
        self.assertTrue(card.runtime_status_label.isVisibleTo(card))
        self.assertTrue(card.runtime_stop_button.isEnabled())
        self.assertTrue(ui.stop_backend_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
