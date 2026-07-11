import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from pandrator.app_state import AppState
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
            "text_processing": {
                "remove_footnotes": True,
            },
        }

        settings_handler.save_global_settings(payload)

        json_path = settings_handler.get_global_settings_path()
        self.assertTrue(os.path.isfile(json_path))
        with open(json_path, "r", encoding="utf-8") as file_handle:
            wrapped = json.load(file_handle)
        self.assertEqual(wrapped["settings"]["llm"]["default_model"], payload["llm"]["default_model"])
        self.assertEqual(wrapped["settings"]["text_processing"]["remove_footnotes"], True)

        loaded = settings_handler.load_global_settings()
        self.assertEqual(loaded["tts"]["openai_audio_endpoint"], payload["tts"]["openai_audio_endpoint"])
        self.assertEqual(loaded["text_processing"]["remove_footnotes"], True)

        db_loaded = state_db_handler.load_latest_app_settings()
        self.assertEqual(db_loaded["llm"]["default_model"], payload["llm"]["default_model"])
        self.assertEqual(db_loaded["text_processing"]["remove_footnotes"], True)

    def test_unchanged_session_config_save_and_read_preserve_modified_time(self):
        session_name = "StableConfig"
        payload = {
            "session_name": session_name,
            "source_file_path": os.path.join(session_handler.OUTPUTS_DIR, session_name, "source.txt"),
        }
        session_handler.save_session_config(session_name, payload)
        config_path = os.path.join(
            session_handler.get_session_path(session_name),
            session_handler.SESSION_CONFIG_FILENAME,
        )
        historical_timestamp = 1_704_164_645
        os.utime(config_path, (historical_timestamp, historical_timestamp))

        session_handler.save_session_config(session_name, payload)
        loaded = session_handler.load_session_config(session_name)

        with closing(sqlite3.connect(state_db_handler.get_db_path())) as connection:
            snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM session_config_snapshots WHERE session_name = ?",
                (session_name,),
            ).fetchone()[0]

        self.assertEqual(loaded, payload)
        self.assertEqual(int(os.path.getmtime(config_path)), historical_timestamp)
        self.assertEqual(snapshot_count, 1)

        changed_payload = {**payload, "source_file_path": payload["source_file_path"] + ".changed"}
        session_handler.save_session_config(session_name, changed_payload)
        with closing(sqlite3.connect(state_db_handler.get_db_path())) as connection:
            changed_snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM session_config_snapshots WHERE session_name = ?",
                (session_name,),
            ).fetchone()[0]

        self.assertGreater(int(os.path.getmtime(config_path)), historical_timestamp)
        self.assertEqual(changed_snapshot_count, 2)

    def test_apply_global_settings_payload_applies_remove_footnotes(self):
        state = AppState()
        self.assertFalse(state.text_processing.remove_footnotes)

        settings_handler.apply_global_settings_payload(
            state,
            {
                "text_processing": {
                    "remove_footnotes": True
                }
            }
        )
        self.assertTrue(state.text_processing.remove_footnotes)

    def test_source_cleaning_phase_limits_roundtrip_and_migrate_legacy_total(self):
        state = AppState()
        settings_handler.apply_global_settings_payload(
            state,
            {"source_cleaning": {"max_iterations": 30}},
        )
        self.assertEqual(sum(state.source_cleaning.phase_max_iterations.values()), 30)

        state.source_cleaning.phase_max_iterations["chapter_marking"] = 25
        state.source_cleaning.max_iterations = sum(state.source_cleaning.phase_max_iterations.values())
        payload = settings_handler.build_global_settings_payload(state)

        restored = AppState()
        settings_handler.apply_global_settings_payload(restored, payload)
        self.assertEqual(
            restored.source_cleaning.phase_max_iterations,
            state.source_cleaning.phase_max_iterations,
        )
        self.assertIsNone(restored.source_cleaning.llm_temperature)

        legacy_payload = {
            "llm": {
                "default_model": state.llm.default_model,
                "provider_configs": [
                    {
                        **provider,
                        "models": [model["id"] if isinstance(model, dict) else model for model in provider["models"]],
                    }
                    for provider in state.llm.provider_configs
                ],
            },
            "source_cleaning": {"llm_temperature": 0.7},
        }
        migrated = AppState()
        settings_handler.apply_global_settings_payload(migrated, legacy_payload)
        default_model_id = migrated.llm.default_model.split("/", 1)[-1]
        default_record = next(
            model
            for provider in migrated.llm.provider_configs
            for model in provider["models"]
            if model["id"] == default_model_id
        )
        self.assertEqual(default_record["default_temperature"], 0.7)
        self.assertIsNone(migrated.source_cleaning.llm_temperature)

    def test_kokoro_default_voice_settings_normalize_language_keys(self):
        state = AppState()

        settings_handler.apply_global_settings_payload(
            state,
            {
                "tts": {
                    "kokoro_default_voices": {
                        "en-us": "af_bella",
                        "JA": "jf_alpha",
                        "empty": "",
                    }
                }
            },
        )

        self.assertEqual(
            state.tts.kokoro_default_voices,
            {
                "en": "af_bella",
                "ja": "jf_alpha",
            },
        )

    def test_legacy_mixed_tts_provider_catalog_migrates_to_service_configs(self):
        state = AppState()

        settings_handler.apply_global_settings_payload(
            state,
            {
                "tts": {
                    "provider_configs": [
                        {
                            "id": "openai",
                            "name": "OpenAI",
                            "provider": "openai",
                            "api_base": "https://openai.example/v1",
                        },
                        {
                            "id": "magpie",
                            "name": "Magpie",
                            "provider": "openai",
                            "api_base": "http://127.0.0.1:9999",
                        },
                        {
                            "id": "custom-local",
                            "name": "Custom Local",
                            "provider": "openai",
                            "api_base": "http://127.0.0.1:9000/v1",
                        },
                    ],
                }
            },
        )

        service_configs = {
            service["id"]: service
            for service in state.tts.service_configs
        }
        self.assertEqual(service_configs["openai"]["api_base"], "https://openai.example/v1")
        self.assertEqual(service_configs["magpie"]["api_base"], "http://127.0.0.1:9999")
        self.assertEqual(
            [provider["id"] for provider in state.tts.provider_configs],
            ["custom-local"],
        )

    def test_legacy_cloud_endpoint_selection_migrates_to_first_class_service(self):
        from pandrator.app_logic import AppLogic

        logic = AppLogic()
        self.addCleanup(logic.shutdown)
        legacy_tts = AppState().tts
        legacy_tts.service = "OpenAI-Compatible"
        legacy_tts.openai_audio_endpoint = "gemini"
        legacy_tts.service_configs = []
        legacy_tts.provider_configs = [
            {
                "id": "gemini",
                "name": "Gemini",
                "provider": "gemini",
                "api_base": "https://gemini.example/openai",
            }
        ]

        logic._normalize_tts_service_state(legacy_tts)

        self.assertEqual(legacy_tts.service, "Google Gemini")
        self.assertEqual(legacy_tts.openai_audio_endpoint, "")
        self.assertEqual(legacy_tts.provider_configs, [])

    def test_legacy_custom_service_label_migrates_to_custom(self):
        from pandrator.app_logic import AppLogic

        logic = AppLogic()
        self.addCleanup(logic.shutdown)
        legacy_tts = AppState().tts
        legacy_tts.service = "OpenAI-Compatible"
        legacy_tts.openai_audio_endpoint = "my-server"
        legacy_tts.provider_configs = [
            {
                "id": "my-server",
                "name": "My Server",
                "provider": "openai",
                "api_base": "http://127.0.0.1:9000/v1",
            }
        ]

        logic._normalize_tts_service_state(legacy_tts)

        self.assertEqual(legacy_tts.service, "Custom")
        self.assertEqual(legacy_tts.openai_audio_endpoint, "my-server")

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
        with closing(sqlite3.connect(state_db_handler.get_db_path())) as connection:
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

        with closing(sqlite3.connect(state_db_handler.get_db_path())) as connection:
            deleted_at = connection.execute(
                "SELECT deleted_at FROM trash_entries WHERE trash_path = ?",
                (trash_path,),
            ).fetchone()[0]
        self.assertTrue(deleted_at)

    def test_chatterbox_settings_serialization_and_model_aware_defaults(self):
        from pandrator.app_logic import AppLogic
        state = AppState()
        
        # 1. Test defaults
        self.assertEqual(state.tts.chatterbox_temperature, 0.8)
        self.assertEqual(state.tts.chatterbox_top_p, 0.95)
        self.assertTrue(state.tts.chatterbox_norm_loudness)

        # 2. Test apply payload
        payload = {
            "tts": {
                "chatterbox_temperature": 0.9,
                "chatterbox_top_p": 0.8,
                "chatterbox_norm_loudness": False,
            }
        }
        settings_handler.apply_global_settings_payload(state, payload)
        self.assertEqual(state.tts.chatterbox_temperature, 0.9)
        self.assertEqual(state.tts.chatterbox_top_p, 0.8)
        self.assertFalse(state.tts.chatterbox_norm_loudness)

        # 3. Test build payload
        serialized = settings_handler.build_global_settings_payload(state)
        self.assertEqual(serialized["tts"]["chatterbox_temperature"], 0.9)
        self.assertEqual(serialized["tts"]["chatterbox_top_p"], 0.8)
        self.assertFalse(serialized["tts"]["chatterbox_norm_loudness"])

        # 4. Test model-aware defaults
        logic = AppLogic()
        logic.state.tts.service = "Chatterbox"
        logic.state.tts.chatterbox_top_p = 0.95
        
        # Switch to multilingual -> top_p should go to 1.0
        logic.on_tts_model_changed("chatterbox-multilingual")
        self.assertEqual(logic.state.tts.chatterbox_top_p, 1.0)
        
        # Switch to turbo -> top_p should go to 0.95
        logic.on_tts_model_changed("chatterbox-turbo")
        self.assertEqual(logic.state.tts.chatterbox_top_p, 0.95)
        
        # User manual override should NOT be overwritten
        logic.state.tts.chatterbox_top_p = 0.85
        logic.on_tts_model_changed("chatterbox-multilingual")
        self.assertEqual(logic.state.tts.chatterbox_top_p, 0.85)


if __name__ == "__main__":
    unittest.main()
