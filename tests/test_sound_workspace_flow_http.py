"""Joined named-user sound workflow, with synthetic native boundaries."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from flask.wrappers import Request

from pandrator.logic import audio_cpp_sfx
from pandrator.web import audio_import_routes, sound_imports
from pandrator.web import models as m
from pandrator.web.access.contracts import WorkspaceNotFound
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator_mcp.clients.application_sounds import ApplicationSoundMethods
from pandrator_mcp.schemas.sounds import GenerateSoundInput
from pandrator_mcp.tools.sounds import generate_sound
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import scoped_token
from tests.test_sound_generation import _write_wave
from tests.test_sound_import_workspace_access import upload
from tests.test_sound_library_workspace_http import case as case  # noqa: F401
from tests.test_sound_library_workspace_http import counts
from tests.test_sound_library_workspace_http import library as library  # noqa: F401
from tests.test_sound_library_workspace_http import no_network as no_network  # noqa: F401
from tests.test_voice_lifecycle_idempotency import silent_wav


def synthetic_identity(_settings, request):
    return {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": request["model_id"],
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "synthetic-workspace-fixture",
        "minimum_runtime_version": "0.9.0",
    }


def test_member_generation_is_owned_by_selected_workspace(library, monkeypatch):
    f, u = library, library.users
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda *_a, **_kw: None)
    monkeypatch.setattr(audio_cpp_sfx, "request_runtime_identity", synthetic_identity)
    result = body(
        u.alice.post(
            "/api/v1/sounds/generate",
            query_string={"workspace_id": u.workspace_id},
            json={"prompt": "A short synthetic chime", "duration_seconds": 4, "seed": 123},
            headers=headers(u.ah, "member-workspace-generation"),
        ),
        202,
    )
    with f.case.database.snapshot_session() as session:
        job = session.get_one(m.Job, result["job_id"])
        candidate = session.get_one(m.SoundVariant, result["candidate_id"])
        assert job.workspace_id == candidate.workspace_id == u.workspace_id
        assert job.payload_json["workspace_id"] == u.workspace_id

    # Complete retries must not probe a changed runtime or cross namespaces.
    def forbidden(*_args, **_kwargs):
        raise AssertionError("A completed generation retry must not prepare runtime inputs")

    monkeypatch.setattr(audio_cpp_sfx, "request_runtime_identity", forbidden)
    replay = u.alice.post(
        "/api/v1/sounds/generate",
        query_string={"workspace_id": u.workspace_id},
        json={"prompt": "A short synthetic chime", "duration_seconds": 4, "seed": 123},
        headers=headers(u.ah, "member-workspace-generation"),
    )
    assert body(replay, 202) == result and replay.headers["Idempotency-Replayed"] == "true"
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    assert (
        u.alice.post(
            "/api/v1/sounds/generate",
            query_string={"workspace_id": u.workspace_id},
            json={},
            headers=headers(u.ah, "member-workspace-generation"),
        ).status_code
        == 404
    )


class SoundClient(ApplicationSoundMethods):
    """Use the actual MCP request builders against the authenticated HTTP app."""

    def __init__(self, client, csrf):
        self.client, self.csrf = client, csrf

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        method = options.get("method", "GET")
        response = self.client.open(
            path,
            method=method,
            query_string=options.get("parameters"),
            json=options.get("body"),
            headers=headers(self.csrf, options.get("idempotency_key", "")),
        )
        return body(response, 202 if path.endswith("/generate") else 200)

    def _request_binary_json(self, path: str, **options: Any) -> dict[str, Any]:
        return body(
            self.client.put(
                path,
                data=options["body"],
                query_string=options.get("parameters"),
                content_type=options["content_type"],
                headers={**self.csrf, **options["extra_headers"]},
            ),
            201,
        )


def test_mcp_member_inference_worker_and_peer_project_use_survive_creator_departure(
    library,
    monkeypatch,
):
    f, u = library, library.users
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda *_a, **_kw: None)
    monkeypatch.setattr(audio_cpp_sfx, "request_runtime_identity", synthetic_identity)
    effects = []

    def native(_request, path, *, settings, cancel_event, progress, before_io=None):
        assert before_io is not None
        before_io()
        effects.append("synthetic-native")
        return _write_wave(path)

    monkeypatch.setattr(audio_cpp_sfx, "generate_sound", native)
    _token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.run"}, workspace=u.workspace_id
    )
    client = SoundClient(f.case.app.test_client(), bearer)
    runtime: Any = SimpleNamespace(require_application=lambda: client)
    outcome = generate_sound(
        runtime,
        GenerateSoundInput.model_validate(
            {
                "prompt": "A synthetic bell",
                "duration_seconds": 1.0,
                "seed": 991,
                "workspace_id": u.workspace_id,
                "idempotency_key": "mcp-member-sound-joined",
            }
        ),
    )
    assert not isinstance(outcome, dict)
    receipt = outcome.result
    assert Worker(
        f.services.jobs, "joined-sound-worker", f.services.workflow_handlers.handlers()
    ).run_once()
    assert effects == ["synthetic-native"]
    candidate = client.get_sound_candidate(receipt["candidate_id"], workspace_id=u.workspace_id)
    assert candidate["status"] == "ready" and candidate["artifact_id"]
    # An inference-only credential cannot use EDIT even though its owner is a member.
    assert (
        f.case.app.test_client()
        .post(
            "/api/v1/sounds/save",
            query_string={"workspace_id": u.workspace_id},
            json={"candidate_id": candidate["id"], "name": "Not writable by this token"},
            headers=headers(bearer, "member-no-write-scope"),
        )
        .status_code
        == 403
    )
    saved = body(
        u.bob.post(
            "/api/v1/sounds/save",
            query_string={"workspace_id": u.workspace_id},
            json={"candidate_id": candidate["id"], "name": "Team chime"},
            headers=headers(u.bh, "peer-save-generated-sound"),
        )
    )
    project = body(
        u.bob.post(
            "/api/v1/sessions",
            json={
                "name": "Shared sound placement",
                "workflow_kind": "voiceover",
                "workspace_id": u.workspace_id,
            },
            headers=headers(u.bh, "peer-sound-project"),
        ),
        201,
    )
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    cue = body(
        u.bob.post(
            f"/api/v1/sessions/{project['id']}/sound-plan/cues",
            json={
                "expected_revision": 0,
                "variant_id": candidate["id"],
                "anchor": {"kind": "source_time", "time_seconds": 0.0},
                "playback": {"mode": "overlay"},
            },
            headers=headers(u.bh, "peer-place-generated-sound"),
        ),
        201,
    )
    assert cue["cue"]["variant_id"] == candidate["id"]
    assert (
        body(
            u.bob.get(
                f"/api/v1/sounds/{saved['item_id']}", query_string={"workspace_id": u.workspace_id}
            )
        )["name"]
        == "Team chime"
    )
    content = f"/api/v1/artifacts/{candidate['artifact_id']}/content"
    assert u.bob.get(content).status_code == 200
    assert u.alice.get(content).status_code == 404


def test_binary_multipart_and_mcp_imports_keep_personal_and_member_receipts_separate(library):
    f, u = library, library.users
    own = body(upload(u.alice, u.ah), 201)
    shared = body(upload(u.alice, u.ah, query={"workspace_id": u.workspace_id}), 201)
    peer = body(upload(u.bob, u.bh, method="POST", query={"workspace_id": u.workspace_id}), 201)
    assert len({own["id"], shared["id"], peer["id"]}) == 3
    before = counts(f)
    replay = upload(u.alice, u.ah, query={"workspace_id": u.workspace_id})
    assert body(replay, 201) == shared and replay.headers["Idempotency-Replayed"] == "true"
    assert counts(f) == before
    mcp = SoundClient(u.bob, u.bh).import_audio_reference(
        target="sound",
        filename="peer.wav",
        body=silent_wav(),
        workspace_id=u.workspace_id,
        idempotency_key="mcp-peer-import-sound",
    )
    with f.case.database.snapshot_session() as session:
        for result, workspace in (
            (own, f.homes[u.alice_id]),
            (shared, u.workspace_id),
            (peer, u.workspace_id),
            (mcp, u.workspace_id),
        ):
            candidate = session.get_one(m.SoundVariant, result["id"])
            artifact = session.get_one(m.Artifact, candidate.artifact_id)
            assert artifact.workspace_id == candidate.workspace_id == workspace
            assert artifact.role == "sound.original" and artifact.session_id is None
    assert (
        u.bob.get(
            f"/api/v1/sounds/candidates/{own['id']}", query_string={"workspace_id": u.workspace_id}
        ).status_code
        == 404
    )


def test_foreign_generation_and_import_deny_before_body_runtime_or_staging(library, monkeypatch):
    f, u = library, library.users
    before = counts(f)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Foreign sound requests must stop before private I/O")

    monkeypatch.setattr(Request, "get_json", forbidden)
    monkeypatch.setattr(audio_cpp_sfx, "request_runtime_identity", forbidden)
    monkeypatch.setattr(audio_import_routes, "prepare_audio_import", forbidden)
    for client, csrf in ((u.alice, u.ah), (u.owner, u.oh)):
        query = {"workspace_id": f.homes[u.bob_id]}
        assert (
            client.post(
                "/api/v1/sounds/generate",
                query_string=query,
                data=b"{malformed",
                content_type="application/json",
                headers=headers(csrf, "foreign-generation-scope"),
            ).status_code
            == 404
        )
        for method in ("PUT", "POST"):
            assert upload(client, csrf, method=method, query=query).status_code == 404
    assert counts(f) == before
    assert not (f.services.paths.root / "sounds/uploads").exists()


def test_service_entrypoints_prove_workspace_before_runtime_and_audio_inspection(
    library, monkeypatch
):
    from pandrator.web import sound_service

    f, u = library, library.users
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=u.alice)))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied service entry must not prepare private I/O")

    monkeypatch.setattr(sound_service, "prepare_sound_generation", forbidden)
    monkeypatch.setattr(sound_service, "prepare_sound_import", forbidden)
    monkeypatch.setattr(sound_imports, "prepare_import", forbidden)
    foreign = f.homes[u.bob_id]
    with execution_actor_scope(actor):
        with pytest.raises(WorkspaceNotFound):
            f.services.sounds.prepare_generation({}, workspace_id=foreign)
        with f.case.database.immediate_session() as session, pytest.raises(WorkspaceNotFound):
            f.services.sounds.generate_in_session(session, {}, workspace_id=foreign)
        with pytest.raises(WorkspaceNotFound):
            f.services.sounds.prepare_import(
                Path("/never-read.wav"), "denied", workspace_id=foreign
            )
        with pytest.raises(WorkspaceNotFound):
            sound_imports.import_audio(
                f.case.database,
                f.services.paths,
                f.services.artifacts,
                Path("/never-read.wav"),
                "denied",
                workspace_id=foreign,
            )


@pytest.mark.parametrize("phase", ["stream", "prepared"])
def test_team_departure_during_import_stops_inspection_or_publication(library, monkeypatch, phase):
    f, u = library, library.users
    retained = f.services.paths.root / "sounds/uploads/retained.wav"
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes(b"Prior upload")
    before = counts(f)
    original = audio_import_routes.prepare_audio_import

    def depart():
        with f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))

    def prepare(*args, **kwargs):
        assert phase == "prepared", "Stream departure must stop before inspection"
        prepared = original(*args, **kwargs)
        depart()
        return prepared

    class DepartingStream(io.BytesIO):
        changed = False

        def readinto(self, buffer):
            if not self.changed:
                self.changed = True
                depart()
            return super().readinto(buffer)

    monkeypatch.setattr(audio_import_routes, "prepare_audio_import", prepare)
    response = upload(
        u.alice,
        u.ah,
        query={"workspace_id": u.workspace_id},
        stream=DepartingStream(silent_wav()) if phase == "stream" else None,
    )
    assert response.status_code == 404, response.get_json()
    assert counts(f) == before and retained.read_bytes() == b"Prior upload"
    assert list(retained.parent.iterdir()) == [retained]


def test_generation_default_and_selected_workspace_have_separate_retry_namespaces(
    library, monkeypatch
):
    u = library.users
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda *_a, **_kw: None)
    monkeypatch.setattr(audio_cpp_sfx, "request_runtime_identity", synthetic_identity)
    payload = {"prompt": "A synthetic soft chime", "seed": 733}
    personal = body(
        u.alice.post(
            "/api/v1/sounds/generate",
            json=payload,
            headers=headers(u.ah, "same-key-personal-and-team"),
        ),
        202,
    )
    shared = body(
        u.alice.post(
            "/api/v1/sounds/generate",
            query_string={"workspace_id": u.workspace_id},
            json=payload,
            headers=headers(u.ah, "same-key-personal-and-team"),
        ),
        202,
    )
    assert personal["candidate_id"] != shared["candidate_id"]
    for query, expected in (({}, personal), ({"workspace_id": u.workspace_id}, shared)):
        response = u.alice.post(
            "/api/v1/sounds/generate",
            query_string=query,
            json=payload,
            headers=headers(u.ah, "same-key-personal-and-team"),
        )
        assert (
            body(response, 202) == expected and response.headers["Idempotency-Replayed"] == "true"
        )


@pytest.mark.parametrize("phase", ["body", "prepared"])
def test_generation_rechecks_current_scope_before_retry_and_enqueue(library, monkeypatch, phase):
    f, u = library, library.users
    token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read", "app.run"}, workspace=u.workspace_id
    )
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda *_a, **_kw: None)
    monkeypatch.setattr(audio_cpp_sfx, "request_runtime_identity", synthetic_identity)
    before = counts(f)

    def revoke():
        with f.case.database.immediate_session() as session:
            session.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()

    original = Request.get_json

    def parse(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.path == "/api/v1/sounds/generate":
            revoke()
        return result

    prepared_original = f.services.sounds.prepare_generation

    def prepare(*args, **kwargs):
        result = prepared_original(*args, **kwargs)
        revoke()
        return result

    if phase == "body":
        monkeypatch.setattr(Request, "get_json", parse)
    else:
        monkeypatch.setattr(f.services.sounds, "prepare_generation", prepare)
    response = f.case.app.test_client().post(
        "/api/v1/sounds/generate",
        query_string={"workspace_id": u.workspace_id},
        json={"prompt": "Synthetic sound", "seed": 9},
        headers=headers(bearer, "revoked-sound-before-admission"),
    )
    assert response.status_code == 403, response.get_json()
    assert counts(f) == before
