"""Voice-only raw audio ingress follows current caller/workspace authority."""

from __future__ import annotations

import io
from typing import Any

import pytest
from sqlalchemy import func, select

from pandrator.web import audio_import_routes as routes
from pandrator.web import models as m
from pandrator_mcp.clients.application_sounds import ApplicationSoundMethods
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401
from tests.test_voice_lifecycle_idempotency import silent_wav


def upload(client, csrf, workspace=None, *, key="reference-audio-key", stream=None):
    content = silent_wav()
    options = (
        {"data": content}
        if stream is None
        else {"input_stream": stream, "content_length": len(content)}
    )
    return client.put(
        "/api/v1/voices/reference-audio/import",
        query_string={} if workspace is None else {"workspace_id": workspace},
        content_type="application/octet-stream",
        headers={**headers(csrf, key), "X-Audio-Filename": "reference.wav"},
        **options,
    )


def test_binary_reference_uses_caller_home_or_selected_team_and_isolates_receipts(
    case, monkeypatch
):
    users = team(case, monkeypatch)
    own = body(upload(users.alice, users.ah), 201)
    shared = body(upload(users.alice, users.ah, users.workspace_id), 201)
    peer = body(upload(users.bob, users.bh, users.workspace_id), 201)
    assert (
        own["workspace_id"] != shared["workspace_id"] == peer["workspace_id"] == users.workspace_id
    )
    assert len({own["artifact_id"], shared["artifact_id"], peer["artifact_id"]}) == 3
    with case.database.snapshot_session() as session:
        for result in (own, shared, peer):
            row = session.get_one(m.Artifact, result["artifact_id"])
            assert row.role == "voice.reference" and row.workspace_id == result["workspace_id"]
        assert session.scalar(select(func.count()).select_from(m.SoundItem)) == 0
    replay = upload(users.alice, users.ah, users.workspace_id)
    assert body(replay, 201) == shared and replay.headers["Idempotency-Replayed"] == "true"
    with case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert upload(users.alice, users.ah, users.workspace_id).status_code == 404


def test_raw_reference_denial_precedes_body_preparation_and_asset_rows(case, monkeypatch):
    users = team(case, monkeypatch)
    personal = body(
        users.bob.post("/api/v1/voices", json={"name": "Bob private"}, headers=users.bh), 201
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Denied raw import must not inspect or publish audio")

    monkeypatch.setattr(routes, "prepare_audio_import", forbidden)
    for caller, csrf in ((users.alice, users.ah), (users.owner, users.oh)):
        assert upload(caller, csrf, personal["workspace_id"]).status_code == 404
    _token, read = scoped_token(case, users.alice_id, {"app.read"}, workspace=users.workspace_id)
    assert upload(users.alice, read, users.workspace_id).status_code == 403
    for query in ({"workspace_id": ""}, {"workspace_id": "x" * 81}, {"session_id": "session"}):
        response = users.alice.put(
            "/api/v1/voices/reference-audio/import",
            query_string=query,
            data=silent_wav(),
            content_type="application/octet-stream",
            headers={
                **headers(users.ah, "invalid-reference-scope"),
                "X-Audio-Filename": "reference.wav",
            },
        )
        assert response.status_code == 422
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Artifact)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.ApiIdempotency)
                .where(m.ApiIdempotency.operation_id == "importVoiceReferenceAudio")
            )
            == 0
        )


@pytest.mark.parametrize("phase", ["stream", "prepared"])
def test_departure_during_import_rolls_back_and_cleans_only_owned_staging(case, monkeypatch, phase):
    users = team(case, monkeypatch)
    services = case.app.extensions["pandrator"]["services"]
    keep = services.paths.root / "sounds" / "uploads" / "retained.wav"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(b"Retained pre-existing input")
    original = routes.prepare_audio_import
    calls = []

    def depart():
        with case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id))
            )

    def prepare(*args, **kwargs):
        calls.append("prepare")
        assert phase == "prepared", "Departure during streaming must stop before inspection"
        result = original(*args, **kwargs)
        depart()
        return result

    class DepartingStream(io.BytesIO):
        changed = False

        def readinto(self, buffer):
            if not self.changed:
                self.changed = True
                depart()
            return super().readinto(buffer)

    monkeypatch.setattr(routes, "prepare_audio_import", prepare)
    response = upload(
        users.alice,
        users.ah,
        users.workspace_id,
        stream=DepartingStream(silent_wav()) if phase == "stream" else None,
    )
    assert response.status_code == 404, response.get_json()
    assert calls == ([] if phase == "stream" else ["prepare"])
    assert keep.read_bytes() == b"Retained pre-existing input"
    assert list(keep.parent.iterdir()) == [keep]
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.Artifact)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.ApiIdempotency)
                .where(m.ApiIdempotency.operation_id == "importVoiceReferenceAudio")
            )
            == 0
        )


class BinaryVoiceClient(ApplicationSoundMethods):
    def __init__(self, client, csrf):
        self.client, self.csrf = client, csrf

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        raise AssertionError("Raw voice reference import must use binary transport.")

    def _request_binary_json(self, path: str, **options: Any) -> dict[str, Any]:
        response = self.client.put(
            path,
            data=options["body"],
            query_string=options.get("parameters"),
            content_type=options["content_type"],
            headers={**self.csrf, **options["extra_headers"]},
        )
        return body(response, 201)


def test_actual_mcp_binary_builder_keeps_selected_voice_workspace(case, monkeypatch):
    users = team(case, monkeypatch)
    client = BinaryVoiceClient(users.alice, users.ah)
    result = client.import_audio_reference(
        target="voice_reference",
        filename="reference.wav",
        body=silent_wav(),
        idempotency_key="mcp-shared-reference",
        workspace_id=users.workspace_id,
    )
    assert result["workspace_id"] == users.workspace_id
    with case.database.snapshot_session() as session:
        assert session.get_one(m.Artifact, result["artifact_id"]).workspace_id == users.workspace_id


def test_canonical_legacy_reference_receipt_without_new_metadata_is_retained(case):
    csrf = {"X-CSRF-Token": case.csrf}
    first = body(upload(case.client, csrf), 201)
    expected = {key: value for key, value in first.items() if key != "workspace_id"}
    with case.database.immediate_session() as session:
        receipt = session.scalar(
            select(m.ApiIdempotency).where(
                m.ApiIdempotency.operation_id == "importVoiceReferenceAudio",
                m.ApiIdempotency.idempotency_key == "reference-audio-key",
            )
        )
        assert receipt is not None
        receipt.response_json = expected
    replay = upload(case.client, csrf)
    assert body(replay, 201) == expected and replay.headers["Idempotency-Replayed"] == "true"
