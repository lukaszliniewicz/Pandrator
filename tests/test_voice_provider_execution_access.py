"""Stored member actors/leases, guarded dispatch and durable provider responses."""

from __future__ import annotations

import socket
import threading
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import (
    HostActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.auth import Principal
from pandrator.web.voice_provider_effects import (
    VoiceProviderEffect,
    VoiceProviderEffectError,
    binding_fingerprint,
    voice_provider_effect_summary,
)
from pandrator.web.voice_provider_settings import (
    capture_voice_provider_settings,
    check_voice_provider_settings,
)
from pandrator.web.workflow_voice import _provider_endpoint_fingerprint
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_voice_sample_execution_access import (
    claimed,
    enqueue,
    job,
    run,
)
from tests.test_voice_sample_execution_access import sample_case as sample_case  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Voice provider worker acceptance must not access the network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def provider_case(sample_case, monkeypatch):
    f = sample_case
    f.calls.clear()
    f.hook.update(before=None, during=None, fail=None)
    f.cancel = threading.Event()
    with f.case.database.immediate_session() as session:
        session.add(
            m.WorkspaceSetting(
                workspace_id=f.users.workspace_id,
                key="services.tts",
                revision=3,
                value_json={
                    "provider_configs": [
                        {
                            "id": "xtts",
                            "connection_mode": "external",
                            "api_base": "http://synthetic.invalid",
                        }
                    ]
                },
            )
        )

    def transport(operation, before_io):
        if f.hook["before"]:
            f.hook["before"]()
        before_io()
        f.calls.append(operation)
        if f.hook["during"]:
            f.hook["during"]()
        if f.hook["fail"]:
            raise RuntimeError("Synthetic transport outcome uncertain")

    def upload(*_args, before_io, **_kwargs):
        transport("upload", before_io)
        return "opaque/provider:voice?version=1"

    def delete(*_args, before_io, **_kwargs):
        transport("delete", before_io)
        return True

    monkeypatch.setattr(f.services.workflow_handlers.tts_providers, "upload_voice", upload)
    monkeypatch.setattr(f.services.workflow_handlers.tts_providers, "delete_voice", delete)
    return f


def payload(f, operation):
    with f.case.database.snapshot_session() as session:
        voice = session.get_one(m.Voice, f.voice["id"])
        result = {
            "workspace_id": f.users.workspace_id,
            "voice_id": voice.id,
            "service_id": "xtts",
            "expected_voice_revision": voice.revision,
            **capture_voice_provider_settings(session, f.users.workspace_id),
        }
    if operation == "voice.publish":
        result.update(
            sample_id=f.sid, sample_artifact_id=f.source.id, sample_sha256=f.source.content_hash
        )
    return result


def binding(f):
    with f.case.database.snapshot_session() as session:
        voice = session.get(m.Voice, f.voice["id"])
        return (
            deepcopy((voice.metadata_json or {}).get("providers", {}).get("xtts"))
            if voice
            else None
        )


def seed_binding(f):
    with f.case.database.immediate_session() as session:
        voice = session.get_one(m.Voice, f.voice["id"])
        voice.metadata_json = {
            "providers": {
                "xtts": {
                    "voice_id": "opaque/provider:voice?version=1",
                    "sample_id": f.sid,
                    "managed_by": "pandrator",
                    "resource_kind": "uploaded_reference",
                    "endpoint_fingerprint": _provider_endpoint_fingerprint(
                        "http://synthetic.invalid"
                    ),
                }
            }
        }


def call(f, operation, data, progress=lambda *_args: None):
    handler = (
        f.services.workflow_handlers.publish_voice
        if operation == "voice.publish"
        else f.services.workflow_handlers.unpublish_voice
    )
    return handler(data, progress, f.cancel)


def test_member_worker_upload_removal_receipts_and_real_actor_records(provider_case):
    f = provider_case
    for operation, state in (("voice.publish", "ready"), ("voice.unpublish", "removed")):
        queued = enqueue(f, operation, payload(f, operation))
        run(f)
        saved = job(f, queued.id)
        assert saved.status == "succeeded", saved.error_message
        assert saved.external_effect_json["state"] == "completed"
        assert saved.external_effect_json["binding_state"] == state
        assert saved.external_effect_json["handler_result"] == saved.result_json
        with f.case.database.snapshot_session() as session:
            assert session.get_one(m.JobActorRecord, queued.id).user_id == f.users.alice_id
    assert f.calls == ["upload", "delete"] and binding(f) is None


@pytest.mark.parametrize("operation", ["voice.publish", "voice.unpublish"])
def test_membership_loss_inside_transport_before_io_never_dispatches(provider_case, operation):
    f = provider_case
    if operation == "voice.unpublish":
        seed_binding(f)
    original = binding(f)
    queued = enqueue(f, operation, payload(f, operation))

    def revoke():
        with f.case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
            )

    f.hook["before"] = revoke
    run(f)
    saved = job(f, queued.id)
    assert saved.status == "failed" and saved.external_effect_json is None
    assert not f.calls and binding(f) == original


