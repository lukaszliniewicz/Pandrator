"""Fresh project access at subtitle and passive dispatch read boundaries."""

from __future__ import annotations

from sqlalchemy import delete, event, select

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.accounts.resource_grant_models import ApiTokenProjectGrant
from pandrator.web.dispatch_preview import get_dispatch_preview
from pandrator.web.models import ApiToken, DispatchBatch, DispatchRun, utcnow
from pandrator.web.projects.models import ProjectMembership
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential

READ = frozenset({"app.read"})


def _subtitle_artifact(case, session_id: str, marker: str) -> str:
    record = case.services.sessions.get(session_id)
    directory = case.services.paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{marker}.srt"
    path.write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{marker} private subtitle.\n",
        encoding="utf-8",
    )
    artifact = case.services.artifacts.register(
        path,
        kind="srt",
        role="transcription",
        session_id=session_id,
    )
    case.services.workflow_handlers._store_srt_document(
        session_id, artifact, "transcription", language="en"
    )
    return artifact.id


def _dispatch_run(case, session_id: str, artifact_id: str, marker: str) -> str:
    with case.database.immediate_session() as session:
        payload = case.services.dispatch.create_in_session(
            session,
            session_id=session_id,
            kind="correction",
            source_artifact_id=artifact_id,
            source_language="en",
            target_language=None,
            instructions="",
            char_limit=6000,
            max_segments_per_batch=40,
            no_remove_subtitles=False,
            context_before=8,
            context_after=2,
            timing_context_mode="none",
            substantial_gap_ms=2000,
            glossary={},
        )
        run_id = payload["id"]

    with case.database.immediate_session() as session:
        run = session.get(DispatchRun, run_id)
        assert run is not None
        batch = session.scalar(select(DispatchBatch).where(DispatchBatch.dispatch_run_id == run_id))
        assert batch is not None
        batch.status = "completed"
        batch.normalized_output_json = [
            {"text": f"{marker} private dispatch output.", "source_cue_ids": [1]}
        ]
        batch.accepted_at = utcnow()
        run.status = "completed"
        run.completed_batch_count = 1
    return run_id


def _read_urls(session_id: str, artifact_id: str, run_id: str) -> dict[str, str]:
    return {
        "subtitle documents": f"/api/v1/sessions/{session_id}/subtitles",
        "subtitle catalog": f"/api/v1/sessions/{session_id}/subtitles/catalog",
        "subtitle review": (
            f"/api/v1/sessions/{session_id}/subtitles/review?artifact_id={artifact_id}"
        ),
        "dispatch list": f"/api/v1/sessions/{session_id}/dispatch-runs",
        "dispatch detail": f"/api/v1/dispatch-runs/{run_id}",
        "dispatch preview": f"/api/v1/dispatch-runs/{run_id}/preview",
    }


def _responses(actor, urls: dict[str, str]):
    return {name: actor.client.get(url, headers=actor.headers) for name, url in urls.items()}


def _project_limited_owner_credential(case):
    actor = credential(case, "owner", scopes=READ, grant_team=False)
    with case.database.immediate_session() as session:
        session.add(
            ApiTokenProjectGrant(
                token_id=actor.token.id,
                project_id=case.a.project_id,
            )
        )
    return actor


def _fixture_resources(case):
    artifact_a = _subtitle_artifact(case, case.a.id, "read-a")
    artifact_b = _subtitle_artifact(case, case.b.id, "read-b")
    run_a = _dispatch_run(case, case.a.id, artifact_a, "read-a")
    run_b = _dispatch_run(case, case.b.id, artifact_b, "read-b")
    return (
        artifact_a,
        artifact_b,
        run_a,
        run_b,
        _read_urls(case.a.id, artifact_a, run_a),
        _read_urls(case.b.id, artifact_b, run_b),
    )


def test_six_http_read_routes_require_current_project_and_credential_access(case):
    (
        artifact_a,
        _artifact_b,
        _run_a,
        _run_b,
        urls_a,
        urls_b,
    ) = _fixture_resources(case)
    owner = credential(case, "owner", scopes=READ)
    member = credential(case, "member", scopes=READ)
    project_viewer = credential(case, "viewer", scopes=READ)

    # Workspace owner/member and explicit project viewer retain legitimate READ.
    for actor in (owner, member, project_viewer):
        responses = _responses(actor, urls_a)
        assert {name: response.status_code for name, response in responses.items()} == {
            name: 200 for name in urls_a
        }
        assert artifact_a in responses["subtitle catalog"].get_data(as_text=True)

    # A member scoped to project A cannot read project B through known IDs.
    project_member = credential(case, "editor", scopes=READ)
    # An app.read-only outsider and an instance-admin credential have no project
    # content grant; app.admin does not replace project READ authority.
    outsider = credential(case, "outsider", scopes=READ)
    admin = credential(case, "outsider", scopes=READ | {"app.admin"})
    project_limited = _project_limited_owner_credential(case)

    for actor, urls in (
        (project_member, urls_b),
        (outsider, urls_a),
        (admin, urls_a),
        (project_limited, urls_b),
    ):
        responses = _responses(actor, urls)
        assert {name: response.status_code for name, response in responses.items()} == {
            name: 404 for name in urls
        }, {
            name: (response.status_code, response.get_json())
            for name, response in responses.items()
        }


