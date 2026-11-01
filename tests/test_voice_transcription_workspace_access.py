"""Current team voice transcription/review authority and private settings fences."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web import voice_routes as routes
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.voice_sample_transcription import (
    check_voice_transcription_settings,
    resolve_voice_transcription_settings,
)
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import (
    ReviewVoiceTranscriptInput,
    TranscribeVoiceSampleInput,
)
from pandrator_mcp.tools.voice_lifecycle import review_voice_transcript, transcribe_voice_sample
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_mcp_voice_crud_workspace_http import VoiceRecordHttpClient
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_crud_workspace_access import create, sample
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401


def _shared(case, members):
    voice = body(create(members.bob, members.bh, wid=members.workspace_id), 201)
    with case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).language = "en"
    sample_id, artifact_id, _path = sample(case, voice)
    return voice, sample_id, artifact_id


def _setting(case, workspace_id, key, value):
    with case.database.immediate_session() as session:
        row = session.get(m.WorkspaceSetting, (workspace_id, key))
        if row is None:
            session.add(m.WorkspaceSetting(workspace_id=workspace_id, key=key, value_json=value))
        else:
            row.value_json = value
            row.revision += 1


def test_member_submission_uses_stored_voice_owner_and_current_actor_before_replay(
    case, monkeypatch
):
    members = team(case, monkeypatch)
    voice, sid, aid = _shared(case, members)
    route = f"/api/v1/voices/{voice['id']}/samples/{sid}/transcribe"
    request_headers = headers(members.ah, "shared-voice-transcription")
    first = body(members.alice.post(route, json={}, headers=request_headers), 202)
    assert first["workspace_id"] == members.workspace_id
    with case.database.snapshot_session() as session:
        job = session.get_one(m.Job, first["id"])
        actor = session.get_one(JobActorRecord, job.id)
        assert actor.user_id == members.alice_id
        assert job.kind == "voice.transcribe" and job.workspace_id == members.workspace_id
        assert job.payload_json == {
            "voice_id": voice["id"],
            "sample_id": sid,
            "sample_artifact_id": aid,
            "workspace_id": members.workspace_id,
            "settings": {"stt_engine": "auto", "stt_backend": "auto", "stt_language": "en"},
            "stt_settings_revisions": {"services.stt": 0, "defaults.stt": 0},
        }
    _setting(case, members.workspace_id, "defaults.stt", {"stt_engine": "whisper"})
    with case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, sid))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A retained retry cannot capture new settings or enqueue")

    monkeypatch.setattr(routes, "capture_voice_transcription_settings", forbidden)
    replay = members.alice.post(route, json={}, headers=request_headers)
    assert body(replay, 202) == first and replay.headers["Idempotency-Replayed"] == "true"
    with case.database.immediate_session() as session:
        session.delete(
            session.get_one(m.WorkspaceMembership, (members.workspace_id, members.alice_id))
        )
    assert members.alice.post(route, json={}, headers=request_headers).status_code == 404


def test_same_retry_key_is_separate_for_team_members_and_personal_owner(case, monkeypatch):
    members = team(case, monkeypatch)
    voice, sid, _aid = _shared(case, members)
    own = body(create(members.alice, members.ah, "Alice private"), 201)
    own_sid, _aid, _path = sample(case, own)
    route = f"/api/v1/voices/{voice['id']}/samples/{sid}/transcribe"
    alice = body(
        members.alice.post(route, json={}, headers=headers(members.ah, "same-sample-job-key")), 202
    )
    bob = body(
        members.bob.post(route, json={}, headers=headers(members.bh, "same-sample-job-key")), 202
    )
    personal = body(
        members.alice.post(
            f"/api/v1/voices/{own['id']}/samples/{own_sid}/transcribe",
            json={},
            headers=headers(members.ah, "same-sample-job-key"),
        ),
        202,
    )
    assert len({alice["id"], bob["id"], personal["id"]}) == 3
    assert personal["workspace_id"] == own["workspace_id"] != members.workspace_id


def test_scope_or_write_or_run_denial_happens_before_settings_and_job(case, monkeypatch):
    members = team(case, monkeypatch)
    voice, sid, _aid = _shared(case, members)
    foreign = body(create(members.bob, members.bh, "Bob private"), 201)
    foreign_sid, _aid, _path = sample(case, foreign)
    route = f"/api/v1/voices/{voice['id']}/samples/{sid}/transcribe"
    with case.database.snapshot_session() as session:
        count = session.scalar(select(func.count()).select_from(m.Job))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied transcription reached protected settings")

    monkeypatch.setattr(routes, "capture_voice_transcription_settings", forbidden)
    for scopes in ({"app.read", "app.write"}, {"app.read", "app.run"}):
        _token, bearer = scoped_token(
            case, members.alice_id, scopes, workspace=members.workspace_id
        )
        assert members.alice.post(route, json={}, headers=bearer).status_code == 403
    for query in ({"workspace_id": foreign["workspace_id"]},):
        assert (
            members.alice.post(route, query_string=query, json={}, headers=members.ah).status_code
            == 404
        )
    for query in ({"workspace_id": ""}, {"workspace_id": "x" * 81}, {"session_id": "anything"}):
        assert (
            members.alice.post(route, query_string=query, json={}, headers=members.ah).status_code
            == 422
        )
    private_route = f"/api/v1/voices/{foreign['id']}/samples/{foreign_sid}/transcribe"
    assert members.alice.post(private_route, json={}, headers=members.ah).status_code == 404
    assert members.owner.post(private_route, json={}, headers=members.oh).status_code == 404
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Job)) == count


def test_shared_transcription_cannot_supply_connections_paths_or_unapproved_service(
    case, monkeypatch
):
    members = team(case, monkeypatch)
    voice, sid, _aid = _shared(case, members)
    route = f"/api/v1/voices/{voice['id']}/samples/{sid}/transcribe"
    for value in (
        {"provider_configs": [{"id": "azure", "api_base": "https://invalid.example"}]},
        {"crispasr_executable": "/private/bin/custom"},
        {"stt_api_base": "https://invalid.example"},
        {"api_key": "SYNTHETIC SENTINEL"},
        {"stt_engine": "azure_mai_transcribe_2"},
        {"stt_engine": "whisper", "stt_backend": "moss"},
    ):
        assert members.alice.post(route, json=value, headers=members.ah).status_code == 422
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Job)) == 0


def test_approved_connection_is_resolved_only_at_runtime_and_revision_fenced(case, monkeypatch):
    members = team(case, monkeypatch)
    voice, sid, _aid = _shared(case, members)
    engine = "azure_mai_transcribe_2"
    _setting(
        case,
        members.workspace_id,
        "services.stt",
        {
            "provider_configs": [
                {
                    "id": engine,
                    "enabled": True,
                    "api_base": "https://approved.invalid",
                    "secret_ref": "SYNTHETIC LOCATOR",
                },
                {
                    "id": "azure_mai_transcribe_1_5",
                    "enabled": False,
                    "api_base": "https://private.invalid",
                },
            ]
        },
    )
    request = members.alice.post(
        f"/api/v1/voices/{voice['id']}/samples/{sid}/transcribe",
        json={"stt_engine": engine},
        headers=members.ah,
    )
    result = body(request, 202)
    assert "SYNTHETIC LOCATOR" not in str(result) and "approved.invalid" not in str(result)
    with case.database.snapshot_session() as session:
        job = session.get_one(m.Job, result["id"])
        payload = dict(job.payload_json)
        settings = resolve_voice_transcription_settings(session, payload, members.workspace_id)
        assert len(settings["provider_configs"]) == 1
        assert settings["provider_configs"][0]["id"] == engine
        assert settings["provider_configs"][0]["secret_ref"] == "SYNTHETIC LOCATOR"
    _setting(case, members.workspace_id, "services.stt", {"provider_configs": []})
    with case.database.snapshot_session() as session:
        with pytest.raises(ValueError, match="settings changed"):
            check_voice_transcription_settings(session, payload, members.workspace_id)
        with pytest.raises(ValueError, match="settings changed"):
            resolve_voice_transcription_settings(session, payload, members.workspace_id)
        with execution_actor_scope(HostActor("pandrator-cli")):
            assert (
                resolve_voice_transcription_settings(
                    session, {"settings": {}}, members.workspace_id
                )
                == {}
            )


def test_transcript_review_is_current_revisioned_team_edit_and_replay(case, monkeypatch):
    members = team(case, monkeypatch)
    voice, sid, _aid = _shared(case, members)
    route = f"/api/v1/voices/{voice['id']}/samples/{sid}/transcript"
    requested = {
        "transcript": "Shared reviewed words",
        "language": "en",
        "expected_voice_revision": 1,
    }
    request_headers = headers(members.ah, "shared-transcript-review")
    saved = members.alice.patch(route, json=requested, headers=request_headers)
    result = body(saved)
    assert result["workspace_id"] == members.workspace_id and result["transcript_reviewed"] is True
    assert result["voice_revision"] == 2 and saved.headers["ETag"] == '"2"'
    replay = members.alice.patch(route, json=requested, headers=request_headers)
    assert body(replay) == result and replay.headers["Idempotency-Replayed"] == "true"
    assert members.bob.patch(route, json=requested, headers=members.bh).status_code == 409
    _token, read = scoped_token(case, members.bob_id, {"app.read"}, workspace=members.workspace_id)
    assert members.bob.patch(route, json=requested, headers=read).status_code == 403
    with case.database.immediate_session() as session:
        session.delete(
            session.get_one(m.WorkspaceMembership, (members.workspace_id, members.alice_id))
        )
    assert members.alice.patch(route, json=requested, headers=request_headers).status_code == 404


def test_actual_mcp_transcribe_and_review_follow_the_shared_voice_owner(case, monkeypatch):
    members = team(case, monkeypatch)
    voice, sid, _aid = _shared(case, members)
    application = VoiceRecordHttpClient(members.alice, members.ah)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    submitted = transcribe_voice_sample(
        runtime,
        TranscribeVoiceSampleInput.model_validate(
            {
                "voice_id": voice["id"],
                "sample_id": sid,
                "settings": {},
                "workspace_id": members.workspace_id,
                "idempotency_key": "transcription-mcp-shared",
            }
        ),
    )
    assert submitted.result["workspace_id"] == members.workspace_id
    actions = [
        action for action in submitted.next_actions if action.tool == "pandrator_get_voice_samples"
    ]
    assert actions[0].arguments == {"voice_id": voice["id"], "workspace_id": members.workspace_id}
    reviewed = review_voice_transcript(
        runtime,
        ReviewVoiceTranscriptInput.model_validate(
            {
                "voice_id": voice["id"],
                "sample_id": sid,
                "transcript": "Reviewed through MCP",
                "expected_voice_revision": 1,
                "idempotency_key": "review-mcp-shared-voice",
            }
        ),
    )
    assert reviewed.result["workspace_id"] == members.workspace_id
    assert reviewed.result["transcript"] == "Reviewed through MCP"
    assert reviewed.next_actions[0].arguments == {
        "voice_id": voice["id"],
        "workspace_id": members.workspace_id,
    }
