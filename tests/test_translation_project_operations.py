"""Selected-language operations retain authority, guards and exact child receipts."""

from __future__ import annotations

import json
import sqlite3
import threading
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import ALL_SCOPES, BootstrapTokenStore, Principal
from pandrator.web.database import Database, sqlite_url
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.project_operation_routes import (
    PROJECT_OPERATION_SCHEMAS,
    project_operation_paths,
    register_project_operation_routes,
)
from pandrator.web.project_operations import (
    ProjectOperationError,
    TranslationProjectOperationService,
)
from pandrator.web.route_context import RouteContext
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_plan_workspace import plan_signature, review_speech_plan
from pandrator.web.translation_projects import create_branches_in_session, create_project_in_session
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def case(tmp_path):
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        public_origin="https://pandrator.example",
    )
    services = SimpleNamespace(**app.extensions["pandrator"])
    if not any(rule.rule.endswith("/operations/preview") for rule in app.url_map.iter_rules()):
        guards = ApiGuards(app, services, testing=True, script_policy="'self'")
        register_project_operation_routes(app, RouteContext(services, guards, Path("/tmp")))
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
    target = services.identity.snapshot(observed_origin="https://pandrator.example/").model_dump(
        mode="json"
    )
    principal = Principal(
        "owner", "owner_session", ALL_SCOPES, None, "loopback", target["instance_id"]
    )
    source = services.sessions.create(
        "EN",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "translate", "export"],
    )
    directory = services.paths.sessions / source.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "corrected.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    checkpoint = services.artifacts.register(
        path, kind="srt", role="correction", session_id=source.id, metadata={"language": "en"}
    )
    with services.database.immediate_session() as db:
        project = create_project_in_session(
            db, source.id, checkpoint.id, "Languages", source.revision, paths=services.paths
        )["project"]
    with services.database.immediate_session() as db:
        project = create_branches_in_session(
            db,
            project["id"],
            project["revision"],
            [{"target_language": lang} for lang in ("ja", "pl", "de")],
            session_forks=services.session_forks,
            paths=services.paths,
            created_directories=[],
        )["project"]
    operation_service = TranslationProjectOperationService(services)
    yield SimpleNamespace(
        app=app,
        client=client,
        headers={"X-CSRF-Token": csrf},
        services=services,
        database=services.database,
        target=target,
        principal=principal,
        project=project,
        branches=project["branches"],
        source=source,
        checkpoint=checkpoint,
        operations=operation_service,
        root=tmp_path,
    )
    services.database.dispose()


def preview(case, action="translate", branches=None, key="preview-operation-1", **kwargs):
    return case.operations.preview(
        case.project["id"],
        [branch["id"] for branch in (branches or case.branches)],
        expected_project_revision=case.project["revision"],
        action=action,
        principal=case.principal,
        target_identity=case.target,
        idempotency_key=key,
        **kwargs,
    )


def execute(case, operation, key="execute-operation-1", **kwargs):
    accepted = sorted(
        {
            value
            for child in operation["children"]
            for value in child.get("preview", {}).get("required_confirmations", [])
        }
    )
    return case.operations.execute(
        operation["id"],
        operation["preview_digest"],
        accepted,
        principal=case.principal,
        target_identity=case.target,
        idempotency_key=key,
        **kwargs,
    )


def jobs(case):
    with case.database.session() as db:
        return list(db.scalars(select(m.Job).order_by(m.Job.created_at)))


def set_settings(case, sid, section, values):
    service = case.services.workspace_settings
    current = service.get(sid, section)
    return service.patch(sid, section, current["revision"], values)


def artifact(case, branch, *, role="translation", parent_id=None, settings=None):
    with case.database.session() as db:
        session_record = db.get(m.SessionRecord, branch["session_id"])
    directory = case.services.paths.sessions / session_record.storage_key
    path = case.services.artifacts.next_available_path(directory / f"{role}.srt")
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTranslated.\n", encoding="utf-8")
    return case.services.artifacts.register(
        path,
        kind="srt",
        role=role,
        session_id=branch["session_id"],
        parent_ids=[parent_id or branch["source_checkpoint_artifact_id"]],
        settings=settings,
        metadata={
            "language": branch["target_language"],
            "source_artifact_id": parent_id or branch["source_checkpoint_artifact_id"],
        },
    )


