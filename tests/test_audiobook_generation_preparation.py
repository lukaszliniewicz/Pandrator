"""Disposable integration coverage for explicit automatic narration prerequisites."""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.execution_actor import UserActor, execution_actor_scope
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.artifacts import ArtifactService
from pandrator.web.audiobook_generation_preparation import (
    GUARD_KEY,
    prepare_audiobook_generation,
)
from pandrator.web.audiobook_setup import preview_audiobook_segmentation
from pandrator.web.auth import AuthService
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.sessions import SessionService
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from pandrator.web.speech_plan_context import plan_signature
from pandrator.web.speech_plan_workspace import selected_text
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root

QWEN = "qwen3_tts_1_7b_base_q8_0"
BREEZE = "breeze_tts_2_q8_0"
VOX = "voxcpm2_q8_0"


@pytest.fixture
def case(tmp_path, monkeypatch):
    # Stub only boundary prediction; production preprocessing, budgets and model
    # metadata still determine every resulting block.
    monkeypatch.setattr(
        "pandrator.logic.sentence_segmenter.split_text",
        lambda text: re.split(r"(?<=[.!?])\s+", text),
    )
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    sessions = SessionService(database)
    record = sessions.create("Automatic refresh", source_language="en")
    artifacts = ArtifactService(database, paths)
    settings = WorkspaceSettingsService(database)
    settings.update(
        record.id,
        "text",
        0,
        {
            "enable_nemo_normalization": False,
            "normalize_all_caps": False,
            "audiobook_chunking": "model",
        },
    )
    settings.update(record.id, "tts", 0, {"service": "audio_cpp", "model": QWEN})
    directory = paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    source_path = directory / "clean.txt"
    source_path.write_text(
        "A careful narrator describes the village and the river beyond the old stone bridge. " * 60
    )
    source = artifacts.register(source_path, kind="text", role="clean_text", session_id=record.id)
    jobs = JobQueue(database)
    handlers = WorkflowHandlers(database, paths)
    services = SimpleNamespace(
        database=database,
        workspace_settings=settings,
        jobs=jobs,
        workflows=WorkflowService(database, jobs),
        workflow_handlers=handlers,
    )
    initial = services.workflows.resolve_stage(record.id, "prepare_text")
    result = handlers.prepare_text(initial.payload, lambda *_: None, threading.Event())
    fixture = SimpleNamespace(
        services=services,
        database=database,
        settings=settings,
        jobs=jobs,
        handlers=handlers,
        record=record,
        source=source,
        source_path=source_path,
        revision_id=result["generation_plan_revision_id"],
    )
    yield fixture
    database.dispose()


def switch(case, model):
    current = case.settings.get(case.record.id, "tts")
    case.settings.update(
        case.record.id,
        "tts",
        current["revision"],
        {
            "service": "audio_cpp",
            "model": model,
        },
    )


def prepare(case, **fields):
    return prepare_audiobook_generation(
        case.services,
        case.record.id,
        speech_plan_revision_id=case.revision_id,
        **fields,
    )


def execute(case, response):
    with case.database.session() as unit:
        job = unit.get(m.Job, response["job_id"])
        payload = json.loads(json.dumps(job.payload_json))
        assert job.kind == "text.prepare"
        assert job.resource_keys_json == [f"session:{case.record.id}"]
    result = case.handlers.prepare_text(payload, lambda *_: None, threading.Event())
    with case.database.immediate_session() as unit:
        unit.get(m.Job, response["job_id"]).status = "completed"
    return result


def state(case):
    with case.database.session() as unit:
        plan = unit.scalar(
            select(m.GenerationPlan).where(m.GenerationPlan.session_id == case.record.id)
        )
        return {
            "active": plan.active_revision_id,
            "revisions": unit.scalar(select(func.count()).select_from(m.GenerationPlanRevision)),
            "artifacts": unit.scalar(select(func.count()).select_from(m.Artifact)),
            "jobs": unit.scalar(select(func.count()).select_from(m.Job)),
            "settings": [
                (row.section, row.revision, row.value_json)
                for row in unit.scalars(select(m.SessionSetting))
            ],
        }


def test_qwen_breeze_vox_transitions_refresh_to_real_budget_without_synthesis(case):
    original = case.revision_id
    assert prepare(case)["status"] == "current"
    switch(case, VOX)
    before = state(case)
    assert prepare(case)["status"] == "current"
    assert state(case) == before
    switch(case, QWEN)
    for model, target in ((BREEZE, 450), (VOX, 350)):
        switch(case, model)
        queued = prepare(case)
        assert queued["status"] == "queued"
        assert queued["target_chars"] == target
        with case.database.session() as unit:
            payload = unit.get(m.Job, queued["job_id"]).payload_json
            assert payload["settings"]["_audiobook_tts_settings"]["model"] == model
            assert payload[GUARD_KEY]["revision_id"] == case.revision_id
        # Duplicate requests cannot enqueue a second replacement.
        with pytest.raises(RevisionConflict):
            prepare(case)
        result = execute(case, queued)
        case.revision_id = result["generation_plan_revision_id"]
        assert result["segmentation_budget"]["target_chars"] == target
        with case.database.session() as unit:
            rows = list(
                unit.scalars(
                    select(m.GenerationSegment).where(
                        m.GenerationSegment.plan_revision_id == case.revision_id
                    )
                )
            )
            assert rows and max(len(row.text) for row in rows) <= target
            assert unit.get(m.SpeechPlanReview, case.revision_id) is None
            assert unit.scalar(select(func.count()).select_from(m.GenerationRun)) == 0
            assert unit.get(m.GenerationPlanRevision, original) is not None
        assert prepare(case)["status"] == "current"


