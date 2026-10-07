"""Compact language navigation stays bounded and preserves pinned identities."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.translation_project_routes import translation_project_paths
from pandrator.web.translation_project_summary import get_compact_project
from pandrator.web.translation_projects import get_project
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def summary_case(tmp_path):
    prepare_web_test_data_root(tmp_path)
    tokens = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    client = app.test_client()
    client.post("/api/v1/auth/bootstrap", json={"token": tokens.issue()})
    services = app.extensions["pandrator"]["services"]
    source = services.sessions.create(
        "Compact source",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "export"],
    )
    with services.database.session() as db:
        checkpoint = m.Artifact(
            session_id=source.id,
            kind="srt",
            role="correction",
            content_hash="source-hash",
            relative_path="source-correction.srt",
            metadata_json={"language": "en"},
        )
        db.add(checkpoint)
        db.flush()
        project = m.TranslationProject(
            name="Compact project",
            source_session_id=source.id,
            checkpoint_artifact_id=checkpoint.id,
            source_content_hash=checkpoint.content_hash,
            source_language="en",
        )
        db.add(project)
        db.flush()
        project_id, checkpoint_id = project.id, checkpoint.id
    yield services, client, source.id, project_id, checkpoint_id
    services.database.dispose()


def add_branches(case, count, *, start=0):
    services, _, _, project_id, _ = case
    result = []
    with services.database.session() as db:
        for index in range(start, start + count):
            record = m.SessionRecord(
                name=f"Language {index}",
                workflow_kind="voiceover",
                source_language="en",
                target_language=f"xx-{index}",
            )
            db.add(record)
            db.flush()
            checkpoint = m.Artifact(
                session_id=record.id,
                kind="srt",
                role="correction",
                content_hash="source-hash",
                relative_path=f"{record.id}/correction.srt",
                metadata_json={"language": "en"},
            )
            db.add(checkpoint)
            db.flush()
            branch = m.TranslationProjectBranch(
                project_id=project_id,
                session_id=record.id,
                target_language=record.target_language,
                source_checkpoint_artifact_id=checkpoint.id,
                source_content_hash="source-hash",
            )
            db.add(branch)
            db.flush()
            result.append(
                {"id": branch.id, "session_id": record.id, "checkpoint_id": checkpoint.id}
            )
    return result


def test_complete_branch_summary_matches_full_basic_projection(summary_case):
    services, client, _, project_id, _ = summary_case
    branches = add_branches(summary_case, 5)
    with services.database.session() as db:
        for index, state in ((1, "current"), (2, "stale")):
            db.add(
                m.Artifact(
                    session_id=branches[index]["session_id"],
                    role="translation",
                    kind="srt",
                    relative_path=f"translation-{index}.srt",
                    state=state,
                )
            )
        db.add(
            m.Job(kind="dubbing.translate", session_id=branches[3]["session_id"], status="queued")
        )
        doc = m.Document(session_id=branches[4]["session_id"], stage="correction")
        db.add(doc)
        db.flush()
        revision = m.DocumentRevision(
            document_id=doc.id, revision_number=1, content_hash="source-hash"
        )
        db.add(revision)
        db.flush()
        db.add(
            m.DispatchRun(
                session_id=branches[4]["session_id"],
                kind="translation",
                output_role="translation",
                source_artifact_id=branches[4]["checkpoint_id"],
                source_revision_id=revision.id,
                source_content_hash="source-hash",
                input_hash="input-hash",
                status="running",
            )
        )
    response = client.get(f"/api/v1/translation-projects/{project_id}?view=compact")
    assert response.status_code == 200, response.get_json()
    project = response.get_json()["project"]
    with services.database.session() as db:
        full = get_project(db, project_id)["project"]
    assert project["branches"] == full["branches"]
    assert {row["translation_status"] for row in project["branches"]} == {
        "ready",
        "completed",
        "stale",
        "running",
    }
    assert project["checkpoint_role"] == "correction"
    assert project["source_status"]["validation_scope"] == "metadata"
    assert not project["source_status"]["source_changed"]


def test_source_and_branch_session_reads_return_same_project_without_enrichment(summary_case):
    services, client, source_id, project_id, checkpoint_id = summary_case
    branches = add_branches(summary_case, 2)
    with services.database.session() as db:
        db.add(
            m.SessionSetting(
                session_id=source_id,
                section="multilingual_setup",
                value_json={"target_languages": ["xx-0", "xx-1"]},
            )
        )
    with (
        patch(
            "pandrator.web.translation_project_routes.enrich_project_payload",
            side_effect=AssertionError,
        ),
        patch("pandrator.web.translation_projects._branch_payload", side_effect=AssertionError),
        patch("pandrator.web.translation_projects.sha256_file", side_effect=AssertionError),
    ):
        source = client.get(f"/api/v1/sessions/{source_id}/translation-project?view=compact")
        branch = client.get(
            f"/api/v1/sessions/{branches[0]['session_id']}/translation-project?view=compact"
        )
        direct = client.get(f"/api/v1/translation-projects/{project_id}?view=compact")
    assert source.status_code == branch.status_code == direct.status_code == 200
    assert (
        source.get_json()["project"] == branch.get_json()["project"] == direct.get_json()["project"]
    )
    assert source.get_json()["correction_checkpoint_artifact_id"] == checkpoint_id
    assert source.get_json()["source_checkpoint_artifact_id"] == checkpoint_id
    assert branch.get_json()["correction_checkpoint_artifact_id"] is None
    assert source.get_json()["setup_state"] == "active"
    assert branch.get_json()["setup_state"] == "none"
    assert set(source.get_json()) == {
        "project",
        "setup",
        "setup_state",
        "setup_blocked_reason",
        "correction_checkpoint_artifact_id",
        "source_checkpoint_artifact_id",
    }


@pytest.mark.parametrize("suffix", ["", "?view=full"])
def test_default_and_explicit_full_keep_enrichment(summary_case, suffix):
    _, client, source_id, project_id, _ = summary_case
    with patch(
        "pandrator.web.translation_project_routes.enrich_project_payload",
        side_effect=lambda _services, payload: {**payload, "full_marker": True},
    ) as enrich:
        direct = client.get(f"/api/v1/translation-projects/{project_id}{suffix}")
        session = client.get(f"/api/v1/sessions/{source_id}/translation-project{suffix}")
    assert direct.status_code == session.status_code == 200
    assert direct.get_json()["full_marker"] and session.get_json()["full_marker"]
    assert enrich.call_count == 2


@pytest.mark.parametrize("view", ["bogus", "", "Compact"])
def test_invalid_view_is_rejected(summary_case, view):
    _, client, source_id, project_id, _ = summary_case
    assert client.get(f"/api/v1/translation-projects/{project_id}?view={view}").status_code == 422
    assert (
        client.get(f"/api/v1/sessions/{source_id}/translation-project?view={view}").status_code
        == 422
    )


def _count_reads(services, project_id):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(services.database.engine, "before_cursor_execute", record)
    try:
        with services.database.session() as db:
            get_compact_project(db, project_id)
    finally:
        event.remove(services.database.engine, "before_cursor_execute", record)
    return len(statements)


def test_query_count_is_constant_for_one_and_twenty_branches(summary_case):
    services, _, _, project_id, _ = summary_case
    add_branches(summary_case, 1)
    one = _count_reads(services, project_id)
    add_branches(summary_case, 19, start=1)
    twenty = _count_reads(services, project_id)
    assert one == twenty
    assert one <= 12


def test_historical_payloads_and_generation_runs_are_not_hydrated(summary_case):
    services, client, _, project_id, _ = summary_case
    branch = add_branches(summary_case, 1)[0]
    large = {"history_payload": "x" * 10_000}
    with services.database.session() as db:
        plan = m.GenerationPlan(session_id=branch["session_id"])
        db.add(plan)
        db.flush()
        revision = m.GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="plan-hash", settings_json=large
        )
        db.add(revision)
        db.flush()
        for index in range(40):
            db.add(
                m.Job(
                    session_id=branch["session_id"],
                    kind="dubbing.translate",
                    status="completed",
                    payload_json=large,
                )
            )
            db.add(
                m.Artifact(
                    session_id=branch["session_id"],
                    kind="srt",
                    role="translation",
                    state="stale",
                    relative_path=f"history-{index}.srt",
                    metadata_json=large,
                )
            )
            db.add(
                m.GenerationRun(
                    session_id=branch["session_id"],
                    plan_revision_id=revision.id,
                    sequence_number=index + 1,
                    status="completed",
                )
            )
    hydrated = []

    def record(_session, instance):
        hydrated.append(type(instance))

    event.listen(Session, "loaded_as_persistent", record)
    try:
        response = client.get(f"/api/v1/translation-projects/{project_id}?view=compact")
    finally:
        event.remove(Session, "loaded_as_persistent", record)
    assert response.status_code == 200, response.get_json()
    assert not set(hydrated) & {
        m.Artifact,
        m.Job,
        m.GenerationRun,
        m.DispatchRun,
        m.DocumentRevision,
        m.GenerationPlan,
        m.GenerationPlanRevision,
    }
    assert "history_payload" not in response.get_data(as_text=True)
    assert response.get_json()["project"]["branches"][0]["translation_status"] == "stale"


def test_source_status_detects_new_selection_without_file_hashing(summary_case):
    services, client, source_id, project_id, _ = summary_case
    with services.database.session() as db:
        newer = m.Artifact(
            session_id=source_id,
            kind="srt",
            role="correction",
            state="current",
            content_hash="new-hash",
            relative_path="new-source.srt",
        )
        db.add(newer)
        db.flush()
        db.add(
            m.SessionStageSelection(session_id=source_id, stage_key="correct", artifact_id=newer.id)
        )
        newer_id = newer.id
    payload = client.get(f"/api/v1/translation-projects/{project_id}?view=compact").get_json()[
        "project"
    ]
    assert payload["source_status"]["source_changed"]
    assert payload["source_status"]["current_source"]["artifact_id"] == newer_id


def test_new_correction_changes_transcription_pinned_source(summary_case):
    services, client, source_id, project_id, checkpoint_id = summary_case
    with services.database.session() as db:
        db.get(m.Artifact, checkpoint_id).role = "transcription"
    baseline = client.get(f"/api/v1/translation-projects/{project_id}?view=compact").get_json()[
        "project"
    ]
    assert baseline["checkpoint_role"] == "transcription"
    assert not baseline["source_status"]["source_changed"]
    with services.database.session() as db:
        correction = m.Artifact(
            session_id=source_id,
            kind="srt",
            role="correction",
            content_hash="corrected-hash",
            relative_path="corrected-source.srt",
        )
        db.add(correction)
        db.flush()
        correction_id = correction.id
    changed = client.get(f"/api/v1/translation-projects/{project_id}?view=compact").get_json()[
        "project"
    ]
    assert changed["source_status"]["source_changed"]
    assert changed["source_status"]["current_checkpoint"]["artifact_id"] == correction_id
    assert changed["source_status"]["current_checkpoint"]["role"] == "correction"


def test_compact_enforces_branch_count_cap(summary_case):
    _, client, _, project_id, _ = summary_case
    add_branches(summary_case, 101)
    assert client.get(f"/api/v1/translation-projects/{project_id}?view=compact").status_code == 409


def test_openapi_documents_compact_get_views_only():
    paths = translation_project_paths()
    for path in (
        "/api/v1/sessions/{sessionId}/translation-project",
        "/api/v1/translation-projects/{projectId}",
    ):
        assert paths[path]["get"]["parameters"][0]["schema"] == {
            "type": "string",
            "enum": ["full", "compact"],
            "default": "full",
        }
    assert (
        paths["/api/v1/sessions/{sessionId}/translation-project"]["post"]["parameters"][0]["name"]
        == "Idempotency-Key"
    )
