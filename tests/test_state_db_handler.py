import json
import os
import sqlite3
import tempfile
import unittest

from pandrator.logic.state_db_handler import StateDBHandler
from pandrator.logic import audio_variant_handler


class StateDBHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.handler = StateDBHandler(app_root=self.temp_dir.name)
        self.handler.initialize_database()

    def _session_dir(self, name: str) -> str:
        path = os.path.join(self.temp_dir.name, "Outputs", name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_database_creation_and_schema(self):
        db_path = self.handler.db_path
        self.assertTrue(os.path.isfile(db_path))

        with sqlite3.connect(db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        expected_tables = {
            "app_settings_current",
            "app_settings_history",
            "sessions",
            "session_config_snapshots",
            "session_payload_index",
            "dubbing_runs",
            "dubbing_steps",
            "dubbing_artifacts",
            "session_audio_versions",
            "trash_entries",
        }
        self.assertTrue(expected_tables.issubset(tables))

    def test_corruption_recovery(self):
        corrupt_root = tempfile.TemporaryDirectory()
        self.addCleanup(corrupt_root.cleanup)
        corrupt_db_path = os.path.join(corrupt_root.name, "pandrator_state.sqlite3")
        with open(corrupt_db_path, "wb") as file_handle:
            file_handle.write(b"this is not sqlite")

        handler = StateDBHandler(app_root=corrupt_root.name)
        handler.initialize_database()

        backups = [
            name
            for name in os.listdir(corrupt_root.name)
            if name.startswith("pandrator_state.sqlite3.corrupt.")
        ]
        self.assertTrue(backups)
        self.assertTrue(os.path.isfile(handler.db_path))

    def test_app_settings_roundtrip(self):
        payload = {
            "llm": {
                "default_model": "openai/gpt-5.4-mini",
                "provider_configs": [{"id": "openai", "api_key": "secret"}],
                "request_timeout_seconds": 120,
            },
            "tts": {
                "openai_audio_endpoint": "openai",
                "provider_configs": [{"id": "openai", "api_key": "secret-tts"}],
            },
        }
        self.handler.save_app_settings(payload, version=2)
        loaded = self.handler.load_latest_app_settings()
        self.assertEqual(loaded["llm"]["default_model"], payload["llm"]["default_model"])
        self.assertEqual(loaded["tts"]["openai_audio_endpoint"], payload["tts"]["openai_audio_endpoint"])

    def test_session_reindex_valid_missing_malformed_and_legacy(self):
        valid_session = "ValidSession"
        valid_dir = self._session_dir(valid_session)
        with open(os.path.join(valid_dir, "session_config.json"), "w", encoding="utf-8") as file_handle:
            json.dump(
                {
                    "version": 1,
                    "state": {
                        "session_name": valid_session,
                        "source_file_path": os.path.join(valid_dir, "book.txt"),
                        "tts": {"service": "XTTS", "language": "en"},
                    },
                },
                file_handle,
            )

        with open(os.path.join(valid_dir, f"{valid_session}_sentences.json"), "w", encoding="utf-8") as file_handle:
            json.dump(
                [
                    {"sentence_number": "1", "original_sentence": "Hello", "tts_generated": "yes"},
                    {"sentence_number": "2", "original_sentence": "World", "tts_generated": "no"},
                ],
                file_handle,
            )

        malformed_session = "MalformedSession"
        malformed_dir = self._session_dir(malformed_session)
        with open(os.path.join(malformed_dir, "session_config.json"), "w", encoding="utf-8") as file_handle:
            file_handle.write("{not valid json")

        legacy_session = "LegacySession"
        legacy_dir = self._session_dir(legacy_session)
        staging_dir = os.path.join(legacy_dir, "_dubbing_staging")
        os.makedirs(staging_dir, exist_ok=True)
        with open(os.path.join(staging_dir, "legacy.srt"), "w", encoding="utf-8") as file_handle:
            file_handle.write("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        with open(os.path.join(staging_dir, "legacy_speech_blocks.json"), "w", encoding="utf-8") as file_handle:
            json.dump([{"number": 1, "text": "Hello"}], file_handle)
        with open(os.path.join(staging_dir, "final_output.mp4"), "wb") as file_handle:
            file_handle.write(b"video")

        self.handler.reindex_all_sessions()
        rows = self.handler.list_sessions()
        names = {row["session_name"] for row in rows}
        self.assertIn(valid_session, names)
        self.assertIn(malformed_session, names)
        self.assertIn(legacy_session, names)

        valid_row = next(row for row in rows if row["session_name"] == valid_session)
        self.assertEqual(valid_row["generated_sentences"], 1)
        self.assertEqual(valid_row["total_sentences"], 2)

        legacy_preview = self.handler.get_session_preview(legacy_session)
        self.assertTrue(legacy_preview["runs"])
        self.assertTrue(any(run.get("legacy") for run in legacy_preview["runs"]))

    def test_active_run_artifact_selection_uses_active_flag(self):
        session_name = "RunSelection"
        self._session_dir(session_name)

        first = self.handler.create_dubbing_run(session_name=session_name, set_active=True)
        second = self.handler.create_dubbing_run(session_name=session_name, set_active=True)
        first_path = os.path.join(first["run_dir"], "first.srt")
        second_path = os.path.join(second["run_dir"], "second.srt")
        with open(first_path, "w", encoding="utf-8") as file_handle:
            file_handle.write("first")
        with open(second_path, "w", encoding="utf-8") as file_handle:
            file_handle.write("second")

        self.handler.register_dubbing_artifact(first["run_id"], "transcribed_srt", first_path, is_current=True)
        self.handler.register_dubbing_artifact(second["run_id"], "transcribed_srt", second_path, is_current=True)

        selected = self.handler.get_active_dubbing_artifact(session_name, ["transcribed_srt"])
        self.assertEqual(os.path.abspath(second_path), selected)

        self.handler.set_active_dubbing_run(session_name, first["run_id"])
        selected_after_switch = self.handler.get_active_dubbing_artifact(session_name, ["transcribed_srt"])
        self.assertEqual(os.path.abspath(first_path), selected_after_switch)

    def test_get_dubbing_steps_returns_initialized_and_updated_step_states(self):
        session_name = "DubbingSteps"
        self._session_dir(session_name)

        run = self.handler.create_dubbing_run(session_name=session_name, set_active=True)
        steps = self.handler.get_dubbing_steps(run["run_id"])

        self.assertTrue(steps)
        self.assertTrue(all(step["status"] == "pending" for step in steps))

        self.handler.record_dubbing_step(run["run_id"], "transcribe", "running", "Starting")
        self.handler.record_dubbing_step(run["run_id"], "translate", "completed", "Done")

        updated_steps = {
            step["step_key"]: step
            for step in self.handler.get_dubbing_steps(run["run_id"])
        }
        self.assertEqual(updated_steps["transcribe"]["status"], "running")
        self.assertEqual(updated_steps["translate"]["status"], "completed")
        self.assertEqual(updated_steps["translate"]["detail"], "Done")

    def test_list_reusable_sources_dedupes_and_filters_missing_paths(self):
        source_root = os.path.join(self.temp_dir.name, "source_library")
        os.makedirs(source_root, exist_ok=True)

        reusable_source_path = os.path.join(source_root, "shared_source.txt")
        with open(reusable_source_path, "w", encoding="utf-8") as file_handle:
            file_handle.write("Reusable source")
        missing_source_path = os.path.join(source_root, "missing_source.txt")

        session_a = "SourceSessionA"
        session_b = "SourceSessionB"
        session_c = "SourceSessionC"
        self._session_dir(session_a)
        self._session_dir(session_b)
        self._session_dir(session_c)

        payload_base = {"tts": {"service": "XTTS", "language": "en"}}
        self.handler.save_session_config_snapshot(
            session_name=session_a,
            payload={**payload_base, "source_file_path": reusable_source_path},
        )
        self.handler.save_session_config_snapshot(
            session_name=session_b,
            payload={**payload_base, "source_file_path": reusable_source_path},
        )
        self.handler.save_session_config_snapshot(
            session_name=session_c,
            payload={**payload_base, "source_file_path": missing_source_path},
        )

        reusable_rows = self.handler.list_reusable_sources(limit=50, include_missing=False)
        reusable_paths = [str(row.get("source_path") or "") for row in reusable_rows]
        reusable_abs = os.path.abspath(reusable_source_path)
        missing_abs = os.path.abspath(missing_source_path)

        self.assertIn(reusable_abs, reusable_paths)
        self.assertNotIn(missing_abs, reusable_paths)
        self.assertEqual(reusable_paths.count(reusable_abs), 1)

        reusable_rows_with_missing = self.handler.list_reusable_sources(limit=50, include_missing=True)
        reusable_paths_with_missing = [str(row.get("source_path") or "") for row in reusable_rows_with_missing]
        self.assertIn(missing_abs, reusable_paths_with_missing)

    def test_session_source_display_and_audio_versions_are_indexed(self):
        session_name = "AudioVersionSession"
        session_dir = self._session_dir(session_name)
        source_docx = os.path.join(session_dir, "Novel Source.docx")
        internal_txt = os.path.join(session_dir, "Novel Source.txt")
        with open(source_docx, "wb") as file_handle:
            file_handle.write(b"docx")
        with open(internal_txt, "w", encoding="utf-8") as file_handle:
            file_handle.write("text")

        sentences = [
            {"sentence_number": "1", "original_sentence": "One", "tts_generated": "yes"},
            {"sentence_number": "2", "original_sentence": "Two", "tts_generated": "yes"},
        ]
        sentences_path = os.path.join(session_dir, f"{session_name}_sentences.json")
        with open(sentences_path, "w", encoding="utf-8") as file_handle:
            json.dump(sentences, file_handle)
        self.handler.update_session_sentence_index(session_name, sentences, sentences_path)

        source_wavs_dir = os.path.join(session_dir, "Sentence_wavs")
        os.makedirs(source_wavs_dir, exist_ok=True)
        with open(os.path.join(source_wavs_dir, f"{session_name}_sentence_1.wav"), "wb") as file_handle:
            file_handle.write(b"source-one")
        with open(os.path.join(source_wavs_dir, f"{session_name}_sentence_2.wav"), "wb") as file_handle:
            file_handle.write(b"source-two")

        rvc_settings = {"rvc_model": "Bright Voice", "pitch": 0}
        rvc_path = audio_variant_handler.rvc_variant_sentence_path(
            session_dir,
            rvc_settings,
            session_name,
            "1",
            ensure_dir=True,
        )
        with open(rvc_path, "wb") as file_handle:
            file_handle.write(b"rvc-one")
        audio_variant_handler.register_rvc_variant_sentence(session_dir, rvc_settings, "1")

        self.handler.save_session_config_snapshot(
            session_name=session_name,
            payload={
                "session_name": session_name,
                "source_file_path": internal_txt,
                "source_display_path": source_docx,
                "tts": {"service": "XTTS", "language": "en"},
            },
            session_path=session_dir,
        )

        row = next(item for item in self.handler.list_sessions() if item["session_name"] == session_name)
        self.assertEqual(row["source_display_name"], "Novel Source.docx")
        self.assertEqual(os.path.abspath(row["source_display_path"]), os.path.abspath(source_docx))
        self.assertEqual(row["audio_version_count"], 2)
        self.assertIn("RVC", row["audio_version_summary"])
        self.assertIn("partial", row["audio_version_summary"])

        preview = self.handler.get_session_preview(session_name)
        versions = preview["audio_versions"]
        version_ids = {version["variant_id"] for version in versions}
        self.assertIn("source", version_ids)
        rvc_versions = [version for version in versions if version["kind"] == "rvc"]
        self.assertEqual(len(rvc_versions), 1)
        self.assertEqual(rvc_versions[0]["model_name"], "Bright Voice")
        self.assertEqual(rvc_versions[0]["sentence_count"], 1)
        self.assertEqual(rvc_versions[0]["total_sentences"], 2)

    def test_artifact_registration_and_preview(self):
        session_name = "ArtifactsSession"
        self._session_dir(session_name)
        run = self.handler.create_dubbing_run(session_name=session_name, set_active=True)
        run_dir = run["run_dir"]
        os.makedirs(run_dir, exist_ok=True)

        artifacts = {
            "transcribed_srt": "step1.srt",
            "translated_srt": "step2_translated.srt",
            "speech_blocks": "blocks_speech_blocks.json",
            "synced_video": "video_synced.mp4",
            "equalized_srt": "step2_translated_equalized.srt",
            "final_video_soft": "final_soft.mp4",
        }
        for role, filename in artifacts.items():
            path = os.path.join(run_dir, filename)
            with open(path, "wb") as file_handle:
                file_handle.write(role.encode("utf-8"))
            self.handler.register_dubbing_artifact(run["run_id"], role, path, is_current=True)

        preview = self.handler.get_session_preview(session_name)
        self.assertTrue(preview["runs"])
        artifact_roles = {
            artifact["role"]
            for artifact in preview["runs"][0]["artifacts"]
        }
        self.assertTrue(set(artifacts.keys()).issubset(artifact_roles))


if __name__ == "__main__":
    unittest.main()