def test_manual_74_remains_manual_across_models(case):
    current = case.settings.get(case.record.id, "text")
    case.settings.patch(
        case.record.id,
        "text",
        current["revision"],
        {
            "audiobook_chunking": "manual",
            "max_sentence_length": 74,
        },
    )
    first = execute(case, prepare(case))
    case.revision_id = first["generation_plan_revision_id"]
    assert first["segmentation_budget"]["target_chars"] == 74
    for model in (BREEZE, VOX):
        switch(case, model)
        before = state(case)
        assert prepare(case)["status"] == "current"
        assert state(case) == before


@pytest.mark.parametrize(
    "change",
    [
        "segment",
        "topology",
        "optimized",
        "annotated",
        "performance",
        "missing_provenance",
        "voice_selection",
    ],
)
def test_preserves_authored_or_unknown_plans(case, change):
    switch(case, BREEZE)
    with case.database.immediate_session() as unit:
        revision = unit.get(m.GenerationPlanRevision, case.revision_id)
        row = unit.scalar(
            select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == revision.id)
        )
        if change == "segment":
            row.revision += 1
        elif change == "topology":
            revision.operation_json = {"action": "split"}
        elif change == "optimized":
            row.optimized_text = "Authored speech text."
        elif change == "annotated":
            artifact = unit.get(m.Artifact, revision.settings_json["_source_artifact_id"])
            artifact.metadata_json = {"segmentation": {"policy_applied": False}}
        elif change == "performance":
            unit.add(
                m.PerformancePlan(
                    session_id=case.record.id,
                    plan_revision_id=revision.id,
                    base_signature=plan_signature(unit, revision.id),
                )
            )
        elif change == "voice_selection":
            row.voice = "Authored voice"
        else:
            revision.settings_json = {
                key: value
                for key, value in revision.settings_json.items()
                if key != "_source_artifact_id"
            }
    before = state(case)
    result = prepare(case)
    assert result["status"] == "preserved"
    assert result["message"]
    assert state(case) == before


def test_plain_review_does_not_disable_automatic_refresh(case):
    with case.database.immediate_session() as unit:
        unit.add(
            m.SpeechPlanReview(
                revision_id=case.revision_id, content_hash=plan_signature(unit, case.revision_id)
            )
        )
    switch(case, BREEZE)
    assert prepare(case)["status"] == "queued"


def test_explicit_historical_settings_and_other_workflow_are_read_only(case):
    switch(case, BREEZE)
    with case.database.immediate_session() as unit:
        run = m.GenerationRun(
            session_id=case.record.id,
            plan_revision_id=case.revision_id,
            settings_hash="old",
            settings_snapshot_json={},
            status="completed",
        )
        unit.add(run)
        unit.flush()
        run_id = run.id
    before = state(case)
    assert prepare(case, settings_source_run_id=run_id)["status"] == "current"
    assert state(case) == before
    with pytest.raises(ValueError, match="unavailable"):
        prepare(case, settings_source_run_id="another-session-run")
    with case.database.immediate_session() as unit:
        unit.get(m.SessionRecord, case.record.id).workflow_kind = "voiceover"
    before = state(case)
    assert prepare(case)["status"] == "current"
    assert state(case) == before


@pytest.mark.parametrize("when", ["before_start", "before_publish"])
@pytest.mark.parametrize(
    "change",
    ["plan", "segment", "text", "settings", "global_settings", "performance", "selection", "file"],
)
def test_concurrent_drift_never_publishes(case, monkeypatch, when, change):
    switch(case, BREEZE)
    queued = prepare(case)
    with case.database.session() as unit:
        payload = json.loads(json.dumps(unit.get(m.Job, queued["job_id"]).payload_json))
    before = state(case)

    def drift():
        if change == "file":
            case.source_path.write_text("Externally changed source text.")
            return
        with case.database.immediate_session() as unit:
            revision = unit.get(m.GenerationPlanRevision, case.revision_id)
            if change == "plan":
                unit.get(m.GenerationPlan, revision.plan_id).active_revision_id = None
            elif change == "segment":
                row = unit.scalar(
                    select(m.GenerationSegment).where(
                        m.GenerationSegment.plan_revision_id == revision.id
                    )
                )
                row.text = "Concurrent authored text."
                row.revision += 1
            elif change == "text":
                case.source.metadata_json = {"edited": True}
                unit.get(m.Artifact, case.source.id).content_hash = "new text hash"
            elif change == "performance":
                unit.add(
                    m.PerformancePlan(
                        session_id=case.record.id,
                        plan_revision_id=revision.id,
                        base_signature="concurrent",
                    )
                )
            elif change == "global_settings":
                workspace = initial_personal_workspace(unit)
                unit.add(
                    m.WorkspaceSetting(
                        workspace_id=workspace.id,
                        key="defaults.text",
                        value_json={"remove_quotation_marks": True},
                    )
                )
            elif change == "selection":
                selection = unit.get(m.SessionStageSelection, (case.record.id, "clean_source"))
                selection.revision += 1
            else:
                row = unit.get(m.SessionSetting, (case.record.id, "tts"))
                row.revision += 1
                row.value_json = {**row.value_json, "model": VOX}

    if when == "before_start":
        drift()
    else:
        # Progress occurs after preprocessing but before the final writer.
        def progress(value, _detail=None):
            if value == 0.9:
                drift()

    with pytest.raises(RevisionConflict):
        case.handlers.prepare_text(
            payload, progress if when == "before_publish" else lambda *_: None, threading.Event()
        )
    after = state(case)
    assert after["revisions"] == before["revisions"]
    assert after["artifacts"] == before["artifacts"]