@pytest.mark.parametrize(
    "operation,loss",
    [
        ("voice.publish", "membership"),
        ("voice.unpublish", "membership"),
        ("voice.publish", "lease"),
        ("voice.unpublish", "lease"),
        ("voice.publish", "binding"),
        ("voice.unpublish", "binding"),
        ("voice.publish", "voice"),
        ("voice.unpublish", "voice"),
        ("voice.publish", "settings"),
        ("voice.unpublish", "settings"),
        ("voice.publish", "archive"),
        ("voice.publish", "revocation"),
    ],
)
def test_response_receipt_survives_without_overwriting_local_state(provider_case, operation, loss):
    f = provider_case
    if operation == "voice.unpublish":
        seed_binding(f)
    original = binding(f)
    replacement = {**(original or {"voice_id": "newer-binding"}), "other": True}
    queued = enqueue(f, operation, payload(f, operation))

    def change():
        with f.case.database.immediate_session() as session:
            if loss == "membership":
                session.delete(
                    session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
                )
            elif loss == "lease":
                session.get_one(m.Job, queued.id).lease_expires_at = m.utcnow() - timedelta(
                    seconds=1
                )
            elif loss == "binding":
                voice = session.get_one(m.Voice, f.voice["id"])
                voice.metadata_json = {"providers": {"xtts": replacement}}
            elif loss == "voice":
                session.delete(session.get_one(m.Voice, f.voice["id"]))
            elif loss == "settings":
                row = session.scalar(
                    select(m.WorkspaceSetting).where(
                        m.WorkspaceSetting.workspace_id == f.users.workspace_id,
                        m.WorkspaceSetting.key == "services.tts",
                    )
                )
                row.revision += 1
            elif loss == "archive":
                session.get_one(m.Workspace, f.users.workspace_id).state = "archived"
            else:
                session.get_one(m.ApiToken, f.token.id).revoked_at = m.utcnow()

    f.hook["during"] = change
    with claimed(f, queued) as (data, _lease):
        with pytest.raises(VoiceProviderEffectError, match="provider_effect_local_not_applied"):
            call(f, operation, data)
    saved = job(f, queued.id)
    assert saved.status == "running" and saved.result_json is None
    assert saved.external_effect_json["state"] == "completed"
    assert saved.external_effect_json["binding_state"] == "not_applied"
    assert saved.external_effect_json["provider_voice_id"] == "opaque/provider:voice?version=1"
    assert binding(f) == (
        replacement if loss == "binding" else None if loss == "voice" else original
    )
    assert f.calls == ["upload" if operation == "voice.publish" else "delete"]


