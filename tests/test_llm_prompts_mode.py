import unittest
from unittest.mock import patch
import os
import sqlite3
import tempfile

from pandrator.app_state import AppState
from pandrator.app_logic import AppLogic
from pandrator.logic import session_handler, settings_handler, state_db_handler

class LLMPromptsModeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.original_app_root = settings_handler.APP_ROOT_DIR
        self.original_outputs_dir = session_handler.OUTPUTS_DIR
        self.original_default_handler = state_db_handler.DEFAULT_HANDLER

        settings_handler.APP_ROOT_DIR = self.temp_dir.name
        session_handler.OUTPUTS_DIR = os.path.join(self.temp_dir.name, "Outputs")
        os.makedirs(session_handler.OUTPUTS_DIR, exist_ok=True)

        state_db_handler.DEFAULT_HANDLER = state_db_handler.StateDBHandler(app_root=self.temp_dir.name)
        state_db_handler.initialize_database()

        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        settings_handler.APP_ROOT_DIR = self.original_app_root
        session_handler.OUTPUTS_DIR = self.original_outputs_dir
        state_db_handler.DEFAULT_HANDLER = self.original_default_handler

    def test_default_llm_settings_combined_prompt(self):
        state = AppState()
        self.assertFalse(state.llm.use_multi_stage)
        self.assertTrue(state.llm.combined_prompt.enabled)
        self.assertTrue("preprocess and clean" in state.llm.combined_prompt.prompt_text)
        self.assertTrue(state.llm.first_prompt.enabled)
        self.assertFalse(state.llm.second_prompt.enabled)
        self.assertFalse(state.llm.third_prompt.enabled)

    @patch("pandrator.logic.llm_handler.process_text")
    @patch("pandrator.app_logic.AppLogic._ensure_llm_model_loaded")
    def test_run_llm_processing_single_mode(self, mock_load, mock_process):
        mock_load.return_value = True
        mock_process.return_value = "Optimized Output"

        logic = AppLogic()
        logic.state.llm.use_multi_stage = False
        logic.state.llm.combined_prompt.prompt_text = "Combined:"
        logic.state.llm.combined_prompt.enabled = True

        result, count = logic._run_llm_processing("Hello World")

        self.assertEqual(result, "Optimized Output")
        self.assertEqual(count, 1)
        mock_process.assert_called_once_with(
            "Hello World",
            "Combined:",
            False,
            model_name=None,
            llm_settings=logic.state.llm
        )

    @patch("pandrator.logic.llm_handler.process_text")
    @patch("pandrator.app_logic.AppLogic._ensure_llm_model_loaded")
    def test_run_llm_processing_multi_stage_mode(self, mock_load, mock_process):
        mock_load.return_value = True
        mock_process.side_effect = lambda text, prompt, *args, **kwargs: f"{text} -> [{prompt}]"

        logic = AppLogic()
        logic.state.llm.use_multi_stage = True
        logic.state.llm.first_prompt.prompt_text = "Prompt1"
        logic.state.llm.first_prompt.enabled = True
        logic.state.llm.second_prompt.prompt_text = "Prompt2"
        logic.state.llm.second_prompt.enabled = True
        logic.state.llm.third_prompt.prompt_text = "Prompt3"
        logic.state.llm.third_prompt.enabled = False  # Disabled prompt should be skipped

        result, count = logic._run_llm_processing("Input")

        self.assertEqual(result, "Input -> [Prompt1] -> [Prompt2]")
        self.assertEqual(count, 2)
        self.assertEqual(mock_process.call_count, 2)

    @patch("pandrator.logic.llm_handler.process_text")
    @patch("pandrator.app_logic.AppLogic._ensure_llm_model_loaded")
    def test_llm_processing_receives_nemo_normalized_sentence(self, mock_load, mock_process):
        mock_load.return_value = True
        mock_process.return_value = "LLM Output"

        logic = AppLogic()
        logic.state.llm.combined_prompt.prompt_text = "Combined:"
        logic.state.llm.combined_prompt.enabled = True
        logic.state.processed_sentences = [{"original_sentence": "Chapter twelve."}]

        result, _ = logic._run_llm_processing(logic.state.processed_sentences[0]["original_sentence"])

        self.assertEqual(result, "LLM Output")
        mock_process.assert_called_once_with(
            "Chapter twelve.",
            "Combined:",
            False,
            model_name=None,
            llm_settings=logic.state.llm,
        )

    def test_load_session_legacy_migration(self):
        # Create a session directory
        session_name = "LegacySession"
        session_path = os.path.join(session_handler.OUTPUTS_DIR, session_name)
        os.makedirs(session_path, exist_ok=True)

        # Write custom legacy config payload (e.g. second prompt enabled)
        config_payload = {
            "llm": {
                "first_prompt": {"prompt_text": "P1", "enabled": True},
                "second_prompt": {"prompt_text": "P2", "enabled": True},
                "third_prompt": {"prompt_text": "P3", "enabled": False}
            }
        }
        session_handler.save_session_config(session_name, config_payload)

        # Load the session via AppLogic
        logic = AppLogic()
        logic.load_session(session_name)

        # Assert that use_multi_stage is migrated to True since legacy prompts were enabled
        self.assertTrue(logic.state.llm.use_multi_stage)
        self.assertEqual(logic.state.llm.first_prompt.prompt_text, "P1")
        self.assertEqual(logic.state.llm.second_prompt.prompt_text, "P2")

    def test_load_session_default_no_migration(self):
        session_name = "ModernSession"
        session_path = os.path.join(session_handler.OUTPUTS_DIR, session_name)
        os.makedirs(session_path, exist_ok=True)

        # Modern config (or old config with only first_prompt enabled, which maps to default)
        config_payload = {
            "llm": {
                "use_multi_stage": False,
                "first_prompt": {"prompt_text": "P1", "enabled": True},
                "second_prompt": {"prompt_text": "P2", "enabled": False},
                "third_prompt": {"prompt_text": "P3", "enabled": False}
            }
        }
        session_handler.save_session_config(session_name, config_payload)

        logic = AppLogic()
        logic.load_session(session_name)

        # Assert that use_multi_stage remains False
        self.assertFalse(logic.state.llm.use_multi_stage)

    def test_app_startup_and_session_load_do_not_touch_session_config(self):
        session_name = "StableOnOpen"
        session_path = os.path.join(session_handler.OUTPUTS_DIR, session_name)
        os.makedirs(session_path, exist_ok=True)
        source_path = os.path.join(session_path, "source.txt")
        with open(source_path, "w", encoding="utf-8") as file_handle:
            file_handle.write("Source")

        session_handler.save_session_config(
            session_name,
            {
                "session_name": session_name,
                "source_file_path": source_path,
                "source_display_path": source_path,
                "original_source_file_path": source_path,
            },
        )
        config_path = os.path.join(session_path, session_handler.SESSION_CONFIG_FILENAME)
        historical_timestamp = 1_704_164_645
        os.utime(config_path, (historical_timestamp, historical_timestamp))

        logic = AppLogic()
        logic.load_session(session_name)

        with sqlite3.connect(state_db_handler.get_db_path()) as connection:
            indexed_modified_at = connection.execute(
                "SELECT config_modified_at FROM sessions WHERE session_name = ?",
                (session_name,),
            ).fetchone()[0]

        self.assertEqual(int(os.path.getmtime(config_path)), historical_timestamp)
        self.assertEqual(indexed_modified_at, "2024-01-02T03:04:05+00:00")

if __name__ == "__main__":
    unittest.main()
