"""Named-user RUN admission and real leased Worker sound publication."""

from __future__ import annotations

import hashlib
import io
import socket
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from pandrator.logic import audio_cpp_assets as assets
from pandrator.logic import audio_cpp_sfx as sfx
from pandrator.web import models as m
from pandrator.web import sound_generation_admission as admission
from pandrator.web.access.contracts import AccessDenied, WorkspaceNotFound
from pandrator.web.access.sound_generation_policy import require_sound_generation_access
from pandrator.web.accounts.auth_models import BrowserAuthSession
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import team
from tests.test_provider_workspace_access import scoped_token, sql_capture
from tests.test_sound_generation import _write_wave

REQUEST = {"prompt": "rain on a roof", "seed": 7, "duration_seconds": 1}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Sound generation tests permit only synthetic model I/O")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def token_actor(f, *, user_id=None, workspace=None, scopes=None):
    token, bearer = scoped_token(
        f.case,
        user_id or f.users.alice_id,
        {"app.read", "app.run"} if scopes is None else scopes,
        workspace=f.users.workspace_id if workspace is None else workspace,
    )
    principal = f.case.auth.resolve_api_token(
        bearer["Authorization"].removeprefix("Bearer "),
        network_zone="loopback",
        target_instance_id=f.services.identity.snapshot(
            observed_origin="http://localhost"
        ).instance_id,
    )
    assert principal is not None
    actor = actor_from_principal(principal)
    assert type(actor) is UserActor
    return actor, token


@pytest.fixture
def generation_case(case, monkeypatch):
    users = team(case, monkeypatch)
    services = case.app.extensions["pandrator"]["services"]
    events = []
    hook = {"runtime": None, "native": None, "progress": None}
    identity = {"provider": "synthetic", "runtime_sha256": "a" * 64}

    def runtime(*_args, **_kwargs):
        events.append("runtime")
        if hook["runtime"]:
            hook["runtime"]()
        return dict(identity)

    def native(_request, path, *, settings, cancel_event, progress, before_io=None):
        if before_io is not None:
            before_io()
        events.append("native")
        progress("generate", 0, 1)
        if hook["progress"]:
            hook["progress"]()
        if before_io is not None:
            before_io()
        properties = _write_wave(path)
        if hook["native"]:
            hook["native"]()
        return properties

    monkeypatch.setattr(sfx, "require_backend", lambda *_args: None)
    monkeypatch.setattr(sfx, "request_runtime_identity", runtime)
    monkeypatch.setattr(sfx, "generate_sound", native)
    f = SimpleNamespace(
        case=case,
        users=users,
        services=services,
        events=events,
        hook=hook,
        identity=identity,
    )
    f.actor, f.token = token_actor(f)
    f.prepared = admission.prepare_generation(REQUEST, {})
    events.clear()
    return f


def admit(f, *, actor=None, workspace=None, payload=None):
    with (
        execution_actor_scope(f.actor if actor is None else actor),
        f.case.database.immediate_session() as session,
    ):
        return admission.admit_generation_in_session(
            session,
            f.services.jobs,
            f.services.paths,
            REQUEST if payload is None else payload,
            f.prepared,
            workspace_id=workspace,
        )


def run(f):
    assert Worker(
        f.services.jobs,
        "sound-generation-access-worker",
        f.services.workflow_handlers.handlers(),
    ).run_once()


def job(f, identifier):
    with f.case.database.snapshot_session() as session:
        row = session.get_one(m.Job, identifier)
        session.expunge(row)
        return row


@contextmanager
def claimed(f, receipt):
    row = f.services.jobs.claim("sound-generation-policy-worker", lease_seconds=300)
    assert row is not None and row.id == receipt["job_id"]
    lease: JobLease = {
        "job_id": row.id,
        "owner_id": "sound-generation-policy-worker",
        "lease_generation": row.lease_generation,
    }
    marker = current_job_lease.set(lease)
    try:
        yield {**row.payload_json, "_job_id": row.id, "_lease_generation": row.lease_generation}
    finally:
        current_job_lease.reset(marker)


def test_caller_personal_and_selected_member_team_have_separate_cache_roots(generation_case):
    f = generation_case
    browser = actor_from_principal(current_browser_principal(replace(f.case, client=f.users.alice)))
    personal = admit(f, actor=browser)
    shared = admit(f, workspace=f.users.workspace_id)
    assert personal["candidate_id"] != shared["candidate_id"]
    assert admit(f, workspace=f.users.workspace_id)["candidate_id"] == shared["candidate_id"]
    assert admit(f, actor=browser)["candidate_id"] == personal["candidate_id"]
    with f.case.database.snapshot_session() as session:
        home = session.get_one(m.Workspace, session.get_one(m.Job, personal["job_id"]).workspace_id)
        assert home.personal_owner_user_id == f.users.alice_id
        assert session.get_one(m.Job, shared["job_id"]).workspace_id == f.users.workspace_id
        assert (
            session.get_one(m.SoundVariant, shared["candidate_id"]).workspace_id
            == f.users.workspace_id
        )
    assert "app.write" not in f.actor.scopes and "app.admin" not in f.actor.scopes


