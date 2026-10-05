"""Actual synthesis dispatch/payload/decode against a disposable local server."""

from __future__ import annotations

import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from pandrator.logic import tts_handler


@pytest.fixture
def speech_server():
    body = io.BytesIO()
    with wave.open(body, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(22050)
        output.writeframes(b"\0\0" * 441)
    audio = body.getvalue()
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append((self.path, payload))
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests_seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("service", ["audio_cpp", "audio.cpp", "audio-cpp", "audiocpp"])
def test_audio_cpp_alias_reaches_actual_synthesis_request_and_decoder(speech_server, service):
    base_url, seen = speech_server
    settings = {
        "service": service,
        "model": "fireredtts3_instruct_q8_0",
        "generation_prompt": "Quiet baritone",
        "language": "en",
    }
    original = dict(settings)
    with requests.Session() as session:
        session.trust_env = False
        audio = tts_handler.text_to_audio(
            "Hello.\n   Again.",
            settings,
            audio_cpp_base_url=base_url,
            request_session=session,
            max_attempts=1,
            # Endpoint/local execution ownership is qualified separately. This
            # exercises the real dispatch, payload, HTTP and WAV decoder.
            _audio_cpp_lock_held=True,
        )
    assert audio is not None and len(audio) == 20
    assert settings == original
    assert len(seen) == 1
    assert seen[0][0] == "/v1/audio/speech"
    assert seen[0][1]["model"] == settings["model"]
    assert seen[0][1]["input"] == "Hello. Again."
