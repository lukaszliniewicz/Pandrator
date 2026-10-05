"""Native failure boundaries for HTTP mutations and their durable handoffs."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select

from pandrator.web.models import (
    AgentRun,
    AgentStep,
    ApiIdempotency,
    AppSetting,
    AppSettingHistory,
    Artifact,
    DocumentRevision,
    Job,
    JobEvent,
)
from tests import test_subtitle_passage_review as passage_review_tests


@pytest.fixture
def route_case():
    case = passage_review_tests.PassageReviewRouteTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _counts(case, models):
    with case.services.database.session() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in models)


@contextmanager
def _database_failure(database, event_name, listener):
    event.listen(database.engine, event_name, listener)
    try:
        yield
    finally:
        event.remove(database.engine, event_name, listener)


def _agent_source(case):
    path = case.session_dir / "source.txt"
    path.write_text("Source for cleaning.", encoding="utf-8")
    artifact = case.services.artifacts.register(
        path, kind="text", role="source", session_id=case.session.id
    )
    with case.services.database.session() as session:
        asset = case.services.source_library.ensure_for_artifact_in_session(session, artifact.id)
        asset_id = asset.id
    case.services.source_library.attach(case.session.id, asset_id)
    return artifact


def _failed_run(case, source):
    job = case.services.jobs.enqueue(
        "source.clean",
        {"session_id": case.session.id, "settings": {"agentic": True}},
        session_id=case.session.id,
        resource_keys=[f"session:{case.session.id}", "service:llm"],
    )
    with case.services.database.session() as session:
        session.get(Job, job.id).status = "failed"
        run = AgentRun(
            session_id=case.session.id,
            source_artifact_id=source.id,
            job_id=job.id,
            status="failed",
            source_content_hash=source.content_hash,
            settings_hash="settings-hash",
            settings_json={"agentic": True},
            error_message="Original failure",
            checkpoint_revision=1,
        )
        session.add(run)
        session.flush()
        session.add(
            AgentStep(
                agent_run_id=run.id,
                ordinal=0,
                unit_key="accepted:0",
                phase="transform",
                status="completed",
                output_json={"text": "Kept checkpoint"},
            )
        )
        return run.id, job.id


@pytest.mark.parametrize("command", ["create", "resume"])
@pytest.mark.parametrize("phase", ["enqueue", "link", "commit"])
def test_agent_handoff_failure_rolls_back_run_job_and_event(route_case, command, phase):
    case = route_case
    source = _agent_source(case)
    run_id, previous_job_id = _failed_run(case, source) if command == "resume" else (None, None)
    models = (AgentRun, AgentStep, Job, JobEvent)
    before = _counts(case, models)
    queue = case.services.jobs
    original_enqueue = queue.enqueue_in_session
    injected = []

    def enqueue_then_fail(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        injected.append("enqueue")
        raise RuntimeError("handoff refused")

    def link_failure(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE agent_runs SET") and "job_id=" in statement:
            injected.append("link")
            raise RuntimeError("handoff refused")

    def commit_failure(connection):
        linked = connection.scalar(
            select(AgentRun.job_id).where(
                AgentRun.session_id == case.session.id,
                AgentRun.job_id.is_not(None),
                AgentRun.job_id != previous_job_id
                if previous_job_id
                else AgentRun.job_id.is_not(None),
            )
        )
        if linked:
            injected.append("commit")
            raise RuntimeError("handoff refused")

    fault = (
        patch.object(queue, "enqueue_in_session", side_effect=enqueue_then_fail)
        if phase == "enqueue"
        else _database_failure(
            case.services.database,
            "before_cursor_execute" if phase == "link" else "commit",
            link_failure if phase == "link" else commit_failure,
        )
    )
    path = (
        f"/api/v1/agent-runs/{run_id}/resume"
        if run_id
        else f"/api/v1/sessions/{case.session.id}/agent-runs"
    )
    with fault, pytest.raises(RuntimeError, match="handoff refused"):
        case.client.post(
            path, headers=case.headers, json={"source_artifact_id": source.id, "settings": {}}
        )
    assert injected == [phase]
    assert _counts(case, models) == before
    if run_id:
        with case.services.database.session() as session:
            run = session.get(AgentRun, run_id)
            assert (run.status, run.job_id, run.error_message, run.checkpoint_revision) == (
                "failed",
                previous_job_id,
                "Original failure",
                1,
            )
    # The same command is retryable after a refused handoff, not stranded.
    retry = case.client.post(
        path, headers=case.headers, json={"source_artifact_id": source.id, "settings": {}}
    )
    assert retry.status_code == 202, retry.get_json()
    with case.services.database.session() as session:
        result = retry.get_json()
        run = session.get(AgentRun, result["id"])
        job = session.get(Job, result["job_id"])
        assert run.job_id == job.id and job.payload_json["agent_run_id"] == run.id
        assert run.source_content_hash == source.content_hash
    if run_id:
        refused = case.client.post(path, headers=case.headers)
        assert refused.status_code == 409
        case.services.jobs.reconcile()
        assert case.client.post(path, headers=case.headers).status_code == 409


def _subtitle_body(case, canonical):
    body = {
        "source_artifact_id": case.source_id,
        "expected_source_hash": case.source_hash,
        "expected_revision": case.source_revision,
    }
    if canonical:
        body["expected_composition_hash"] = case.column["composition_hash"]
        body["passages"] = [
            {
                "id": "route-passage",
                "text": "Changed canonical passage.",
                "speaker": "NARRATOR",
                "start_ms": 0,
                "end_ms": 2000,
            }
        ]
    else:
        body["segments"] = [
            {
                "text": "Changed ordinary review.",
                "speaker": "NARRATOR",
                "start_ms": 0,
                "end_ms": 2000,
            }
        ]
    return body


@pytest.mark.parametrize("command", ["create", "resume"])
def test_agent_job_and_run_remain_invisible_until_handoff_commits(route_case, command):
    case = route_case
    source = _agent_source(case)
    run_id, previous_job_id = _failed_run(case, source) if command == "resume" else (None, None)
    before = _counts(case, (AgentRun, Job, JobEvent))
    original_enqueue = case.services.jobs.enqueue_in_session
    observations = []

    def observe_pending_handoff(*args, **kwargs):
        job = original_enqueue(*args, **kwargs)
        # A second native connection sees committed state, as a queue worker
        # would. Neither the claim nor the job may escape this transaction.
        observations.append(_counts(case, (AgentRun, Job, JobEvent)))
        with case.services.database.session() as session:
            assert session.get(Job, job.id) is None
            if run_id:
                run = session.get(AgentRun, run_id)
                assert (run.status, run.job_id) == ("failed", previous_job_id)
        return job

    path = (
        f"/api/v1/agent-runs/{run_id}/resume"
        if run_id
        else f"/api/v1/sessions/{case.session.id}/agent-runs"
    )
    with patch.object(
        case.services.jobs, "enqueue_in_session", side_effect=observe_pending_handoff
    ):
        response = case.client.post(
            path, headers=case.headers, json={"source_artifact_id": source.id, "settings": {}}
        )
    assert response.status_code == 202, response.get_json()
    assert observations == [before]


@pytest.mark.parametrize("phase", ["complete", "commit"])
def test_canonical_http_failure_cleans_publication_and_retry_identity(route_case, phase):
    case = route_case
    models = (Artifact, DocumentRevision, ApiIdempotency)
    before = _counts(case, models)
    destination = case.session_dir / "reviewed_correction_r2.srt"
    injected = []

    def commit_failure(connection):
        if connection.scalar(
            select(func.count())
            .select_from(DocumentRevision)
            .where(DocumentRevision.revision_number == 2)
        ):
            injected.append("commit")
            raise RuntimeError("subtitle completion refused")

    fault = (
        patch.object(
            case.services.idempotency,
            "complete",
            side_effect=RuntimeError("subtitle completion refused"),
        )
        if phase == "complete"
        else _database_failure(case.services.database, "commit", commit_failure)
    )
    path = f"/api/v1/sessions/{case.session.id}/subtitles/correction/passage-review"
    body = _subtitle_body(case, True)
    headers = {**case.headers, "Idempotency-Key": "canonical-rollback"}
    with fault:
        failed = case.client.post(path, json=body, headers=headers)
    assert failed.status_code == 409, failed.get_json()
    if phase == "commit":
        assert injected == ["commit"]
    assert not destination.exists()
    assert _counts(case, models) == before
    saved = case.client.post(path, json=body, headers=headers)
    replay = case.client.post(path, json=body, headers=headers)
    assert saved.status_code == replay.status_code == 201
    assert saved.get_json() == replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("keyed", [False, True])
def test_subtitle_cleanup_failure_preserves_commit_error(route_case, canonical, keyed):
    case = route_case
    destination = case.session_dir / "reviewed_correction_r2.srt"
    before = _counts(case, (Artifact, DocumentRevision, ApiIdempotency))
    original_unlink = Path.unlink
    injected = []

    def deny_owned_unlink(path, *args, **kwargs):
        if path == destination:
            injected.append("unlink")
            raise OSError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    def commit_failure(connection):
        if connection.scalar(
            select(func.count())
            .select_from(DocumentRevision)
            .where(DocumentRevision.revision_number == 2)
        ):
            raise RuntimeError("primary commit error")

    suffix = "passage-review" if canonical else "review"
    path = f"/api/v1/sessions/{case.session.id}/subtitles/correction/{suffix}"
    headers = {**case.headers, **({"Idempotency-Key": "cleanup-primary"} if keyed else {})}
    with (
        _database_failure(case.services.database, "commit", commit_failure),
        patch.object(Path, "unlink", deny_owned_unlink),
    ):
        response = case.client.post(path, json=_subtitle_body(case, canonical), headers=headers)
    assert response.status_code == 409 and "primary commit error" in str(response.get_json())
    assert injected == ["unlink"]
    assert _counts(case, (Artifact, DocumentRevision, ApiIdempotency)) == before


def test_global_stt_migration_and_history_roll_back_with_replacement(route_case):
    case = route_case
    with case.services.database.session() as session:
        session.add(
            AppSetting(
                key="defaults.stt",
                revision=1,
                value_json={"stt_language": "en", "subtitle_max_chars_per_line": 39},
            )
        )
    before = _counts(case, (AppSetting, AppSettingHistory))
    injected = []

    def commit_failure(connection):
        if (
            connection.scalar(select(AppSetting.revision).where(AppSetting.key == "defaults.stt"))
            == 2
        ):
            injected.append("commit")
            raise RuntimeError("settings commit refused")

    body = {"value": {"stt_language": "pl", "subtitle_max_chars_per_line": 38}}
    headers = {**case.headers, "If-Match": '"1"'}
    with _database_failure(case.services.database, "commit", commit_failure):
        failed = case.client.put("/api/v1/settings/defaults.stt", json=body, headers=headers)
    assert (
        failed.status_code == 422 and failed.get_json()["error"]["code"] == "credential_unavailable"
    )
    assert injected == ["commit"]
    assert _counts(case, (AppSetting, AppSettingHistory)) == before
    with case.services.database.session() as session:
        record = session.get(AppSetting, "defaults.stt")
        assert record.revision == 1 and record.value_json == {
            "stt_language": "en",
            "subtitle_max_chars_per_line": 39,
        }
        assert session.get(AppSetting, "defaults.subtitles") is None
    saved = case.client.put("/api/v1/settings/defaults.stt", json=body, headers=headers)
    assert saved.status_code == 200 and saved.headers["ETag"] == '"2"'


@pytest.mark.parametrize("canonical", [False, True])
def test_direct_subtitle_service_preserves_commit_error_when_cleanup_fails(route_case, canonical):
    case = route_case
    destination = case.session_dir / "reviewed_correction_r2.srt"
    original_unlink = Path.unlink
    before = _counts(case, (Artifact, DocumentRevision))
    denied = []

    def deny_owned_unlink(path, *args, **kwargs):
        if path == destination:
            denied.append(path)
            raise OSError("cleanup denied")
        return original_unlink(path, *args, **kwargs)

    def commit_failure(connection):
        if connection.scalar(
            select(func.count())
            .select_from(DocumentRevision)
            .where(DocumentRevision.revision_number == 2)
        ):
            raise RuntimeError("primary direct commit error")

    body = _subtitle_body(case, canonical)
    values = body.pop("passages" if canonical else "segments")
    revision = body.pop("expected_revision")
    command = (
        case.services.subtitle_review.save_passage_review
        if canonical
        else case.services.subtitle_review.save_review
    )
    with (
        _database_failure(case.services.database, "commit", commit_failure),
        patch.object(Path, "unlink", deny_owned_unlink),
    ):
        with pytest.raises(RuntimeError, match="primary direct commit error"):
            command(case.session.id, "correction", revision, values, **body)
    assert denied == [destination]
    assert _counts(case, (Artifact, DocumentRevision)) == before