def test_different_latest_clean_text_is_preserved(case):
    other = case.source_path.with_name("other.txt")
    other.write_text("A different document.")
    ArtifactService(case.database, case.handlers.paths).register(
        other, kind="text", role="clean_text", session_id=case.record.id
    )
    switch(case, BREEZE)
    before = state(case)
    assert prepare(case)["status"] == "preserved"
    assert state(case) == before


def test_explicit_prepared_text_selection_advances_atomically(case):
    with case.database.immediate_session() as unit:
        revision = unit.get(m.GenerationPlanRevision, case.revision_id)
        old_artifact_id = revision.settings_json["_source_artifact_id"]
        selection = unit.get(m.SessionStageSelection, (case.record.id, "prepare_text"))
        selection.artifact_id = old_artifact_id
        previous_revision = selection.revision
    assert selected_text(case.services, case.record.id)["artifact_id"] == old_artifact_id
    switch(case, BREEZE)
    result = execute(case, prepare(case))
    assert selected_text(case.services, case.record.id)["artifact_id"] == result["artifact_id"]
    with case.database.session() as unit:
        selection = unit.get(m.SessionStageSelection, (case.record.id, "prepare_text"))
        assert selection.artifact_id == result["artifact_id"]
        assert selection.revision == previous_revision + 1
        assert (
            unit.get(m.SessionStageSelection, (case.record.id, "clean_source")).artifact_id
            == case.source.id
        )


def test_segmentation_preview_overrides_use_production_budgets_without_writes(case):
    before = state(case)
    for model, target in ((QWEN, 350), (BREEZE, 450), (VOX, 350)):
        result = preview_audiobook_segmentation(
            case.services, case.record.id, tts={"service": "audio_cpp", "model": model}
        )
        assert result["segmentation"]["target_chars"] == target
        assert result["model_default_segmentation"]["target_chars"] == target
    result = preview_audiobook_segmentation(
        case.services,
        case.record.id,
        tts={"service": "audio_cpp", "model": VOX},
        text={"audiobook_chunking": "manual", "max_sentence_length": 74},
    )
    assert result["segmentation"]["target_chars"] == 74
    assert result["model_default_segmentation"]["target_chars"] == 350
    assert state(case) == before


def test_invalid_execution_actor_cannot_enqueue(case):
    switch(case, BREEZE)
    before = state(case)
    with execution_actor_scope(object()), pytest.raises(AccessDenied):
        prepare(case)
    assert state(case) == before


def test_current_plan_does_not_require_edit_but_refresh_does(case):
    auth = AuthService(case.database)
    owner = auth.initial_account()
    scopes = frozenset({"app.read", "app.run"})
    browser = auth.browser_sessions.issue(
        owner.user_id,
        owner.auth_revision,
        subject="read-run",
        principal_kind="owner_session",
        scopes=scopes,
    )
    actor = UserActor(
        owner.user_id,
        owner.auth_revision,
        "read-run",
        "owner_session",
        scopes,
        browser_session_id=browser.record_id,
    )
    with execution_actor_scope(actor):
        assert prepare(case)["status"] == "current"
    switch(case, BREEZE)
    before = state(case)
    with execution_actor_scope(actor), pytest.raises(AccessDenied):
        prepare(case)
    assert state(case) == before


def test_plan_bound_sound_placements_are_preserved(case):
    switch(case, BREEZE)
    with case.database.immediate_session() as unit:
        workspace = initial_personal_workspace(unit)
        variant = m.SoundVariant(workspace_id=workspace.id)
        sound = ProjectSoundPlan(
            session_id=case.record.id, revision=1, speech_plan_revision_id=case.revision_id
        )
        unit.add_all([variant, sound])
        unit.flush()
        unit.add(
            ProjectSoundCue(
                logical_id=m.new_id(),
                plan_id=sound.id,
                variant_id=variant.id,
                anchor_json={},
                playback_json={},
            )
        )
    before = state(case)
    assert prepare(case)["status"] == "preserved"
    assert state(case) == before