@pytest.mark.parametrize(
    "transport", ["expired_token", "logged_out_browser", "explicit_revocation"]
)
def test_natural_transport_end_continues_but_explicit_token_revocation_denies(
    provider_case, transport
):
    f = provider_case
    actor = f.actor
    if transport == "logged_out_browser":
        with f.users.alice.session_transaction() as state:
            browser = f.case.auth.browser_sessions.resolve(state["auth_session"])
        actor = actor_from_principal(
            Principal(
                browser.subject,
                browser.kind,
                browser.scopes,
                None,
                "loopback",
                f.services.identity.snapshot(observed_origin="http://localhost").instance_id,
                user_id=browser.user_id,
                auth_revision=browser.auth_revision,
                browser_session_id=browser.session_id,
                client_id=browser.client_id,
            )
        )
        assert actor is not None
    queued = enqueue(f, "voice.publish", payload(f, "voice.publish"), actor=actor)
    with f.case.database.immediate_session() as session:
        if transport == "logged_out_browser":
            from pandrator.web.accounts.auth_models import BrowserAuthSession

            session.get_one(BrowserAuthSession, actor.browser_session_id).revoked_at = m.utcnow()
        elif transport == "explicit_revocation":
            session.get_one(m.ApiToken, f.token.id).revoked_at = m.utcnow()
        else:
            session.get_one(m.ApiToken, f.token.id).expires_at = m.utcnow() - timedelta(seconds=1)
    run(f)
    saved = job(f, queued.id)
    assert saved.status == ("failed" if transport == "explicit_revocation" else "succeeded"), (
        saved.error_message
    )
    assert f.calls == ([] if transport == "explicit_revocation" else ["upload"])


def test_completed_retry_precedes_changed_or_missing_voice_validation_and_never_repeats(
    provider_case,
):
    f = provider_case
    queued = enqueue(f, "voice.publish", payload(f, "voice.publish"))
    with claimed(f, queued) as (data, _lease):
        first = call(f, "voice.publish", data)
        with f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.Voice, f.voice["id"]))
        assert call(f, "voice.publish", data) == first
    assert f.calls == ["upload"]


def test_uncertain_response_retry_never_repeats_and_does_not_need_voice(provider_case):
    f = provider_case
    queued = enqueue(f, "voice.publish", payload(f, "voice.publish"))
    f.hook["fail"] = True
    with claimed(f, queued) as (data, _lease):
        with pytest.raises(RuntimeError, match="uncertain"):
            call(f, "voice.publish", data)
        with f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.Voice, f.voice["id"]))
        with pytest.raises(VoiceProviderEffectError, match="provider_effect_uncertain"):
            call(f, "voice.publish", data)
    assert f.calls == ["upload"] and job(f, queued.id).external_effect_json["state"] == "uncertain"


def test_prepared_takeover_denies_old_callback_and_dispatching_takeover_refuses(provider_case):
    f = provider_case
    queued = enqueue(f, "voice.publish", payload(f, "voice.publish"))
    with claimed(f, queued) as (data, lease):
        effect = VoiceProviderEffect.current(
            f.case.database, "voice.publish", f.users.workspace_id, f.voice["id"], "xtts"
        )
        with f.case.database.snapshot_session() as session:
            baseline = binding_fingerprint(session.get_one(m.Voice, f.voice["id"]), "xtts")

        def prove(session):
            from pandrator.web.access.voice_sample_execution_policy import (
                require_voice_sample_execution_workspace,
            )

            return require_voice_sample_execution_workspace(session, data, "voice.publish")

        old = effect.reserve({"synthetic": "request"}, baseline, prove)
        lease["lease_generation"] += 1
        data["_lease_generation"] += 1
        with f.case.database.immediate_session() as session:
            session.get_one(m.Job, queued.id).lease_generation = lease["lease_generation"]
        takeover = VoiceProviderEffect.current(
            f.case.database, "voice.publish", f.users.workspace_id, f.voice["id"], "xtts"
        )
        new = takeover.reserve({"synthetic": "request"}, baseline, prove)
        assert old != new
        with pytest.raises(VoiceProviderEffectError, match="superseded"):
            effect.dispatch(old, prove)
        takeover.dispatch(new, prove)
        with pytest.raises(VoiceProviderEffectError, match="uncertain"):
            takeover.reserve({"synthetic": "request"}, baseline, prove)
        assert job(f, queued.id).external_effect_json["state"] == "dispatching"
        with pytest.raises(VoiceProviderEffectError, match="uncertain"):
            call(f, "voice.publish", data)
    assert not f.calls


