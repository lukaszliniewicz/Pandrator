import json
import os
import sqlite3
import tempfile
import unittest

from pandrator.logic import session_handler, settings_handler, state_db_handler


class SettingsAndTrashTests(unittest.TestCase):
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

    def test_settings_json_write_through_and_db_roundtrip(self):
        payload = {
            "llm": {
                "default_model": "openai/gpt-5.4-mini",
                "provider_configs": [{"id": "openai", "api_key": "top-secret"}],
                "request_timeout_seconds": 180,
            },
            "tts": {
                "openai_audio_endpoint": "openai",
                "provider_configs": [{"id": "openai", "api_key": "another-secret"}],
            },
        }

        settings_handler.save_global_settings(payload)

        json_path = settings_handler.get_global_settings_path()
        self.assertTrue(os.path.isfile(json_path))
        with open(json_path, "r", encoding="utf-8") as file_handle:
            wrapped = json.load(file_handle)
        self.assertEqual(wrapped["settings"]["llm"]["default_model"], payload["llm"]["default_model"])

        loaded = settings_handler.load_global_settings()
        self.assertEqual(loaded["tts"]["openai_audio_endpoint"], payload["tts"]["openai_audio_endpoint"])

        db_loaded = state_db_handler.load_latest_app_settings()
        self.assertEqual(db_loaded["llm"]["default_model"], payload["llm"]["default_model"])

    def test_move_restore_and_expired_trash(self):
        session_name = "TrashMe"
        session_path = session_handler.get_session_path(session_name)
        os.makedirs(session_path, exist_ok=True)
        with open(os.path.join(session_path, "sample.txt"), "w", encoding="utf-8") as file_handle:
            file_handle.write("content")

        moved, trash_path = session_handler.move_session_to_trash(session_name, retention_days=1)
        self.assertTrue(moved)
        self.assertTrue(os.path.exists(trash_path))
        self.assertFalse(os.path.exists(session_path))

        restored, restored_path = session_handler.restore_session_from_trash(session_name)
        self.assertTrue(restored)
        self.assertEqual(os.path.abspath(restored_path), os.path.abspath(session_path))
        self.assertTrue(os.path.exists(session_path))

        moved_again, trash_path_again = session_handler.move_session_to_trash(session_name, retention_days=1)
        self.assertTrue(moved_again)

        # Force expiry in DB for deterministic cleanup test.
        with sqlite3.connect(state_db_handler.get_db_path()) as connection:
            connection.execute(
                "UPDATE trash_entries SET expires_at = '2000-01-01T00:00:00+00:00' WHERE trash_path = ?",
                (trash_path_again,),
            )
            connection.commit()

        removed_count, removed_paths = session_handler.empty_expired_trash(retention_days=1)
        self.assertEqual(removed_count, 1)
        self.assertIn(trash_path_again, removed_paths)
        self.assertFalse(os.path.exists(trash_path_again))

    def test_restore_duplicate_name_detection(self):
        session_name = "DuplicateSession"
        session_path = session_handler.get_session_path(session_name)
        os.makedirs(session_path, exist_ok=True)
        with open(os.path.join(session_path, "data.txt"), "w", encoding="utf-8") as file_handle:
            file_handle.write("data")

        moved, _ = session_handler.move_session_to_trash(session_name, retention_days=1)
        self.assertTrue(moved)

        # Recreate active folder to force duplicate restore conflict.
        os.makedirs(session_path, exist_ok=True)
        restored, message = session_handler.restore_session_from_trash(session_name)
        self.assertFalse(restored)
        self.assertIn("already exists", message.lower())

    def test_permanently_delete_trashed_session_removes_index_row(self):
        session_name = "DeleteFromTrash"
        session_path = session_handler.get_session_path(session_name)
        os.makedirs(session_path, exist_ok=True)
        with open(os.path.join(session_path, "data.txt"), "w", encoding="utf-8") as file_handle:
            file_handle.write("data")

        moved, trash_path = session_handler.move_session_to_trash(session_name, retention_days=1)
        self.assertTrue(moved)
        self.assertTrue(os.path.exists(trash_path))

        deleted, deleted_path = session_handler.permanently_delete_trashed_session(
            session_name=session_name,
            trash_path=trash_path,
        )
        self.assertTrue(deleted)
        self.assertEqual(os.path.abspath(deleted_path), os.path.abspath(trash_path))
        self.assertFalse(os.path.exists(trash_path))

        rows = state_db_handler.list_sessions(include_trashed=True)
        self.assertNotIn(session_name, {row["session_name"] for row in rows})

        with sqlite3.connect(state_db_handler.get_db_path()) as connection:
            deleted_at = connection.execute(
                "SELECT deleted_at FROM trash_entries WHERE trash_path = ?",
                (trash_path,),
            ).fetchone()[0]
        self.assertTrue(deleted_at)


if __name__ == "__main__":
    unittest.main()
