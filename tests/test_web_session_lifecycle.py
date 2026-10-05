"""Native HTTP session lifecycle qualification using disposable database/files."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import Connection, event, select
from werkzeug.test import TestResponse

from pandrator.web.api import create_app
from pandrator.web.application_services import ApplicationServices
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import Artifact, Job, SessionRecord
from tests.web_test_support import prepare_web_test_data_root


def _record(database: Database, session_id: str) -> dict[str, Any]:
    with database.engine.connect() as connection:
        return deepcopy(
            dict(
                connection.execute(
                    select(SessionRecord.__table__).where(SessionRecord.id == session_id)
                )
                .mappings()
                .one()
            )
        )


def _artifacts(database: Database) -> list[dict[str, Any]]:
    with database.engine.connect() as connection:
        return deepcopy(
            [
                dict(row)
                for row in connection.execute(
                    select(Artifact.__table__).order_by(Artifact.id)
                ).mappings()
            ]
        )


def _job(database: Database, job_id: str) -> dict[str, Any]:
    with database.engine.connect() as connection:
        return deepcopy(
            dict(connection.execute(select(Job.__table__).where(Job.id == job_id)).mappings().one())
        )


@dataclass
class LifecycleFixture:
    app: Flask
    client: FlaskClient
    services: ApplicationServices
    csrf: str
    session_id: str
    file: Path
    file_bytes: bytes
    artifact_id: str

    @property
    def database(self) -> Database:
        return self.services.database

    def request(
        self,
        operation: str,
        *,
        revision: int | str | None = 1,
        client: FlaskClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        request_headers = {"X-CSRF-Token": self.csrf} if headers is None else dict(headers)
        if revision is not None:
            request_headers["If-Match"] = f'"{revision}"'
        return (client or self.client).open(
            f"/api/v1/sessions/{self.session_id}"
            + ("" if operation == "delete" else f"/{operation}"),
            method="DELETE" if operation == "delete" else "POST",
            headers=request_headers,
        )

    def snapshot(self) -> tuple[dict[str, Any], list[dict[str, Any]], bytes]:
        return (
            _record(self.database, self.session_id),
            _artifacts(self.database),
            self.file.read_bytes(),
        )

    def assert_unchanged(
        self,
        before: tuple[dict[str, Any], list[dict[str, Any]], bytes],
    ) -> None:
        assert self.snapshot() == before


@pytest.fixture
def lifecycle(tmp_path: Path) -> Iterator[LifecycleFixture]:
    root = tmp_path / "data"
    prepare_web_test_data_root(root)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=root,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]["services"]
    try:
        client = app.test_client()
        authenticated = client.post("/api/v1/auth/bootstrap", json={"token": token})
        assert authenticated.status_code == 200
        csrf = authenticated.get_json()["csrf_token"]
        policy = client.patch(
            "/api/v1/session-trash-policy",
            json={"expected_revision": 0, "days": 3},
            headers={"X-CSRF-Token": csrf},
        )
        assert policy.status_code == 200
        assert policy.get_json() == {"days": 3, "revision": 1}
        created = client.post(
            "/api/v1/sessions",
            json={
                "name": "Native lifecycle fixture",
                "workflow_kind": "voiceover",
                "source_language": "en",
                "target_language": "pl",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201
        assert created.headers["ETag"] == '"1"'
        payload = created.get_json()
        file = services.paths.sessions / payload["storage_key"] / "fixture.txt"
        file_bytes = b"Native session lifecycle fixture bytes.\n"
        file.write_bytes(file_bytes)
        artifact = services.artifacts.register(file, kind="text", session_id=payload["id"])
        yield LifecycleFixture(
            app, client, services, csrf, payload["id"], file, file_bytes, artifact.id
        )
    finally:
        services.close()


def _error(response: TestResponse, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code


def _persisted_matches_response(
    lifecycle: LifecycleFixture, response: TestResponse
) -> dict[str, Any]:
    persisted = _record(lifecycle.database, lifecycle.session_id)
    payload = response.get_json()
    for key, value in payload.items():
        stored = persisted[key]
        if isinstance(value, str) and (key.endswith("_at") or key == "purge_after"):
            assert isinstance(stored, datetime)
            parsed = datetime.fromisoformat(value)
            # SQLite omits tzinfo; models.utcnow stores these timestamps as UTC.
            if stored.tzinfo is None:
                stored = stored.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            assert stored.astimezone(timezone.utc) == parsed.astimezone(timezone.utc)
        else:
            assert stored == value
    assert response.headers["ETag"] == f'"{persisted["revision"]}"'
    return persisted


def _list_ids(lifecycle: LifecycleFixture, *, include_trashed: bool = False) -> set[str]:
    response = lifecycle.client.get(
        "/api/v1/sessions",
        query_string={"include_trashed": str(include_trashed).lower()},
    )
    assert response.status_code == 200
    return {item["id"] for item in response.get_json()["items"]}


def test_http_trash_restore_preserves_identity_artifacts_and_files(
    lifecycle: LifecycleFixture,
) -> None:
    before, artifacts, file_bytes = lifecycle.snapshot()
    assert file_bytes == lifecycle.file_bytes
    fetched = lifecycle.client.get(f"/api/v1/sessions/{lifecycle.session_id}")
    assert fetched.status_code == 200
    assert _persisted_matches_response(lifecycle, fetched) == before
    assert before["revision"] == 1
    trashed = lifecycle.request("delete")
    assert trashed.status_code == 200
    record = _persisted_matches_response(lifecycle, trashed)
    assert (record["revision"], record["status"]) == (2, "trashed")
    assert record["trashed_at"] is not None
    assert record["purge_after"] - record["trashed_at"] == timedelta(days=3)
    assert lifecycle.session_id not in _list_ids(lifecycle)
    assert lifecycle.session_id in _list_ids(lifecycle, include_trashed=True)
    assert _artifacts(lifecycle.database) == artifacts
    assert lifecycle.file.read_bytes() == file_bytes
    restored = lifecycle.request("restore", revision=2)
    assert restored.status_code == 200
    record = _persisted_matches_response(lifecycle, restored)
    assert (record["revision"], record["status"], record["trashed_at"], record["purge_after"]) == (
        3,
        "idle",
        None,
        None,
    )
    for key in ("id", "storage_key", "name", "source_language", "target_language"):
        assert record[key] == before[key]
    assert lifecycle.session_id in _list_ids(lifecycle)
    assert _artifacts(lifecycle.database) == artifacts
    assert lifecycle.file.read_bytes() == file_bytes


@pytest.mark.parametrize("operation", ["delete", "restore"])
def test_stale_lifecycle_revision_preserves_entire_record(
    lifecycle: LifecycleFixture, operation: str
) -> None:
    assert lifecycle.request("delete").status_code == 200
    before = lifecycle.snapshot()
    assert before[0]["revision"] == 2
    _error(lifecycle.request(operation, revision=1), 409, "revision_conflict")
    lifecycle.assert_unchanged(before)


@pytest.mark.parametrize("operation", ["delete", "restore"])
@pytest.mark.parametrize("revision", [None, "invalid"])
def test_missing_or_invalid_precondition_preserves_state(
    lifecycle: LifecycleFixture,
    operation: str,
    revision: str | None,
) -> None:
    if operation == "restore":
        assert lifecycle.request("delete").status_code == 200
    before = lifecycle.snapshot()
    _error(lifecycle.request(operation, revision=revision), 428, "precondition_required")
    lifecycle.assert_unchanged(before)


@pytest.mark.parametrize(
    "status", ["queued", "running", "cancel_requested", "succeeded", "failed", "canceled"]
)
def test_delete_blocks_active_jobs_and_allows_terminal_jobs(
    lifecycle: LifecycleFixture, status: str
) -> None:
    with lifecycle.database.session() as session:
        job = Job(kind="lifecycle.fixture", session_id=lifecycle.session_id, status=status)
        session.add(job)
        session.flush()
        job_id = job.id
    before = lifecycle.snapshot()
    job_before = _job(lifecycle.database, job_id)
    response = lifecycle.request("delete")
    if status in {"queued", "running", "cancel_requested"}:
        _error(response, 409, "session_busy")
        lifecycle.assert_unchanged(before)
    else:
        assert response.status_code == 200
        record = _persisted_matches_response(lifecycle, response)
        assert (record["status"], record["revision"]) == ("trashed", 2)
        assert _artifacts(lifecycle.database) == before[1]
        assert lifecycle.file.read_bytes() == before[2]
    assert _job(lifecycle.database, job_id) == job_before


@pytest.mark.parametrize("operation", ["delete", "restore"])
def test_purging_session_refuses_lifecycle_mutations(
    lifecycle: LifecycleFixture, operation: str
) -> None:
    with lifecycle.database.session() as session:
        record = session.get(SessionRecord, lifecycle.session_id)
        assert record is not None
        record.status = "purging"
    before = lifecycle.snapshot()
    _error(lifecycle.request(operation), 409, "session_busy")
    lifecycle.assert_unchanged(before)


class InjectedCommitFailure(RuntimeError):
    """Private fault raised after the native transaction has flushed its mutation."""


@pytest.mark.parametrize("operation,expected_status", [("delete", "trashed"), ("restore", "idle")])
def test_real_connection_commit_failure_rolls_back_lifecycle(
    lifecycle: LifecycleFixture,
    operation: str,
    expected_status: str,
) -> None:
    if operation == "restore":
        assert lifecycle.request("delete").status_code == 200
    before = lifecycle.snapshot()
    revision = before[0]["revision"]
    witnesses: list[tuple[Connection, str, int]] = []

    def refuse_commit(connection: Connection) -> None:
        status, current_revision = connection.execute(
            select(SessionRecord.status, SessionRecord.revision).where(
                SessionRecord.id == lifecycle.session_id
            )
        ).one()
        if (status, current_revision) == (expected_status, revision + 1):
            assert connection.in_transaction()
            witnesses.append((connection, status, current_revision))
            raise InjectedCommitFailure("controlled lifecycle commit refusal")

    engine = lifecycle.database.engine
    event.listen(engine, "commit", refuse_commit)
    try:
        with pytest.raises(InjectedCommitFailure, match="controlled lifecycle commit refusal"):
            lifecycle.request(operation, revision=revision)
    finally:
        event.remove(engine, "commit", refuse_commit)
    assert len(witnesses) == 1
    assert witnesses[0][1:] == (expected_status, revision + 1)
    lifecycle.assert_unchanged(before)


def test_reindex_reports_only_target_anomalies_without_mutations(
    lifecycle: LifecycleFixture, tmp_path: Path
) -> None:
    folder = lifecycle.file.parent
    missing = folder / "missing.txt"
    missing.write_bytes(b"registered missing fixture")
    missing_artifact = lifecycle.services.artifacts.register(
        missing, kind="text", session_id=lifecycle.session_id
    )
    missing.unlink()
    changed = folder / "changed.txt"
    original_changed = b"original"
    changed_bytes = b"changed fixture with a different size"
    changed.write_bytes(original_changed)
    changed_artifact = lifecycle.services.artifacts.register(
        changed, kind="text", session_id=lifecycle.session_id
    )
    changed.write_bytes(changed_bytes)
    deleted = folder / "deleted.txt"
    deleted.write_bytes(b"deleted missing fixture")
    deleted_artifact = lifecycle.services.artifacts.register(
        deleted, kind="text", session_id=lifecycle.session_id
    )
    deleted.unlink()
    outside = tmp_path / "outside.txt"
    outside_bytes = b"controlled unmanaged file retained"
    outside.write_bytes(outside_bytes)
    other_created = lifecycle.client.post(
        "/api/v1/sessions",
        json={"name": "Other lifecycle fixture", "workflow_kind": "voiceover"},
        headers={"X-CSRF-Token": lifecycle.csrf},
    )
    assert other_created.status_code == 201
    other = other_created.get_json()
    other_file = lifecycle.services.paths.sessions / other["storage_key"] / "other-missing.txt"
    other_file.write_bytes(b"other session missing fixture")
    other_artifact = lifecycle.services.artifacts.register(
        other_file, kind="text", session_id=other["id"]
    )
    other_file.unlink()
    with lifecycle.database.session() as session:
        deleted_row = session.get(Artifact, deleted_artifact.id)
        assert deleted_row is not None
        deleted_row.state = "deleted"
        escaped = Artifact(
            session_id=lifecycle.session_id,
            kind="text",
            relative_path="../outside.txt",
            size_bytes=len(outside_bytes),
        )
        session.add(escaped)
        session.flush()
        escaped_id = escaped.id
    before = lifecycle.snapshot()
    other_before = _record(lifecycle.database, other["id"])
    response = lifecycle.request("reindex")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["session_id"] == lifecycle.session_id
    reports = payload["reports"]
    assert len(reports) == 3
    by_id = {report["artifact_id"]: report for report in reports}
    assert {key: value["status"] for key, value in by_id.items()} == {
        missing_artifact.id: "missing",
        changed_artifact.id: "changed",
        escaped_id: "escaped",
    }
    assert by_id[changed_artifact.id]["expected_size"] == len(original_changed)
    assert by_id[changed_artifact.id]["actual_size"] == len(changed_bytes)
    assert {lifecycle.artifact_id, deleted_artifact.id, other_artifact.id}.isdisjoint(by_id)
    lifecycle.assert_unchanged(before)
    assert _record(lifecycle.database, other["id"]) == other_before
    assert changed.read_bytes() == changed_bytes
    assert outside.read_bytes() == outside_bytes
    assert not missing.exists() and not deleted.exists() and not other_file.exists()


@pytest.mark.parametrize("operation", ["delete", "restore", "reindex"])
def test_missing_session_returns_not_found_without_touching_existing_session(
    lifecycle: LifecycleFixture, operation: str
) -> None:
    before = lifecycle.snapshot()
    response = lifecycle.client.open(
        "/api/v1/sessions/00000000-0000-4000-8000-000000000000"
        + ("" if operation == "delete" else f"/{operation}"),
        method="DELETE" if operation == "delete" else "POST",
        headers={"X-CSRF-Token": lifecycle.csrf, "If-Match": '"1"'},
    )
    _error(response, 404, "not_found")
    lifecycle.assert_unchanged(before)


@pytest.mark.parametrize("operation", ["delete", "restore", "reindex"])
@pytest.mark.parametrize(
    "auth,status,code",
    [
        ("anonymous", 401, "authentication_required"),
        ("cookie_without_csrf", 403, "csrf_failed"),
        ("read_only_bearer", 403, "scope_denied"),
    ],
)
def test_lifecycle_auth_boundaries_preserve_state(
    lifecycle: LifecycleFixture,
    operation: str,
    auth: str,
    status: int,
    code: str,
) -> None:
    revision = 1
    if operation == "restore":
        assert lifecycle.request("delete").status_code == 200
        revision = 2
    if auth == "cookie_without_csrf":
        client = lifecycle.client
        headers: dict[str, str] = {}
    else:
        client = lifecycle.app.test_client()
        headers = {}
        if auth == "read_only_bearer":
            _token, raw = lifecycle.services.auth.create_api_token(
                "Lifecycle read only fixture", scopes=("app.read",)
            )
            headers["Authorization"] = f"Bearer {raw}"
    before = lifecycle.snapshot()
    _error(
        lifecycle.request(operation, revision=revision, client=client, headers=headers),
        status,
        code,
    )
    lifecycle.assert_unchanged(before)