def test_settings_shape_missing_fence_and_direct_host_compatibility(provider_case):
    f = provider_case
    with f.case.database.snapshot_session() as session:
        captured = capture_voice_provider_settings(session, f.users.workspace_id)
        assert captured == {"tts_settings_revisions": {"services.tts": 3, "defaults.tts": 0}}
        with execution_actor_scope(f.actor):
            for fence in (
                None,
                {},
                {"services.tts": True, "defaults.tts": 0},
                {"services.tts": -1, "defaults.tts": 0},
            ):
                with pytest.raises(ValueError, match="invalid captured"):
                    check_voice_provider_settings(
                        session, {"tts_settings_revisions": fence}, f.users.workspace_id
                    )
            with pytest.raises(ValueError, match="no captured"):
                check_voice_provider_settings(session, {}, f.users.workspace_id)
    data = payload(f, "voice.publish")
    for actor in (None, f.actor):
        with execution_actor_scope(actor), pytest.raises(AccessDenied):
            call(f, "voice.publish", data)
    data.pop("tts_settings_revisions")
    with execution_actor_scope(HostActor("pandrator-cli")):
        result = call(f, "voice.publish", data)
    assert result["provider_voice_id"] and f.calls == ["upload"]
    with f.case.database.snapshot_session() as session:
        assert not session.scalars(select(m.Job.external_effect_json)).all()


def test_source_change_after_upload_is_stale_and_cancellation_keeps_receipt(provider_case):
    f = provider_case
    queued = enqueue(f, "voice.publish", payload(f, "voice.publish"))

    def source_change():
        f.cancel.set()
        with f.case.database.immediate_session() as session:
            session.get_one(m.Voice, f.voice["id"]).revision += 1

    f.hook["during"] = source_change
    with claimed(f, queued) as (data, _lease):
        result = call(f, "voice.publish", data)
    saved = job(f, queued.id)
    assert saved.external_effect_json["binding_state"] == "stale"
    current_binding = binding(f)
    assert current_binding is not None
    assert current_binding["status"] == "stale" and result["cancellation_requested_after_upload"]
    assert saved.status == "running" and saved.result_json is None


def test_recording_after_replaced_lease_never_clobbers_new_job_result(provider_case):
    f = provider_case
    queued = enqueue(f, "voice.publish", payload(f, "voice.publish"))
    original_generation = None

    def replace_lease():
        with f.case.database.immediate_session() as session:
            row = session.get_one(m.Job, queued.id)
            row.lease_generation += 1
            row.lease_owner = "replacement-worker"
            row.status = "succeeded"
            row.result_json = {"replacement": "canonical result"}

    f.hook["during"] = replace_lease
    with claimed(f, queued) as (data, lease):
        original_generation = lease["lease_generation"]
        with pytest.raises(VoiceProviderEffectError, match="local_not_applied"):
            call(f, "voice.publish", data)
    saved = job(f, queued.id)
    assert saved.status == "succeeded" and saved.result_json == {"replacement": "canonical result"}
    assert saved.lease_owner == "replacement-worker"
    assert saved.lease_generation == original_generation + 1
    assert saved.external_effect_json["lease_generation"] == original_generation
    assert saved.external_effect_json["state"] == "completed"
    assert saved.external_effect_json["binding_state"] == "not_applied"
    assert binding(f) is None


def configure_audio_cpp(f):
    with f.case.database.immediate_session() as session:
        row = session.scalar(
            select(m.WorkspaceSetting).where(
                m.WorkspaceSetting.workspace_id == f.users.workspace_id,
                m.WorkspaceSetting.key == "services.tts",
            )
        )
        row.value_json = {
            "provider_configs": [
                {
                    "id": "audio_cpp",
                    "connection_mode": "external",
                    "api_base": "http://synthetic.invalid",
                }
            ]
        }
        row.revision += 1


def test_audio_cpp_links_and_removes_without_a_remote_receipt(provider_case):
    f = provider_case
    configure_audio_cpp(f)
    for operation in ("voice.publish", "voice.unpublish"):
        data = {**payload(f, operation), "service_id": "audio_cpp"}
        queued = enqueue(f, operation, data)
        run(f)
        saved = job(f, queued.id)
        assert saved.status == "succeeded", saved.error_message
        assert saved.result_json["linked"] is True
        assert saved.external_effect_json is None
    assert not f.calls


