from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from flask import Flask
from pydantic import ValidationError

from pandrator.web import audiobook_routes
from pandrator.web.audiobook_openapi import VOICE_SETUP_SCHEMAS, audiobook_paths
from pandrator.web.audiobook_routes import register_audiobook_routes
from pandrator.web.audiobook_schemas import VoiceSetupConfigureRequest
from pandrator.web.database import Database
from pandrator.web.generation_controls import (
    get_generation_controls,
    save_generation_controls,
)
from pandrator.web.models import OutcomePlan, SessionSetting
from pandrator.web.sessions import SessionService
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.voice_setup import configure_voice_setup, get_voice_setup
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root

REVISION = "a" * 64


class _Reservation:
    def __init__(self, response=None):
        self.response = response
        self.replayed = response is not None
        self.record = SimpleNamespace()


class _DatabaseStub:
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


class _IdempotencyStub:
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


class _GuardsStub:
    def __init__(self):
        self.scopes = []

    def require_scope(self, scope):
        self.scopes.append(scope)

        def decorate(function):
            return function

        return decorate

    @staticmethod
    def principal():
        return SimpleNamespace(subject="test")

    @staticmethod
    def error_response(code, message, status, details=None):
        from flask import jsonify

        return jsonify({"error": {"code": code, "message": message}}), status


class VoiceSetupTests:
    @pytest.fixture(autouse=True)
    def setup_database(self, tmp_path):
        paths = prepare_web_test_data_root(tmp_path)
        self.database = Database(paths.database)
        self.settings = WorkspaceSettingsService(self.database)
        self.services = SimpleNamespace(workspace_settings=self.settings)
        sessions = SessionService(self.database)
        self.audiobook = sessions.create("Voice setup audiobook")
        self.voiceover = sessions.create(
            "Voice setup voiceover", workflow_kind="voiceover"
        )
        yield
        self.database.dispose()

    def read(self, session_id):
        with self.database.session() as session:
            return get_voice_setup(self.services, session, session_id)

    def configure(self, session_id, mode, expected_revision=None):
        current = self.read(session_id)
        token = expected_revision or current["configuration_revision"]
        with self.database.immediate_session() as session:
            return configure_voice_setup(
                self.services,
                session,
                session_id,
                expected_revision=token,
                mode=mode,
            )

    @pytest.mark.parametrize("workflow", ["audiobook", "voiceover"])
    def test_get_supports_both_workflows_and_reports_legacy_adoption(self, workflow):
        record = self.audiobook if workflow == "audiobook" else self.voiceover
        setup = self.read(record.id)

        assert setup["session_id"] == record.id
        assert setup["workflow_kind"] == workflow
        assert setup["mode"] == "single_voice"
        assert setup["casting_enabled"] is False
        assert setup["legacy_voice_overrides"] is False
        assert set(setup["tts"]) == {"service", "model", "voice", "language"}
        assert len(setup["configuration_revision"]) == 64

        current = self.settings.get(record.id, "tts")
        self.settings.update(
            record.id,
            "tts",
            current["revision"],
            {"voice_mode_version": 0},
        )
        assert self.read(record.id)["legacy_voice_overrides"] is True

    def test_multi_voice_changes_only_casting_and_mode_marker(self):
        record = self.voiceover
        with self.database.session() as session:
            controls_before = save_generation_controls(
                session,
                record.id,
                expected_revision=0,
                characters=[
                    {
                        "id": "c-manual",
                        "display_name": "Manual speaker",
                        "voice_category": "unspecified",
                    }
                ],
                cast={
                    "characters": {"c-manual": {"voice": "ManualVoice"}},
                    "source_speakers": {"S1": {"voice": "SourceVoice"}},
                },
            )
        text_before = self.settings.get(record.id, "text")
        self.settings.update(
            record.id,
            "text",
            text_before["revision"],
            {
                "llm_tts_document_optimization": False,
                "llm_tts_annotation_mode": "off",
                "llm_tts_annotation_only": False,
                "editorial_marker": "preserve",
            },
        )
        tts_before = self.settings.get(record.id, "tts")
        self.settings.update(
            record.id,
            "tts",
            tts_before["revision"],
            {
                "casting_enabled": False,
                "voice_mode_version": 1,
                "service": "gemini",
                "model": "model-x",
                "voice": "base-voice",
                "language": "en",
                "performance_enabled": True,
                "generation_prompt": "Keep this direction.",
            },
        )
        baseline = self.settings.get(record.id, "tts")
        text_baseline = self.settings.get(record.id, "text")
        expected_revision = self.read(record.id)["configuration_revision"]

        result = self.configure(record.id, "multi_voice", expected_revision)

        assert result["mode"] == "multi_voice"
        assert result["legacy_voice_overrides"] is False
        text_after = self.settings.get(record.id, "text")
        tts_after = self.settings.get(record.id, "tts")
        assert text_after["revision"] == text_baseline["revision"]
        assert text_after["effective"] == text_baseline["effective"]
        assert tts_after["effective"]["casting_enabled"] is True
        assert tts_after["effective"]["voice_mode_version"] == 1
        for key in (
            "service",
            "model",
            "voice",
            "language",
            "performance_enabled",
            "generation_prompt",
        ):
            assert tts_after["effective"][key] == baseline["effective"][key]
        assert tts_after["revision"] == baseline["revision"] + 1
        with self.database.session() as session:
            controls_after = get_generation_controls(session, record.id)
            assert controls_after["revision"] == controls_before["revision"]
            assert controls_after["characters"] == controls_before["characters"]
            assert controls_after["cast"] == controls_before["cast"]
            assert session.get(OutcomePlan, record.id) is None
            stored = session.get(SessionSetting, (record.id, "tts"))
            assert stored.value_json["performance_enabled"] is True

        back_to_single = self.configure(
            record.id,
            "single_voice",
            result["configuration_revision"],
        )
        assert back_to_single["casting_enabled"] is False
        assert self.settings.get(record.id, "text")["effective"] == text_baseline[
            "effective"
        ]

    def test_stale_revision_rejects_write_without_changing_voice_mode(self):
        record = self.audiobook
        tts = self.settings.get(record.id, "tts")
        self.settings.update(
            record.id,
            "tts",
            tts["revision"],
            {"voice_mode_version": 0, "casting_enabled": False},
        )
        stale = self.read(record.id)["configuration_revision"]
        tts = self.settings.get(record.id, "tts")
        self.settings.update(
            record.id,
            "tts",
            tts["revision"],
            {"language": "pl"},
        )

        with pytest.raises(RevisionConflict):
            self.configure(record.id, "multi_voice", stale)

        current = self.settings.get(record.id, "tts")
        assert current["effective"]["casting_enabled"] is False
        assert current["effective"]["voice_mode_version"] == 0
        assert current["effective"]["language"] == "pl"

    def test_non_voice_workflow_is_rejected(self):
        record = SessionService(self.database).create(
            "Not a voice setup", workflow_kind="subtitles"
        )
        with self.database.session() as session:
            with pytest.raises(ValueError, match="audiobook and voiceover"):
                get_voice_setup(self.services, session, record.id)


