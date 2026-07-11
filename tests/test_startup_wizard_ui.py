import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

from pandrator.gui.dialogs.startup_wizard_dialog import StartupWizardDialog, wizard_icon
from pandrator.gui.main_window import MainWindow


class _FakeLogic(QObject):
    state_changed = pyqtSignal()
    app_notification = pyqtSignal(str, int)
    show_error = pyqtSignal(str, str)
    dubbing_video_saved = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.state = SimpleNamespace(
            session_name="Untitled Session",
            wizard=SimpleNamespace(
                setup_completed_version=1,
                show_on_startup=False,
            ),
        )

    def list_indexed_sessions(self):
        return [
            {
                "session_name": "Demo",
                "dubbing_mode": True,
                "config_modified_at": "2026-07-11",
            }
        ]

    def list_reusable_sources(self, limit=500):
        return []

    def is_rvc_available(self):
        return False

    def list_llm_provider_configs(self):
        return [{"id": "openai"}]

    def list_tts_service_configs(self):
        return [{"id": "local", "api_base": "http://127.0.0.1:8000"}]

    def list_voice_library(self):
        return []

    def _persist_global_settings(self, force=False):
        return None


class StartupWizardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_tiles_load_icons_and_drive_hidden_compatibility_state(self):
        logic = _FakeLogic()
        with patch.object(StartupWizardDialog, "_gpu_guidance", return_value="GPU guidance"):
            dialog = StartupWizardDialog(logic)
        self.addCleanup(dialog.close)

        self.assertFalse(wizard_icon("subtitles").isNull())
        self.assertTrue(dialog.task_kind_tiles["subtitles"].isChecked())
        self.assertTrue(dialog.source_mode_tiles["file"].isChecked())

        dialog.task_kind_tiles["audiobook"].click()
        self.app.processEvents()
        self.assertEqual(dialog.task_kind_combo.currentData(), "audiobook")
        self.assertTrue(dialog.source_mode_tiles["paste"].isEnabled())
        self.assertFalse(dialog.source_mode_tiles["url"].isEnabled())

    def test_provider_handoff_hides_wizard_and_return_chip_restores_same_page(self):
        logic = _FakeLogic()

        def create_minimal_tabs(window):
            window.session_tab = SimpleNamespace(session_name_label=SimpleNamespace(setText=lambda _text: None))
            window.providers_tab = QWidget()
            window.audio_processing_tab = QWidget()
            window.tab_widget.addTab(QWidget(), "Session")
            window.tab_widget.addTab(window.audio_processing_tab, "Audio")
            window.tab_widget.addTab(window.providers_tab, "Providers")

        with patch.object(MainWindow, "_create_tabs", create_minimal_tabs), patch.object(
            StartupWizardDialog, "_gpu_guidance", return_value="GPU guidance"
        ):
            window = MainWindow(logic)
            self.addCleanup(window.close)
            window.show()
            window.open_startup_wizard()
            self.app.processEvents()

            dialog = window._startup_wizard
            dialog.stack.setCurrentWidget(dialog.setup_page)
            dialog._open_providers()
            self.app.processEvents()

            self.assertTrue(dialog.isHidden())
            self.assertTrue(window.return_to_wizard_button.isVisible())
            self.assertIs(window.tab_widget.currentWidget(), window.providers_tab)

            window.return_to_wizard_button.click()
            self.app.processEvents()
            self.assertTrue(dialog.isVisible())
            self.assertFalse(window.return_to_wizard_button.isVisible())
            self.assertIs(dialog.stack.currentWidget(), dialog.setup_page)


if __name__ == "__main__":
    unittest.main()
