"""Sound ingress preserves private ownership and current publication authority."""

from __future__ import annotations

import io
from typing import Any

import pytest
from sqlalchemy import func, select

from pandrator.web import audio_import_routes as routes
from pandrator.web import models as m
from pandrator.web.accounts.identity import initial_personal_workspace
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_evidence_access_routes import body
from tests.test_project_sharing_routes import login
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401
from tests.test_voice_lifecycle_idempotency import silent_wav


def upload(client, csrf, *, method="PUT", key="owned-sound-import", query=None, stream=None,
           target="sound"):
    path = "/api/v1/sounds/import" if target == "sound" else "/api/v1/voices/reference-audio/import"
    headers = {**csrf, "Idempotency-Key": key, "X-Audio-Filename": "sound.wav"}
    data = silent_wav()
    if method == "POST":
        return client.post(
            path, query_string=query,
            data={"file": (io.BytesIO(data), "sound.wav")},
            headers=headers, content_type="multipart/form-data",
        )
    options = {"data": data} if stream is None else {
        "input_stream": stream, "content_length": len(data),
    }
    return client.put(
        path, query_string=query,
        headers=headers, content_type="application/octet-stream", **options,
    )


def counts(case) -> tuple[int | None, ...]:
    with case.database.snapshot_session() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (m.Artifact, m.SoundVariant, m.ApiIdempotency))


def forbidden(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("A denied sound import must not inspect audio")


@pytest.mark.parametrize("role", ["user", "administrator"])
@pytest.mark.parametrize("method", ["PUT", "POST"])
def test_other_accounts_are_denied_before_body_preparation(case, monkeypatch, role, method):
    new_account(case, "other", role=role)
    enable(case, monkeypatch)
    client, csrf = login(case, "other")
    monkeypatch.setattr(routes, "prepare_audio_import", forbidden)
    before = counts(case)
    with case.database.snapshot_session() as session:
        original = initial_personal_workspace(session).id
    response = upload(client, csrf, method=method, query={"workspace_id": original})
    assert response.status_code == 404, response.get_json()
    assert counts(case) == before
    root = case.app.extensions["pandrator"]["services"].paths.root
    assert not (root / "sounds/uploads").exists()


def test_solo_import_preserves_initial_ownership_and_canonical_replay(case):
    csrf = {"X-CSRF-Token": case.csrf}
    result = body(upload(case.client, csrf), 201)
    before = counts(case)
    replay = upload(case.client, csrf)
    assert body(replay, 201) == result
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert counts(case) == before
    with case.database.snapshot_session() as session:
        candidate = session.get_one(m.SoundVariant, result["id"])
        artifact = session.get_one(m.Artifact, candidate.artifact_id)
        workspace = session.get_one(m.Workspace, artifact.workspace_id)
        assert workspace.personal_owner_user_id == case.actor.user_id
        assert artifact.workspace_id == candidate.workspace_id
        assert artifact.role == "sound.original" and artifact.session_id is None
    assert "workspace_id" not in result  # Preserve the existing response contract.


def test_named_original_owner_can_import_and_foreign_replay_stays_private(case, monkeypatch):
    new_account(case, "other")
    enable(case, monkeypatch)
    owner, oh = login(case, "owner")
    other, ah = login(case, "other")
    expected = body(upload(owner, oh), 201)
    before = counts(case)
    monkeypatch.setattr(routes, "prepare_audio_import", forbidden)
    with case.database.snapshot_session() as session:
        original = initial_personal_workspace(session).id
    assert upload(other, ah, query={"workspace_id": original}).status_code == 404
    assert counts(case) == before
    assert expected["id"]


@pytest.mark.parametrize("query", [{"workspace_id": " team "}, {"session_id": "session"}])
def test_invalid_sound_scope_is_rejected_without_inspection(case, monkeypatch, query):
    monkeypatch.setattr(routes, "prepare_audio_import", forbidden)
    before = counts(case)
    assert upload(case.client, {"X-CSRF-Token": case.csrf}, query=query).status_code == 422
    assert counts(case) == before


@pytest.mark.parametrize("phase", ["stream", "prepared"])
@pytest.mark.parametrize("target", ["sound", "voice_reference"])
def test_current_token_revocation_stops_publication_and_cleans_owned_staging(
    case, monkeypatch, phase, target,
):
    token, raw = case.auth.create_api_token(
        "revocable-sound-import", scopes=("app.read", "app.write"),
    )
    original = routes.prepare_audio_import
    calls = []
    before = counts(case)
    root = case.app.extensions["pandrator"]["services"].paths.root
    retained = root / "sounds/uploads/retained.wav"
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes(b"Keep this pre-existing file")

    def revoke():
        with case.database.immediate_session() as session:
            session.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()

    def prepare(*args, **kwargs):
        calls.append("prepare")
        assert phase == "prepared", "Streaming revocation must precede inspection"
        result = original(*args, **kwargs)
        revoke()
        return result

    class RevokingStream(io.BytesIO):
        changed = False

        def readinto(self, buffer):
            if not self.changed:
                self.changed = True
                revoke()
            return super().readinto(buffer)

    monkeypatch.setattr(routes, "prepare_audio_import", prepare)
    response = upload(
        case.app.test_client(), {"Authorization": f"Bearer {raw}"},
        stream=RevokingStream(silent_wav()) if phase == "stream" else None,
        target=target,
    )
    assert response.status_code == 403, response.get_json()
    assert calls == ([] if phase == "stream" else ["prepare"])
    assert counts(case) == before
    assert retained.read_bytes() == b"Keep this pre-existing file"
    assert list(retained.parent.iterdir()) == [retained]
    staging = root / "sounds/staging"
    assert not staging.exists() or not list(staging.iterdir())
