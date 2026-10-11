"""Disposable authenticated API/static server for Playwright."""

from __future__ import annotations

import atexit
import base64
import io
import json
import os
import shutil
import tempfile
import threading
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from waitress import serve

from pandrator.web.api import create_app
from pandrator.web.auth import AuthService
from pandrator.web.jobs import JobQueue, Worker, noop_handler
from pandrator.web.workflow_handlers import WorkflowHandlers

root = Path(tempfile.mkdtemp(prefix="pandrator-playwright-"))
atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
# Browser tests must not initialize GPU drivers on the host desktop. Tests
# that need a hardware-specific UI state supply their own capability fixture.
gpu_probe = patch(
    "pandrator.web.capabilities.probe_gpu",
    return_value={"available": False, "devices": [], "guidance": "CPU-only browser test workspace."},
)
gpu_probe.start()
atexit.register(gpu_probe.stop)
app = create_app(data_root=root, testing=False)
AuthService(app.extensions["pandrator"]["database"]).initialize_owner("pandrator-e2e")
database = app.extensions["pandrator"]["database"]
paths = app.extensions["pandrator"]["paths"]

# Opt-in fixture for exercising the real Google creation route, durable worker,
# SQLite readback and UI without cloud credentials or provider calls.
if os.environ.get("PANDRATOR_GOOGLE_TTS_E2E_FIXTURE") == "1":
    from pandrator.web.models import AppSetting
    from pandrator.web.tts_provider_contracts import TtsHealth

    os.environ["PANDRATOR_E2E_GEMINI_KEY"] = "pandrator-browser-fixture-key"
    os.environ["PANDRATOR_E2E_VERTEX_JSON"] = json.dumps({"project_id": "pandrator-browser-fixture"})
    with database.session() as db_session:
        db_session.add(AppSetting(key="services.tts", value_json={"provider_configs": [
            {"id": "gemini", "api_key_env": "PANDRATOR_E2E_GEMINI_KEY"},
            {"id": "vertex_ai", "api_key_env": "PANDRATOR_E2E_VERTEX_JSON", "vertex_project": "pandrator-browser-fixture", "vertex_location": "global"},
        ]}, revision=1))

    fixture_voices: dict[str, list[dict]] = {"gemini": [], "vertex_ai": []}
    wav_output = io.BytesIO()
    with wave.open(wav_output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(b"\0\0" * 24000 * 5)
    fixture_audio = base64.b64encode(wav_output.getvalue()).decode()

    def create_fixture_google_voice(endpoint, **options):
        provider = "vertex_ai" if "aiplatform.googleapis.com" in endpoint else "gemini"
        record = {
            "id": f"voice_e2e_{uuid.uuid4().hex}", "display_name": options["display_name"],
            "language_code": options["language_code"], "type": "prompted",
            "expire_time": (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(),
        }
        fixture_voices[provider].append(record)
        return {**record, "sample_audio": {"data": fixture_audio, "mime_type": "audio/wav"},
                "usage": {"total_input_tokens": 220, "total_output_tokens": 1100}}

    def list_fixture_google_voices(endpoint, **_options):
        provider = "vertex_ai" if "aiplatform.googleapis.com" in endpoint else "gemini"
        return [{"id": "Kore", "type": "prebuilt"}, *fixture_voices[provider]]

    google_patches = [
        patch("pandrator.logic.google_tts_catalogue_http.create_google_voice", side_effect=create_fixture_google_voice),
        patch("pandrator.web.tts_providers.get_google_voice_catalog", side_effect=list_fixture_google_voices),
        patch("pandrator.web.tts_providers.GoogleTtsAdapter.health", return_value=TtsHealth(True, True)),
        patch("pandrator.logic.tts_handler._vertex_access_token", return_value=("pandrator-browser-fixture-token", "pandrator-browser-fixture")),
    ]
    for google_patch in google_patches:
        google_patch.start()
        atexit.register(google_patch.stop)

worker = Worker(
    JobQueue(database),
    "playwright-worker",
    {"noop": noop_handler, **WorkflowHandlers(database, paths).handlers()},
)
worker_thread = threading.Thread(target=worker.run_forever, kwargs={"poll_interval": 0.05}, daemon=True)
worker_thread.start()


def stop_worker() -> None:
    worker.stop()
    worker_thread.join(timeout=3)


atexit.register(stop_worker)
# Playwright creates a fresh browser context for every test and runs both
# browser projects against this one disposable server. Waitress otherwise
# retains inactive keep-alive channels for 120 seconds and reaches its default
# 100-channel ceiling late in the suite, even though the pages have closed.
serve(
    app,
    host="127.0.0.1",
    port=8098,
    # Match the production CLI default: event streams also occupy worker threads.
    threads=12,
    connection_limit=512,
    channel_timeout=15,
    cleanup_interval=5,
)