def _route_client(monkeypatch):
    database = _DatabaseStub()
    idempotency = _IdempotencyStub()
    guards = _GuardsStub()
    services = SimpleNamespace(database=database, idempotency=idempotency)
    app = Flask(__name__)
    app.testing = True
    register_audiobook_routes(app, SimpleNamespace(services=services, guards=guards))
    return app.test_client(), database, idempotency, guards


def test_voice_setup_request_and_openapi_contract():
    request = VoiceSetupConfigureRequest(
        expected_revision=REVISION,
        mode="multi_voice",
    )
    assert request.model_dump() == {
        "expected_revision": REVISION,
        "mode": "multi_voice",
    }
    with pytest.raises(ValidationError):
        VoiceSetupConfigureRequest(
            expected_revision=REVISION,
            mode="multi_voice",
            unexpected=True,
        )
    assert set(VOICE_SETUP_SCHEMAS) == {"VoiceSetupConfigureRequest"}
    documented = audiobook_paths()["/api/v1/sessions/{sessionId}/voice-setup"]
    assert documented["get"]["operationId"] == "getVoiceSetup"
    assert documented["patch"]["operationId"] == "configureVoiceSetup"
    assert documented["patch"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"] == "#/components/schemas/VoiceSetupConfigureRequest"


def test_voice_setup_route_uses_shared_idempotency_contract(monkeypatch):
    client, database, idempotency, guards = _route_client(monkeypatch)
    calls = []

    def snapshot(_services, _session, session_id):
        calls.append(("snapshot", session_id))
        return {"session_id": session_id, "configuration_revision": REVISION}

    def configure(_services, _session, session_id, **payload):
        calls.append(("configure", session_id, payload))
        return {"session_id": session_id, "configuration_revision": REVISION}

    monkeypatch.setattr(audiobook_routes, "get_voice_setup", snapshot)
    monkeypatch.setattr(audiobook_routes, "configure_voice_setup", configure)
    response = client.patch(
        "/api/v1/sessions/session-1/voice-setup",
        json={"expected_revision": REVISION, "mode": "multi_voice"},
        headers={"Idempotency-Key": "voice-setup-1"},
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
    begin = next(event[1] for event in idempotency.events if event[0] == "begin")
    assert begin["operation_id"] == "configureVoiceSetup"
    assert database.events == ["immediate_session"]
    assert "app.read" in guards.scopes
    assert "app.write" in guards.scopes

    idempotency.replay = ({"session_id": "session-1", "replayed": True}, 200)
    replay = client.patch(
        "/api/v1/sessions/session-1/voice-setup",
        json={"expected_revision": REVISION, "mode": "multi_voice"},
        headers={"Idempotency-Key": "voice-setup-2"},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json["replayed"] is True