def test_audio_cpp_rechecks_pinned_file_after_progress_before_binding_writer(provider_case):
    f = provider_case
    configure_audio_cpp(f)
    data = {**payload(f, "voice.publish"), "service_id": "audio_cpp"}
    queued = enqueue(f, "voice.publish", data)

    def change_file(fraction, _detail):
        if fraction == 0.1:
            f.path.write_bytes(b"Changed pinned file")

    with claimed(f, queued) as (data, _lease), pytest.raises(ValueError):
        call(f, "voice.publish", data, change_file)
    with f.case.database.snapshot_session() as session:
        assert not (session.get_one(m.Voice, f.voice["id"]).metadata_json or {}).get("providers")
    assert job(f, queued.id).external_effect_json is None and not f.calls


def test_wrong_identity_or_malformed_retained_receipt_denies_before_io(provider_case):
    f = provider_case
    queued = enqueue(f, "voice.publish", payload(f, "voice.publish"))
    from tests.test_voice_provider_job_effects import receipt

    with claimed(f, queued) as (data, _lease):
        for value in ({}, receipt()):
            with f.case.database.immediate_session() as session:
                session.get_one(m.Job, queued.id).external_effect_json = value
            with pytest.raises(
                VoiceProviderEffectError, match="provider_effect_(invalid|identity)"
            ):
                call(f, "voice.publish", data)
    assert not f.calls


@pytest.mark.parametrize("operation", ["voice.publish", "voice.unpublish"])
def test_member_worker_real_provider_transport_never_uses_host_environment_key(
    sample_case,
    monkeypatch,
    operation,
):
    """Keep Worker, Registry, hydration and handler real; mock only HTTP requests."""
    from requests import Response

    f = sample_case
    sentinel = "HOST_ENV_SENTINEL_NOT_A_REAL_SECRET"
    monkeypatch.setenv("KOBOLD_QWEN_API_KEY", sentinel)
    with f.case.database.immediate_session() as session:
        session.add(
            m.WorkspaceSetting(
                workspace_id=f.users.workspace_id,
                key="services.tts",
                revision=1,
                value_json={
                    "provider_configs": [
                        {
                            "id": "kobold_qwen",
                            "connection_mode": "external",
                            "api_base": "http://synthetic.invalid",
                        }
                    ]
                },
            )
        )
        if operation == "voice.unpublish":
            voice = session.get_one(m.Voice, f.voice["id"])
            voice.metadata_json = {
                "providers": {
                    "kobold_qwen": {
                        "voice_id": "pandrator-workspace-voice",
                        "sample_id": f.sid,
                        "managed_by": "pandrator",
                        "resource_kind": "uploaded_reference",
                        "endpoint_fingerprint": _provider_endpoint_fingerprint(
                            "http://synthetic.invalid"
                        ),
                    }
                }
            }
    data = {**payload(f, operation), "service_id": "kobold_qwen"}
    queued = enqueue(f, operation, data)
    outgoing = []

    def http_request(url, *, headers, **_kwargs):
        # The real transport invokes the actual receipt/proof callback before
        # this mock, proving the real path reached the dispatch boundary.
        with f.case.database.snapshot_session() as session:
            assert session.get_one(m.JobActorRecord, queued.id).user_id == f.users.alice_id
            assert session.get_one(m.Job, queued.id).external_effect_json["state"] == "dispatching"
        outgoing.append((url, dict(headers)))
        response = Response()
        response.status_code = 200
        response._content = b'{"id":"pandrator-workspace-voice"}'
        response.url = url
        return response

    monkeypatch.setattr("requests.post", http_request)
    monkeypatch.setattr("requests.delete", http_request)
    run(f)
    saved = job(f, queued.id)
    assert saved.status == "succeeded", saved.error_message
    assert saved.external_effect_json["binding_state"] == (
        "ready" if operation == "voice.publish" else "removed"
    )
    assert len(outgoing) == 1 and "/v1/audio/voices" in outgoing[0][0]
    assert all(sentinel not in str(value) for value in outgoing[0][1].values())


