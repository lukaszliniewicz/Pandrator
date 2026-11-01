"""Requester attribution survives HTTP, CLI and deferred audit persistence."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from pandrator.web import cli
from pandrator.web.accounts.actor_models import AuditActorRecord, JobActorRecord
from pandrator.web.accounts.auth_models import BrowserAuthSession
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_storage,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.api import create_app
from pandrator.web.audit import AuditService
from pandrator.web.auth import AuthService, BootstrapTokenStore, Principal
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import AuditEvent, Job
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class ActorCase:
    app: Flask
    client: FlaskClient
    database: Database
    auth: AuthService
    audit: AuditService
    queue: JobQueue
    csrf: str
    user_id: str
    auth_revision: int
    browser_id: str


@pytest.fixture
def case(tmp_path: Path) -> Iterator[ActorCase]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    launch = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]
    client = app.test_client()
    try:
        response = client.post("/api/v1/auth/bootstrap", json={"token": launch})
        assert response.status_code == 200
        account = services["auth"].initial_account()
        with services["database"].session() as session:
            browser_id = session.scalar(select(BrowserAuthSession.id))
            assert browser_id is not None
        yield ActorCase(
            app,
            client,
            services["database"],
            services["auth"],
            services["audit"],
            services["jobs"],
            response.get_json()["csrf_token"],
            account.user_id,
            account.auth_revision,
            browser_id,
        )
    finally:
        services["quick_transcriptions"].stop_maintenance()
        services["database"].dispose()


def api_principal(case: ActorCase) -> tuple[Principal, str]:
    _record, raw = case.auth.create_api_token("synthetic actor credential")
    principal = case.auth.resolve_api_token(raw, network_zone="loopback", target_instance_id=None)
    assert principal is not None
    return principal, raw


def record_audit(case: ActorCase, principal: Principal, request_id: str) -> None:
    case.audit.record(
        principal=principal,
        request_id=request_id,
        traceparent=None,
        action="synthetic.action",
        method="POST",
        path="/synthetic",
        status_code=201,
        duration_ms=1,
    )


def test_http_job_captures_browser_requester_and_ignores_payload_claims(case: ActorCase) -> None:
    outer = HostActor("pandrator-cli")
    with execution_actor_scope(outer):
        response = case.client.post(
            "/api/v1/jobs",
            json={
                "kind": "noop",
                "payload": {"actor_user_id": "invented-user", "actor_source": "host"},
            },
            headers={"X-CSRF-Token": case.csrf},
        )
        assert current_execution_actor.get() == outer
    assert response.status_code == 202, response.get_json()
    assert "actor_context_json" not in response.get_json()
    with case.database.session() as session:
        record = session.get(JobActorRecord, response.get_json()["id"])
        assert record is not None
        actor = actor_from_storage(record.user_id, record.auth_revision, record.context_json)
        assert isinstance(actor, UserActor)
        assert actor.user_id == case.user_id
        assert actor.auth_revision == case.auth_revision
        assert actor.browser_session_id == case.browser_id
        assert actor.token_id is None
    assert current_execution_actor.get() is None


def test_http_job_captures_api_credential_separately_from_the_user(case: ActorCase) -> None:
    principal, raw = api_principal(case)
    response = case.app.test_client().post(
        "/api/v1/jobs",
        json={"kind": "noop"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 202, response.get_json()
    with case.database.session() as session:
        record = session.get(JobActorRecord, response.get_json()["id"])
        assert record is not None
        actor = actor_from_storage(record.user_id, record.auth_revision, record.context_json)
        assert isinstance(actor, UserActor)
        assert actor.user_id == case.user_id
        assert actor.token_id == principal.token_id
        assert actor.browser_session_id is None
        assert raw not in json.dumps(record.context_json)
    assert current_execution_actor.get() is None


def test_denied_http_request_cannot_inherit_outer_host_authority(case: ActorCase) -> None:
    outer = HostActor("pandrator-cli")
    with execution_actor_scope(outer):
        response = case.app.test_client().post("/api/v1/jobs", json={"kind": "noop"})
        assert response.status_code == 401
        assert current_execution_actor.get() == outer
    with case.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(JobActorRecord)) == 0


def test_request_handler_exception_restores_outer_execution_context(case: ActorCase) -> None:
    outer = HostActor("pandrator-cli")

    def fail_enqueue(*_args: Any, **_kwargs: Any) -> None:
        actor = current_execution_actor.get()
        assert isinstance(actor, UserActor) and actor.user_id == case.user_id
        raise RuntimeError("synthetic handler failure")

    with (
        execution_actor_scope(outer),
        patch.object(case.queue, "enqueue_in_session", side_effect=fail_enqueue),
    ):
        with pytest.raises(RuntimeError, match="synthetic handler failure"):
            case.client.post(
                "/api/v1/jobs", json={"kind": "noop"}, headers={"X-CSRF-Token": case.csrf}
            )
        assert current_execution_actor.get() == outer


def test_http_audit_keeps_the_actor_even_when_logout_revokes_the_credential(
    case: ActorCase,
) -> None:
    response = case.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": case.csrf})
    assert response.status_code == 200
    records = [record for record in case.audit.list() if record["action"] == "auth_logout"]
    assert len(records) == 1
    assert records[0]["user_id"] == case.user_id
    assert records[0]["auth_revision"] == case.auth_revision
    assert records[0]["browser_session_id"] == case.browser_id
    with case.database.session() as session:
        browser = session.get(BrowserAuthSession, case.browser_id)
        assert browser is not None and browser.revoked_at is not None


def test_busy_audit_replay_keeps_actor_and_deduplicates_both_records(case: ActorCase) -> None:
    principal, raw = api_principal(case)
    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        record_audit(case, principal, "synthetic-spooled-actor")
        pending = next((case.database.path.parent / "audit-pending").glob("*.json"))
        contents = pending.read_text()
        assert raw not in contents
        assert json.loads(contents)["actor"]["user_id"] == case.user_id
        writer.rollback()
    case.audit.cleanup()
    assert not pending.exists()
    pending.write_text(contents)
    case.audit.cleanup()
    assert not pending.exists()
    with case.database.session() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.request_id == "synthetic-spooled-actor")
        ).all()
        assert len(events) == 1
        record = session.get(AuditActorRecord, events[0].id)
        assert record is not None and record.user_id == case.user_id
        actor = actor_from_storage(record.user_id, record.auth_revision, record.context_json)
        assert isinstance(actor, UserActor) and actor.token_id == principal.token_id


@pytest.mark.parametrize("shape", ["legacy", "future_version", "null_object"])
def test_old_audit_spool_stays_unknown_and_invalid_new_actor_is_retained(
    case: ActorCase, shape: str
) -> None:
    principal, _raw = api_principal(case)
    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        record_audit(case, principal, "synthetic-spool-shape")
        pending = next((case.database.path.parent / "audit-pending").glob("*.json"))
        payload = json.loads(pending.read_text())
        writer.rollback()
    if shape == "legacy":
        del payload["actor"]
    elif shape == "future_version":
        payload["actor"]["context_json"]["version"] = 999
    else:
        payload["actor"] = {"user_id": None, "auth_revision": None, "context_json": None}
    pending.write_text(json.dumps(payload))
    case.audit.cleanup()
    with case.database.session() as session:
        saved = session.scalar(
            select(AuditEvent).where(AuditEvent.request_id == "synthetic-spool-shape")
        )
        if shape == "legacy":
            assert not pending.exists() and saved is not None
            assert session.get(AuditActorRecord, saved.id) is None
        else:
            assert pending.exists() and saved is None


def test_present_json_null_audit_actor_is_rejected(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    record_audit(case, principal, "synthetic-null-sidecar")
    with case.database.immediate_session() as session:
        identifier = session.scalar(
            select(AuditEvent.id).where(AuditEvent.request_id == "synthetic-null-sidecar")
        )
        assert identifier is not None
        session.execute(
            update(AuditActorRecord)
            .where(AuditActorRecord.audit_event_id == identifier)
            .values(user_id=None, auth_revision=None, context_json=None)
        )
        assert (
            session.scalar(
                text("SELECT context_json FROM audit_actors WHERE audit_event_id = :id"),
                {"id": identifier},
            )
            == "null"
        )
    with pytest.raises(ValueError, match="saved audit actor is invalid"):
        case.audit.list()


def test_audit_event_and_actor_insert_are_atomic(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    invalid = replace(principal, user_id="missing-synthetic-user")
    with pytest.raises(IntegrityError):
        record_audit(case, invalid, "synthetic-rollback")
    with case.database.session() as session:
        assert (
            session.scalar(
                select(AuditEvent.id).where(AuditEvent.request_id == "synthetic-rollback")
            )
            is None
        )


def test_audit_list_bulk_loads_attribution(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    for index in range(12):
        record_audit(case, principal, f"synthetic-bulk-{index}")
    selects: list[str] = []

    def observe(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", observe)
    try:
        listed = case.audit.list()
    finally:
        event.remove(case.database.engine, "before_cursor_execute", observe)
    assert len(selects) == 2
    synthetic = [item for item in listed if item["request_id"].startswith("synthetic-bulk-")]
    assert len(synthetic) == 12
    assert all(item["user_id"] == case.user_id for item in synthetic)
    assert all(item["token_id"] == principal.token_id for item in synthetic)


def test_local_cli_admission_records_host_operator_and_restores_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = prepare_web_test_data_root(tmp_path)
    assert cli.main(["--data-dir", str(tmp_path), "--json", "job", "enqueue", "noop"]) == 0
    job_id = json.loads(capsys.readouterr().out)["id"]
    database = Database(paths.database)
    try:
        with database.session() as session:
            record = session.get(JobActorRecord, job_id)
            assert record is not None and record.user_id is None and record.auth_revision is None
            assert actor_from_storage(
                record.user_id, record.auth_revision, record.context_json
            ) == HostActor("pandrator-cli")
    finally:
        database.dispose()
    assert current_execution_actor.get() is None


def test_remote_cli_does_not_bind_host_authority() -> None:
    observed: list[object] = []

    def remote(_args: argparse.Namespace) -> int:
        observed.append(current_execution_actor.get())
        return 0

    with patch.object(cli, "command_remote", side_effect=remote):
        assert cli.main(["--server", "http://synthetic.invalid", "job", "list"]) == 0
    assert observed == [None]


def test_local_cli_error_restores_context(capsys: pytest.CaptureFixture[str]) -> None:
    def fail(_args: argparse.Namespace) -> int:
        assert current_execution_actor.get() == HostActor("pandrator-cli")
        raise ValueError("synthetic CLI failure")

    with patch.object(cli, "command_job_enqueue", side_effect=fail):
        assert cli.main(["job", "enqueue", "noop"]) == 2
    assert "synthetic CLI failure" in capsys.readouterr().err
    assert current_execution_actor.get() is None
