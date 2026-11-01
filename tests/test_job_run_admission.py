"""Real credentials, writer races and synthetic workers at RUN boundaries."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pydub import AudioSegment
from sqlalchemy import delete, event, func, select

from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.access.job_policy import JobNotFound
from pandrator.web.access.speech_preview_policy import require_preview_replay
from pandrator.web.accounts.actor_authorization import require_actor_in_session
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.execution_actor import HostActor, UserActor, execution_actor_scope
from pandrator.web.accounts.models import User, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.credentials import (
    database_reference,
    tts_service_credential_key,
    upsert_credential,
)
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.jobs import Worker
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    Artifact,
    Job,
    JobActorRecord,
    UsageEvent,
    WorkspaceSetting,
    new_id,
    utcnow,
)
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.voice_provider_settings import capture_voice_provider_settings
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential
from tests.test_job_access_routes import no_network as no_network

PREVIEW = "/api/v1/services/tts/openai/preview"
BODY = {"text": "A synthetic private preview."}
RUN = frozenset({"app.read", "app.write", "app.run"})


def configure(case, name="editor"):
    workspace = case.personal[case.users[name]]
    key = tts_service_credential_key("openai")
    with case.database.immediate_session() as session:
        session.add_all(
            [
                WorkspaceSetting(
                    workspace_id=workspace,
                    key="services.tts",
                    value_json={
                        "provider_configs": [
                            {
                                "id": "openai",
                                "api_base": f"https://{name}.invalid/v1",
                                "secret_ref": database_reference(key),
                                "default_model": "tts-1",
                                "default_voice": f"{name}-voice",
                                "models": ["tts-1"],
                                "settings": {"nested_fixture": {"name": name}},
                            }
                        ]
                    },
                ),
                WorkspaceSetting(
                    workspace_id=workspace,
                    key="defaults.tts",
                    value_json={"language": "pl", "generation_prompt": f"{name} directions"},
                ),
            ]
        )
        upsert_credential(
            session, key, "Synthetic", f"synthetic-{name}-key", workspace_id=workspace
        )
    return workspace


def counts(case):
    with case.database.snapshot_session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Job, ApiIdempotency, Artifact, UsageEvent)
        )


def submit(case, *, key=None, name="editor", subject=None):
    caller = credential(case, name, scopes=RUN, subject=subject, grant_team=False)
    headers = {**caller.headers, **({"Idempotency-Key": key} if key else {})}
    response = caller.client.post(PREVIEW, headers=headers, json=BODY)
    assert response.status_code == 202, response.get_json()
    identifier = response.get_json()["id"]
    with case.database.immediate_session() as session:
        session.get(Job, identifier).max_attempts = 1
    return caller, identifier, response, headers


def synthetic_worker(case, monkeypatch, *, synthesis=None):
    handlers = case.services.workflow_handlers
    monkeypatch.setattr(
        handlers, "_tts_urls", lambda _settings: {"kobold_qwen_base_url": "fake://qwen"}
    )
    prepare = Mock(side_effect=lambda settings, **_kwargs: settings)
    ensure = Mock()
    synthesize = Mock(side_effect=synthesis or (lambda *_args, **_kwargs: AudioSegment.silent(25)))
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference", prepare)
    monkeypatch.setattr(handlers, "_ensure_qwen_cloned_voice", ensure)
    monkeypatch.setattr(handlers.tts_providers, "synthesize", synthesize)
    worker = Worker(
        case.services.jobs, "synthetic-private-preview", {"tts.preview": handlers.preview_tts_voice}
    )
    return worker, prepare, ensure, synthesize


def test_actual_ordinary_user_namespace_replay_credentials_output_and_usage(case, monkeypatch):
    workspace = configure(case)
    configure(case, "owner")
    caller, identifier, first, headers = submit(case, key="preview-owned-replay")
    settings = first.get_json()["payload_json"]["settings"]
    assert settings["voice"] == "editor-voice"
    assert settings["generation_prompt"] == "editor directions"
    assert settings["language"] == "pl"
    assert first.get_json()["payload_json"]["workspace_id"] == workspace
    catalogue = case.services.tts_catalogue
    original = catalogue.preview_settings
    monkeypatch.setattr(
        catalogue,
        "preview_settings",
        Mock(side_effect=AssertionError("Replay read mutable settings")),
    )
    replay = caller.client.post(PREVIEW, headers=headers, json=BODY)
    assert replay.status_code == 202 and replay.get_json() == first.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    monkeypatch.setattr(catalogue, "preview_settings", original)
    worker, prepare, ensure, synthesize = synthetic_worker(case, monkeypatch)
    assert worker.run_once()
    completed = case.services.jobs.get(identifier)
    assert completed.status == "succeeded", completed.error_message
    runtime = synthesize.call_args.args[1]
    assert (
        next(item for item in runtime["provider_configs"] if item["id"] == "openai")["api_key"]
        == "synthetic-editor-key"
    )
    assert (
        prepare.call_args.kwargs["workspace_id"]
        == ensure.call_args.kwargs["workspace_id"]
        == workspace
    )
    with case.database.snapshot_session() as session:
        job = session.get(Job, identifier)
        actor = session.get(JobActorRecord, identifier)
        artifact = session.get(Artifact, completed.result_json["artifact_id"])
        usage = session.scalars(select(UsageEvent).where(UsageEvent.job_id == identifier)).one()
        assert actor.user_id == case.users["editor"]
        assert job.workspace_id == artifact.workspace_id == usage.workspace_id == workspace
        assert job.project_id is artifact.project_id is usage.project_id is None
        assert f"tts-previews/{workspace}/" in artifact.relative_path
        assert case.services.paths.managed_path(artifact.relative_path).is_file()
        assert "synthetic-editor-key" not in str(artifact.metadata_json)


@pytest.mark.parametrize("loss", ["grant", "owner_membership"])
def test_preview_denies_before_receipt_or_settings(case, monkeypatch, loss):
    workspace = configure(case)
    caller = credential(case, "editor", scopes=RUN, grant_team=False)
    with case.database.immediate_session() as session:
        if loss == "grant":
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == caller.token.id
                )
            )
        else:
            session.execute(
                delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace)
            )
    lookup = Mock(side_effect=AssertionError("Denied preview read settings"))
    monkeypatch.setattr(case.services.tts_catalogue, "preview_settings", lookup)
    before = counts(case)
    response = caller.client.post(
        PREVIEW, headers={**caller.headers, "Idempotency-Key": "denied-preview-new"}, json=BODY
    )
    assert response.status_code == 404, response.get_json()
    assert counts(case) == before
    lookup.assert_not_called()


def test_same_subject_other_admin_cannot_replay_preview(case, monkeypatch):
    configure(case)
    _, _, _, _ = submit(case, key="preview-same-subject", subject="shared-synthetic-subject")
    foreign = credential(
        case, "outsider", scopes=RUN, subject="shared-synthetic-subject", grant_team=False
    )
    lookup = Mock(wraps=case.services.tts_catalogue.preview_settings)
    monkeypatch.setattr(case.services.tts_catalogue, "preview_settings", lookup)
    before = counts(case)
    response = foreign.client.post(
        PREVIEW, headers={**foreign.headers, "Idempotency-Key": "preview-same-subject"}, json=BODY
    )
    assert response.status_code == 404, response.get_json()
    assert counts(case) == before
    lookup.assert_called_once()
    assert lookup.call_args.kwargs["workspace_id"] == case.personal[case.users["outsider"]]


def test_same_subject_other_admin_cannot_probe_receipt_conflict(case):
    configure(case)
    submit(case, key="preview-hidden-conflict", subject="shared-synthetic-subject")
    foreign = credential(
        case, "outsider", scopes=RUN, subject="shared-synthetic-subject", grant_team=False
    )
    before = counts(case)
    response = foreign.client.post(
        PREVIEW,
        headers={**foreign.headers, "Idempotency-Key": "preview-hidden-conflict"},
        json={**BODY, "text": "Different digest must not disclose the saved request."},
    )
    assert response.status_code == 404, response.get_json()
    assert counts(case) == before


@pytest.mark.parametrize("failure", ["missing", "invalid", "enqueue"])
def test_preview_failures_do_not_leave_reservations(case, monkeypatch, failure):
    configure(case)
    caller = credential(case, "editor", scopes=RUN, grant_team=False)
    if failure == "missing":
        monkeypatch.setattr(
            case.services.tts_catalogue, "preview_settings", Mock(return_value=None)
        )
    elif failure == "invalid":
        monkeypatch.setattr(
            case.services.tts_catalogue,
            "preview_settings",
            Mock(side_effect=ValueError("Synthetic invalid settings")),
        )
    else:
        monkeypatch.setattr(
            case.services.jobs,
            "enqueue_in_session",
            Mock(side_effect=ValueError("Synthetic admission failure")),
        )
    before = counts(case)
    response = caller.client.post(
        PREVIEW,
        headers={**caller.headers, "Idempotency-Key": "preview-failure-rollback"},
        json=BODY,
    )
    assert response.status_code == (404 if failure == "missing" else 422)
    assert counts(case) == before


def test_settings_capture_uses_caller_unit_and_copies_nested_values(case, monkeypatch):
    workspace = configure(case)
    catalogue = case.services.tts_catalogue
    with case.database.immediate_session() as session:
        row = session.get(WorkspaceSetting, (workspace, "services.tts"))
        row.value_json = {
            **row.value_json,
            "provider_configs": [
                {**row.value_json["provider_configs"][0], "default_voice": "last-moment-voice"}
            ],
        }
        session.flush()
        pending = User(id=new_id(), display_name="Do not flush", is_active=True)
        session.add(pending)
        with monkeypatch.context() as local:
            local.setattr(
                case.database,
                "snapshot_session",
                Mock(side_effect=AssertionError("Opened another snapshot")),
            )
            settings = catalogue.preview_settings(
                "openai",
                model=None,
                voice=None,
                language=None,
                workspace_id=workspace,
                db_session=session,
            )
        assert settings["voice"] == "last-moment-voice"
        settings["provider_configs"][0]["settings"]["nested_fixture"]["name"] = "changed copy"
        assert (
            row.value_json["provider_configs"][0]["settings"]["nested_fixture"]["name"] == "editor"
        )
        assert pending in session.new
        session.expunge(pending)


def test_preview_writer_wait_rechecks_grant_before_lookup(case, monkeypatch):
    configure(case)
    caller = credential(case, "editor", scopes=RUN, grant_team=False)
    ready, release, waiting = threading.Event(), threading.Event(), threading.Event()
    finished = Future()
    original = ApiGuards.access_context
    thread_name = "preview-held-writer"

    def capture(guards):
        context = original(guards)
        if threading.current_thread().name == thread_name:
            ready.set()
            assert release.wait(10)
        return context

    def before_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            threading.current_thread().name == thread_name
            and statement.strip().upper() == "BEGIN IMMEDIATE"
        ):
            waiting.set()

    def request_preview():
        try:
            finished.set_result(
                case.app.test_client().post(
                    PREVIEW,
                    headers={**caller.headers, "Idempotency-Key": "preview-writer-revoke"},
                    json=BODY,
                )
            )
        except BaseException as error:
            finished.set_exception(error)

    monkeypatch.setattr(ApiGuards, "access_context", capture)
    lookup = Mock(side_effect=AssertionError("Revoked waiting request read settings"))
    monkeypatch.setattr(case.services.tts_catalogue, "preview_settings", lookup)
    event.listen(case.database.engine, "before_cursor_execute", before_sql)
    thread = threading.Thread(target=request_preview, name=thread_name)
    try:
        thread.start()
        assert ready.wait(10)
        with case.database.immediate_session() as session:
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == caller.token.id
                )
            )
            release.set()
            assert waiting.wait(5)
        thread.join(10)
        assert not thread.is_alive()
        response = finished.result(1)
        assert response.status_code == 404, response.get_json()
        assert counts(case) == (0, 0, 0, 0)
        lookup.assert_not_called()
    finally:
        release.set()
        thread.join(10)
        event.remove(case.database.engine, "before_cursor_execute", before_sql)
        assert not thread.is_alive()


@pytest.mark.parametrize("transport", ["api_expiry", "browser_logout", "browser_expiry"])
def test_admitted_preview_survives_natural_transport_end(case, monkeypatch, transport):
    workspace = configure(case)
    if transport == "api_expiry":
        caller, identifier, _, _ = submit(case)
        actor = caller.actor
        with case.database.immediate_session() as session:
            session.get(ApiToken, caller.token.id).expires_at = utcnow() - timedelta(seconds=1)
    else:
        store = case.auth.browser_sessions
        issued = store.issue(
            case.users["editor"],
            1,
            subject="synthetic-browser",
            principal_kind="owner_session",
            scopes=RUN,
        )
        principal = store.resolve(issued.raw_token)
        actor = UserActor(
            user_id=principal.user_id,
            auth_revision=principal.auth_revision,
            subject=principal.subject,
            principal_kind=principal.kind,
            scopes=principal.scopes,
            browser_session_id=principal.session_id,
            client_id=principal.client_id,
        )
        settings = case.services.tts_catalogue.preview_settings(
            "openai", model=None, voice=None, language=None, workspace_id=workspace
        )
        with case.database.snapshot_session() as session:
            captured = capture_voice_provider_settings(session, workspace)
        with execution_actor_scope(actor):
            identifier = case.services.jobs.enqueue(
                "tts.preview",
                {"text": BODY["text"], "settings": settings, "workspace_id": workspace, **captured},
                workspace_id=workspace,
            ).id
        if transport == "browser_logout":
            store.revoke(issued.raw_token)
        else:
            with case.database.immediate_session() as session:
                row = session.get(BrowserAuthSession, principal.session_id)
                row.idle_expires_at = row.absolute_expires_at = utcnow() - timedelta(seconds=1)
    with case.database.snapshot_session() as session, pytest.raises(PermissionError):
        require_actor_in_session(session, actor, required_scopes=("app.run",))
    worker, _, _, synthesize = synthetic_worker(case, monkeypatch)
    assert worker.run_once()
    completed = case.services.jobs.get(identifier)
    assert completed.status == "succeeded", completed.error_message
    assert synthesize.call_count == 1


@pytest.mark.parametrize("loss", ["token", "grant", "epoch", "account"])
def test_revocation_before_worker_prevents_credential_and_provider_work(case, monkeypatch, loss):
    configure(case)
    caller, identifier, _, headers = submit(case, key="preview-revoked-replay")
    with case.database.immediate_session() as session:
        if loss == "token":
            session.get(ApiToken, caller.token.id).revoked_at = utcnow()
        elif loss == "grant":
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == caller.token.id
                )
            )
        elif loss == "epoch":
            session.get(UserCredential, case.users["editor"]).auth_revision += 1
        else:
            session.get(User, case.users["editor"]).is_active = False
    before = counts(case)
    response = caller.client.post(PREVIEW, headers=headers, json=BODY)
    assert response.status_code == (403 if loss == "grant" else 401), response.get_json()
    assert counts(case) == before
    import pandrator.web.workflow_handlers as module

    hydrate = Mock(side_effect=AssertionError("Revoked worker loaded credentials"))
    monkeypatch.setattr(module, "hydrate_tts_settings", hydrate)
    worker, prepare, ensure, synthesize = synthetic_worker(case, monkeypatch)
    assert worker.run_once()
    completed = case.services.jobs.get(identifier)
    assert completed.status == "failed" and completed.error_code == "AccessDenied"
    hydrate.assert_not_called()
    prepare.assert_not_called()
    ensure.assert_not_called()
    synthesize.assert_not_called()
    assert counts(case)[2:] == (0, 0)


@pytest.mark.parametrize("at", ["reference", "synthesis", "prepared_file"])
def test_revocation_fences_next_effect_and_unpublished_audio(case, monkeypatch, at):
    workspace = configure(case)
    caller, identifier, _, _ = submit(case)

    revocations = []

    def revoke():
        with case.database.immediate_session() as session:
            session.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == caller.token.id
                )
            )
        revocations.append(at)

    def synthesize(*_args, **_kwargs):
        if at == "synthesis":
            revoke()
        return AudioSegment.silent(25)

    worker, _, ensure, synthesize_mock = synthetic_worker(case, monkeypatch, synthesis=synthesize)
    if at == "reference":

        def prepare(settings, **_kwargs):
            revoke()
            return settings

        monkeypatch.setattr(
            case.services.workflow_handlers, "prepare_audio_cpp_voice_reference", prepare
        )
    elif at == "prepared_file":
        artifacts = case.services.workflow_handlers.artifacts
        original = artifacts.prepare_registration

        def prepare_file(*args, **kwargs):
            prepared = original(*args, **kwargs)
            revoke()
            return prepared

        monkeypatch.setattr(artifacts, "prepare_registration", prepare_file)
    assert worker.run_once()
    assert revocations == [at]
    completed = case.services.jobs.get(identifier)
    assert completed.status == "failed" and completed.error_code == "AccessDenied"
    assert synthesize_mock.call_count == (0 if at == "reference" else 1)
    if at == "reference":
        ensure.assert_not_called()
    assert counts(case)[2:] == (0, 0)
    assert not list((case.services.paths.artifacts / "tts-previews" / workspace).glob("*.wav"))


def warning(case, *, hidden=False):
    source = case.b if hidden else case.a
    row = case.services.jobs.enqueue(
        "export.variant",
        {"session_id": source.id, "settings": {"video_tail_extension_policy": "ask"}},
        session_id=source.id,
    )
    with case.database.immediate_session() as session:
        job = session.get(Job, row.id)
        job.status, job.error_code = "failed", "VideoTailExtensionRequired"
        job.result_json = {"captured": "synthetic warning"}
    return row.id


@pytest.mark.parametrize("action", ["stop", "extend"])
def test_export_run_checks_roles_before_state_and_replay(case, action):
    identifier, hidden = warning(case), warning(case, hidden=True)
    viewer = credential(case, "viewer")
    outsider = credential(case, "outsider")
    editor = credential(case, "editor")

    def path(value):
        return f"/api/v1/jobs/{value}/video-tail-decision"

    before = counts(case)
    assert (
        viewer.client.post(
            path(identifier), headers=viewer.headers, json={"action": action}
        ).status_code
        == 403
    )
    for caller, target in ((outsider, identifier), (editor, hidden)):
        assert (
            caller.client.post(
                path(target), headers=caller.headers, json={"action": action}
            ).status_code
            == 404
        )
    assert counts(case) == before
    first = editor.client.post(path(identifier), headers=editor.headers, json={"action": action})
    assert first.status_code == (200 if action == "stop" else 202), first.get_json()
    replay = editor.client.post(path(identifier), headers=editor.headers, json={"action": action})
    assert replay.status_code == first.status_code
    for key in ("id", "session_id", "kind", "payload_json", "result_json"):
        assert replay.get_json()[key] == first.get_json()[key]
    assert counts(case)[0] == before[0] + int(action == "extend")
    if action == "extend":
        with case.database.snapshot_session() as session:
            actor = session.get(JobActorRecord, first.get_json()["id"])
            assert actor.user_id == case.users["editor"]
    with case.database.immediate_session() as session:
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == case.users["editor"],
            )
        )
    before = counts(case)
    denied = editor.client.post(path(identifier), headers=editor.headers, json={"action": action})
    assert denied.status_code == 404
    assert counts(case) == before


def test_export_continuation_replay_reauthorizes_actual_child(case):
    identifier, hidden = warning(case), warning(case, hidden=True)
    with case.database.immediate_session() as session:
        row = session.get(Job, identifier)
        row.result_json = {"video_tail_decision": "extend", "continuation_job_id": hidden}
    editor = credential(case, "editor")
    before = counts(case)
    response = editor.client.post(
        f"/api/v1/jobs/{identifier}/video-tail-decision",
        headers=editor.headers,
        json={"action": "extend"},
    )
    assert response.status_code == 404, response.get_json()
    assert counts(case) == before


def test_preview_replay_policy_requires_run_and_exact_private_kind(case):
    workspace = configure(case)
    caller, identifier, _, _ = submit(case)
    with case.database.snapshot_session() as session:
        assert (
            require_preview_replay(session, AccessContext(caller.actor), identifier).id
            == identifier
        )
    read_only = credential(case, "editor", scopes=frozenset({"app.read"}), grant_team=False)
    with case.database.snapshot_session() as session, pytest.raises(AccessDenied):
        require_preview_replay(session, AccessContext(read_only.actor), identifier)
    unrelated = case.services.jobs.enqueue("noop", {}, workspace_id=workspace)
    with case.database.snapshot_session() as session, pytest.raises(JobNotFound):
        require_preview_replay(session, AccessContext(caller.actor), unrelated.id)


def test_preview_worker_rejects_payload_workspace_before_loading_credentials(case, monkeypatch):
    configure(case)
    _, identifier, _, _ = submit(case)
    with case.database.immediate_session() as session:
        row = session.get(Job, identifier)
        row.payload_json = {**row.payload_json, "workspace_id": case.personal[case.users["owner"]]}
    import pandrator.web.workflow_handlers as module

    hydrate = Mock(side_effect=AssertionError("Misrooted work loaded credentials"))
    monkeypatch.setattr(module, "hydrate_tts_settings", hydrate)
    worker, _, _, synthesize = synthetic_worker(case, monkeypatch)
    assert worker.run_once()
    assert case.services.jobs.get(identifier).status == "failed"
    hydrate.assert_not_called()
    synthesize.assert_not_called()
    assert counts(case)[2:] == (0, 0)


def test_preview_lease_loss_during_synthesis_fences_output(case, monkeypatch):
    workspace = configure(case)
    _, identifier, _, _ = submit(case)

    def lose_lease(*_args, **_kwargs):
        with case.database.immediate_session() as session:
            session.get(Job, identifier).lease_generation += 1
        return AudioSegment.silent(25)

    worker, _, _, synthesize = synthetic_worker(case, monkeypatch, synthesis=lose_lease)
    assert worker.run_once()
    assert synthesize.call_count == 1
    assert counts(case)[2:] == (0, 0)
    assert not list((case.services.paths.artifacts / "tts-previews" / workspace).glob("*.wav"))


def test_retry_attempt_cannot_overwrite_an_existing_preview_reference(case, monkeypatch):
    configure(case)
    _, identifier, _, _ = submit(case)
    worker, _, _, _ = synthetic_worker(case, monkeypatch)
    assert worker.run_once()
    first = case.services.jobs.get(identifier)
    assert first.status == "succeeded", first.error_message
    with case.database.snapshot_session() as session:
        original = session.get(Artifact, first.result_json["artifact_id"])
        original_path = case.services.paths.managed_path(original.relative_path)
        original_bytes = original_path.read_bytes()
    # Trusted internal retry with the same durable job ID must retain the old file.
    with execution_actor_scope(HostActor("pandrator-cli")):
        repeated = case.services.workflow_handlers.preview_tts_voice(
            {**first.payload_json, "_job_id": identifier}, lambda *_args: None, threading.Event()
        )
    with case.database.snapshot_session() as session:
        replacement = session.get(Artifact, repeated["artifact_id"])
        assert replacement.id != original.id
        assert replacement.relative_path != original.relative_path
        assert case.services.paths.managed_path(replacement.relative_path).is_file()
    assert original_path.read_bytes() == original_bytes