def finish_job(case, job_id, result):
    # Exercise JobQueue.complete's actual succeeded status/result projection.
    with case.database.immediate_session() as db:
        job = db.get(m.Job, job_id)
        job.status = "running"
        job.lease_owner = "fixture-worker"
        job.lease_generation = 1
        job.lease_expires_at = m.utcnow() + timedelta(minutes=1)
    case.services.jobs.complete(job_id, "fixture-worker", result, lease_generation=1)


def reviewed_plan(case, branch, *, reviewed=True):
    with case.database.session() as db:
        session_record = db.get(m.SessionRecord, branch["session_id"])
        session_record.workflow_kind = "voiceover"
        session_record.included_stages_json = ["translate", "generate_audio", "export"]
        outcome = db.get(m.OutcomePlan, branch["session_id"])
        value = deepcopy(outcome.value_json)
        value["transformations"]["generate_audio"] = True
        value["deliverables"]["voiceover"] = True
        outcome.value_json = value
    translated = artifact(case, branch)
    set_settings(
        case,
        branch["session_id"],
        "tts",
        {
            "service": "gemini",
            "model": "gemini-2.5-flash-tts",
            "voice": "Kore",
            "voice_mode_version": 1,
            "casting_enabled": False,
            "performance_enabled": False,
            "language": branch["target_language"],
        },
    )
    plan = case.services.generation.create_plan(
        branch["session_id"],
        source_revision_id=None,
        settings={
            "_source_artifact_id": translated.id,
            "_source_content_hash": translated.content_hash,
            "_prepared_for_review": True,
        },
        segments=[{"text": "Translated.", "language": branch["target_language"]}],
    )
    revision_id = plan["active_revision_id"]
    if reviewed:
        with case.database.immediate_session() as db:
            review_speech_plan(
                db,
                branch["session_id"],
                revision_id=revision_id,
                content_signature=plan_signature(db, revision_id),
            )
    return revision_id, translated


def test_preview_skips_blocked_language_and_replays_without_jobs(case):
    set_settings(case, case.branches[1]["session_id"], "translation", {"target_language": "fr"})
    operation = preview(case)
    assert operation["status"] == "preview"
    assert operation["eligible_count"] == operation["child_job_count"] == 2
    assert operation["skipped_count"] == 1
    assert [child["target_language"] for child in operation["children"]] == ["ja", "pl", "de"]
    assert jobs(case) == []
    repeated = preview(case)
    assert repeated["id"] == operation["id"]
    with case.database.session() as db:
        assert db.scalar(select(func.count()).select_from(m.TranslationProjectOperation)) == 1
        assert db.scalar(select(func.count()).select_from(m.WorkflowExecutionPlan)) == 2
    started = execute(case, operation)
    assert started["status"] == "running"
    assert len(jobs(case)) == 2
    replayed = execute(case, operation)
    assert [child.get("job_id") for child in started["children"]] == [
        child.get("job_id") for child in replayed["children"]
    ]
    assert len(jobs(case)) == 2


def test_changed_project_source_does_not_move_pinned_branch_inputs(case):
    operation = preview(case)
    path = case.services.paths.managed_path(case.checkpoint.relative_path)
    path.write_text("Changed project source", encoding="utf-8")
    with case.database.session() as db:
        db.get(m.Artifact, case.checkpoint.id).state = "historical"
        db.get(m.SessionRecord, case.source.id).revision += 1
    result = execute(case, operation)
    assert all(child["state"] == "queued" for child in result["children"])
    assert {job.payload_json["source_artifact_id"] for job in jobs(case)} == {
        branch["source_checkpoint_artifact_id"] for branch in case.branches
    }


