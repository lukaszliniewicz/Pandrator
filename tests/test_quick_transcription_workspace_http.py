"""Owner-only quick transcription through real admission, workers and MCP builders."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import UserActor, current_execution_actor
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import Worker
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.transcription import TranscribeInput
from pandrator_mcp.tools.transcription import transcribe
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token

PRIVATE_TEXT = "A private transcription remains with its owner."


def _services(case):
    return case.app.extensions["pandrator"]["services"]


def _wave_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x10\x00" * 3200)
    return buffer.getvalue()


def _queue(case, client, auth, *, format="txt"):
    content = _wave_bytes()
    created = body(
        client.post(
            "/api/v1/transcriptions",
            json={
                "filename": "private.wav",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "format": format,
                "engine": "whisper",
                "language": "en",
            },
            headers=headers(auth, str(uuid4())),
        ),
        201,
    )
    identifier = created["id"]
    body(client.put(f"/api/v1/transcriptions/{identifier}/chunks/0", data=content, headers=auth))
    started = body(
        client.post(f"/api/v1/transcriptions/{identifier}/start", json={}, headers=auth), 202
    )
    return started, content


def _fake_asr(case, monkeypatch, expected_user, effect=None):
    def recognize(scratch, _normalized, _settings, **kwargs):
        actor, lease = current_execution_actor.get(), current_job_lease.get()
        assert type(actor) is UserActor and actor.user_id == expected_user
        assert lease is not None and callable(kwargs.get("before_io"))
        assert "run_func" not in kwargs
        kwargs["before_io"]()
        timed = kwargs["require_word_timestamps"]
        root = Path(scratch)
        words = root / "recognized.json"
        words.write_text(
            json.dumps(
                {
                    "crispasr": {"backend": "whisper", "language": "en"},
                    "transcription": [
                        {
                            "id": "one",
                            "text": PRIVATE_TEXT,
                            "offsets": {"from": 0, "to": 200},
                            "words": [{"text": PRIVATE_TEXT, "offsets": {"from": 0, "to": 200}}]
                            if timed
                            else [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        srt = root / "recognized.srt"
        srt.write_text(f"1\n00:00:00,000 --> 00:00:00,200\n{PRIVATE_TEXT}\n", encoding="utf-8")
        if effect is not None:
            effect()
        return SimpleNamespace(
            word_timestamps_path=str(words),
            srt_path=str(srt),
            engine="whisper",
            compute_backend="cpu",
            resolved_language="en",
        )

    recognized = Mock(side_effect=recognize)
    monkeypatch.setattr(
        "pandrator.web.quick_transcription.transcribe_source_file_with_metadata", recognized
    )
    return recognized


def _run(case, started, *, late_progress=False):
    services = _services(case)
    handlers = services.workflow_handlers.handlers()
    if late_progress:
        original = handlers["transcription.quick"]

        def handler(payload, progress, cancel_event):
            def report(value, detail=None):
                if value == 1:
                    raise RuntimeError("Synthetic late reporting fault")
                progress(value, detail)

            return original(payload, report, cancel_event)

        handlers["transcription.quick"] = handler
    assert Worker(services.jobs, "private-transcription-journey", handlers).run_once()
    return services.jobs.get(started["job_id"])


def _personal_workspace(case, user):
    with case.database.snapshot_session() as unit:
        workspace = unit.scalar(
            select(m.Workspace.id).where(m.Workspace.personal_owner_user_id == user)
        )
        assert workspace is not None
        return workspace


def _read_result(client, auth, identifier):
    metadata = body(client.get(f"/api/v1/transcriptions/{identifier}", headers=auth))
    assert metadata["status"] == "succeeded" and metadata["result_available"]
    assert metadata["progress"] == 1
    result = client.get(f"/api/v1/transcriptions/{identifier}/result?format=txt", headers=auth)
    assert result.status_code == 200
    assert PRIVATE_TEXT in result.get_data(as_text=True)
    return metadata


def _assert_private_roots_and_empty_generic_result(case, started, user):
    with case.database.snapshot_session() as unit:
        record = unit.get_one(m.QuickTranscription, started["id"])
        job = unit.get_one(m.Job, started["job_id"])
        owner = unit.get_one(m.QuickTranscriptionOwner, record.id)
        assert owner.user_id == user and job.workspace_id == owner.workspace_id
        assert unit.get_one(m.Workspace, owner.workspace_id).kind == "personal"
        assert job.session_id is None and job.project_id is None and job.workflow_run_id is None
        assert unit.get_one(m.JobActorRecord, job.id).user_id == user
        assert not job.result_json
        assert PRIVATE_TEXT not in json.dumps(job.payload_json)
        assert unit.scalar(select(func.count()).select_from(m.SessionRecord)) == 0
        assert unit.scalar(select(func.count()).select_from(m.Artifact)) == 0
        assert unit.scalar(select(func.count()).select_from(m.Document)) == 0


@pytest.mark.parametrize("format", ["txt", "srt"])
def test_default_single_user_real_normalization_keeps_private_results(case, monkeypatch, format):
    auth = {"X-CSRF-Token": case.csrf}
    recognized = _fake_asr(case, monkeypatch, case.actor.user_id)
    started, _content = _queue(case, case.client, auth, format=format)
    finished = _run(case, started)
    assert finished.status == "succeeded", finished.error_message
    metadata = _read_result(case.client, auth, started["id"])
    assert metadata["available_formats"] == (
        ["txt", "json"] if format == "txt" else ["txt", "srt", "json"]
    )
    _assert_private_roots_and_empty_generic_result(case, started, case.actor.user_id)
    with case.database.snapshot_session() as unit:
        assert unit.scalar(select(func.count()).select_from(m.User)) == 1
    directory = _services(case).quick_transcriptions.root / started["id"]
    assert not (directory / "source.wav").exists()
    assert all(path.name.startswith("result.") for path in directory.iterdir())
    assert recognized.call_count == 1


@pytest.mark.parametrize("scopes", [{"app.run"}, {"app.read", "app.run"}])
def test_named_user_private_run_credentials_do_not_share_with_team_or_admin(
    case, monkeypatch, scopes
):
    people = team(case, monkeypatch)
    workspace = _personal_workspace(case, people.bob_id)
    _, credential = scoped_token(case, people.bob_id, scopes, workspace=workspace)
    client = case.app.test_client()
    _fake_asr(case, monkeypatch, people.bob_id)
    started, _content = _queue(case, client, credential)
    finished = _run(case, started)
    assert finished.status == "succeeded", finished.error_message
    _read_result(people.bob, people.bh, started["id"])
    _assert_private_roots_and_empty_generic_result(case, started, people.bob_id)
    if "app.read" not in scopes:
        assert (
            client.get(f"/api/v1/transcriptions/{started['id']}", headers=credential).status_code
            == 403
        )
    for outsider in (people.alice, people.owner):
        assert outsider.get(f"/api/v1/transcriptions/{started['id']}").status_code == 404
        assert (
            outsider.get(f"/api/v1/transcriptions/{started['id']}/result?format=txt").status_code
            == 404
        )
        assert outsider.get(f"/api/v1/jobs/{started['job_id']}").status_code == 404


@pytest.mark.parametrize("revocation", ["token", "auth_revision"])
def test_queued_explicit_revocation_precedes_private_settings_and_source(
    case, monkeypatch, revocation
):
    people = team(case, monkeypatch)
    token, credential = scoped_token(
        case,
        people.bob_id,
        {"app.read", "app.run"},
        workspace=_personal_workspace(case, people.bob_id),
    )
    started, content = _queue(case, case.app.test_client(), credential)
    with case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE quick_transcriptions SET settings_json = :bad WHERE id = :id"),
            {"bad": "{PRIVATE-invalid", "id": started["id"]},
        )
        if revocation == "token":
            unit.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()
        else:
            changed = unit.execute(
                text(
                    "UPDATE user_credentials SET auth_revision = auth_revision + 1 WHERE user_id = :id"
                ),
                {"id": people.bob_id},
            )
            assert changed.rowcount == 1
    normalize = Mock(side_effect=AssertionError("Revoked private source reached normalization"))
    recognize = Mock(side_effect=AssertionError("Revoked private source reached a model"))
    monkeypatch.setattr("pandrator.web.quick_transcription.run_cancellable", normalize)
    monkeypatch.setattr(
        "pandrator.web.quick_transcription.transcribe_source_file_with_metadata", recognize
    )
    finished = _run(case, started)
    assert finished.status == "failed" and "JSON" not in (finished.error_message or "")
    normalize.assert_not_called()
    recognize.assert_not_called()
    directory = _services(case).quick_transcriptions.root / started["id"]
    assert (directory / "source.wav").read_bytes() == content
    assert not list(directory.glob("result.*"))


def test_inflight_scope_loss_stops_result_but_structural_cleanup_removes_own_input(
    case, monkeypatch
):
    people = team(case, monkeypatch)
    token, credential = scoped_token(
        case,
        people.bob_id,
        {"app.read", "app.run"},
        workspace=_personal_workspace(case, people.bob_id),
    )
    started, _content = _queue(case, case.app.test_client(), credential)

    def revoke():
        with case.database.immediate_session() as unit:
            unit.get_one(m.ApiToken, token.id).scopes_json = ["app.read"]

    _fake_asr(case, monkeypatch, people.bob_id, effect=revoke)
    finished = _run(case, started)
    assert finished.status == "failed"
    directory = _services(case).quick_transcriptions.root / started["id"]
    assert not list(directory.glob("result.*")) and not (directory / "source.wav").exists()
    response = people.bob.get(f"/api/v1/transcriptions/{started['id']}")
    assert body(response)["result_available"] is False


@pytest.mark.parametrize("fault", ["uncertain_commit", "late_progress"])
def test_committed_private_result_survives_worker_reporting_failure(case, monkeypatch, fault):
    auth = {"X-CSRF-Token": case.csrf}
    _fake_asr(case, monkeypatch, case.actor.user_id)
    started, _content = _queue(case, case.client, auth)
    if fault == "uncertain_commit":
        original = case.database.immediate_session
        raised = False

        @contextmanager
        def immediate(*args, **kwargs):
            nonlocal raised
            with original(*args, **kwargs) as unit:
                before = unit.scalar(
                    select(m.QuickTranscription.state).where(
                        m.QuickTranscription.id == started["id"]
                    )
                )
                yield unit
                unit.flush()
                after = unit.scalar(
                    select(m.QuickTranscription.state).where(
                        m.QuickTranscription.id == started["id"]
                    )
                )
                published = before == "submitted" and after == "finished"
            if published and not raised:
                raised = True
                raise RuntimeError("Synthetic uncertain commit")

        monkeypatch.setattr(case.database, "immediate_session", immediate)
    finished = _run(case, started, late_progress=fault == "late_progress")
    assert finished.status == "failed"
    metadata = _read_result(case.client, auth, started["id"])
    assert metadata["inline_result"] is True
    with case.database.snapshot_session() as unit:
        assert unit.get_one(m.QuickTranscription, started["id"]).state == "finished"
    directory = _services(case).quick_transcriptions.root / started["id"]
    assert (directory / "result.json").is_file() and (directory / "result.txt").is_file()


class _HttpApplication(ApplicationClient):
    def __init__(self, client, auth):
        self.client, self.auth = client, auth
        self.timeout_seconds = 10
        self.calls: list[tuple[str, str]] = []

    def _send(self, path, *, binary=False, **options):
        method = options.get("method", "GET")
        self.calls.append((method, path))
        credential = dict(self.auth)
        if options.get("idempotency_key"):
            credential["Idempotency-Key"] = options["idempotency_key"]
        response = self.client.open(
            path,
            method=method,
            query_string=options.get("parameters"),
            headers=credential,
            **({"data": options.get("body")} if binary else {"json": options.get("body")}),
        )
        value = response.get_json()
        assert isinstance(value, dict)
        if response.status_code >= 400:
            raise PandratorMcpError("scope_denied", str(value.get("error")))
        return value

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        return self._send(path, **options)

    def _request_binary_json(self, path: str, **options: Any) -> dict[str, Any]:
        return self._send(path, binary=True, **options)


def test_existing_inline_mcp_upload_builder_queues_owner_bound_worker(case, monkeypatch):
    people = team(case, monkeypatch)
    _, credential = scoped_token(
        case,
        people.bob_id,
        {"app.read", "app.run"},
        workspace=_personal_workspace(case, people.bob_id),
    )
    client = case.app.test_client()
    application = _HttpApplication(client, credential)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    arguments = TranscribeInput.model_validate(
        {
            "source": {
                "kind": "base64",
                "filename": "private.wav",
                "data": base64.b64encode(_wave_bytes()).decode(),
            },
            "format": "txt",
            "engine": "whisper",
            "language": "en",
            "idempotency_key": str(uuid4()),
            "wait_seconds": 0,
        }
    )
    queued = transcribe(runtime, arguments).result
    assert isinstance(queued, dict) and queued["job_id"]
    _fake_asr(case, monkeypatch, people.bob_id)
    finished = _run(case, queued)
    assert finished.status == "succeeded", finished.error_message
    _read_result(client, credential, queued["id"])
    assert application.calls == [
        ("POST", "/api/v1/transcriptions"),
        ("PUT", f"/api/v1/transcriptions/{queued['id']}/chunks/0"),
        ("POST", f"/api/v1/transcriptions/{queued['id']}/start"),
    ]
