"""Original-owner gates for legacy host-local tools, without real local services."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydub import AudioSegment
from sqlalchemy import event, func, select

from pandrator.logic import rvc_handler
from pandrator.logic import xtts_trainer_handler as trainer
from pandrator.web import models as m
from pandrator.web import workflow_voice
from pandrator.web.access.artifact_policy import ArtifactNotFound
from pandrator.web.access.contracts import AccessDenied, WorkspaceNotFound
from pandrator.web.access.local_tools_policy import (
    LocalToolOperation as Op,
)
from pandrator.web.access.local_tools_policy import (
    require_local_tool_inputs,
    require_local_tool_workspace,
)
from pandrator.web.accounts.execution_actor import (
    HostActor,
    actor_from_principal,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.auth import ALL_SCOPES
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import Worker
from pandrator.web.model_base import new_id
from pandrator.web.schemas import TrainingCreateRequest
from pandrator.web.training_lifecycle import TrainingNotFound
from pandrator.web.training_publication import require_training_execution_access
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_collaboration_authentication import new_account
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_project_sharing_routes import login
from tests.test_provider_workspace_access import scoped_token
from tests.test_voice_lifecycle_idempotency import silent_wav


def test_named_user_training_list_denies_before_original_run_body(design, tmp_path):
    f, users = design, design.users
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=users.alice)))
    assert actor is not None
    with f.case.database.immediate_session() as session:
        owner = initial_personal_workspace(session)
        session.add(
            m.TrainingRun(
                workspace_id=owner.id,
                model_name="private-original-owner-model",
                settings_json={"private": "protected owner training body"},
            )
        )
    statements = []

    def observe(_c, _cu, sql, _p, _co, _e):
        statements.append(sql)

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    denied = False
    items = []
    try:
        with execution_actor_scope(actor):
            try:
                items = f.services.training.list()
            except (AccessDenied, WorkspaceNotFound):
                denied = True
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    bodies = sum("training_runs.settings_json" in sql for sql in statements)
    (tmp_path / "training-list-boundary.json").write_text(
        json.dumps(
            {"denied": denied, "training_body_queries": bodies, "returned_rows": len(items)},
            indent=2,
        )
    )
    assert denied
    assert bodies == 0 and items == []


def capture_sql(database, operation):
    sql = []

    def observe(_c, _cu, statement, _p, _co, _e):
        sql.append(statement)

    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        result = operation()
    finally:
        event.remove(database.engine, "before_cursor_execute", observe)
    return result, sql


def actor_for(f, client):
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=client)))
    assert actor is not None
    return actor


def api_owner(t, scopes=None, *, workspace=None):
    token, bearer = scoped_token(
        t.f.case, t.owner.user_id, scopes or ALL_SCOPES, workspace=workspace or t.workspace
    )
    principal = t.f.case.auth.resolve_api_token(
        bearer["Authorization"].removeprefix("Bearer "),
        network_zone="loopback",
        target_instance_id=t.f.services.identity.snapshot(
            observed_origin="http://localhost"
        ).instance_id,
    )
    assert principal is not None
    actor = actor_from_principal(principal)
    assert actor is not None
    return actor, token


@pytest.fixture
def tools(design):
    f = design
    other_account = new_account(f.case, "other-local-admin", "administrator")
    other_client, admin_auth = login(f.case, "other-local-admin")
    owner = actor_for(f, f.users.owner)
    user = actor_for(f, f.users.alice)
    unrelated = actor_for(f, other_client)
    with f.case.database.snapshot_session() as session:
        workspace = initial_personal_workspace(session).id
        foreign = session.scalar(
            select(Workspace.id).where(Workspace.personal_owner_user_id == user.user_id)
        )
    assert foreign is not None

    def register(name, contents, kind, owner_id=workspace, session_id=None):
        path = f.services.paths.uploads / name
        path.write_bytes(contents)
        with execution_actor_scope(HostActor("pandrator-cli")):
            return f.services.artifacts.register(
                path,
                kind=kind,
                role="upload",
                workspace_id=owner_id,
                session_id=session_id,
                metadata={"private": "protected input body"},
            )

    audio = register("local-source.wav", silent_wav(), "audio")
    text = register("local-source.txt", b"Synthetic training transcript.", "text")
    weights = register("local-model.pth", b"synthetic weights", "model")
    index = register("local-model.index", b"synthetic index", "model")
    other = register("foreign.wav", silent_wav(), "audio", foreign)
    with f.case.database.immediate_session() as session:
        voice = m.Voice(
            workspace_id=workspace,
            name="Original owner voice",
            metadata_json={"private": "protected voice body"},
        )
        foreign_voice = m.Voice(
            workspace_id=foreign,
            name="Foreign voice",
            metadata_json={"private": "foreign voice body"},
        )
        session.add_all([voice, foreign_voice])
        session.flush()
        voice_id, foreign_voice_id = voice.id, foreign_voice.id
    return SimpleNamespace(
        f=f,
        owner=owner,
        user=user,
        admin=unrelated,
        admin_client=other_client,
        admin_id=other_account.user_id,
        admin_auth=admin_auth,
        workspace=workspace,
        foreign=foreign,
        audio=audio,
        text=text,
        weights=weights,
        index=index,
        other=other,
        voice_id=voice_id,
        foreign_voice_id=foreign_voice_id,
    )


def training_payload(t, name="local-narrator"):
    return TrainingCreateRequest(
        model_name=name,
        source_artifact_id=t.audio.id,
        source_text_artifact_id=t.text.id,
        voice_id=t.voice_id,
        settings={"epochs": 1},
    )


def seed_training(t, status="failed"):
    request = training_payload(t, "seed-" + new_id())
    with t.f.case.database.immediate_session() as session:
        row = m.TrainingRun(
            workspace_id=t.workspace,
            model_name=request.model_name,
            voice_id=t.voice_id,
            source_artifact_id=t.audio.id,
            source_text_artifact_id=t.text.id,
            status=status,
            settings_json={"private": "protected run snapshot"},
        )
        session.add(row)
        session.flush()
        return row.id


def forbidden(*_a, **_k):
    pytest.fail("Denied local-tool operation crossed a protected effect boundary")


def protected_body_queries(sql):
    return [
        statement
        for statement in sql
        if any(
            column in statement
            for column in (
                "training_runs.settings_json",
                "artifacts.metadata_json",
                "voices.metadata_json",
            )
        )
    ]


def test_policy_owner_positive_scopes_resources_and_exact_operation_type(tools):
    t = tools
    expected = {
        Op.RVC_READ: {"app.read"},
        Op.RVC_CONVERT: {"app.read", "app.run"},
        Op.RVC_MODEL_WRITE: {"app.admin", "app.read", "app.write"},
        Op.TRAINING_READ: {"app.admin", "app.read"},
        Op.TRAINING_RUN: {"app.admin", "app.read", "app.run"},
        Op.TRAINING_CANCEL: {"app.admin", "app.read", "app.cancel"},
    }
    actor, token = api_owner(t)
    for operation, scopes in expected.items():
        with execution_actor_scope(actor), t.f.case.database.snapshot_session() as session:
            assert require_local_tool_workspace(session, operation) == t.workspace
        for scope in scopes:
            limited = replace(actor, scopes=actor.scopes - {scope})
            with execution_actor_scope(limited), t.f.case.database.snapshot_session() as session:
                with pytest.raises(AccessDenied):
                    require_local_tool_workspace(session, operation)
    with t.f.case.database.immediate_session() as session:
        session.delete(session.get_one(ApiTokenWorkspaceGrant, (token.id, t.workspace)))
    with execution_actor_scope(actor), t.f.case.database.snapshot_session() as session:
        with pytest.raises(WorkspaceNotFound):
            require_local_tool_workspace(session, Op.RVC_READ)
    with execution_actor_scope(t.owner), t.f.case.database.snapshot_session() as session:
        with pytest.raises(TypeError):
            require_local_tool_workspace(session, cast(Any, "rvc_read"))
    marker = current_execution_actor.set(cast(Any, object()))
    try:
        with t.f.case.database.snapshot_session() as session:
            with pytest.raises(AccessDenied):
                require_local_tool_workspace(session, Op.RVC_READ)
    finally:
        current_execution_actor.reset(marker)


def test_user_and_unrelated_admin_http_denials_precede_bodies_probe_queue_and_replay(
    tools, monkeypatch
):
    t, f = tools, tools.f
    failed = seed_training(t)
    active = seed_training(t, "queued")
    monkeypatch.setattr(rvc_handler, "is_rvc_available", forbidden)
    monkeypatch.setattr(rvc_handler, "get_rvc_models", forbidden)
    monkeypatch.setattr(f.services.jobs, "enqueue_in_session", forbidden)
    monkeypatch.setattr(f.services.jobs, "request_cancel_in_session", forbidden)
    monkeypatch.setattr(f.services.idempotency, "begin", forbidden)
    with f.case.database.snapshot_session() as session:
        before_counts = {
            model: session.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.ApiIdempotency)
        }

    def requests():
        for client, auth in ((f.users.alice, f.users.ah), (t.admin_client, t.admin_auth)):
            for method, path, payload in [
                ("GET", "/api/v1/rvc/models", None),
                (
                    "POST",
                    "/api/v1/rvc/models",
                    {"pth_artifact_id": t.weights.id, "index_artifact_id": t.index.id},
                ),
                (
                    "POST",
                    "/api/v1/rvc/convert",
                    {"source_artifact_id": t.audio.id, "settings": {"rvc_model": "local"}},
                ),
                ("GET", "/api/v1/training", None),
                ("POST", "/api/v1/training", training_payload(t).model_dump()),
                ("POST", f"/api/v1/training/{failed}/retry", {}),
                ("POST", f"/api/v1/training/{active}/cancel", {}),
            ]:
                response = client.open(
                    path,
                    method=method,
                    json=payload,
                    headers={**auth, "Idempotency-Key": "denied-local-tool"},
                )
                assert response.status_code in {403, 404}, (path, response.get_json())

    _result, sql = capture_sql(f.case.database, requests)
    assert protected_body_queries(sql) == []
    with f.case.database.snapshot_session() as session:
        assert session.get_one(m.TrainingRun, active).status == "queued"
        assert all(
            session.scalar(select(func.count()).select_from(model)) == count
            for model, count in before_counts.items()
        )


def test_training_lifecycle_gate_is_before_resolver_bodies_and_effects(tools, monkeypatch):
    t, f = tools, tools.f
    failed = seed_training(t)
    active = seed_training(t, "queued")
    monkeypatch.setattr(f.services.jobs, "enqueue_in_session", forbidden)
    monkeypatch.setattr(f.services.jobs, "request_cancel_in_session", forbidden)

    def denied_calls():
        for actor in (t.user, t.admin):
            for operation in (
                lambda: f.services.training.list(),
                lambda: f.services.training.start(training_payload(t)),
                lambda: f.services.training.retry(failed),
                lambda: f.services.training.cancel(active),
            ):
                with execution_actor_scope(actor), pytest.raises((AccessDenied, WorkspaceNotFound)):
                    operation()
        with f.case.database.immediate_session() as session:
            session.get_one(m.User, t.owner.user_id).is_active = False
        with execution_actor_scope(t.admin), pytest.raises(WorkspaceNotFound):
            f.services.training.list()

    _result, sql = capture_sql(f.case.database, denied_calls)
    assert protected_body_queries(sql) == []


def test_original_owner_training_admission_list_retry_cancel_and_rvc_discovery(tools, monkeypatch):
    t, f = tools, tools.f
    with execution_actor_scope(t.owner):
        created, job = f.services.training.start(training_payload(t))
        assert f.services.training.list()[0]["id"] == created.id
        assert f.services.training.cancel(created.id)["status"] == "canceled"
        retried, retry_job = f.services.training.retry(created.id)
        assert retried.id != created.id and retry_job.id != job.id
        assert f.services.training.cancel(retried.id)["status"] == "canceled"
    health = []
    monkeypatch.setattr(rvc_handler, "is_rvc_available", lambda: health.append(True) or True)
    monkeypatch.setattr(rvc_handler, "get_rvc_models", lambda _root: ["synthetic-local-model"])
    response = f.users.owner.get("/api/v1/rvc/models")
    assert response.status_code == 200 and response.get_json()["items"] == ["synthetic-local-model"]
    assert health == [True]


def test_input_headers_reject_foreign_deleted_voice_and_processing_scope_before_bodies(tools):
    t, f = tools, tools.f

    def check(ids, voice=None, exception: type[Exception] = ArtifactNotFound):
        def invoke():
            with execution_actor_scope(t.owner), f.case.database.snapshot_session() as session:
                with pytest.raises(exception):
                    require_local_tool_inputs(
                        session, Op.RVC_CONVERT, artifact_ids=ids, voice_id=voice
                    )

        _result, sql = capture_sql(f.case.database, invoke)
        assert protected_body_queries(sql) == []

    check([t.other.id])
    check([new_id()])
    check([t.audio.id], t.foreign_voice_id, WorkspaceNotFound)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Artifact, t.other.id).state = "deleted"
    check([t.other.id])
    source = body(
        f.users.owner.post(
            "/api/v1/sessions",
            json={"name": "Local model source", "workflow_kind": "voiceover"},
            headers=headers(f.users.oh, "local-input-session"),
        ),
        201,
    )
    path = f.services.paths.uploads / "session-model.pth"
    path.write_bytes(b"synthetic session model")
    with execution_actor_scope(t.owner):
        associated = f.services.artifacts.register(
            path, kind="model", role="upload", session_id=source["id"]
        )
    actor, _token = api_owner(t, {"app.admin", "app.read", "app.write"})

    def scoped():
        with execution_actor_scope(actor), f.case.database.snapshot_session() as session:
            with pytest.raises(AccessDenied):
                require_local_tool_inputs(session, Op.RVC_MODEL_WRITE, artifact_ids=[associated.id])

    _result, sql = capture_sql(f.case.database, scoped)
    assert protected_body_queries(sql) == []


def test_direct_user_workers_and_actorless_record_borrowing_deny_before_bodies_io(
    tools, monkeypatch
):
    t, f = tools, tools.f
    identifier = seed_training(t, "queued")
    context = replace(
        f.case.app.extensions["pandrator"]["workflow_handlers"]._voice_workflow_context(),
        _resolve_input=forbidden,
    )
    monkeypatch.setattr(rvc_handler, "is_rvc_available", forbidden)
    monkeypatch.setattr(trainer, "start_training", forbidden)
    payload = {
        **training_payload(t).model_dump(),
        "training_id": identifier,
        "workspace_id": t.workspace,
    }

    def direct():
        with execution_actor_scope(t.owner):
            for function, data in (
                (workflow_voice.convert_with_rvc, {"source_artifact_id": t.audio.id}),
                (
                    workflow_voice.upload_rvc_model,
                    {"pth_artifact_id": t.weights.id, "index_artifact_id": t.index.id},
                ),
                (workflow_voice.train_xtts, payload),
            ):
                with pytest.raises(AccessDenied):
                    function(context, data, lambda *_a: None, threading.Event())

    _result, sql = capture_sql(f.case.database, direct)
    assert protected_body_queries(sql) == []
    with execution_actor_scope(t.owner):
        queued = f.services.jobs.enqueue(
            "rvc.convert",
            {
                "source_artifact_id": t.audio.id,
                "workspace_id": t.workspace,
                "settings": {"rvc_model": "local"},
            },
            workspace_id=t.workspace,
        )
    marker = current_job_lease.set(
        {"job_id": queued.id, "owner_id": "not-a-grant", "lease_generation": 1}
    )
    try:
        with execution_actor_scope(None), f.case.database.snapshot_session() as session:
            with pytest.raises(AccessDenied):
                require_local_tool_workspace(session, Op.RVC_READ, workspace_id=t.workspace)
    finally:
        current_job_lease.reset(marker)


def fake_local_services(t, monkeypatch, *, health=None, conversion=None, before_publish=None):
    calls = {"health": 0, "upload": 0, "conversion": 0, "trainer": 0}

    def available():
        calls["health"] += 1
        if health:
            health()
        return True

    def upload(_weights, _index, root):
        calls["upload"] += 1
        target = Path(root) / "synthetic-rvc"
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.pth").write_bytes(b"synthetic local model")
        return target.name

    def convert(audio, _settings):
        calls["conversion"] += 1
        if conversion:
            conversion()
        return audio

    def train(settings, **kwargs):
        calls["trainer"] += 1
        root = t.f.services.paths.models / "xtts"
        staging = root / ".downloads" / (".training-" + new_id())
        staging.mkdir(parents=True)
        for name in trainer.XTTS_MODEL_BUNDLE_FILENAMES:
            (staging / name).write_bytes(b"synthetic training bundle")
        if before_publish:
            before_publish()
        kwargs["publish_callback"](staging, root / settings["model_name"])
        return True, "Synthetic training completed"

    monkeypatch.setattr(rvc_handler, "is_rvc_available", available)
    monkeypatch.setattr(rvc_handler, "upload_rvc_model", upload)
    monkeypatch.setattr(rvc_handler, "process_with_rvc", convert)
    monkeypatch.setattr(trainer, "start_training", train)
    return calls


def enqueue_rvc(t, kind, actor):
    payload = (
        {"pth_artifact_id": t.weights.id, "index_artifact_id": t.index.id}
        if kind == "rvc.model.upload"
        else {"source_artifact_id": t.audio.id, "settings": {"rvc_model": "synthetic-rvc"}}
    )
    payload["workspace_id"] = t.workspace
    with execution_actor_scope(actor):
        return t.f.services.jobs.enqueue(kind, payload, workspace_id=t.workspace)


def run_one(t, *, kind=None, progress_hook=None):
    registry = t.f.case.app.extensions["pandrator"]["workflow_handlers"].handlers()
    if progress_hook is not None:
        original = registry[kind]

        def wrapped(payload, progress, cancel):
            def callback(value, message=None):
                progress_hook(message)
                progress(value, message)

            return original(payload, callback, cancel)

        registry = {**registry, kind: wrapped}
    assert Worker(t.f.services.jobs, "local-tools-worker", registry).run_once()


def assert_unpublished(t, role):
    with t.f.case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(m.Artifact).where(m.Artifact.role == role)
            )
            == 0
        )


def test_registered_owner_rvc_upload_conversion_training_survive_browser_logout(tools, monkeypatch):
    t = tools
    calls = fake_local_services(t, monkeypatch)
    upload = enqueue_rvc(t, "rvc.model.upload", t.owner)
    conversion = enqueue_rvc(t, "rvc.convert", t.owner)
    with execution_actor_scope(t.owner):
        run, training = t.f.services.training.start(training_payload(t))
    assert t.f.users.owner.post("/api/v1/auth/logout", headers=t.f.users.oh).status_code == 200
    for _job in (upload, conversion, training):
        run_one(t)
    for queued in (upload, conversion, training):
        actual = t.f.services.jobs.get(queued.id)
        assert actual.status == "succeeded", actual.error_message
    assert calls["upload"] == calls["conversion"] == calls["trainer"] == 1
    with t.f.case.database.snapshot_session() as session:
        assert session.get_one(m.TrainingRun, run.id).status == "succeeded"
        outputs = list(
            session.scalars(
                select(m.Artifact).where(
                    m.Artifact.role.in_(["rvc_model", "rvc_audio", "xtts_model"])
                )
            )
        )
        assert len(outputs) == 3 and all(row.workspace_id == t.workspace for row in outputs)
    with execution_actor_scope(t.owner), t.f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            require_local_tool_workspace(session, Op.RVC_READ)


def test_registered_token_natural_expiry_preserves_conversion_and_training(tools, monkeypatch):
    t = tools
    actor, token = api_owner(t)
    calls = fake_local_services(t, monkeypatch)
    conversion = enqueue_rvc(t, "rvc.convert", actor)
    with execution_actor_scope(actor):
        _run, training = t.f.services.training.start(training_payload(t))
    with t.f.case.database.immediate_session() as session:
        session.get_one(m.ApiToken, token.id).expires_at = m.utcnow() - timedelta(seconds=1)
    run_one(t)
    run_one(t)
    for queued in (conversion, training):
        actual = t.f.services.jobs.get(queued.id)
        assert actual.status == "succeeded", actual.error_message
    assert calls["conversion"] == calls["trainer"] == 1
    with execution_actor_scope(actor), t.f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            require_local_tool_workspace(session, Op.RVC_READ)


def test_late_rvc_health_revocation_blocks_audio_read_and_model_install(tools, monkeypatch):
    t = tools
    actor, token = api_owner(t)

    def revoke():
        with t.f.case.database.immediate_session() as session:
            session.get_one(m.ApiToken, token.id).revoked_at = m.utcnow()

    calls = fake_local_services(t, monkeypatch, health=revoke)
    reads = []
    monkeypatch.setattr(
        AudioSegment, "from_file", lambda *_a, **_k: reads.append(True) or forbidden()
    )
    queued = enqueue_rvc(t, "rvc.convert", actor)
    run_one(t)
    assert t.f.services.jobs.get(queued.id).status == "failed"
    assert reads == [] and calls["conversion"] == 0
    assert_unpublished(t, "rvc_audio")

    # A separate valid owner credential qualifies admission; role loss happens in the host health callback.
    def demote():
        with t.f.case.database.immediate_session() as session:
            session.get_one(m.User, t.owner.user_id).is_instance_admin = False

    calls = fake_local_services(t, monkeypatch, health=demote)
    before_models = set((t.f.services.paths.models / "rvc").rglob("*"))
    upload = enqueue_rvc(t, "rvc.model.upload", t.owner)
    run_one(t)
    assert t.f.services.jobs.get(upload.id).status == "failed" and calls["upload"] == 0
    assert set((t.f.services.paths.models / "rvc").rglob("*")) == before_models
    assert_unpublished(t, "rvc_model")


def test_late_rvc_conversion_account_loss_blocks_export_and_registration(tools, monkeypatch):
    t = tools

    def disable():
        with t.f.case.database.immediate_session() as session:
            session.get_one(m.User, t.owner.user_id).is_active = False

    calls = fake_local_services(t, monkeypatch, conversion=disable)
    exports = []
    monkeypatch.setattr(
        AudioSegment, "export", lambda *_a, **_k: exports.append(True) or forbidden()
    )
    queued = enqueue_rvc(t, "rvc.convert", t.owner)
    run_one(t)
    assert t.f.services.jobs.get(queued.id).status == "failed"
    assert calls["conversion"] == 1 and exports == []
    assert_unpublished(t, "rvc_audio")


def test_training_late_role_loss_before_trainer_and_publication_keeps_failure_bookkeeping(
    tools, monkeypatch
):
    t = tools
    calls = fake_local_services(t, monkeypatch)
    with execution_actor_scope(t.owner):
        run, queued = t.f.services.training.start(training_payload(t, "trainer-gate"))

    def before_trainer(message):
        if message == "Validating XTTS trainer":
            with t.f.case.database.immediate_session() as session:
                session.get_one(m.User, t.owner.user_id).is_instance_admin = False

    run_one(t, kind="training.xtts", progress_hook=before_trainer)
    assert t.f.services.jobs.get(queued.id).status == "failed" and calls["trainer"] == 0
    with t.f.case.database.snapshot_session() as session:
        assert session.get_one(m.TrainingRun, run.id).status == "failed"
    with t.f.case.database.immediate_session() as session:
        session.get_one(m.User, t.owner.user_id).is_instance_admin = True

    def before_publish():
        with t.f.case.database.immediate_session() as session:
            session.get_one(m.User, t.owner.user_id).is_instance_admin = False

    calls = fake_local_services(t, monkeypatch, before_publish=before_publish)
    with execution_actor_scope(t.owner):
        second, job = t.f.services.training.start(training_payload(t, "publication-gate"))
    run_one(t)
    assert t.f.services.jobs.get(job.id).status == "failed" and calls["trainer"] == 1
    with t.f.case.database.snapshot_session() as session:
        assert session.get_one(m.TrainingRun, second.id).status == "failed"
    assert not (t.f.services.paths.models / "xtts" / "publication-gate").exists()
    assert_unpublished(t, "xtts_model")


def test_training_execution_header_refuses_deleted_input_before_any_body(tools):
    t = tools
    with execution_actor_scope(t.owner):
        run, queued = t.f.services.training.start(training_payload(t))
    claimed = t.f.services.jobs.claim("local-gate-header", lease_seconds=300)
    assert claimed is not None and claimed.id == queued.id
    payload = {
        **claimed.payload_json,
        "_job_id": claimed.id,
        "_lease_generation": claimed.lease_generation,
    }
    marker = current_job_lease.set(
        {
            "job_id": claimed.id,
            "owner_id": "local-gate-header",
            "lease_generation": claimed.lease_generation,
        }
    )
    try:
        with t.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, t.audio.id).state = "deleted"

        def proof():
            with execution_actor_scope(t.owner), t.f.case.database.snapshot_session() as session:
                with pytest.raises(ArtifactNotFound):
                    require_training_execution_access(session, payload)

        _result, sql = capture_sql(t.f.case.database, proof)
        assert protected_body_queries(sql) == []
    finally:
        current_job_lease.reset(marker)


def test_original_owner_foreign_training_id_is_sql_scoped_before_hydration(tools):
    t = tools
    with t.f.case.database.immediate_session() as session:
        foreign = m.TrainingRun(
            workspace_id=t.foreign,
            model_name="foreign-private-run",
            source_artifact_id=t.other.id,
            settings_json={"private": "must not hydrate"},
            status="failed",
        )
        session.add(foreign)
        session.flush()
        identifier = foreign.id
    loaded = []

    def body_loaded(row, _context):
        loaded.append(row.id)

    event.listen(m.TrainingRun, "load", body_loaded)
    try:
        with execution_actor_scope(t.owner):
            with pytest.raises(TrainingNotFound):
                t.f.services.training.retry(identifier)
            with pytest.raises(TrainingNotFound):
                t.f.services.training.cancel(identifier)
    finally:
        event.remove(m.TrainingRun, "load", body_loaded)
    assert loaded == []


def test_actorless_model_upload_denies_before_workspace_queries_or_effects(monkeypatch, tmp_path):
    from contextlib import nullcontext

    from pandrator.web.access import local_tools_policy as policy
    from pandrator.web.accounts.administration import AdministratorRequired

    calls = {"workspace": 0, "roots": 0, "resolve": 0}
    workspace = SimpleNamespace(id="synthetic-workspace")
    session = SimpleNamespace(no_autoflush=nullcontext())

    def owner(*_a, **_k):
        calls["workspace"] += 1
        return workspace

    def roots(*_a, **_k):
        calls["roots"] += 1
        return workspace

    def resolve(*_a):
        calls["resolve"] += 1
        raise RuntimeError("Unauthorised handler reached input resolution")

    monkeypatch.setattr(policy, "initial_personal_workspace", owner)
    monkeypatch.setattr(policy, "voice_execution_workspace", roots)
    monkeypatch.setattr(workflow_voice, "voice_execution_workspace", roots, raising=False)
    context = SimpleNamespace(
        database=SimpleNamespace(session=lambda: nullcontext(session)), _resolve_input=resolve
    )
    denied = []
    with execution_actor_scope(None):
        for invoke in (
            lambda: require_local_tool_workspace(cast(Any, session), Op.RVC_MODEL_WRITE),
            lambda: workflow_voice.upload_rvc_model(
                cast(Any, context),
                {"pth_artifact_id": "weights", "index_artifact_id": "index"},
                lambda *_a: None,
                threading.Event(),
            ),
        ):
            try:
                invoke()
            except AdministratorRequired:
                denied.append(True)
            except RuntimeError:
                denied.append(False)
            else:
                denied.append(False)
    (tmp_path / "actorless-model-upload-boundary.json").write_text(
        json.dumps({"denied": denied, "calls": calls}, indent=2)
    )
    assert denied == [True, True]
    assert calls == {"workspace": 0, "roots": 0, "resolve": 0}


def test_http_missing_resource_classes_are_opaque_and_unexpected_errors_still_raise(
    design, monkeypatch
):
    from pandrator.web import api_routes
    from pandrator.web.access.contracts import ProjectNotFound
    from pandrator.web.access.session_policy import SessionNotFound

    monkeypatch.setattr(rvc_handler, "is_rvc_available", forbidden)
    monkeypatch.setattr(rvc_handler, "get_rvc_models", forbidden)
    for error_type in (WorkspaceNotFound, ProjectNotFound, SessionNotFound, ArtifactNotFound):

        def missing(*_a, _error_type=error_type, **_k):
            raise _error_type()

        monkeypatch.setattr(api_routes, "require_local_tool_workspace", missing)
        response = design.users.owner.get("/api/v1/rvc/models")
        assert response.status_code == 404
        assert response.get_json()["error"]["message"] == "Resource not found."

    def unexpected(*_a, **_k):
        raise ValueError("Unexpected local-tool error")

    monkeypatch.setattr(api_routes, "require_local_tool_workspace", unexpected)
    with pytest.raises(ValueError, match="Unexpected local-tool error"):
        design.users.owner.get("/api/v1/rvc/models")
