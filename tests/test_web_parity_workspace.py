import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import AppSetting, Artifact, AudioTake, GenerationRun
from pandrator.web.workspace import BUILTIN_DEFAULTS, adapt_runtime_settings


class WebParityWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def create_session(self, kind="voiceover"):
        response = self.client.post("/api/v1/sessions", json={"name": "Parity", "workflow_kind": kind}, headers=self.headers)
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def test_session_setting_precedence_and_secret_free_snapshot(self):
        record = self.create_session()
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            session.add(AppSetting(key="defaults.tts", value_json={"service": "Kokoro", "speed": 0.9, "api_key": "not-a-snapshot"}))
            session.add(AppSetting(key="services.tts", value_json={"provider_configs": [{"id": "local", "api_base": "http://127.0.0.1:9000"}], "kokoro_base_url": "http://127.0.0.1:8880"}))
        response = self.client.put(
            f"/api/v1/sessions/{record['id']}/settings/tts",
            json={"value": {"speed": 1.1, "voice": "Ada"}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, response.status_code, response.get_json())
        resolved = self.client.post(
            f"/api/v1/sessions/{record['id']}/settings/resolve",
            json={"sections": ["tts"], "overrides": {"tts": {"speed": 1.2, "max_tokens": 2048}}},
            headers=self.headers,
        ).get_json()
        self.assertEqual("Kokoro", resolved["value"]["tts"]["service"])
        self.assertEqual(1.2, resolved["value"]["tts"]["speed"])
        self.assertEqual("Ada", resolved["value"]["tts"]["voice"])
        self.assertNotIn("api_key", resolved["value"]["tts"])
        self.assertEqual(2048, resolved["value"]["tts"]["max_tokens"])
        self.assertEqual("http://127.0.0.1:8880", resolved["value"]["tts"]["kokoro_base_url"])
        self.assertEqual("local", resolved["value"]["tts"]["provider_configs"][0]["id"])
        self.assertEqual(64, len(resolved["settings_hash"]))

    def test_global_defaults_endpoint_exposes_builtins_and_revisioned_values(self):
        services = self.client.get("/api/v1/services/tts").get_json()
        self.assertEqual("XTTS", services["default_service"])
        self.assertEqual(3, services["builtin_defaults"]["max_attempts"])
        defaults = self.client.get("/api/v1/defaults/subtitles")
        self.assertEqual(200, defaults.status_code, defaults.get_json())
        self.assertEqual(2, defaults.get_json()["builtin"]["max_lines"])
        saved = self.client.put(
            "/api/v1/settings/defaults.subtitles",
            json={"value": {"max_chars_per_line": 52}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())
        effective = self.client.get("/api/v1/defaults/subtitles").get_json()
        self.assertEqual(52, effective["effective"]["max_chars_per_line"])
        self.assertEqual(2, effective["effective"]["max_lines"])

    def test_tts_provider_defaults_fit_between_global_and_session_overrides(self):
        record = self.create_session()
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            session.add(AppSetting(key="defaults.tts", value_json={"service": "Kokoro", "speed": 0.9}))
            session.add(AppSetting(key="services.tts", value_json={"provider_configs": [{"id": "kokoro", "name": "Kokoro", "api_base": "http://127.0.0.1:8880", "default_model": "kokoro", "default_voice": "af_bella", "default_voices": {"kokoro": "af_bella"}, "settings": {"speed": 0.8, "max_attempts": 4}}]}))
        inherited = self.client.post(f"/api/v1/sessions/{record['id']}/settings/resolve", json={"sections": ["tts"]}, headers=self.headers).get_json()["value"]["tts"]
        self.assertEqual(0.8, inherited["speed"])
        self.assertEqual(4, inherited["max_attempts"])
        self.assertEqual("af_bella", inherited["voice"])
        saved = self.client.put(f"/api/v1/sessions/{record['id']}/settings/tts", json={"value": {"speed": 1.1}}, headers={**self.headers, "If-Match": '"0"'})
        self.assertEqual(200, saved.status_code, saved.get_json())
        resolved = self.client.post(f"/api/v1/sessions/{record['id']}/settings/resolve", json={"sections": ["tts"], "overrides": {"tts": {"speed": 1.2}}}, headers=self.headers).get_json()["value"]["tts"]
        self.assertEqual(1.2, resolved["speed"])

    def test_tts_voice_preview_is_a_durable_job_with_selected_catalogue_values(self):
        response = self.client.post(
            "/api/v1/services/tts/kokoro/preview",
            json={"text": "A short preview.", "model": "kokoro", "voice": "af_heart"},
            headers=self.headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())
        payload = response.get_json()["payload_json"]
        self.assertEqual("A short preview.", payload["text"])
        self.assertEqual("Kokoro", payload["settings"]["service"])
        self.assertEqual("kokoro", payload["settings"]["model"])
        self.assertEqual("af_heart", payload["settings"]["voice"])

    def test_web_setting_names_are_adapted_to_runtime_handler_contracts(self):
        tts = adapt_runtime_settings("tts", {"model": "model-a", "voice": "speaker-a", "speed": 1.1})
        self.assertEqual("model-a", tts["xtts_model"])
        self.assertEqual("speaker-a", tts["speaker"])
        self.assertEqual(1.1, tts["speed"])

        subtitles = adapt_runtime_settings(
            "subtitles",
            {"max_chars_per_line": 52, "max_lines": 2, "min_duration_ms": 800, "merge_threshold_ms": 300},
        )
        self.assertEqual(52, subtitles["subtitle_max_chars_per_line"])
        self.assertEqual(2, subtitles["subtitle_max_lines"])
        self.assertEqual(800, subtitles["subtitle_min_duration_ms"])
        self.assertEqual(300, subtitles["subtitle_merge_threshold"])

        rvc = adapt_runtime_settings("rvc", {"enabled": True, "model": "narrator"})
        self.assertTrue(rvc["enable_rvc"])
        self.assertEqual("narrator", rvc["rvc_model"])

    def test_runtime_aliases_do_not_override_explicit_expert_values(self):
        adapted = adapt_runtime_settings("tts", {"model": "web-model", "xtts_model": "explicit-model"})
        self.assertEqual("explicit-model", adapted["xtts_model"])

    def test_web_defaults_match_the_supported_subtitle_and_vad_contract(self):
        self.assertEqual(48, BUILTIN_DEFAULTS["subtitles"]["max_chars_per_line"])
        self.assertEqual(833, BUILTIN_DEFAULTS["subtitles"]["min_duration_ms"])
        self.assertEqual(100, BUILTIN_DEFAULTS["stt"]["crispasr_vad_min_silence_ms"])
        self.assertEqual(300, BUILTIN_DEFAULTS["stt"]["crispasr_vad_max_speech_seconds"])

    def test_outcome_plan_supports_translation_without_correction(self):
        record = self.create_session()
        current = self.client.get(f"/api/v1/sessions/{record['id']}/outcome-plan").get_json()
        plan = current["value"]
        plan["transformations"].update({"transcribe": False, "correct": False, "translate": True, "generate_audio": True})
        plan["inputs"].update({"translation": "source", "generation": "translation"})
        response = self.client.put(
            f"/api/v1/sessions/{record['id']}/outcome-plan",
            json={"value": plan},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, response.status_code, response.get_json())
        keys = [item["key"] for item in response.get_json()["pipeline"]]
        self.assertIn("translate", keys)
        self.assertNotIn("correct", keys)

    def test_translation_parent_selection_is_honored_even_when_correction_exists(self):
        record = self.create_session()
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            source = Artifact(session_id=record["id"], kind="srt", role="upload", relative_path="artifacts/source.srt", metadata_json={"original_filename": "source.srt"})
            correction = Artifact(session_id=record["id"], kind="srt", role="correction", relative_path="artifacts/correction.srt")
            session.add_all([source, correction])
            session.flush()
            source_id = source.id
            correction_id = correction.id
        current = self.client.get(f"/api/v1/sessions/{record['id']}/outcome-plan").get_json()
        current["value"]["inputs"]["translation"] = "source"
        current["value"]["transformations"]["translate"] = True
        saved = self.client.put(
            f"/api/v1/sessions/{record['id']}/outcome-plan",
            json={"value": current["value"]},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())
        queued = self.client.post(f"/api/v1/sessions/{record['id']}/stages/translate/run", json={}, headers=self.headers)
        self.assertEqual(202, queued.status_code, queued.get_json())
        self.assertEqual(source_id, queued.get_json()["payload_json"]["source_artifact_id"])
        self.assertNotEqual(correction_id, queued.get_json()["payload_json"]["source_artifact_id"])

    def test_chunk_upload_is_resumable_and_creates_global_source(self):
        record = self.create_session()
        content = b"abcdefghij"
        initialized = self.client.post(
            "/api/v1/uploads/init",
            json={"filename": "meeting.srt", "size_bytes": len(content), "session_id": record["id"], "sha256": hashlib.sha256(content).hexdigest(), "chunk_size": 1024 * 1024},
            headers=self.headers,
        )
        self.assertEqual(201, initialized.status_code, initialized.get_json())
        upload_id = initialized.get_json()["id"]
        response = self.client.put(
            f"/api/v1/uploads/{upload_id}/chunks/0",
            data=content,
            headers={**self.headers, "Content-Type": "application/octet-stream", "X-Chunk-SHA256": hashlib.sha256(content).hexdigest()},
        )
        self.assertEqual(200, response.status_code, response.get_json())
        status = self.client.get(f"/api/v1/uploads/{upload_id}").get_json()
        self.assertEqual([0], status["received"])
        completed = self.client.post(f"/api/v1/uploads/{upload_id}/complete", headers=self.headers)
        self.assertEqual(201, completed.status_code, completed.get_json())
        sources = self.client.get("/api/v1/sources").get_json()["items"]
        attached = self.client.get(f"/api/v1/sessions/{record['id']}/sources").get_json()["items"]
        self.assertEqual(completed.get_json()["source_asset_id"], sources[0]["id"])
        self.assertTrue(attached[0]["attachment"]["is_current"])

    def test_generation_segment_edits_stale_existing_takes_and_pause_is_safe(self):
        record = self.create_session("audiobook")
        plan = self.client.post(
            f"/api/v1/sessions/{record['id']}/generation-plan",
            json={"segments": [{"text": "First segment."}, {"text": "Second segment."}]},
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())
        listed = self.client.get(f"/api/v1/sessions/{record['id']}/generation-segments").get_json()
        first = listed["items"][0]
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            session.add(AudioTake(generation_segment_id=first["id"], kind="tts", status="completed", is_active=True))
        edited = self.client.patch(
            f"/api/v1/generation-segments/{first['id']}",
            json={"text": "Edited first segment."},
            headers={**self.headers, "If-Match": f'"{first["revision"]}"'},
        )
        self.assertEqual("stale", edited.get_json()["status"])
        with database.session() as session:
            take = session.query(AudioTake).one()
            self.assertEqual("stale", take.status)
        started = self.client.post(f"/api/v1/sessions/{record['id']}/generation-runs", json={}, headers=self.headers)
        self.assertEqual(202, started.status_code, started.get_json())
        paused = self.client.post(f"/api/v1/generation-runs/{started.get_json()['id']}/pause", headers=self.headers)
        self.assertEqual("pausing", paused.get_json()["status"])

    def test_multiple_generation_take_artifacts_remain_current(self):
        record = self.create_session("audiobook")
        paths = self.app.extensions["pandrator"]["paths"]
        service = ArtifactService(self.app.extensions["pandrator"]["database"], paths)
        directory = paths.sessions / record["storage_key"] / "generation"
        directory.mkdir(parents=True, exist_ok=True)
        first = directory / "first.wav"
        second = directory / "second.wav"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        service.register(first, kind="audio", role="generation_take", session_id=record["id"])
        service.register(second, kind="audio", role="generation_take", session_id=record["id"])
        with self.app.extensions["pandrator"]["database"].session() as session:
            states = [item.state for item in session.query(Artifact).filter(Artifact.role == "generation_take").all()]
        self.assertEqual(["current", "current"], states)


if __name__ == "__main__":
    unittest.main()
