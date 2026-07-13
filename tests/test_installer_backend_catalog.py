import os
import unittest

from PyQt6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton

from pandrator_installer.backend_catalog import (
    PARAKEET_06B_V3_LANGUAGES,
    TTS_BACKENDS,
    TTS_BACKEND_ORDER,
    WHISPER_LARGE_V3_LANGUAGES,
    formatted_crispasr_languages,
)
from pandrator_installer.gui.backend_card import BackendOptionCard


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

    def test_expected_language_catalogue_sizes(self):
        expected_sizes = {
            "kokoro": 9,
            "kobold_qwen": 10,
            "xtts": 16,
            "voxcpm": 30,
            "fishs2": 83,
            "voxtral": 9,
            "silero": 11,
            "chatterbox": 23,
            "magpie": 9,
        }
        self.assertEqual(
            {key: len(value.languages) for key, value in TTS_BACKENDS.items()},
            expected_sizes,
        )

    def test_crispasr_catalogues_both_installed_models(self):
        self.assertEqual(len(WHISPER_LARGE_V3_LANGUAGES), 100)
        self.assertEqual(len(PARAKEET_06B_V3_LANGUAGES), 25)
        formatted = formatted_crispasr_languages()
        self.assertIn("Whisper large-v3 (100)", formatted)
        self.assertIn("Parakeet TDT 0.6B v3 (25)", formatted)

    def test_card_expands_without_a_large_details_button(self):
        card = BackendOptionCard(
            QCheckBox("Engine"),
            "Description",
            languages="English, Polish.",
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


if __name__ == "__main__":
    unittest.main()