@pytest.mark.parametrize(
    "field,corruption",
    [
        ("provider_voice_id", "a-different-remote-voice"),
        ("remote_deleted", False),
        ("remote_deleted", 1),
    ],
)
def test_applied_result_must_match_retained_identity_and_deletion_outcome(
    provider_case,
    field,
    corruption,
):
    f = provider_case
    seed_binding(f)
    queued = enqueue(f, "voice.unpublish", payload(f, "voice.unpublish"))
    with claimed(f, queued) as (data, _lease):
        call(f, "voice.unpublish", data)
        with f.case.database.immediate_session() as session:
            row = session.get_one(m.Job, queued.id)
            corrupted = deepcopy(row.external_effect_json)
            corrupted["handler_result"][field] = corruption
            row.external_effect_json = corrupted
            # JSON's ordinary Python equality considers True == 1. Force this
            # synthetic corruption into storage so retry reads the integer.
            flag_modified(row, "external_effect_json")
        stored = job(f, queued.id).external_effect_json
        assert type(stored["handler_result"][field]) is type(corruption)
        assert voice_provider_effect_summary(stored) is None
        with pytest.raises(VoiceProviderEffectError, match="provider_effect_invalid"):
            call(f, "voice.unpublish", data)
    assert f.calls == ["delete"]


@pytest.mark.parametrize("service_id", ["xtts", "audio_cpp"])
def test_public_sample_delete_keeps_stale_provider_copy_removable(provider_case, service_id):
    f = provider_case
    if service_id == "audio_cpp":
        configure_audio_cpp(f)
    publish = enqueue(f, "voice.publish", {**payload(f, "voice.publish"), "service_id": service_id})
    run(f)
    published = job(f, publish.id)
    assert published.status == "succeeded", published.error_message
    with f.case.database.snapshot_session() as session:
        initial_status = session.get_one(m.Voice, f.voice["id"]).metadata_json["providers"][
            service_id
        ]["status"]

    deleted = f.users.alice.delete(
        f"/api/v1/voices/{f.voice['id']}/samples/{f.sid}",
        query_string={"workspace_id": f.users.workspace_id},
        headers={**f.users.ah, "If-Match": str(published.result_json["voice_revision"])},
    )
    assert deleted.status_code == 200, deleted.get_json()
    fresh_revision = deleted.get_json()["voice_revision"]
    with f.case.database.snapshot_session() as session:
        assert session.get(m.VoiceSample, f.sid) is None
        voice = session.get_one(m.Voice, f.voice["id"])
        registration = voice.metadata_json["providers"][service_id]
        assert voice.revision == fresh_revision
        assert registration["status"] == (initial_status if service_id == "audio_cpp" else "stale")
        assert registration["sample_id"] == f.sid

    admitted = f.users.alice.delete(
        f"/api/v1/voices/{f.voice['id']}/providers/{service_id}",
        query_string={"workspace_id": f.users.workspace_id},
        headers={
            **f.users.ah,
            "If-Match": str(fresh_revision),
            "Idempotency-Key": f"closure2-removal-{service_id}",
        },
    )
    assert admitted.status_code == 202, admitted.get_json()
    identifier = admitted.get_json()["id"]
    with f.case.database.snapshot_session() as session:
        assert session.get_one(m.JobActorRecord, identifier).user_id == f.users.alice_id
        assert (
            session.get_one(m.Job, identifier).payload_json["expected_voice_revision"]
            == fresh_revision
        )
    run(f)
    saved = job(f, identifier)
    assert saved.status == "succeeded", saved.error_message
    if service_id == "audio_cpp":
        assert saved.external_effect_json is None
        assert saved.result_json["linked"] is True and not f.calls
    else:
        assert saved.external_effect_json["state"] == "completed"
        assert saved.external_effect_json["binding_state"] == "removed"
        assert saved.external_effect_json["remote_deleted"] is True
        assert f.calls == ["upload", "delete"]
    with f.case.database.snapshot_session() as session:
        assert service_id not in session.get_one(m.Voice, f.voice["id"]).metadata_json["providers"]
