"""Offline Google discovery/refresh/create boundaries and bounded receipt scope."""

from __future__ import annotations

import hashlib
import json
import socket
from types import SimpleNamespace

import pytest
import requests

from pandrator.logic import google_tts_catalogue_http as http
from pandrator.logic import tts_handler
from pandrator.logic.google_voice_scope import google_voice_scope
from pandrator.web.voice_provider_effects import voice_provider_effect_summary
from tests.test_voice_provider_job_effects import receipt

MODEL = "gemini-3.8-flash-tts"
VERTEX = {"api_base": "https://aiplatform.googleapis.com"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Google transport acceptance must remain offline")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def test_create_callback_denial_escapes_request_sanitization_and_closes_owned_session(monkeypatch):
    calls = []
    owned = SimpleNamespace(
        post=lambda *_args, **_kwargs: pytest.fail("Denied create reached POST"),
        close=lambda: calls.append("close"),
    )
    monkeypatch.setattr(http.requests, "Session", lambda: owned)
    denial = requests.RequestException("Synthetic execution denial")

    def before_io():
        calls.append("proof")
        raise denial

    with pytest.raises(requests.RequestException) as caught:
        http.create_google_voice(
            "https://synthetic.invalid/create",
            headers={},
            model=MODEL,
            display_name="Synthetic",
            prompt="Synthetic",
            before_io=before_io,
        )
    assert caught.value is denial and calls == ["proof", "close"]


def test_create_guards_exact_single_post_and_preserves_caller_session_ownership():
    events = []
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"id":"voice_fixture123"}'

    def post(_url, **options):
        assert events == ["proof"]
        assert options["json"]["store"] is True
        assert options["timeout"] == 60 and options["allow_redirects"] is False
        events.append("post")
        return response

    session = SimpleNamespace(post=post, close=lambda: pytest.fail("Caller session was closed"))
    result = http.create_google_voice(
        "https://synthetic.invalid/create",
        headers={},
        model=MODEL,
        display_name="Synthetic",
        prompt="Synthetic",
        before_io=lambda: events.append("proof"),
        request_session=session,
    )
    assert result["id"] == "voice_fixture123" and events == ["proof", "post"]


@pytest.mark.parametrize("project", [None, "configured-project"])
def test_empty_named_vertex_credential_denies_before_adc_even_with_host_project(
    monkeypatch, project
):
    def poison(*_args, **_kwargs):
        pytest.fail("Named Vertex work touched host ADC or auth transport")

    monkeypatch.setattr("google.auth.default", poison)
    monkeypatch.setattr("google.auth.transport.requests.Request", poison)
    service = {
        **VERTEX,
        "_google_project": "host-metadata-project",
        "vertex_project": project or "",
    }
    with pytest.raises(ValueError, match="credentials"):
        google_voice_scope("vertex_ai", service, "", allow_host_adc=False)
    with pytest.raises(ValueError, match="credentials"):
        tts_handler._vertex_access_token(service, allow_host_adc=False)


def test_explicit_workspace_json_ignores_host_project_and_keeps_scope_fingerprint_format(
    monkeypatch,
):
    monkeypatch.setattr(
        "google.auth.default", lambda **_kwargs: pytest.fail("Explicit JSON discovered ADC")
    )
    service = {**VERTEX, "_google_project": "poison-host-project"}
    credential = json.dumps({"project_id": "workspace-project"})
    scope = google_voice_scope("vertex_ai", service, credential, allow_host_adc=False)
    expected = hashlib.sha256(
        json.dumps(
            ["vertex_ai", VERTEX["api_base"], "workspace-project"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert scope == {"fingerprint": expected, "project": "workspace-project"}
    with pytest.raises(ValueError):
        google_voice_scope("vertex_ai", service, "{}", allow_host_adc=False)


@pytest.mark.parametrize("deny_at", [None, 1, 2])
def test_legacy_scope_guards_adc_discovery_and_each_auth_request_without_refresh(
    monkeypatch, deny_at
):
    events = []
    checks = []
    denial = PermissionError("Synthetic lost authority")

    def prove():
        checks.append("proof")
        if len(checks) == deny_at:
            raise denial

    def transport(*_args, **_kwargs):
        events.append("http")

    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: transport)
    credentials = SimpleNamespace(
        refresh=lambda *_args: pytest.fail("Scope helper refreshed a token")
    )

    def default(*, scopes, request):
        events.append("discovery")
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        request("https://synthetic.invalid/metadata")
        return credentials, "legacy-project"

    monkeypatch.setattr("google.auth.default", default)
    if deny_at is None:
        assert (
            google_voice_scope("vertex_ai", VERTEX, "", before_io=prove)["project"]
            == "legacy-project"
        )
        assert checks == ["proof", "proof"] and events == ["discovery", "http"]
    else:
        with pytest.raises(PermissionError) as caught:
            google_voice_scope("vertex_ai", VERTEX, "", before_io=prove)
        assert caught.value is denial and "http" not in events


def test_legacy_vertex_token_guards_discovery_refresh_and_every_auth_request(monkeypatch):
    events = []

    def transport(*_args, **_kwargs):
        events.append("http")

    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: transport)

    class Credentials:
        valid = False
        token = None

        def refresh(self, request):
            events.append("refresh")
            request("https://synthetic.invalid/token")
            self.valid, self.token = True, "synthetic-token"

    def default(*, scopes, request):
        events.append("discovery")
        request("https://synthetic.invalid/metadata")
        return Credentials(), "legacy-project"

    monkeypatch.setattr("google.auth.default", default)
    assert tts_handler._vertex_access_token(VERTEX, before_io=lambda: events.append("proof")) == (
        "synthetic-token",
        "legacy-project",
    )
    assert events == ["proof", "discovery", "proof", "http", "proof", "refresh", "proof", "http"]


def google_receipt():
    value = receipt()
    value.update(operation="voice.google_design", service_id="gemini", binding_state="ready")
    value["handler_result"] = {
        "voice_id": value["voice_id"],
        "service_id": value["service_id"],
        "provider_voice_id": value["provider_voice_id"],
        "voice_revision": 1,
        "model": MODEL,
        "usage": {"total_tokens": 9},
    }
    return value


@pytest.mark.parametrize(
    "usage",
    [
        {"total_tokens": True},
        {"total_tokens": -1},
        {"total_tokens": 2**63},
        {str(key): 0 for key in range(21)},
        {"bad\nkey": 0},
    ],
)
def test_google_usage_is_bounded_and_not_available_to_other_receipt_operations(usage):
    value = google_receipt()
    assert voice_provider_effect_summary(value) is not None
    value["handler_result"]["usage"] = usage
    assert voice_provider_effect_summary(value) is None
    value = google_receipt()
    value["operation"] = "voice.publish"
    assert voice_provider_effect_summary(value) is None
