from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from flask import Flask
from pydantic import ValidationError

from pandrator.web import audiobook_routes
from pandrator.web.audiobook_openapi import AUDIOBOOK_SCHEMAS, audiobook_paths
from pandrator.web.audiobook_routes import register_audiobook_routes
from pandrator.web.audiobook_schemas import (
    AudiobookSetupConfigureRequest,
    SpeechPlanPreviewRequest,
)
from pandrator.web.idempotency import IdempotencyConflict


REVISION = "a" * 64


class _Reservation:
    def __init__(self, response=None):
        self.response = response
        self.replayed = response is not None
        self.record = SimpleNamespace()


class _Database:
    def __init__(self):
        self.events = []

    @contextmanager
    def session(self):
        self.events.append("session")
        yield object()

    @contextmanager
    def immediate_session(self):
        self.events.append("immediate_session")
        yield object()


class _Idempotency:
    def __init__(self):
        self.events = []
        self.replay = None

    def validate_key(self, key):
        self.events.append(("validate", key))
        if not key:
            raise ValueError("Idempotency-Key is required.")

    def begin(self, session, **kwargs):
        self.events.append(("begin", kwargs))
        return _Reservation(self.replay)

    def complete(self, session, reservation, **kwargs):
        self.events.append(("complete", kwargs))


class _Guards:
    def __init__(self):
        self.scopes = []

    def require_scope(self, scope):
        self.scopes.append(scope)

        def decorator(function):
            return function

        return decorator

    def principal(self):
        return SimpleNamespace(subject="test")

    @staticmethod
    def error_response(code, message, status, details=None):
        from flask import jsonify

        return jsonify({"error": {"code": code, "message": message}}), status


def _client(monkeypatch):
    database = _Database()
    idempotency = _Idempotency()
    guards = _Guards()
    services = SimpleNamespace(database=database, idempotency=idempotency)
    app = Flask(__name__)
    app.testing = True
    register_audiobook_routes(
        app,
        SimpleNamespace(services=services, guards=guards),
    )
    return app.test_client(), database, idempotency, guards


def test_request_models_are_strict_and_bounded():
    payload = AudiobookSetupConfigureRequest(expected_revision=REVISION, mode="single_voice")
    assert payload.expected_revision == REVISION
    assert SpeechPlanPreviewRequest(
        revision_id=" revision ", segment_id="segment", include_request=True
    ).revision_id == "revision"
    with pytest.raises(ValidationError):
        AudiobookSetupConfigureRequest(
            expected_revision=REVISION,
            mode="single_voice",
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        AudiobookSetupConfigureRequest(expected_revision="short", mode="single_voice")
    with pytest.raises(ValidationError):
        SpeechPlanPreviewRequest(revision_id=" ", segment_id="segment")


def test_openapi_exposes_all_audiobook_contracts():
    assert set(AUDIOBOOK_SCHEMAS) == {
        "AudiobookSetupConfigureRequest",
        "SpeechPlanPreviewRequest",
    }
    paths = audiobook_paths()
    setup = paths["/api/v1/sessions/{sessionId}/audiobook-setup"]
    preview = paths["/api/v1/sessions/{sessionId}/speech-plan/preview"]["post"]
    assert set(setup) == {"get", "patch"}
    assert setup["patch"]["parameters"][1]["required"] is True
    assert setup["patch"]["requestBody"]["required"] is True
    assert preview["security"][-1] == {"nativeOAuth": ["app.read"]}
    assert preview["requestBody"]["required"] is True


def test_setup_patch_validates_session_before_reserving(monkeypatch):
    client, database, idempotency, _guards = _client(monkeypatch)
    calls = []

    def snapshot(_services, _session, session_id):
        calls.append(("snapshot", session_id))
        return {"configuration_revision": REVISION}

    def configure(_services, _session, session_id, **payload):
        calls.append(("configure", session_id, payload))
        return {"session_id": session_id, "configuration_revision": REVISION}

    monkeypatch.setattr(audiobook_routes, "get_audiobook_setup", snapshot)
    monkeypatch.setattr(audiobook_routes, "configure_audiobook_setup", configure)
    response = client.patch(
        "/api/v1/sessions/session-1/audiobook-setup",
        json={"expected_revision": REVISION, "mode": "multi_voice"},
        headers={"Idempotency-Key": "audiobook-setup-1"},
    )
    assert response.status_code == 200
    assert calls == [
        ("snapshot", "session-1"),
        (
            "configure",
            "session-1",
            {"expected_revision": REVISION, "mode": "multi_voice"},
        ),
    ]
    assert idempotency.events[1][0] == "begin"
    assert database.events == ["immediate_session"]


def test_setup_patch_replays_completed_idempotency(monkeypatch):
    client, _database, idempotency, _guards = _client(monkeypatch)
    idempotency.replay = ({"configuration_revision": REVISION}, 200)
    monkeypatch.setattr(
        audiobook_routes,
        "get_audiobook_setup",
        lambda *_args: {"configuration_revision": REVISION},
    )
    response = client.patch(
        "/api/v1/sessions/session-1/audiobook-setup",
        json={"expected_revision": REVISION, "mode": "single_voice"},
        headers={"Idempotency-Key": "audiobook-setup-2"},
    )
    assert response.status_code == 200
    assert response.headers["Idempotency-Replayed"] == "true"
    assert response.json == {"configuration_revision": REVISION}


def test_preview_is_read_only_and_maps_revision_conflict(monkeypatch):
    client, _database, idempotency, _guards = _client(monkeypatch)
    idempotency.events.clear()
    monkeypatch.setattr(
        audiobook_routes,
        "preview_speech_segment",
        lambda *_args, **kwargs: {
            "segment_id": kwargs["segment_id"],
            "compilation_only": True,
        },
    )
    response = client.post(
        "/api/v1/sessions/session-1/speech-plan/preview",
        json={"revision_id": "revision", "segment_id": "segment"},
    )
    assert response.status_code == 200
    assert response.json["compilation_only"] is True
    assert idempotency.events == []

    monkeypatch.setattr(
        audiobook_routes,
        "preview_speech_segment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            audiobook_routes.RevisionConflict("Refresh the selected revision.")
        ),
    )
    conflict = client.post(
        "/api/v1/sessions/session-1/speech-plan/preview",
        json={"revision_id": "revision", "segment_id": "segment"},
    )
    assert conflict.status_code == 409
    assert conflict.json["error"]["code"] == "revision_conflict"


def test_setup_conflict_and_missing_key_are_standard_errors(monkeypatch):
    client, _database, _idempotency, _guards = _client(monkeypatch)
    missing = client.patch(
        "/api/v1/sessions/session-1/audiobook-setup",
        json={"expected_revision": REVISION, "mode": "single_voice"},
    )
    assert missing.status_code == 400
    assert missing.json["error"]["code"] == "idempotency_key_required"

    monkeypatch.setattr(
        audiobook_routes,
        "get_audiobook_setup",
        lambda *_args: {"configuration_revision": REVISION},
    )
    monkeypatch.setattr(
        audiobook_routes,
        "configure_audiobook_setup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            IdempotencyConflict("same key had different arguments")
        ),
    )
    conflict = client.patch(
        "/api/v1/sessions/session-1/audiobook-setup",
        json={"expected_revision": REVISION, "mode": "single_voice"},
        headers={"Idempotency-Key": "audiobook-setup-3"},
    )
    assert conflict.status_code == 409
    assert conflict.json["error"]["code"] == "idempotency_conflict"
