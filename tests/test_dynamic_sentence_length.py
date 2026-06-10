import unittest
import os
import tempfile
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

        # Test change to OpenAI (maps to OpenAI-Compatible)
        tab._on_tts_service_changed("OpenAI")
        self.assertEqual(logic.state.text_processing.max_sentence_length, 200)

if __name__ == "__main__":
    unittest.main()
