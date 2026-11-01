"""Public provider admission uses current workspace rights and one job writer."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web import voice_routes as routes
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import PublishVoiceInput, UnpublishVoiceInput
from pandrator_mcp.tools.voice_lifecycle import publish_voice, unpublish_voice
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_crud_workspace_access import create, sample
from tests.test_voice_crud_workspace_access import no_network as no_network  # noqa: F401


def configure(case, workspace_id, *, enabled=True):
    with case.database.immediate_session() as session:
        row = session.get(m.WorkspaceSetting, (workspace_id, "services.tts"))
        if row is None:
            row = m.WorkspaceSetting(workspace_id=workspace_id, key="services.tts", revision=1)
            session.add(row)
        else:
            row.revision += 1
        row.value_json = {"service_configs": [{"id": "xtts", "enabled": enabled,
            "api_base": "https://provider.invalid/v1", "connection_mode": "external"}]}


@pytest.fixture
def provider(case, monkeypatch):
    users = team(case, monkeypatch)
    voice = body(create(users.alice, users.ah, "Shared provider reference", wid=users.workspace_id), 201)
    sid, aid, path = sample(case, voice)
    configure(case, users.workspace_id)
    with case.database.immediate_session() as session:
        row = session.get_one(m.Voice, voice["id"])
        row.metadata_json = {"providers": {"xtts": {
            "voice_id": "managed-reference", "sample_id": sid, "managed_by": "pandrator",
            "resource_kind": "uploaded_reference", "endpoint_fingerprint": "fixture"}}}
    return SimpleNamespace(case=case, users=users, voice=voice, sid=sid, aid=aid, path=path,
        original_receipts=count(case, m.ApiIdempotency),
        services=case.app.extensions["pandrator"]["services"])


def submit(f, *, method="post", client=None, csrf=None, workspace=None, key=None, revision=1):
    query = {} if workspace is None else {"workspace_id": workspace}
    return getattr(client or f.users.alice, method)(
        f"/api/v1/voices/{f.voice['id']}/providers/xtts", query_string=query,
        headers={**(headers(csrf or f.users.ah, key) if key else (csrf or f.users.ah)),
            "If-Match": f'"{revision}"'})


def count(case, model):
    with case.database.snapshot_session() as session:
        return session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("method,kind", [("post", "voice.publish"), ("delete", "voice.unpublish")])
def test_member_admission_uses_stored_owner_current_actor_and_only_atomic_enqueue(provider, monkeypatch, method, kind):
    f = provider
    monkeypatch.setattr(f.services.jobs, "enqueue", lambda *_a, **_kw: pytest.fail("Non-atomic enqueue"))
    response = submit(f, method=method, workspace=f.users.workspace_id)
    queued = body(response, 202)
    assert queued["workspace_id"] == f.users.workspace_id
    with f.case.database.snapshot_session() as session:
        job = session.get_one(m.Job, queued["id"])
        actor = session.get_one(m.JobActorRecord, job.id)
        assert actor.user_id == f.users.alice_id
        assert job.kind == kind and job.workspace_id == f.users.workspace_id
        assert job.project_id is None and job.session_id is None
        assert job.max_attempts == 1 and job.external_effect_json is None
        assert job.payload_json["tts_settings_revisions"] == {"services.tts": 1, "defaults.tts": 0}
        if method == "post":
            assert job.payload_json["sample_id"] == f.sid
            assert job.payload_json["sample_artifact_id"] == f.aid
            assert len(job.payload_json["sample_sha256"]) == 64
        assert "provider.invalid" not in str(job.payload_json)
    assert f.path.exists()


@pytest.mark.parametrize("method", ["post", "delete"])
def test_denials_precede_protected_files_settings_and_enqueue(provider, monkeypatch, method):
    f = provider
    private = body(create(f.users.bob, f.users.bh, "Private provider voice"), 201)
    private_path = f"/api/v1/voices/{private['id']}/providers/xtts"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied provider request read protected state")

    monkeypatch.setattr(routes, "verified_sample_artifact", forbidden)
    monkeypatch.setattr(routes, "workspace_setting_in_workspace", forbidden)
    monkeypatch.setattr(f.services.jobs, "enqueue_in_session", forbidden)
    for client, csrf in ((f.users.alice, f.users.ah), (f.users.owner, f.users.oh)):
        assert getattr(client, method)(private_path, headers={**csrf, "If-Match": '"1"'}).status_code == 404
    for scopes in ({"app.read"}, {"app.read", "app.write"}, {"app.read", "app.run"}):
        _token, bearer = scoped_token(f.case, f.users.alice_id, scopes, workspace=f.users.workspace_id)
        assert submit(f, method=method, csrf=bearer).status_code == 403
    assert submit(f, method=method, workspace=private["workspace_id"]).status_code == 404
    for query in ({"workspace_id": " "}, {"workspace_id": "w" * 81}, {"session_id": "unavailable"}):
        assert getattr(f.users.alice, method)(f"/api/v1/voices/{f.voice['id']}/providers/xtts",
            query_string=query, headers={**f.users.ah, "If-Match": '"1"'}).status_code == 422
    assert count(f.case, m.Job) == 0


@pytest.mark.parametrize("method", ["post", "delete"])
def test_receipts_are_actor_scoped_and_replay_before_mutable_sources_under_current_rights(provider, method):
    f = provider
    key = "shared-provider-admission"
    first = body(submit(f, method=method, key=key), 202)
    bob = body(submit(f, method=method, client=f.users.bob, csrf=f.users.bh, key=key), 202)
    assert first["id"] != bob["id"]
    with f.case.database.immediate_session() as session:
        session.get_one(m.Voice, f.voice["id"]).revision += 1
        session.delete(session.get_one(m.VoiceSample, f.sid))
    replay = submit(f, method=method, key=key)
    assert body(replay, 202) == first and replay.headers["Idempotency-Replayed"] == "true"
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
    assert submit(f, method=method, key=key).status_code == 404
    assert count(f.case, m.Job) == 2


@pytest.mark.parametrize("loss", ["membership", "source"])
def test_post_hash_authority_and_final_source_headers_are_rechecked(provider, monkeypatch, loss):
    f = provider
    real = routes.verified_sample_artifact

    def validate_then_change(*args, **kwargs):
        result = real(*args, **kwargs)
        with f.case.database.immediate_session() as session:
            if loss == "membership":
                session.delete(session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)))
            else:
                session.get_one(m.Artifact, f.aid).state = "deleted"
        return result

    monkeypatch.setattr(routes, "verified_sample_artifact", validate_then_change)
    assert submit(f, key="provider-after-hash").status_code == (404 if loss == "membership" else 409)
    assert count(f.case, m.Job) == 0
    assert count(f.case, m.ApiIdempotency) == f.original_receipts


@pytest.mark.parametrize("method", ["post", "delete"])
def test_configuration_change_between_capture_and_writer_does_not_enqueue(provider, monkeypatch, method):
    f = provider
    capture = routes.capture_voice_provider_settings

    def capture_then_change(session, workspace_id):
        value = capture(session, workspace_id)
        configure(f.case, workspace_id)
        return value

    monkeypatch.setattr(routes, "capture_voice_provider_settings", capture_then_change)
    response = submit(f, method=method, key="provider-settings-race")
    assert response.status_code == 422
    assert "settings changed" in response.get_json()["error"]["message"]
    assert count(f.case, m.Job) == 0
    assert count(f.case, m.ApiIdempotency) == f.original_receipts


@pytest.mark.parametrize("method", ["post", "delete"])
def test_disabled_workspace_connection_is_not_replaced_with_a_builtin(provider, method):
    configure(provider.case, provider.users.workspace_id, enabled=False)
    assert submit(provider, method=method).status_code == 404
    assert count(provider.case, m.Job) == 0


def test_real_mcp_publication_and_removal_builders_reach_the_proved_workspace(provider):
    f = provider

    class LocalProviderClient(ApplicationVoiceMethods):
        def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
            request_headers = {**f.users.ah, "If-Match": f'"{kwargs["if_match_revision"]}"',
                "Idempotency-Key": kwargs["idempotency_key"]}
            options = {"headers": request_headers, "query_string": kwargs.get("parameters")}
            if "body" in kwargs:
                options["json"] = kwargs["body"]
            return body(f.users.alice.open(path, method=kwargs["method"], **options), 202)

        def _request_binary_json(self, path: str, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"Unexpected provider binary transport: {path}")

    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: LocalProviderClient()))
    common = {"voice_id": f.voice["id"], "service_id": "xtts",
        "expected_revision": 1, "workspace_id": f.users.workspace_id}
    outcomes = (
        publish_voice(runtime, PublishVoiceInput.model_validate(common | {"idempotency_key": "mcp-provider-publish"})),
        unpublish_voice(runtime, UnpublishVoiceInput.model_validate(common | {"idempotency_key": "mcp-provider-remove"})),
    )
    for outcome in outcomes:
        assert outcome.result["workspace_id"] == f.users.workspace_id
        assert outcome.work is not None
        queued = body(f.users.alice.get(f"/api/v1/jobs/{outcome.work.id}"))
        assert queued["payload_json"]["workspace_id"] == f.users.workspace_id
        assert queued["payload_json"]["tts_settings_revisions"]["services.tts"] == 1
