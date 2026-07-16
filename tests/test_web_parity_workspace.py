import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import AppSetting, Artifact, AudioTake, GenerationRun, GenerationSegment, Job
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

    def create_session(self, kind="voiceover", name="Parity"):
        response = self.client.post("/api/v1/sessions", json={"name": name, "workflow_kind": kind}, headers=self.headers)
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

    def test_subtitle_workspace_inherits_document_export_defaults(self):
        subtitle = self.create_session(kind="subtitles")
        output = self.client.get(f"/api/v1/sessions/{subtitle['id']}/settings/output").get_json()
        self.assertEqual("subtitles", output["effective"]["export_mode"])
        self.assertEqual("source", output["effective"]["subtitle_selection"])
        self.assertEqual("none", output["effective"]["subtitle_mode"])

        saved = self.client.put(
            f"/api/v1/sessions/{subtitle['id']}/settings/output",
            json={"value": {"export_mode": "text"}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())
        self.assertEqual("text", saved.get_json()["effective"]["export_mode"])
        coerced = self.client.put(
            f"/api/v1/sessions/{subtitle['id']}/settings/output",
            json={"value": {"export_mode": "media", "audio_mode": "mixed", "subtitle_mode": "burned"}},
            headers={**self.headers, "If-Match": '"1"'},
        )
        self.assertEqual("subtitles", coerced.get_json()["effective"]["export_mode"])
        self.assertEqual("preserve", coerced.get_json()["effective"]["audio_mode"])
        self.assertEqual("none", coerced.get_json()["effective"]["subtitle_mode"])

        voiceover = self.create_session(kind="voiceover", name="Voiceover defaults")
        voiceover_output = self.client.get(f"/api/v1/sessions/{voiceover['id']}/settings/output").get_json()
        self.assertEqual("media", voiceover_output["effective"]["export_mode"])

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

    def test_tts_language_default_voice_is_resolved_before_generic_default(self):
        record = self.create_session()
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            session.add(AppSetting(key="defaults.tts", value_json={"service": "Kokoro", "language": "en-gb"}))
            session.add(AppSetting(key="services.tts", value_json={"provider_configs": [{
                "id": "kokoro", "name": "Kokoro", "api_base": "http://127.0.0.1:8880",
                "default_model": "kokoro", "default_voice": "af_heart",
                "default_voices": {"kokoro": "af_heart"},
                "default_voices_by_language": {"kokoro": {"en-gb": "bf_alice"}},
            }]}))
        resolved = self.client.post(
            f"/api/v1/sessions/{record['id']}/settings/resolve",
            json={"sections": ["tts"]}, headers=self.headers,
        ).get_json()["value"]["tts"]
        self.assertEqual("bf_alice", resolved["voice"])

    def test_tts_voice_preview_is_a_durable_job_with_selected_catalogue_values(self):
        response = self.client.post(
            "/api/v1/services/tts/kokoro/preview",
            json={"text": "A short preview.", "model": "kokoro", "voice": "af_heart", "language": "en"},
            headers=self.headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())
        payload = response.get_json()["payload_json"]
        self.assertEqual("A short preview.", payload["text"])
        self.assertEqual("Kokoro", payload["settings"]["service"])
        self.assertEqual("kokoro", payload["settings"]["model"])
        self.assertEqual("af_heart", payload["settings"]["voice"])
        self.assertEqual("en", payload["settings"]["language"])

    def test_qwen_prebuilt_preview_always_selects_customvoice_model(self):
        response = self.client.post(
            "/api/v1/services/tts/kobold_qwen/preview",
            json={"text": "A Qwen preview.", "model": "Voice Cloning", "voice": "Ryan", "language": "en"},
            headers=self.headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())
        settings = response.get_json()["payload_json"]["settings"]
        self.assertEqual("Prebuilt Voices", settings["model"])
        self.assertEqual("Prebuilt Voices", settings["xtts_model"])
        self.assertEqual("Ryan", settings["voice"])
        catalogue = self.client.get("/api/v1/services/tts").get_json()
        qwen = next(item for item in catalogue["services"] if item["id"] == "kobold_qwen")
        self.assertEqual(
            {"Aiden", "Dylan", "Eric", "Ono_Anna", "Ryan", "Serena", "Sohee", "Uncle_Fu", "Vivian"},
            set(qwen["voice_catalogues"]["Prebuilt Voices"]),
        )
        self.assertEqual(["kobo"], qwen["voice_catalogues"]["Voice Cloning"])
        self.assertEqual("kobo", qwen["default_voices"]["Voice Cloning"])
        self.assertIn("Prebuilt Voices", qwen["generation_prompt_models"])
        self.assertNotIn("Voice Cloning", qwen["generation_prompt_models"])

    def test_tts_catalogue_restores_managed_previews_and_marks_unavailable_services(self):
        extension = self.app.extensions["pandrator"]
        preview_path = extension["paths"].artifacts / "tts-previews" / "persisted.wav"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(b"RIFFpreview")
        preview = extension["artifacts"].register(
            preview_path,
            kind="audio",
            role="tts_voice_preview",
            metadata={
                "service_id": "kokoro",
                "model": "kokoro",
                "voice": "af_heart",
                "language": "en-us",
                "preview_text": "Persist me.",
            },
        )
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}, clear=False):
            response = self.client.get("/api/v1/services/tts?refresh=true")
        self.assertEqual(200, response.status_code, response.get_json())
        payload = response.get_json()
        restored = next(item for item in payload["previews"] if item["artifact_id"] == preview.id)
        self.assertEqual("af_heart", restored["voice"])
        self.assertEqual("Persist me.", restored["preview_text"])
        kokoro = next(item for item in payload["services"] if item["id"] == "kokoro")
        self.assertFalse(kokoro["available"])
        self.assertEqual("Service is not running", kokoro["availability_reason"])
        openai = next(item for item in payload["services"] if item["id"] == "openai")
        self.assertFalse(openai["available"])
        self.assertEqual("API key not configured", openai["availability_reason"])

    def test_silero_refresh_exposes_installed_models_and_language_voice_metadata(self):
        model_catalog = [
            {
                "id": "v5_cis_base_nostress",
                "status": {"installed": True},
                "license": {"id": "MIT", "commercial_use_allowed": True},
            },
            {
                "id": "v5_cis_ext",
                "status": {"installed": False},
                "license": {"id": "CC-BY-NC-SA-4.0", "commercial_use_allowed": False},
            },
        ]
        voices = [
            {
                "id": "ukr_igor",
                "display_name": "Igor",
                "language": "ukr",
                "language_name": "Ukrainian",
                "model": "v5_cis_base_nostress",
                "available": True,
            }
        ]
        with patch("socket.create_connection"), patch(
            "pandrator.logic.tts_handler.get_silero_model_catalog",
            return_value=model_catalog,
        ), patch(
            "pandrator.logic.tts_handler.get_silero_voice_catalog",
            return_value=voices,
        ):
            response = self.client.get("/api/v1/services/tts?refresh=true")

        self.assertEqual(200, response.status_code, response.get_json())
        silero = next(
            item for item in response.get_json()["services"] if item["id"] == "silero"
        )
        self.assertEqual(["v5_cis_base_nostress"], silero["models"])
        self.assertEqual(
            ["ukr_igor"],
            silero["voice_catalogues"]["v5_cis_base_nostress"],
        )
        self.assertEqual(
            "ukr_igor",
            silero["default_voices_by_language"]["v5_cis_base_nostress"]["ukr"],
        )
        self.assertEqual(
            "Igor",
            silero["voice_metadata"]["v5_cis_base_nostress:ukr_igor"]["display_name"],
        )

    def test_generate_run_adapts_flat_web_service_ids_after_stage_overrides(self):
        record = self.create_session("audiobook")
        uploaded = self.client.post(
            "/api/v1/uploads",
            data={"session_id": record["id"], "file": (io.BytesIO(b"A short chapter."), "book.txt")},
            headers=self.headers,
            content_type="multipart/form-data",
        )
        self.assertEqual(201, uploaded.status_code, uploaded.get_json())
        queued = self.client.post(
            f"/api/v1/sessions/{record['id']}/stages/generate_audio/run",
            json={
                "service": "kokoro",
                "tts_service": "kokoro",
                "stage_settings": {"generate_audio": {"service": "kokoro", "tts_service": "kokoro"}},
            },
            headers=self.headers,
        )
        self.assertEqual(202, queued.status_code, queued.get_json())
        payload = queued.get_json()["payload_json"]
        self.assertEqual("Kokoro", payload["settings"]["service"])
        self.assertEqual("Kokoro", payload["settings"]["tts_service"])
        self.assertEqual("Kokoro", payload["stage_settings"]["generate_audio"]["service"])
        self.assertEqual("Kokoro", payload["stage_settings"]["generate_audio"]["tts_service"])

    def test_web_setting_names_are_adapted_to_runtime_handler_contracts(self):
        tts = adapt_runtime_settings("tts", {"service": "kokoro", "model": "model-a", "voice": "speaker-a", "speed": 1.1})
        self.assertEqual("Kokoro", tts["service"])
        self.assertEqual("Kokoro", tts["tts_service"])
        self.assertEqual("http://127.0.0.1:8880", tts["kokoro_base_url"])
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
        self.assertEqual(800, BUILTIN_DEFAULTS["stt"]["crispasr_vad_min_silence_ms"])
        self.assertEqual(300, BUILTIN_DEFAULTS["stt"]["crispasr_vad_max_speech_seconds"])
        self.assertEqual("libx264", BUILTIN_DEFAULTS["output"]["burn_video_encoder"])
        self.assertEqual(18, BUILTIN_DEFAULTS["output"]["burn_video_quality"])

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
            json={"segments": [{"text": "First segment.", "source_segment_ids": ["source-paragraph-1"]}, {"text": "Second segment.", "paragraph_break_after": True}]},
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())
        listed = self.client.get(f"/api/v1/sessions/{record['id']}/generation-segments").get_json()
        first = listed["items"][0]
        self.assertEqual(["source-paragraph-1"], first["source_segment_ids"])
        self.assertFalse(first["paragraph_break_after"])
        self.assertTrue(listed["items"][1]["paragraph_break_after"])
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

    def test_artifact_context_exposes_textual_parent_for_comparison(self):
        record = self.create_session("audiobook")
        extension = self.app.extensions["pandrator"]
        paths = extension["paths"]
        artifacts = extension["artifacts"]
        source_path = paths.uploads / "raw-source.txt"
        source_path.write_text("Raw source text", encoding="utf-8")
        source = artifacts.register(source_path, kind="text", role="extracted_text", session_id=record["id"])
        cleaned_path = paths.sessions / "cleaned-preview.txt"
        cleaned_path.write_text("Cleaned source text", encoding="utf-8")
        cleaned = artifacts.register(cleaned_path, kind="text", role="clean_text", session_id=record["id"], parent_ids=[source.id])

        response = self.client.get(f"/api/v1/artifacts/{cleaned.id}/context")

        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual(cleaned.id, response.get_json()["artifact"]["id"])
        self.assertEqual(source.id, response.get_json()["parents"][0]["id"])

    def test_latest_generation_run_includes_its_worker_error(self):
        record = self.create_session("audiobook")
        self.client.post(
            f"/api/v1/sessions/{record['id']}/generation-plan",
            json={"segments": [{"text": "A failed synthesis."}]},
            headers=self.headers,
        )
        started = self.client.post(f"/api/v1/sessions/{record['id']}/generation-runs", json={}, headers=self.headers).get_json()
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            run = session.get(GenerationRun, started["id"])
            job = session.get(Job, started["job_id"])
            run.status = "failed"
            job.status = "failed"
            job.progress = 0.42
            job.error_message = "The speech endpoint rejected the request."
        latest = self.client.get(f"/api/v1/sessions/{record['id']}/generation-runs/latest").get_json()["item"]
        self.assertEqual("failed", latest["status"])
        self.assertEqual(0.42, latest["progress"])
        self.assertEqual("The speech endpoint rejected the request.", latest["error_message"])

    def test_generation_runs_have_readable_labels_and_can_be_deleted_with_their_takes(self):
        record = self.create_session("audiobook")
        self.client.post(
            f"/api/v1/sessions/{record['id']}/generation-plan",
            json={"segments": [{"text": "One narrated sentence."}]},
            headers=self.headers,
        )
        first = self.client.post(
            f"/api/v1/sessions/{record['id']}/generation-runs",
            json={"run_override": {"tts": {"service": "Kokoro", "model": "v1", "voice": "Ada"}}},
            headers=self.headers,
        ).get_json()
        second = self.client.post(
            f"/api/v1/sessions/{record['id']}/generation-runs",
            json={"operation": "rvc", "run_override": {"rvc": {"model": "narrator-v2"}}},
            headers=self.headers,
        ).get_json()
        database = self.app.extensions["pandrator"]["database"]
        paths = self.app.extensions["pandrator"]["paths"]
        take_path = paths.sessions / record["storage_key"] / "generation" / "first.wav"
        take_path.parent.mkdir(parents=True, exist_ok=True)
        take_path.write_bytes(b"test audio")
        artifact = self.app.extensions["pandrator"]["artifacts"].register(
            take_path,
            kind="audio",
            role="generation_take",
            session_id=record["id"],
        )
        with database.session() as session:
            session.get(GenerationRun, first["id"]).status = "completed"
            session.get(GenerationRun, second["id"]).status = "completed"
            segment = session.query(GenerationSegment).one()
            session.add(
                AudioTake(
                    generation_segment_id=segment.id,
                    generation_run_id=first["id"],
                    artifact_id=artifact.id,
                    kind="tts",
                    status="completed",
                    is_active=True,
                )
            )

        runs = self.client.get(f"/api/v1/sessions/{record['id']}/generation-runs").get_json()["items"]
        self.assertEqual([2, 1], [item["sequence_number"] for item in runs])
        self.assertEqual("Run 1: Kokoro · v1 · Ada", runs[1]["label"])
        self.assertIn("RVC narrator-v2", runs[0]["label"])
        self.assertEqual(1, runs[1]["take_count"])

        deleted = self.client.delete(f"/api/v1/generation-runs/{first['id']}", headers=self.headers)
        self.assertEqual(204, deleted.status_code)
        self.assertFalse(take_path.exists())
        remaining = self.client.get(f"/api/v1/sessions/{record['id']}/generation-runs").get_json()["items"]
        self.assertEqual([second["id"]], [item["id"] for item in remaining])
        with database.session() as session:
            self.assertEqual(0, session.query(AudioTake).count())
            self.assertIsNone(session.get(Artifact, artifact.id))

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