@pytest.mark.parametrize("change", ["file", "settings", "checkpoint", "trashed"])
def test_child_guard_changes_block_only_that_child(case, change):
    operation = preview(case)
    branch = case.branches[0]
    with case.database.session() as db:
        row = db.get(m.TranslationProjectBranch, branch["id"])
        if change == "checkpoint":
            row.source_content_hash = "changed"
        if change == "trashed":
            db.get(m.SessionRecord, branch["session_id"]).trashed_at = m.utcnow()
        if change == "file":
            checkpoint = db.get(m.Artifact, row.source_checkpoint_artifact_id)
            case.services.paths.managed_path(checkpoint.relative_path).write_text(
                "changed", encoding="utf-8"
            )
    if change == "settings":
        set_settings(case, branch["session_id"], "translation", {"target_language": "fr"})
    result = execute(case, operation)
    assert result["children"][0]["state"] == "blocked"
    assert len(jobs(case)) == 2


@pytest.mark.parametrize(
    "change", ["project", "expiry", "target", "digest", "integrity", "foreign"]
)
def test_operation_guards_enqueue_nothing(case, change):
    operation = preview(case)
    principal, target, digest = case.principal, case.target, operation["preview_digest"]
    with case.database.session() as db:
        row = db.get(m.TranslationProjectOperation, operation["id"])
        if change == "project":
            db.get(m.TranslationProject, case.project["id"]).revision += 1
        elif change == "expiry":
            row.expires_at = m.utcnow() - timedelta(seconds=1)
        elif change == "integrity":
            value = deepcopy(row.preview_json)
            value["project_revision"] += 1
            row.preview_json = value
    if change == "target":
        target = {**target, "instance_id": "another-instance"}
    elif change == "digest":
        digest = "0" * 64
    elif change == "foreign":
        principal = Principal(
            "another-owner", "owner_session", ALL_SCOPES, None, "loopback", target["instance_id"]
        )
    with pytest.raises((ProjectOperationError, RevisionConflict)):
        case.operations.execute(
            operation["id"],
            digest,
            ["external_provider", "estimated_cost_unknown"],
            principal=principal,
            target_identity=target,
            idempotency_key="guard-execution-key",
        )
    assert jobs(case) == []


def test_crash_after_enqueue_recovers_in_new_service_without_duplicate(case):
    operation = preview(case)
    with patch.object(
        case.operations, "_link_child", side_effect=RuntimeError("simulated process death")
    ):
        with pytest.raises(RuntimeError, match="process death"):
            execute(case, operation)
    assert len(jobs(case)) == 1
    case.operations = TranslationProjectOperationService(case.services)
    result = execute(case, operation)
    assert len(jobs(case)) == 3
    assert len({child["job_id"] for child in result["children"]}) == 3


