"""Finite real-user voice record/sample authority and CREATE receipt isolation."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web import voice_routes as routes
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token, sql_capture
from tests.test_voice_lifecycle_idempotency import silent_wav


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Voice CRUD acceptance must stay local")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def create(client, csrf, name="Shared name", *, wid=None, key=None, profile=None):
    payload = {"name": name}
    if profile is not None:
        payload["profile"] = profile
    return client.post(
        "/api/v1/voices",
        query_string={} if wid is None else {"workspace_id": wid},
        json=payload,
        headers=headers(csrf, key) if key else csrf,
    )


def sample(case, voice):
    services = case.app.extensions["pandrator"]["services"]
    path = services.paths.voices / voice["id"] / "synthetic.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(silent_wav())
    artifact = services.artifacts.register(
        path, kind="audio", role="voice_sample", workspace_id=voice["workspace_id"]
    )
    with case.database.immediate_session() as session:
        row = m.VoiceSample(
            workspace_id=voice["workspace_id"], voice_id=voice["id"], artifact_id=artifact.id
        )
        session.add(row)
        session.flush()
        identifier = row.id
    return identifier, artifact.id, path


def evidence(artifact):
    return {
        "pitch": "low",
        "evidence": {
            "pitch": {"source": "audition_review", "status": "reviewed", "artifact_id": artifact}
        },
    }


def test_personal_create_and_cross_member_team_voice_and_sample_metadata(case, monkeypatch):
    users = team(case, monkeypatch)
    a = body(create(users.alice, users.ah), 201)
    b = body(create(users.bob, users.bh), 201)
    shared = body(create(users.alice, users.ah, wid=users.workspace_id), 201)
    assert len({a["workspace_id"], b["workspace_id"], shared["workspace_id"]}) == 3
    sid, aid, path = sample(case, shared)
    with case.database.immediate_session() as session:
        row = session.get_one(m.Voice, shared["id"])
        row.metadata_json = {
            **row.metadata_json,
            "provenance": "keep",
            "providers": {"synthetic": {"status": "ready", "sample_id": sid}},
        }
    changed = users.bob.patch(
        f"/api/v1/voices/{shared['id']}",
        json={"name": "Teammate edit", "profile": evidence(aid)},
        headers={**users.bh, "If-Match": '"1"'},
    )
    updated = body(changed)
    assert (
        updated["workspace_id"] == users.workspace_id
        and updated["revision"] == 2
        and changed.headers["ETag"] == '"2"'
    )
    _, read = scoped_token(case, users.bob_id, {"app.read"}, workspace=users.workspace_id)
    listed = body(users.bob.get(f"/api/v1/voices/{shared['id']}/samples", headers=read))
    assert listed["workspace_id"] == users.workspace_id and listed["voice_revision"] == 2
    assert listed["items"][0]["id"] == sid and listed["items"][0]["file_status"] == "ready"
    assert (
        users.bob.delete(
            f"/api/v1/voices/{shared['id']}/samples/{sid}", headers={**read, "If-Match": '"2"'}
        ).status_code
        == 403
    )
    deleted = users.bob.delete(
        f"/api/v1/voices/{shared['id']}/samples/{sid}", headers={**users.bh, "If-Match": '"2"'}
    )
    assert body(deleted)["workspace_id"] == users.workspace_id and deleted.headers["ETag"] == '"3"'
    assert not path.exists()
    with case.database.snapshot_session() as session:
        assert session.get_one(m.Artifact, aid).state == "deleted"
        metadata = session.get_one(m.Voice, shared["id"]).metadata_json
        assert (
            metadata["provenance"] == "keep"
            and metadata["providers"]["synthetic"]["status"] == "stale"
        )


def test_foreign_by_id_and_project_viewing_deny_before_payload_or_cleanup(case, monkeypatch):
    users = team(case, monkeypatch)
    private = body(create(users.alice, users.ah), 201)
    shared = body(create(users.alice, users.ah, "Team", wid=users.workspace_id), 201)
    root = body(
        users.owner.post(
            "/api/v1/sessions",
            json={"name": "Private viewed", "workflow_kind": "subtitles"},
            headers=headers(users.oh, "crud-viewed-root"),
        ),
        201,
    )
    current = body(users.owner.get(f"/api/v1/projects/{root['project_id']}/viewers"))
    body(
        users.owner.put(
            f"/api/v1/projects/{root['project_id']}/viewers/{users.alice_id}",
            json={"expected_revision": current["revision"]},
            headers=users.oh,
        )
    )
    owner_voice = body(create(users.owner, users.oh, "Owner private"), 201)
    assert users.alice.get(f"/api/v1/sessions/{root['id']}").status_code == 200

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied voice lookup reached protected payload/files")

    for name in (
        "voice_payload",
        "voice_sample_payload",
        "retire_sample_artifact",
        "remove_managed_files",
        "validate_profile_evidence",
    ):
        monkeypatch.setattr(routes, name, forbidden)
    for client, csrf, voice in (
        (users.owner, users.oh, private),
        (users.bob, users.bh, private),
        (users.alice, users.ah, owner_voice),
    ):
        target = f"/api/v1/voices/{voice['id']}"
        assert client.get(target + "/samples").status_code == 404
        assert (
            client.patch(
                target, json={"name": "Forbidden"}, headers={**csrf, "If-Match": '"1"'}
            ).status_code
            == 404
        )
        assert client.delete(target, headers={**csrf, "If-Match": '"1"'}).status_code == 404
        assert (
            client.delete(
                target + "/samples/missing", headers={**csrf, "If-Match": '"1"'}
            ).status_code
            == 404
        )
    assert (
        users.bob.get(
            f"/api/v1/voices/{shared['id']}/samples",
            query_string={"workspace_id": private["workspace_id"]},
        ).status_code
        == 404
    )
    _, project_only = scoped_token(case, users.alice_id, {"app.read"}, project=root["project_id"])
    assert (
        users.alice.get(
            f"/api/v1/voices/{owner_voice['id']}/samples", headers=project_only
        ).status_code
        == 404
    )
    with case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.bob_id)))
    assert users.bob.get(f"/api/v1/voices/{shared['id']}/samples").status_code == 404


def test_foreign_evidence_sample_and_failed_cas_are_atomic(case, monkeypatch):
    users = team(case, monkeypatch)
    own = body(create(users.alice, users.ah), 201)
    other = body(create(users.bob, users.bh), 201)
    sid, aid, path = sample(case, other)
    bad = create(
        users.alice, users.ah, "Must rollback", key="foreign-evidence", profile=evidence(aid)
    )
    assert bad.status_code == 422
    assert (
        users.alice.patch(
            f"/api/v1/voices/{own['id']}",
            json={"name": "Must rollback", "profile": evidence(aid)},
            headers={**users.ah, "If-Match": '"1"'},
        ).status_code
        == 422
    )
    assert (
        users.alice.delete(
            f"/api/v1/voices/{own['id']}/samples/{sid}", headers={**users.ah, "If-Match": '"1"'}
        ).status_code
        == 404
    )
    assert (
        users.bob.delete(
            f"/api/v1/voices/{other['id']}/samples/{sid}", headers={**users.bh, "If-Match": '"0"'}
        ).status_code
        == 409
    )
    assert (
        users.bob.delete(
            f"/api/v1/voices/{other['id']}", headers={**users.bh, "If-Match": '"0"'}
        ).status_code
        == 409
    )
    assert path.exists()
    with case.database.snapshot_session() as session:
        assert session.get_one(m.Voice, own["id"]).name == "Shared name"
        assert (
            session.scalar(
                select(func.count()).select_from(m.Voice).where(m.Voice.name == "Must rollback")
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.ApiIdempotency)
                .where(m.ApiIdempotency.idempotency_key == "foreign-evidence")
            )
            == 0
        )


def test_create_receipt_isolates_real_users_workspaces_and_preserves_legacy_deleted_retry(
    case, monkeypatch
):
    first = create(
        case.client, {"X-CSRF-Token": case.csrf}, "Legacy exact", key="crud-legacy-exact"
    )
    original = body(first, 201)
    assert (
        case.client.delete(
            f"/api/v1/voices/{original['id']}",
            headers={"X-CSRF-Token": case.csrf, "If-Match": first.headers["ETag"]},
        ).status_code
        == 204
    )
    with case.database.immediate_session() as session:
        receipt = session.scalar(
            select(m.ApiIdempotency).where(m.ApiIdempotency.idempotency_key == "crud-legacy-exact")
        )
        receipt.response_json = {
            k: v for k, v in receipt.response_json.items() if k != "workspace_id"
        }
    users = team(case, monkeypatch)
    replay = create(
        case.client, {"X-CSRF-Token": case.csrf}, "Legacy exact", key="crud-legacy-exact"
    )
    assert body(replay, 201) == {k: v for k, v in original.items() if k != "workspace_id"}
    assert (
        replay.headers["Idempotency-Replayed"] == "true"
        and replay.headers["ETag"] == first.headers["ETag"]
    )
    a, ah = scoped_token(
        case, users.alice_id, {"app.read", "app.write"}, workspace=users.workspace_id
    )
    b, bh = scoped_token(
        case, users.bob_id, {"app.read", "app.write"}, workspace=users.workspace_id
    )
    with case.database.immediate_session() as session:
        for token in (a, b):
            session.get_one(m.ApiToken, token.id).subject = "same-actual-label"
    av = body(
        create(users.alice, ah, "Alice scoped", wid=users.workspace_id, key="same-crud-key"), 201
    )
    bv = body(create(users.bob, bh, "Bob scoped", wid=users.workspace_id, key="same-crud-key"), 201)
    assert av["id"] != bv["id"]
    home = body(create(users.alice, users.ah, "Home command", key="same-root-key"), 201)
    shared = body(
        create(users.alice, users.ah, "Team command", wid=users.workspace_id, key="same-root-key"),
        201,
    )
    assert home["workspace_id"] != shared["workspace_id"]
    _, read = scoped_token(case, users.alice_id, {"app.read"}, workspace=users.workspace_id)
    _, write_only = scoped_token(case, users.alice_id, {"app.write"}, workspace=users.workspace_id)
    for bearer in (read, write_only):
        assert (
            create(
                users.alice, bearer, "Team command", wid=users.workspace_id, key="same-root-key"
            ).status_code
            == 403
        )


@pytest.mark.parametrize("loss", ["scope", "revocation"])
def test_create_actual_writer_rejects_current_credential_loss_before_receipt_or_evidence(
    case, monkeypatch, loss
):
    users = team(case, monkeypatch)
    token, bearer = scoped_token(
        case, users.alice_id, {"app.read", "app.write"}, workspace=users.workspace_id
    )
    original = routes.VoiceCreate.model_validate

    def parsed(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        with case.database.immediate_session() as session:
            row = session.get_one(m.ApiToken, token.id)
            if loss == "scope":
                row.scopes_json = ["app.read"]
            else:
                row.revoked_at = m.utcnow()
        return result

    monkeypatch.setattr(routes.VoiceCreate, "model_validate", parsed)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Lost authority reached evidence or receipt")

    monkeypatch.setattr(routes, "validate_profile_evidence", forbidden)
    monkeypatch.setattr(
        case.app.extensions["pandrator"]["services"].idempotency, "begin", forbidden
    )
    result = create(
        users.alice, bearer, "No effects", wid=users.workspace_id, key="writer-loss-key"
    )
    assert result.status_code == 403, result.get_json()
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(m.Voice).where(m.Voice.name == "No effects")
            )
            == 0
        )


def test_busy_delete_uses_scoped_job_headers_and_cleanup_only_after_commit(case, monkeypatch):
    users = team(case, monkeypatch)
    voice = body(create(users.alice, users.ah, wid=users.workspace_id), 201)
    sid, aid, path = sample(case, voice)
    foreign = body(create(users.owner, users.oh, "Foreign job scope"), 201)
    with case.database.immediate_session() as session:
        job = m.Job(
            kind="voice.synthetic",
            workspace_id=users.workspace_id,
            status="queued",
            resource_keys_json=[f"voice:{voice['id']}"],
            payload_json={"PRIVATE": "large" * 10000},
        )
        session.add(job)
        session.flush()
        jid = job.id
        session.add(
            m.Job(
                kind="voice.synthetic",
                workspace_id=foreign["workspace_id"],
                status="queued",
                resource_keys_json=[f"voice:{voice['id']}"],
                payload_json={"PRIVATE": "other" * 10000},
            )
        )
    target = f"/api/v1/voices/{voice['id']}"
    with sql_capture(case.database) as statements:
        result = users.bob.delete(target, headers={**users.bh, "If-Match": '"1"'})
    assert (
        result.status_code == 409
        and result.get_json()["error"]["code"] == "voice_busy"
        and path.exists()
    )
    queries = [statement for statement, _parameters in statements if "FROM jobs" in statement]
    assert len(queries) == 1 and "jobs.id, jobs.resource_keys_json" in queries[0]
    assert (
        "jobs.workspace_id =" in queries[0]
        and "payload_json" not in queries[0]
        and "result_json" not in queries[0]
    )
    with case.database.immediate_session() as session:
        session.get_one(m.Job, jid).status = "completed"
    real_cleanup = routes.remove_managed_files
    seen = []

    def cleanup(paths):
        with case.database.immediate_session() as session:
            assert session.get(m.Voice, voice["id"]) is None
            assert session.get(m.VoiceSample, sid) is None
            assert session.get_one(m.Artifact, aid).state == "deleted"
        seen.extend(paths)
        assert path.exists()
        real_cleanup(paths)

    monkeypatch.setattr(routes, "remove_managed_files", cleanup)
    assert users.bob.delete(target, headers={**users.bh, "If-Match": '"1"'}).status_code == 204
    assert seen == [path.resolve()] and not path.exists()
    Path(
        ".local-notes/multi-user-2026-10-23/continued-implementation/workspace-sharing-r3/voice-crud-workspace/job-header-sql.txt"
    ).write_text(queries[0])


def test_bundle_protection_and_strict_scope_preserve_records_and_files(case, monkeypatch):
    users = team(case, monkeypatch)
    voice = body(create(users.alice, users.ah), 201)
    sid, _aid, path = sample(case, voice)
    with case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).metadata_json = {"bundled_voice": True}
    for method, target in (
        (users.alice.patch, f"/api/v1/voices/{voice['id']}"),
        (users.alice.delete, f"/api/v1/voices/{voice['id']}"),
        (users.alice.delete, f"/api/v1/voices/{voice['id']}/samples/{sid}"),
    ):
        options = {"headers": {**users.ah, "If-Match": '"1"'}}
        if method == users.alice.patch:
            options["json"] = {"name": "Not changed"}
        assert method(target, **options).status_code == 409
    assert path.exists()
    for query in (
        {"workspace_id": ""},
        {"workspace_id": " "},
        {"workspace_id": "x" * 81},
        {"session_id": ""},
    ):
        assert (
            users.alice.get(f"/api/v1/voices/{voice['id']}/samples", query_string=query).status_code
            == 422
        )
        assert create(users.alice, users.ah, wid=query.get("workspace_id", " ")).status_code == 422