def test_admission_denies_missing_scopes_grants_and_unrelated_admin_before_private_work(
    generation_case,
    monkeypatch,
):
    f = generation_case
    admin = actor_from_principal(current_browser_principal(replace(f.case, client=f.users.owner)))
    with f.case.database.snapshot_session() as session:
        foreign_home = session.scalar(
            select(m.Workspace.id).where(m.Workspace.personal_owner_user_id == f.users.bob_id)
        )
    assert foreign_home is not None

    def private_work(*_args, **_kwargs):
        raise AssertionError("Denied admission must precede seed/cache/file work")

    monkeypatch.setattr(admission, "_resolved_request", private_work)
    denied_actors = [
        replace(f.actor, scopes=frozenset({scope})) for scope in ("app.read", "app.run")
    ]
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(ApiTokenWorkspaceGrant, (f.token.id, f.users.workspace_id)))
    denied_actors.append(f.actor)
    for actor in denied_actors:
        with pytest.raises((AccessDenied, WorkspaceNotFound)):
            admit(f, actor=actor, workspace=f.users.workspace_id)
    with pytest.raises(WorkspaceNotFound):
        admit(f, actor=admin, workspace=foreign_home)
    assert f.events == []


def test_registered_member_worker_publishes_after_natural_token_expiry_and_browser_logout(
    generation_case,
):
    f = generation_case
    for index, transport in enumerate(("token", "browser")):
        payload = {**REQUEST, "seed": 7 + index}
        f.prepared = admission.prepare_generation(payload, {})
        actor = (
            f.actor
            if transport == "token"
            else actor_from_principal(
                current_browser_principal(replace(f.case, client=f.users.alice))
            )
        )
        assert type(actor) is UserActor
        receipt = admit(f, actor=actor, workspace=f.users.workspace_id, payload=payload)
        with f.case.database.immediate_session() as session:
            if transport == "token":
                session.get_one(m.ApiToken, f.token.id).expires_at = m.utcnow() - timedelta(
                    seconds=1
                )
            else:
                session.get_one(
                    BrowserAuthSession, actor.browser_session_id
                ).revoked_at = m.utcnow()
        run(f)
        stored = job(f, receipt["job_id"])
        assert stored.status == "succeeded", stored.error_message
        with f.case.database.snapshot_session() as session:
            candidate = session.get_one(m.SoundVariant, receipt["candidate_id"])
            artifact = session.get_one(m.Artifact, candidate.artifact_id)
            assert candidate.state == "ready"
            assert artifact.workspace_id == candidate.workspace_id == f.users.workspace_id
    assert f.events.count("native") == 2


def test_current_revocation_grant_departure_archive_and_account_loss_clean_only_attempt(
    generation_case,
):
    f = generation_case
    staging = f.services.paths.root / "sounds" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    retained = staging / "retained.wav"
    retained.write_bytes(b"Prior attempt")
    for loss in ("revoke", "grant", "membership", "archive", "account"):
        receipt = admit(f, workspace=f.users.workspace_id)

        def change(loss=loss):
            with f.case.database.immediate_session() as session:
                if loss == "revoke":
                    session.get_one(m.ApiToken, f.token.id).revoked_at = m.utcnow()
                elif loss == "grant":
                    session.delete(
                        session.get_one(ApiTokenWorkspaceGrant, (f.token.id, f.users.workspace_id))
                    )
                elif loss == "membership":
                    session.delete(
                        session.get_one(
                            m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id)
                        )
                    )
                elif loss == "archive":
                    session.get_one(m.Workspace, f.users.workspace_id).state = "archived"
                else:
                    session.get_one(m.User, f.users.alice_id).is_active = False

        for boundary in f.hook:
            f.hook[boundary] = None
        boundary = {"revoke": "runtime", "grant": "progress"}.get(loss, "native")
        f.hook[boundary] = change
        run(f)
        assert job(f, receipt["job_id"]).status == "failed"
        with f.case.database.immediate_session() as session:
            candidate = session.get_one(m.SoundVariant, receipt["candidate_id"])
            assert candidate.artifact_id is None and candidate.state != "ready"
            candidate.state = "discarded"
            session.get_one(m.ApiToken, f.token.id).revoked_at = None
            if loss == "grant":
                session.add(
                    ApiTokenWorkspaceGrant(token_id=f.token.id, workspace_id=f.users.workspace_id)
                )
            if loss == "membership":
                session.add(
                    m.WorkspaceMembership(
                        workspace_id=f.users.workspace_id, user_id=f.users.alice_id, role="member"
                    )
                )
            session.get_one(m.Workspace, f.users.workspace_id).state = "active"
            session.get_one(m.User, f.users.alice_id).is_active = True
        assert list(staging.iterdir()) == [retained] and retained.read_bytes() == b"Prior attempt"


