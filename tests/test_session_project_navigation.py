"""Lightweight project memberships survive session detail and event refreshes."""

import pytest
from sqlalchemy import event

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.projects.artifact_identity import resolve_artifact_ownership
from pandrator.web.projects.identity import create_session_project
from pandrator.web.session_routes import session_project_memberships
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def navigation_case(tmp_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Unexpected network call")

    monkeypatch.setattr("requests.sessions.Session.request", unexpected)
    monkeypatch.setattr("socket.socket.connect", unexpected)
    monkeypatch.setattr(
        "pandrator.web.capabilities.CapabilityService.get", lambda *_args, **_kwargs: {}
    )
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()})
    services = app.extensions["pandrator"]
    database = services["database"]
    with database.session() as session:
        root_record = SessionRecord(id="root", name="Root", source_language="en")
        create_session_project(session, root_record)
        branch_record = SessionRecord(
            id="branch", name="Japanese", source_language="en", target_language="ja"
        )
        create_session_project(session, branch_record, project_id=root_record.project_id)
        other_record = SessionRecord(id="other", name="Independent")
        create_session_project(session, other_record)
        session.flush()
        artifact_ownership = resolve_artifact_ownership(session, session_id="root")
        session.add(
            Artifact(
                id="checkpoint",
                session_id="root",
                workspace_id=artifact_ownership.workspace_id,
                project_id=artifact_ownership.project_id,
                kind="txt",
                role="correction",
                relative_path="root/checkpoint.txt",
            )
        )
        session.flush()
        session.add(
            TranslationProject(
                id="project",
                name="Book languages",
                source_session_id="root",
                project_root_id=root_record.project_id,
                checkpoint_artifact_id="checkpoint",
                source_content_hash="source",
                source_language="en",
            )
        )
        session.flush()
        session.add(
            TranslationProjectBranch(
                project_id="project",
                session_id="branch",
                target_language="ja",
                source_checkpoint_artifact_id="checkpoint",
                source_content_hash="source",
            )
        )
    yield client, database
    services["tts_catalogue"].close()
    services["tts_catalogue"].providers.close()
    database.dispose()


def test_list_detail_and_event_memberships_match_without_readiness(navigation_case, monkeypatch):
    client, database = navigation_case
    expected = {
        "root": {
            "id": "project",
            "name": "Book languages",
            "source_session_id": "root",
            "role": "source",
        },
        "branch": {
            "id": "project",
            "name": "Book languages",
            "source_session_id": "root",
            "role": "branch",
            "target_language": "ja",
        },
        "other": None,
    }

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Navigation must not inspect project readiness")

    monkeypatch.setattr("pandrator.web.project_readiness.enrich_project_payload", unexpected)
    listed = client.get("/api/v1/sessions").get_json()["items"]
    assert {row["id"]: row["translation_project"] for row in listed} == expected
    for session_id, membership in expected.items():
        response = client.get(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 200
        assert response.get_json()["translation_project"] == membership
        assert response.headers["ETag"] == '"1"'
    for view in ("full", "compact"):
        response = client.get(f"/api/v1/events/snapshot?view={view}")
        assert response.status_code == 200
        rows = response.get_json()["sessions"]["items"]
        assert {row["id"]: row["translation_project"] for row in rows} == expected
    assert client.get("/api/v1/sessions/missing").status_code == 404


def test_membership_helper_is_scalar_bounded_and_empty_ids_do_not_query(navigation_case):
    _client, database = navigation_case
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        assert session_project_memberships(database, []) == {}
        assert statements == []
        membership = session_project_memberships(database, ["root", "branch", "other"])
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert len(statements) == 2
    assert all(" IN (" in sql for sql in statements)
    assert not any("checkpoint_artifact_id" in sql or "content_hash" in sql for sql in statements)
    membership["branch"]["name"] = "Changed"
    assert session_project_memberships(database, ["branch"])["branch"]["name"] == "Book languages"
    assert "root" not in session_project_memberships(database, ["branch"])
