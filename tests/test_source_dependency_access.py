"""Current selected source attachments publish bytes within target authority."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import sqlite

from pandrator.web import models as m
from pandrator.web.access.source_dependency_visibility import (
    current_source_dependency_clause,
)
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_principal,
    execution_actor_scope,
)
from pandrator.web.models import (
    Document,
    DocumentRevision,
    TimedWord,
)
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.source_document_reads import (
    list_revision_words,
    list_session_documents,
)
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import scoped_token
from tests.test_source_library_workspace_http import case as case
from tests.test_source_library_workspace_http import library as library
from tests.test_source_library_workspace_http import no_network as no_network


def _project(client, csrf, *, workspace_id: str | None = None):
    values = {"name": f"Source dependency {uuid4()}", "workflow_kind": "subtitles"}
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    return body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, str(uuid4()))),
        201,
    )


def _source_artifact(f, workspace_id: str, name: str, *, project_id: str | None = None):
    path = f.services.paths.uploads / f"{uuid4()}.txt"
    path.write_text(name, encoding="utf-8")
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="text",
            role="upload",
            project_id=project_id,
            workspace_id=workspace_id,
        )
    asset = f.services.source_library.ensure_for_artifact(
        artifact.id,
        display_name=name,
        kind="text",
        workspace_id=workspace_id,
    )
    return artifact.id, asset.id, path.read_bytes()


def _artifact_bytes(f, artifact_id: str) -> bytes:
    with f.case.database.snapshot_session() as session:
        artifact = session.get_one(m.Artifact, artifact_id)
        relative_path = artifact.relative_path
    return f.services.paths.managed_path(relative_path).read_bytes()


def _attach(client, csrf, record, asset_id: str, *, role: str):
    current = body(client.get(f"/api/v1/sessions/{record['id']}"))
    return body(
        client.post(
            f"/api/v1/sessions/{record['id']}/sources",
            json={"source_asset_id": asset_id, "role": role},
            headers={
                **headers(csrf, str(uuid4())),
                "If-Match": str(current["revision"]),
            },
        ),
        201,
    )


def _share_viewer(client, csrf, record, user_id: str) -> None:
    path = f"/api/v1/projects/{record['project_id']}/viewers"
    revision = body(client.get(path))["revision"]
    body(
        client.put(
            f"{path}/{user_id}",
            json={"expected_revision": revision},
            headers=csrf,
        )
    )


def _alice_actor(f) -> UserActor:
    principal = current_browser_principal(replace(f.case, client=f.u.alice))
    actor = actor_from_principal(principal)
    assert type(actor) is UserActor
    return actor


def _viewed_source(f, *, role: str = "primary"):
    target = _project(f.u.owner, f.u.oh)
    artifact_id = f.original.artifact_id
    assert artifact_id is not None
    attachment = _attach(f.u.owner, f.u.oh, target, f.original.id, role=role)
    _share_viewer(f.u.owner, f.u.oh, target, f.u.alice_id)
    _token, bearer = scoped_token(
        f.case, f.u.alice_id, {"app.read"}, project=target["project_id"]
    )
    return target, artifact_id, attachment, bearer


def test_project_token_reads_selected_global_and_other_project_source_bytes_only(library):
    f, u = library, library.u
    target = _project(u.owner, u.oh)
    origin = _project(u.owner, u.oh)
    origin_artifact, origin_asset, origin_expected = _source_artifact(
        f,
        f.original.workspace_id,
        "private source from another project",
        project_id=origin["project_id"],
    )
    global_artifact = f.original.artifact_id
    assert global_artifact is not None
    global_expected = _artifact_bytes(f, global_artifact)
    _attach(u.owner, u.oh, target, f.original.id, role="primary")
    _attach(u.owner, u.oh, target, origin_asset, role="secondary")
    _share_viewer(u.owner, u.oh, target, u.alice_id)
    _token, bearer = scoped_token(
        f.case, u.alice_id, {"app.read"}, project=target["project_id"]
    )
    api = f.case.app.test_client()

    for artifact_id, expected in (
        (global_artifact, global_expected),
        (origin_artifact, origin_expected),
    ):
        response = api.get(f"/api/v1/artifacts/{artifact_id}/content", headers=bearer)
        assert response.status_code == 200 and response.data == expected
        assert api.get(f"/api/v1/artifacts/{artifact_id}/context", headers=bearer).status_code == 404

    listed = body(api.get("/api/v1/artifacts", headers=bearer))["items"]
    assert not {global_artifact, origin_artifact}.intersection(item["id"] for item in listed)
    assert api.get(
        "/api/v1/sources", headers=bearer, query_string={"workspace_id": f.original.workspace_id}
    ).status_code == 404

    unselected_artifact, unselected_asset, _expected = _source_artifact(
        f, f.original.workspace_id, "unselected private source"
    )
    assert (
        api.get(f"/api/v1/artifacts/{unselected_artifact}/content", headers=bearer).status_code
        == 404
    )
    _, write_bearer = scoped_token(
        f.case,
        u.alice_id,
        {"app.read", "app.write"},
        project=target["project_id"],
    )
    denied = api.post(
        f"/api/v1/sessions/{target['id']}/sources",
        json={"source_asset_id": unselected_asset, "role": "reference"},
        headers={**headers(write_bearer, str(uuid4())), "If-Match": "1"},
    )
    assert denied.status_code == 403


def test_ordinary_content_hit_skips_dependency_sql_and_miss_uses_combined_fallback(library):
    f, u = library, library.u
    artifact_id = f.original.artifact_id
    assert artifact_id is not None
    statements: list[str] = []

    def observe(_connection, _cursor, sql, _parameters, _context, _executemany):
        if "from artifacts" in sql.lower():
            statements.append(sql.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        ordinary = u.owner.get(f"/api/v1/artifacts/{artifact_id}/content")
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    assert ordinary.status_code == 200
    assert len(statements) == 1
    assert not any(
        name in statements[0]
        for name in ("source_assets", "session_sources", "project_sound_cues")
    )

    _target, selected_artifact, _attachment, bearer = _viewed_source(f)
    statements.clear()
    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        response = f.case.app.test_client().get(
            f"/api/v1/artifacts/{selected_artifact}/content", headers=bearer
        )
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    assert response.status_code == 200
    assert len(statements) == 2
    assert "source_assets" not in statements[0] and "session_sources" not in statements[0]
    assert all(
        name in statements[1]
        for name in ("source_assets", "session_sources", "project_sound_cues")
    )


def test_detached_historical_attachment_does_not_publish_source_bytes(library):
    f, u = library, library.u
    target, artifact_id, attachment, bearer = _viewed_source(f)
    api = f.case.app.test_client()
    path = f"/api/v1/artifacts/{artifact_id}/content"
    assert api.get(path, headers=bearer).status_code == 200

    with f.case.database.immediate_session() as session:
        session.get_one(m.SessionSource, attachment["id"]).is_current = False
    assert api.get(path, headers=bearer).status_code == 404
    assert body(api.get(f"/api/v1/sessions/{target['id']}/sources", headers=bearer))["items"]
    with f.case.database.immediate_session() as session:
        session.get_one(m.SessionSource, attachment["id"]).is_current = True
    assert api.get(path, headers=bearer).status_code == 200

    response = u.owner.delete(
        f"/api/v1/sessions/{target['id']}/sources/{attachment['id']}",
        headers={**headers(u.oh, str(uuid4())), "If-Match": "1"},
    )
    assert response.status_code == 204
    with f.case.database.snapshot_session() as session:
        assert session.get(m.SessionSource, attachment["id"]) is None
    assert api.get(path, headers=bearer).status_code == 404


@pytest.mark.parametrize("loss", ["project_revoked", "target_trashed"])
def test_revocation_or_target_trash_denies_before_managed_file_access(library, monkeypatch, loss):
    f, u = library, library.u
    target, artifact_id, _attachment, bearer = _viewed_source(f)
    api = f.case.app.test_client()
    path = f"/api/v1/artifacts/{artifact_id}/content"
    assert api.get(path, headers=bearer).status_code == 200

    with f.case.database.immediate_session() as session:
        if loss == "project_revoked":
            session.delete(session.get_one(ProjectMembership, (target["project_id"], u.alice_id)))
        else:
            session.get_one(m.SessionRecord, target["id"]).trashed_at = m.utcnow()

    def forbidden_managed_path(*_args, **_kwargs):
        pytest.fail("Denied source bytes reached managed-path resolution")

    with monkeypatch.context() as blocked:
        blocked.setattr(type(f.services.paths), "managed_path", forbidden_managed_path)
        assert api.get(path, headers=bearer).status_code == 404


def test_source_dependency_sql_requires_workspace_root_and_optional_target_intersections():
    actor = UserActor(
        user_id="source-dependency-user",
        auth_revision=1,
        subject="source-dependency-token",
        principal_kind="api_token",
        scopes=frozenset({"app.read"}),
        token_id="source-dependency-token",
    )
    statement = select(m.Artifact.id).where(
        current_source_dependency_clause(
            actor, project_id="target-project", session_id="target-session"
        )
    )
    sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))

    assert "source_assets.artifact_id = artifacts.id" in sql
    assert "source_assets.workspace_id = artifacts.workspace_id" in sql
    assert "source_assets.workspace_id = projects.workspace_id" in sql
    assert "projects.workspace_id = artifacts.workspace_id" in sql
    assert "projects.id = 'target-project'" in sql
    assert "sessions.id = 'target-session'" in sql
    assert "source_assets.state = 'current'" in sql
    assert "session_sources.is_current IS 1" in sql
    assert "sessions_1.id = projects.source_session_id" in sql
    assert "sessions_1.trashed_at IS NULL" in sql
    assert "artifacts.state != 'deleted'" in sql


def test_foreign_session_document_and_word_reads_are_denied_before_private_bodies(
    library,
):
    f, u = library, library.u
    private = _project(u.bob, u.bh)
    with f.case.database.immediate_session() as session:
        document = Document(session_id=private["id"], stage="transcription", language="en")
        session.add(document)
        session.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash="private revision body",
        )
        session.add(revision)
        session.flush()
        session.add(
            TimedWord(
                revision_id=revision.id,
                ordinal=4,
                text="private word body",
                start_ms=400,
                end_ms=490,
                metadata_json={"private": "body"},
            )
        )
        revision_id = revision.id

    statements: list[str] = []

    def observe(_connection, _cursor, sql, _parameters, _context, _executemany):
        statements.append(sql.lower())

    actor = _alice_actor(f)
    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with execution_actor_scope(actor), pytest.raises(KeyError):
            list_session_documents(f.case.database, private["id"])
        assert not any("from documents" in sql or "from artifacts" in sql for sql in statements)

        statements.clear()
        with execution_actor_scope(actor), pytest.raises(KeyError):
            list_revision_words(f.case.database, revision_id, cursor=0, limit=10)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    assert any("select documents.session_id" in sql for sql in statements)
    assert not any("from timed_words" in sql for sql in statements)
    assert not any("select document_revisions." in sql for sql in statements)


def test_authorized_document_and_sparse_word_projections_keep_nulls_and_metadata(library):
    f, u = library, library.u
    target = _project(u.owner, u.oh)
    _share_viewer(u.owner, u.oh, target, u.alice_id)
    with f.case.database.immediate_session() as session:
        document = Document(session_id=target["id"], stage="transcription", language="en")
        session.add(document)
        session.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash="authorized revision",
        )
        session.add(revision)
        session.flush()
        session.add_all(
            [
                TimedWord(
                    revision_id=revision.id,
                    ordinal=3,
                    text="first",
                    start_ms=300,
                    end_ms=390,
                    metadata_json={"tags": ["kept"]},
                ),
                TimedWord(
                    revision_id=revision.id,
                    ordinal=10,
                    text="second",
                    start_ms=1000,
                    end_ms=1090,
                    speaker="speaker-a",
                    confidence=0.75,
                    metadata_json={"tags": ["also-kept"]},
                ),
            ]
        )
        revision_id = revision.id
        document_id = document.id

    with execution_actor_scope(_alice_actor(f)):
        documents = list_session_documents(f.case.database, target["id"])
        first = list_revision_words(f.case.database, revision_id, cursor=0, limit=1)
        second = list_revision_words(f.case.database, revision_id, cursor=4, limit=1)

    assert [item["id"] for item in documents["items"]] == [document_id]
    projected_revision = documents["items"][0]["revisions"][0]
    assert projected_revision["id"] == revision_id and projected_revision["artifact"] is None
    assert [item["ordinal"] for item in first["items"]] == [3]
    assert first["items"][0]["segment_id"] is None
    assert first["items"][0]["speaker"] is None
    assert first["items"][0]["confidence"] is None
    assert first["items"][0]["metadata_json"] == {"tags": ["kept"]}
    assert first["next_cursor"] == 4
    assert [item["ordinal"] for item in second["items"]] == [10]
    assert second["items"][0]["speaker"] == "speaker-a"
    assert second["items"][0]["confidence"] == 0.75
    assert second["next_cursor"] is None


def test_none_actor_document_and_word_reads_keep_the_existing_query_counts(library):
    f, u = library, library.u
    target = _project(u.owner, u.oh)
    with f.case.database.immediate_session() as session:
        document = Document(session_id=target["id"], stage="transcription", language="en")
        session.add(document)
        session.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash="none compatibility",
        )
        session.add(revision)
        session.flush()
        revision_id = revision.id

    statements: list[str] = []

    def observe(_connection, _cursor, sql, _parameters, _context, _executemany):
        if sql.lstrip().lower().startswith("select"):
            statements.append(sql.lower())

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with execution_actor_scope(None):
            list_session_documents(f.case.database, target["id"])
        document_selects = len(statements)
        statements.clear()
        with execution_actor_scope(None):
            list_revision_words(f.case.database, revision_id, cursor=0, limit=10)
        word_selects = len(statements)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)

    assert document_selects == 4
    assert word_selects == 2