def test_job_candidate_workspace_and_lease_headers_deny_before_private_hydration(generation_case):
    f = generation_case
    receipt = admit(f, workspace=f.users.workspace_id)
    with execution_actor_scope(f.actor), f.case.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            require_sound_generation_access(session, {"_job_id": receipt["job_id"]})
    with claimed(f, receipt) as data, execution_actor_scope(f.actor):
        for changes in (
            {"candidate_id": "foreign"},
            {"workspace_id": "foreign"},
            {"workspace_id": " " + f.users.workspace_id},
            {"_job_id": "foreign"},
            {"_lease_generation": 100},
        ):
            with (
                sql_capture(f.case.database) as statements,
                f.case.database.snapshot_session() as session,
            ):
                with pytest.raises((AccessDenied, WorkspaceNotFound)):
                    require_sound_generation_access(session, {**data, **changes})
            assert not any(
                "jobs.payload_json" in sql or "sound_variants.provenance_json" in sql
                for sql, _ in statements
            )
        with f.case.database.immediate_session() as session:
            session.get_one(m.Job, receipt["job_id"]).kind = "voice.transcribe"
        with f.case.database.snapshot_session() as session:
            with pytest.raises(WorkspaceNotFound):
                require_sound_generation_access(session, data)


def test_legacy_host_and_actorless_execution_cannot_borrow_saved_named_actor(generation_case):
    f = generation_case
    receipt = admit(f, workspace=f.users.workspace_id)
    with claimed(f, receipt) as data:
        for actor in (None, HostActor("pandrator-cli"), cast(Any, object())):
            with execution_actor_scope(actor), f.case.database.snapshot_session() as session:
                with pytest.raises(AccessDenied):
                    require_sound_generation_access(session, data)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Job, receipt["job_id"]).status = "failed"
        session.get_one(m.SoundVariant, receipt["candidate_id"]).state = "discarded"
    for actor in (None, HostActor("pandrator-cli")):
        with execution_actor_scope(actor), f.case.database.immediate_session() as session:
            legacy = admission.admit_generation_in_session(
                session,
                f.services.jobs,
                f.services.paths,
                REQUEST,
                f.prepared,
            )
        run(f)
        assert job(f, legacy["job_id"]).status == "succeeded"
        with f.case.database.immediate_session() as session:
            session.get_one(m.SoundVariant, legacy["candidate_id"]).state = "discarded"


def test_worker_acquisition_context_allows_verified_reuse_and_only_current_admin_download(
    generation_case,
    monkeypatch,
    tmp_path,
):
    f = generation_case
    data = b"synthetic pinned model"
    spec = {
        "id": "synthetic",
        "label": "Synthetic",
        "filename": "model.gguf",
        "url": "https://huggingface.co/synthetic/never-requested/resolve/pinned/model.gguf",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    destination = tmp_path / "synthetic-model" / "model.gguf"
    opened = []

    def opener(*_args, **_kwargs):
        opened.append("synthetic-network")
        return io.BytesIO(data)

    original = sfx.generate_sound

    def native(*args, **kwargs):
        kwargs["before_io"]()
        assets.ensure_trusted_file(spec, destination, opener=opener)
        return original(*args, **kwargs)

    monkeypatch.setattr(sfx, "generate_sound", native)
    denied = admit(f, workspace=f.users.workspace_id)
    run(f)
    assert job(f, denied["job_id"]).status == "failed"
    assert not opened and not destination.exists()
    admin, _token = token_actor(
        f, user_id=f.case.actor.user_id, scopes={"app.read", "app.run", "app.admin"}
    )
    allowed = admit(f, actor=admin, workspace=f.users.workspace_id)
    run(f)
    assert job(f, allowed["job_id"]).status == "succeeded"
    assert opened == ["synthetic-network"] and destination.read_bytes() == data
    # Keep the verified asset but bypass the ready candidate cache for Member inference.
    with f.case.database.immediate_session() as session:
        session.get_one(m.SoundVariant, allowed["candidate_id"]).state = "discarded"
    reused = admit(f, workspace=f.users.workspace_id)
    run(f)
    assert job(f, reused["job_id"]).status == "succeeded"
    assert opened == ["synthetic-network"]


def test_final_precommit_authority_loss_rolls_back_publication_and_owned_file(
    generation_case,
    monkeypatch,
):
    f = generation_case
    receipt = admit(f, workspace=f.users.workspace_id)
    original = f.services.artifacts.register_in_session

    def lose_membership(session, *args, **kwargs):
        artifact = original(session, *args, **kwargs)
        session.delete(
            session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
        )
        return artifact

    monkeypatch.setattr(f.services.artifacts, "register_in_session", lose_membership)
    run(f)
    assert job(f, receipt["job_id"]).status == "failed"
    with f.case.database.snapshot_session() as session:
        candidate = session.get_one(m.SoundVariant, receipt["candidate_id"])
        assert candidate.artifact_id is None and candidate.state != "ready"
        assert session.get_one(m.WorkspaceMembership, (f.users.workspace_id, f.users.alice_id))
    for directory in ("staging", "audio"):
        path = f.services.paths.root / "sounds" / directory
        assert not path.exists() or not list(path.iterdir())
