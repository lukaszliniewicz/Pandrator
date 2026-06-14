import unittest
import os
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from pandrator.app_state import AppState
from pandrator.app_logic import AppLogic
from pandrator.gui.widgets.session_tab import SessionTab
from pandrator.logic import state_db_handler, settings_handler, session_handler

class DynamicSentenceLengthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance()
        if cls.app is None:
            cls.app = QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_app_root = settings_handler.APP_ROOT_DIR
        self.original_outputs_dir = session_handler.OUTPUTS_DIR
        self.original_default_handler = state_db_handler.DEFAULT_HANDLER

        settings_handler.APP_ROOT_DIR = self.temp_dir.name
        session_handler.OUTPUTS_DIR = os.path.join(self.temp_dir.name, "Outputs")
        os.makedirs(session_handler.OUTPUTS_DIR, exist_ok=True)

        state_db_handler.DEFAULT_HANDLER = state_db_handler.StateDBHandler(app_root=self.temp_dir.name)
        state_db_handler.initialize_database()

    def tearDown(self):
        settings_handler.APP_ROOT_DIR = self.original_app_root
        session_handler.OUTPUTS_DIR = self.original_outputs_dir
        state_db_handler.DEFAULT_HANDLER = self.original_default_handler
        self.temp_dir.cleanup()

    def test_default_sentence_length(self):
        state = AppState()
        self.assertEqual(state.text_processing.max_sentence_length, 200)

    def test_dynamic_sentence_length_switching(self):
        logic = AppLogic()
        tab = SessionTab(logic=logic)
        
        # Test change to Kokoro
        tab._on_tts_service_changed("Kokoro")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 350)
        
        # Test change to FishS2
        tab._on_tts_service_changed("FishS2")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 350)

        # Test change to VoxCPM
        tab._on_tts_service_changed("VoxCPM")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 300)

        # Test change to Voxtral
        tab._on_tts_service_changed("Voxtral")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 300)

        # Test change to Chatterbox
        tab._on_tts_service_changed("Chatterbox")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 350)

        # Test change to XTTS
        tab._on_tts_service_changed("XTTS")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 200)

        # Test change to OpenAI.
        tab._on_tts_service_changed("OpenAI")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 200)

    def test_pdf_source_import_runs_in_background_and_opens_review_after_completion(self):
        logic = AppLogic()
        tab = SessionTab(logic=logic)
        self.addCleanup(tab.close)

        def import_pdf(_path, reset_session=False, progress_callback=None):
            self.assertFalse(reset_session)
            progress_callback("Ingesting PDF page 1/2...")
            time.sleep(0.03)
            progress_callback("Ingesting PDF page 2/2...")
            return True

        with (
            patch.object(logic, "select_source_file", side_effect=import_pdf),
            patch.object(tab, "_review_selected_source") as review_selected_source,
        ):
            tab._start_pdf_source_import("book.pdf", ".pdf", False)
            self.assertIsNotNone(tab._source_import_thread)

            deadline = time.time() + 2
            while not review_selected_source.called and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        self.assertTrue(review_selected_source.called)
        review_selected_source.assert_called_once_with("book.pdf", ".pdf")
        self.assertIsNone(tab._source_import_thread)
        self.assertIsNone(tab._source_import_progress_dialog)

    def test_background_playback_stop_marshals_timer_cleanup_to_ui_thread(self):
        logic = AppLogic()
        timer_stop_threads = []
        logic.playlist_timer = SimpleNamespace(
            stop=lambda: timer_stop_threads.append(threading.current_thread())
        )
        logic.playback_handler = SimpleNamespace(stop=lambda: None)

        worker = threading.Thread(target=logic.stop_playback)
        worker.start()
        worker.join(timeout=1)

        self.assertEqual(timer_stop_threads, [])
        self.app.processEvents()
        self.assertEqual(timer_stop_threads, [threading.main_thread()])

if __name__ == "__main__":
    unittest.main()