def test_subtitle_review_denies_before_loading_artifacts_or_segments(case):
    (_artifact_a, artifact_b, _run_a, _run_b, _urls_a, _urls_b) = _fixture_resources(case)
    project_member = credential(case, "editor", scopes=READ)
    protected_reads = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.casefold().split())
        if any(
            marker in normalized
            for marker in ("from artifacts", "from document_revisions", "from segments")
        ):
            protected_reads.append(normalized)

    event.listen(case.database.engine, "before_cursor_execute", record_statement)
    try:
        with execution_actor_scope(project_member.actor):
            try:
                case.services.subtitle_review.review(case.b.id, [artifact_b])
            except SessionNotFound:
                pass
            else:
                raise AssertionError("review exposed a session outside the project grant")
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record_statement)

    assert protected_reads == []


def test_dispatch_reader_rechecks_access_with_caller_owned_session(case):
    (_artifact_a, _artifact_b, run_a, run_b, _urls_a, _urls_b) = _fixture_resources(case)
    project_limited = _project_limited_owner_credential(case)
    dispatch = case.services.dispatch

    with execution_actor_scope(project_limited.actor):
        with case.database.snapshot_session() as session:
            assert any(
                row["id"] == run_a for row in dispatch.list_runs(case.a.id, db_session=session)
            )
            assert dispatch.get(run_a, db_session=session)["id"] == run_a
            try:
                dispatch.list_runs(case.b.id, db_session=session)
            except SessionNotFound:
                pass
            else:
                raise AssertionError("list_runs exposed a project outside the credential ceiling")
            try:
                dispatch.get(run_b, db_session=session)
            except SessionNotFound:
                pass
            else:
                raise AssertionError("get exposed a project outside the credential ceiling")


def test_dispatch_preview_denies_before_loading_batch_payload(case):
    (_artifact_a, _artifact_b, _run_a, run_b, _urls_a, _urls_b) = _fixture_resources(case)
    project_member = credential(case, "editor", scopes=READ)
    batch_reads = []

    def record_batch_read(_connection, _cursor, statement, _parameters, _context, _many):
        if "from dispatch_batches" in " ".join(statement.casefold().split()):
            batch_reads.append(statement)

    event.listen(case.database.engine, "before_cursor_execute", record_batch_read)
    try:
        with execution_actor_scope(project_member.actor):
            try:
                get_dispatch_preview(case.services, run_b)
            except SessionNotFound:
                pass
            else:
                raise AssertionError("preview exposed a run outside the project grant")
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record_batch_read)

    assert batch_reads == []


def test_http_reads_see_revoked_project_view_and_credential_grant(case):
    (_artifact_a, _artifact_b, run_a, _run_b, urls_a, _urls_b) = _fixture_resources(case)
    project_viewer = credential(case, "viewer", scopes=READ)
    limited_owner = _project_limited_owner_credential(case)
    assert (
        project_viewer.client.get(
            urls_a["subtitle catalog"], headers=project_viewer.headers
        ).status_code
        == 200
    )
    assert (
        limited_owner.client.get(
            urls_a["dispatch detail"], headers=limited_owner.headers
        ).status_code
        == 200
    )

    with case.database.immediate_session() as session:
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == case.a.project_id,
                ProjectMembership.user_id == case.users["viewer"],
            )
        )
        session.execute(
            delete(ApiTokenProjectGrant).where(
                ApiTokenProjectGrant.token_id == limited_owner.token.id,
                ApiTokenProjectGrant.project_id == case.a.project_id,
            )
        )

    assert (
        project_viewer.client.get(
            urls_a["subtitle review"], headers=project_viewer.headers
        ).status_code
        == 404
    )
    assert (
        limited_owner.client.get(
            urls_a["dispatch preview"], headers=limited_owner.headers
        ).status_code
        == 404
    )

    # A caller-supplied actor is still checked against the current credential
    # row in the reader's existing transaction.
    with case.database.immediate_session() as session:
        token = session.get(ApiToken, limited_owner.token.id)
        assert token is not None
        token.revoked_at = utcnow()
    with execution_actor_scope(limited_owner.actor):
        with case.database.snapshot_session() as session:
            try:
                case.services.dispatch.get(run_a, db_session=session)
            except AccessDenied:
                pass
            else:
                raise AssertionError("get accepted a revoked credential actor")