def test_cancel_recovers_unlinked_committed_job_and_blocks_pending(case):
    operation = preview(case)
    with patch.object(case.operations, "_link_child", side_effect=RuntimeError("process death")):
        with pytest.raises(RuntimeError):
            execute(case, operation)
    case.operations = TranslationProjectOperationService(case.services)
    result = case.operations.cancel(
        operation["id"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="cancel-crashed-key",
    )
    assert result["status"] == "canceled"
    assert len(jobs(case)) == 1 and jobs(case)[0].status == "canceled"
    execute(case, operation)
    assert len(jobs(case)) == 1


def test_cancellation_between_children_prevents_next_enqueue(case):
    operation = preview(case)
    original = case.operations._link_child

    def cancel_after_link(*args, **kwargs):
        original(*args, **kwargs)
        case.operations.cancel(
            operation["id"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="cancel-between-children",
        )

    with patch.object(case.operations, "_link_child", side_effect=cancel_after_link):
        result = execute(case, operation)
    assert result["status"] == "canceled"
    assert len(jobs(case)) == 1


def test_actual_success_result_and_failed_only_retry_preserve_completed(case):
    started = execute(case, preview(case))
    good = artifact(case, case.branches[0])
    finish_job(case, started["children"][0]["job_id"], {"artifact_id": good.id})
    with case.database.session() as db:
        db.get(m.Job, started["children"][1]["job_id"]).status = "failed"
        db.get(m.Job, started["children"][2]["job_id"]).status = "canceled"
    state = case.operations.get(
        started["id"], principal=case.principal, target_identity=case.target
    )
    assert state["status"] == "partial"
    assert state["children"][0]["state"] == "completed"
    assert state["children"][0]["result"]["artifact_id"] == good.id
    retried = case.operations.retry_preview(
        started["id"],
        principal=case.principal,
        target_identity=case.target,
        expected_project_revision=case.project["revision"],
        idempotency_key="retry-preview-operation",
    )
    assert retried["existing_count"] == 1 and retried["retrying_count"] == 2
    assert retried["children"][0]["retained"]["result"]["artifact_id"] == good.id
    execute(case, retried, key="execute-retry-operation")
    assert len(jobs(case)) == 5
    with case.database.session() as db:
        assert db.get(m.Artifact, good.id).state == "current"


@pytest.mark.parametrize("wrong", ["language", "source", "missing", "receipt"])
def test_translation_requires_matching_produced_artifact_receipt(case, wrong):
    branch = case.branches[0]
    started = execute(case, preview(case, branches=[branch]))
    produced = artifact(case, branch)
    if wrong in {"language", "source"}:
        with case.database.session() as db:
            item = db.get(m.Artifact, produced.id)
            metadata = deepcopy(item.metadata_json)
            if wrong == "language":
                metadata.pop("language")
            else:
                metadata["source_artifact_id"] = case.checkpoint.id
            item.metadata_json = metadata
    elif wrong == "missing":
        case.services.paths.managed_path(produced.relative_path).unlink()
    result = {} if wrong == "receipt" else {"artifact_id": produced.id}
    finish_job(case, started["children"][0]["job_id"], result)
    state = case.operations.get(
        started["id"], principal=case.principal, target_identity=case.target
    )
    assert state["children"][0]["state"] == "failed"
    assert "result" not in state["children"][0]


def test_cancel_running_and_completed_children_preserves_success(case):
    started = execute(case, preview(case))
    good = artifact(case, case.branches[0])
    finish_job(case, started["children"][0]["job_id"], {"artifact_id": good.id})
    with case.database.session() as db:
        db.get(m.Job, started["children"][1]["job_id"]).status = "running"
    canceled = case.operations.cancel(
        started["id"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="cancel-running-operation",
    )
    assert canceled["status"] == "canceling"
    assert [child["state"] for child in canceled["children"]] == [
        "completed",
        "canceling",
        "canceled",
    ]
    replayed = case.operations.cancel(
        started["id"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="cancel-running-operation",
    )
    assert replayed["revision"] == canceled["revision"]
    assert jobs(case)[0].status == "succeeded"


def test_generation_requires_review_and_does_not_prepare_or_approve(case):
    branch = case.branches[0]
    revision_id, _ = reviewed_plan(case, branch, reviewed=False)
    with patch.object(
        case.services.generation,
        "plan_refresher",
        side_effect=AssertionError("Implicit preparation"),
    ):
        blocked = preview(case, "generate", branches=[branch])
        assert blocked["eligible_count"] == 0
        with case.database.immediate_session() as db:
            assert db.get(m.SpeechPlanReview, revision_id) is None
            review_speech_plan(
                db,
                branch["session_id"],
                revision_id=revision_id,
                content_signature=plan_signature(db, revision_id),
            )
        operation = preview(case, "generate", branches=[branch], key="reviewed-generation-preview")
        assert operation["eligible_count"] == 1
        assert operation["children"][0]["preview"]["language_preflight"]
        started = execute(case, operation)
    assert started["children"][0]["generation_run_id"]
    assert jobs(case)[0].kind == "generation.run"
    execute(case, operation)
    assert len(jobs(case)) == 1


@pytest.mark.parametrize("change", ["review", "input", "settings"])
def test_generation_rechecks_captured_review_input_settings(case, change):
    branch = case.branches[0]
    revision_id, _ = reviewed_plan(case, branch)
    operation = preview(case, "generate", branches=[branch])
    assert operation["eligible_count"] == 1
    if change == "review":
        with case.database.session() as db:
            db.delete(db.get(m.SpeechPlanReview, revision_id))
    elif change == "input":
        artifact(case, branch)
    else:
        set_settings(case, branch["session_id"], "tts", {"voice": "Puck"})
    result = execute(case, operation)
    assert result["children"][0]["state"] == "blocked"
    assert jobs(case) == []


def test_generation_crash_receipt_is_recovered_and_canceled(case):
    branch = case.branches[0]
    reviewed_plan(case, branch)
    operation = preview(case, "generate", branches=[branch])
    with patch.object(case.operations, "_link_child", side_effect=RuntimeError("process death")):
        with pytest.raises(RuntimeError):
            execute(case, operation)
    case.operations = TranslationProjectOperationService(case.services)
    canceled = case.operations.cancel(
        operation["id"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="cancel-generation-crash",
    )
    assert canceled["children"][0]["state"] == "canceled"
    with case.database.session() as db:
        assert (
            db.get(m.GenerationRun, canceled["children"][0]["generation_run_id"]).status
            == "canceled"
        )
    assert len(jobs(case)) == 1


@pytest.mark.parametrize("authority", ["wrapper", "in_session", "operation"])
def test_running_generation_cancel_authorities_agree_and_preserve_completed(case, authority):
    branch = case.branches[0]
    reviewed_plan(case, branch)
    started = execute(case, preview(case, "generate", branches=[branch]))
    child = started["children"][0]
    with case.database.session() as db:
        db.get(m.Job, child["job_id"]).status = "running"
        db.get(m.GenerationRun, child["generation_run_id"]).status = "running"
    if authority == "wrapper":
        case.services.generation.cancel(child["generation_run_id"])
    elif authority == "in_session":
        with case.database.immediate_session() as db:
            case.services.generation.cancel_in_session(db, child["generation_run_id"])
    else:
        case.operations.cancel(
            started["id"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="cancel-running-generation",
        )
    with case.database.session() as db:
        run = db.get(m.GenerationRun, child["generation_run_id"])
        assert run.cancel_requested is True and run.status == "cancel_requested"
        assert db.get(m.Job, child["job_id"]).status == "cancel_requested"
    finish_job(case, child["job_id"], {})
    completed = case.operations.cancel(
        started["id"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="cancel-completed-generation",
    )
    assert completed["children"][0]["state"] == "completed"
    assert completed["status"] == "completed"
    with case.database.session() as db:
        run = db.get(m.GenerationRun, child["generation_run_id"])
        assert run.status == "completed" and run.cancel_requested is False


def test_generation_actual_language_preflight_blocks_unsupported_model(case):
    branch = case.branches[0]
    reviewed_plan(case, branch)
    set_settings(case, branch["session_id"], "tts", {"service": "silero", "silero_model": "v3_en"})
    operation = preview(case, "generate", branches=[branch])
    assert operation["eligible_count"] == 0
    assert "does not support" in operation["children"][0]["reason"]
    assert jobs(case) == []


def test_subtitle_export_needs_translation_but_no_audio_and_uses_plural_receipts(case):
    for branch in case.branches:
        artifact(case, branch)
    operation = preview(case, "export", export_kind="subtitles")
    assert operation["eligible_count"] == 3
    assert all(child["preview"]["execution_mode"] == "direct" for child in operation["children"])
    started = execute(case, operation)
    for child in started["children"]:
        with case.database.session() as db:
            job = db.get(m.Job, child["job_id"])
            settings = job.payload_json["settings"]
            assert settings["export_mode"] == "subtitles"
            assert settings["subtitle_mode"] == "translation"
            assert settings["subtitle_format"] == "srt"
            assert settings["subtitle_selection"] == "translation"
            assert not list(db.scalars(select(m.GenerationRun)))
        produced = case.services.workflow_handlers.export(
            job.payload_json, lambda *_: None, threading.Event()
        )
        with case.database.session() as db:
            exported = db.get(m.Artifact, produced["artifact_ids"][0])
            assert exported.role == "export_subtitle_translation"
            assert exported.metadata_json["language"] == child["target_language"]
        finish_job(case, child["job_id"], produced)
    result = case.operations.get(
        operation["id"], principal=case.principal, target_identity=case.target
    )
    assert result["status"] == "completed"
    assert all(
        child["result"]["artifact_ids"] == [child["result"]["artifact_id"]]
        for child in result["children"]
    )
    canceled = case.operations.cancel(
        operation["id"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="cancel-completed-export",
    )
    assert canceled["status"] == "completed"


@pytest.mark.parametrize("wrong", ["role", "settings", "source", "missing"])
def test_export_cannot_claim_wrong_or_missing_artifact_completion(case, wrong):
    branch = case.branches[0]
    translated = artifact(case, branch)
    started = execute(case, preview(case, "export", branches=[branch], export_kind="subtitles"))
    with case.database.session() as db:
        settings = db.get(m.Job, started["children"][0]["job_id"]).payload_json["settings"]
    produced = artifact(
        case,
        branch,
        role="translation" if wrong == "role" else "export_subtitle_translation",
        parent_id=branch["source_checkpoint_artifact_id"] if wrong == "source" else translated.id,
        settings={} if wrong == "settings" else settings,
    )
    if wrong == "missing":
        case.services.paths.managed_path(produced.relative_path).unlink()
    finish_job(case, started["children"][0]["job_id"], {"artifact_ids": [produced.id]})
    result = case.operations.get(
        started["id"], principal=case.principal, target_identity=case.target
    )
    assert result["children"][0]["state"] == "failed"
    assert "result" not in result["children"][0]


def create_dispatch(case, branch):
    with case.database.session() as db:
        source = db.get(m.Artifact, branch["source_checkpoint_artifact_id"])
    case.services.workflow_handlers._store_srt_document(
        branch["session_id"], source, "correction", language="en"
    )
    with case.database.immediate_session() as db:
        return case.services.dispatch.create_in_session(
            db,
            session_id=branch["session_id"],
            kind="translation",
            source_artifact_id=source.id,
            source_language="en",
            target_language=branch["target_language"],
            instructions="",
            char_limit=1000,
            max_segments_per_batch=10,
            no_remove_subtitles=True,
            context_before=0,
            context_after=0,
            timing_context_mode="none",
            substantial_gap_ms=2000,
            glossary={},
        )


def test_passive_dispatch_stays_awaiting_agent_and_partial_retry_is_manual(case):
    started = execute(case, preview(case))
    branch = case.branches[0]
    dispatch = create_dispatch(case, branch)
    finish_job(case, started["children"][0]["job_id"], {"dispatch_run_id": dispatch["id"]})
    state = case.operations.get(
        started["id"], principal=case.principal, target_identity=case.target
    )
    assert state["children"][0]["state"] == "awaiting_agent"
    assert state["children"][0]["run_id"] == dispatch["id"]
    assert state["children"][0]["manual_resume_required"]
    canceled = case.operations.cancel(
        started["id"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="cancel-passive-operation",
    )
    assert canceled["status"] == "canceling"
    assert canceled["children"][0]["state"] == "awaiting_agent"
    with case.database.session() as db:
        run = db.get(m.DispatchRun, dispatch["id"])
        run.completed_batch_count = 1
        run.status = "failed"
    retried = case.operations.retry_preview(
        started["id"],
        principal=case.principal,
        target_identity=case.target,
        expected_project_revision=case.project["revision"],
        idempotency_key="retry-passive-operation",
    )
    assert retried["children"][0]["state"] == "skipped"
    assert retried["children"][0]["manual_resume_required"]
    assert retried["children"][0]["dispatch_run_id"] == dispatch["id"]
    execute(case, retried, key="execute-passive-retry")
    assert len(jobs(case)) == 5


def test_atomic_workflow_guard_blocks_cancel_committed_before_enqueue(case):
    operation = preview(case)
    original = case.services.workflow_plans.execute
    canceled = False

    def cancel_before_transaction(**kwargs):
        nonlocal canceled
        if not canceled:
            case.operations.cancel(
                operation["id"],
                principal=case.principal,
                target_identity=case.target,
                idempotency_key="atomic-cancellation-key",
            )
            canceled = True
        return original(**kwargs)

    with patch.object(
        case.services.workflow_plans, "execute", side_effect=cancel_before_transaction
    ):
        result = execute(case, operation)
    assert result["status"] == "canceled"
    assert jobs(case) == []


def test_routes_strict_schema_idempotency_and_foreign_principal(case):
    url = f"/api/v1/translation-projects/{case.project['id']}/operations/preview"
    body = {
        "selected_branch_ids": [branch["id"] for branch in case.branches],
        "expected_project_revision": case.project["revision"],
        "action": "translate",
    }
    missing = case.client.post(url, json=body, headers=case.headers)
    assert missing.status_code == 400
    headers = {**case.headers, "Idempotency-Key": "http-preview-operation"}
    bad = case.client.post(url, json={**body, "api_key": "secret-never-returned"}, headers=headers)
    assert bad.status_code == 422 and "secret-never-returned" not in bad.get_data(as_text=True)
    created = case.client.post(url, json=body, headers=headers)
    assert created.status_code == 201, created.get_json()
    assert jobs(case) == []
    assert (
        case.client.post(url, json=body, headers=headers).get_json()["id"]
        == created.get_json()["id"]
    )
    for name, model in PROJECT_OPERATION_SCHEMAS.items():
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        assert schema["additionalProperties"] is False, name
    assert len(project_operation_paths()) == 5


def test_missing_confirmations_and_invalid_branch_selection_enqueue_nothing(case):
    operation = preview(case)
    required = {
        value
        for child in operation["children"]
        for value in child["preview"]["required_confirmations"]
    }
    assert required
    with pytest.raises(ProjectOperationError, match="confirmation"):
        case.operations.execute(
            operation["id"],
            operation["preview_digest"],
            [],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="unconfirmed-operation",
        )
    duplicate = [case.branches[0]["id"]] * 2
    with pytest.raises(ValueError, match="unique"):
        case.operations.preview(
            case.project["id"],
            duplicate,
            expected_project_revision=case.project["revision"],
            action="translate",
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="duplicate-preview",
        )
    with pytest.raises(ProjectOperationError, match="belong"):
        case.operations.preview(
            case.project["id"],
            [case.source.id],
            expected_project_revision=case.project["revision"],
            action="translate",
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="foreign-branch-preview",
        )
    assert jobs(case) == []


def test_public_preview_redacts_nested_provider_secrets(case):
    original = case.services.workflow_plans.create

    def with_secret(**kwargs):
        plan = original(**kwargs)
        plan["selected_providers"].append({"api_key": "provider-secret-never-returned"})
        return plan

    with patch.object(case.services.workflow_plans, "create", side_effect=with_secret):
        operation = preview(case)
    assert "provider-secret-never-returned" not in json.dumps(operation)


def test_migration_up_down_on_disposable_database_copy(case, tmp_path):
    copied_path = tmp_path / "migration-copy.sqlite3"
    with sqlite3.connect(case.database.path) as source, sqlite3.connect(copied_path) as destination:
        source.backup(destination)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations")
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(copied_path))
    command.downgrade(config, "0051_translation_projects")
    copied = Database(copied_path)
    assert not inspect(copied.engine).has_table("translation_project_operations")
    copied.dispose()
    command.upgrade(config, "0052_translation_project_operations")
    copied = Database(copied_path)
    assert inspect(copied.engine).has_table("translation_project_operations")
    with copied.session() as db:
        assert db.scalar(select(func.count()).select_from(m.TranslationProjectBranch)) == 3
    copied.dispose()
    assert "secret-never-returned" not in json.dumps(project_operation_paths())
