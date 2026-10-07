"""Transcription checkpoints remain pinned as independent language work starts."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.artifacts import sha256_file
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.models import (
    Artifact,
    AudioTake,
    Base,
    Document,
    DocumentRevision,
    GenerationRun,
    GenerationSegment,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SessionStageSelection,
    TranslationProject,
    utcnow,
)
from pandrator.web.multilingual_setup import MultilingualSetup, deferred_source_outcome, write_setup
from pandrator.web.project_readiness import enrich_project_payload
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.translation_projects import (
    TranslationProjectConflict,
    _checkpoint,
    create_branches_in_session,
    create_project_in_session,
    get_project,
    get_session_project,
    update_project_source_in_session,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def source_app(tmp_path):
    prepare_web_test_data_root(tmp_path)
    tokens = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    yield app, tokens
    app.extensions["pandrator"]["services"].database.dispose()


@pytest.fixture
def source_case(source_app):
    app, _tokens = source_app
    services = app.extensions["pandrator"]["services"]
    source = services.sessions.create(
        "Transcribed source",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["transcribe", "export"],
    )
    yield services, source


def _text(services, source, role="transcription", *, language="en", reviewed=False):
    directory = services.paths.sessions / source.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{role}.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n")
    artifact = services.artifacts.register(
        path,
        kind="srt",
        role=role,
        session_id=source.id,
        metadata={"language": language},
    )
    with services.database.session() as db:
        document = Document(session_id=source.id, stage=role, language=language)
        db.add(document)
        db.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash=artifact.content_hash,
            reviewed=reviewed,
        )
        db.add(revision)
        db.flush()
        document.active_revision_id = revision.id
        db.get(Artifact, artifact.id).metadata_json = {
            "language": language,
            "revision_id": revision.id,
        }
    return artifact


def _project(services, source, artifact, *, branches=False):
    with services.database.immediate_session() as db:
        return create_project_in_session(
            db,
            source.id,
            artifact.id,
            "Languages",
            source.revision,
            paths=services.paths,
            create_planned_branches=branches,
            session_forks=services.session_forks,
            created_directories=[],
        )["project"]


@pytest.mark.parametrize("role", ["transcription", "correction"])
def test_checkpoint_role_drives_local_translation_source_and_outcome(source_case, role):
    services, source = source_case
    checkpoint = _text(services, source, role)
    with services.database.session() as db:
        write_setup(db, source.id, MultilingualSetup(target_languages=["ja"]))
    project = _project(services, source, checkpoint, branches=True)
    assert project["checkpoint_role"] == role
    assert project["checkpoint_revision_id"]
    branch = project["branches"][0]
    assert branch["source_checkpoint_role"] == role
    assert branch["source_checkpoint_revision_id"] != project["checkpoint_revision_id"]
    assert branch["source_checkpoint_artifact_id"] != checkpoint.id
    with services.database.session() as db:
        clone = db.get(Artifact, branch["source_checkpoint_artifact_id"])
        assert clone.session_id == branch["session_id"]
        assert clone.content_hash == checkpoint.content_hash
        translation = db.get(SessionSetting, (branch["session_id"], "translation"))
        assert translation.value_json["source_artifact_id"] == clone.id
        assert translation.value_json["target_language"] == "ja"
        outcome = db.get(OutcomePlan, branch["session_id"]).value_json
        assert outcome["inputs"]["translation"] == (
            "source" if role == "transcription" else "correction"
        )
        assert outcome["inputs"]["generation"] == "translation"
        assert outcome["transformations"]["correct"] is False
    settings = services.workspace_settings.get(branch["session_id"], "subtitles")
    assert settings["effective"]["language_defaults"] is True
    assert settings["subtitle_profiles"]["target"]["language"] == "ja"
    assert (
        settings["subtitle_profiles"]["target"]["limits"]["max_chars_per_line"]["effective"] == 16
    )
    captured = services.workflows.resolve_stage(branch["session_id"], "translate")
    assert captured.source_artifact_id == branch["source_checkpoint_artifact_id"]


def test_public_fork_rejects_transcription(source_case):
    services, source = source_case
    checkpoint = _text(services, source)
    with services.database.immediate_session() as db:
        with pytest.raises(ValueError, match="only after correction or translation"):
            services.session_forks.fork_in_session(db, source.id, checkpoint.id)


@pytest.mark.parametrize(
    "defect",
    [
        "foreign_artifact",
        "hash",
        "file",
        "translation",
        "unknown_role",
        "stale",
        "revision",
        "revision_owner",
        "revision_stage",
        "inactive_revision",
    ],
)
def test_checkpoint_rejects_invalid_identity(source_case, defect):
    services, source = source_case
    checkpoint = _text(services, source)
    foreign = services.sessions.create("Other", source_language="en")
    with services.database.session() as db:
        artifact = db.get(Artifact, checkpoint.id)
        revision = db.get(DocumentRevision, artifact.metadata_json["revision_id"])
        document = db.get(Document, revision.document_id)
        if defect == "foreign_artifact":
            artifact.session_id = foreign.id
        elif defect == "hash":
            artifact.content_hash = "changed"
        elif defect == "file":
            services.paths.managed_path(artifact.relative_path).write_text("changed")
        elif defect in {"translation", "unknown_role"}:
            artifact.role = "translation" if defect == "translation" else "unknown"
        elif defect == "stale":
            artifact.state = "stale"
        elif defect == "revision":
            artifact.metadata_json = {**artifact.metadata_json, "revision_id": "missing"}
        elif defect == "revision_owner":
            document.session_id = foreign.id
        elif defect == "revision_stage":
            document.stage = "correction"
        elif defect == "inactive_revision":
            document.active_revision_id = None
    with services.database.session() as db:
        with pytest.raises((KeyError, TranslationProjectConflict)):
            _checkpoint(db, services.paths, source.id, checkpoint.id)


def test_checkpoint_rejects_changed_pin_hash(source_case):
    services, source = source_case
    checkpoint = _text(services, source)
    with services.database.session() as db:
        with pytest.raises(TranslationProjectConflict, match="hash changed"):
            _checkpoint(db, services.paths, source.id, checkpoint.id, expected_hash="old-hash")


def test_optional_deferred_setup_requires_known_source_and_respects_selection(source_case):
    services, source = source_case
    with services.database.session() as db:
        write_setup(db, source.id, MultilingualSetup(target_languages=["ja"]))
        db.get(SessionRecord, source.id).source_language = "auto"
    with services.database.session() as db:
        assert (
            get_session_project(db, source.id, paths=services.paths)["setup_state"]
            == "awaiting_source"
        )
    checkpoint = _text(services, source, language="auto")
    with services.database.session() as db:
        pending = get_session_project(db, source.id, paths=services.paths)
        assert pending["setup_state"] == "blocked"
        assert "known source language" in pending["setup_blocked_reason"]
        db.get(SessionRecord, source.id).source_language = "en"
    with services.database.session() as db:
        ready = get_session_project(db, source.id, paths=services.paths)
        assert ready["setup_state"] == "ready"
        assert ready["source_checkpoint_artifact_id"] == checkpoint.id
        assert ready["correction_checkpoint_artifact_id"] == checkpoint.id
        # An explicit clear must not rediscover a historical current artifact.
        db.get(SessionStageSelection, (source.id, "transcribe")).artifact_id = None
    with services.database.session() as db:
        assert (
            get_session_project(db, source.id, paths=services.paths)["setup_state"]
            == "awaiting_source"
        )


def test_source_status_tracks_transcription_then_later_correction_without_repinning(source_case):
    services, source = source_case
    checkpoint = _text(services, source)
    project = _project(services, source, checkpoint)
    payload = {"project": project}
    original = deepcopy(payload)
    first = enrich_project_payload(services, payload)["project"]["source_status"]
    assert payload == original
    assert first["source_changed"] is False
    assert first["pinned_checkpoint"]["role"] == "transcription"
    assert first["current_checkpoint"]["artifact_id"] == checkpoint.id
    assert first["current_correction"]["artifact_id"] is None
    correction = _text(services, source, "correction")
    second = enrich_project_payload(services, payload)["project"]["source_status"]
    assert second["source_changed"] is True
    assert second["current_checkpoint"]["role"] == "correction"
    assert second["current_correction"]["artifact_id"] == correction.id
    with services.database.session() as db:
        assert get_project(db, project["id"])["project"]["checkpoint_artifact_id"] == checkpoint.id
        assert (
            get_session_project(db, source.id, paths=services.paths)[
                "source_checkpoint_artifact_id"
            ]
            == checkpoint.id
        )


@pytest.mark.parametrize("correct", [False, True])
def test_deferred_outcome_keeps_chosen_source_input(correct):
    result = deferred_source_outcome(
        {"transformations": {"correct": correct, "translate": True}},
        workflow_kind="subtitles",
    )
    expected = "correction" if correct else "source"
    assert result["inputs"] == {"translation": expected, "generation": expected}
    assert result["transformations"]["correct"] is correct
    assert result["transformations"]["translate"] is False


def test_internal_transcription_fork_preserves_media_ancestry_guard(source_case):
    services, source = source_case
    checkpoint = _text(services, source)
    with services.database.session() as db:
        plan = MediaEditPlan(session_id=source.id)
        db.add(plan)
        db.flush()
        revision = MediaEditPlanRevision(
            plan_id=plan.id,
            revision_number=1,
            content_hash="edit-hash",
            source_media_artifact_id=checkpoint.id,
            editorial_transcript_artifact_id=checkpoint.id,
            duration_ms=1000,
        )
        db.add(revision)
        db.flush()
        plan.active_revision_id = revision.id
        metadata = {
            "plan_id": plan.id,
            "media_edit_revision_id": revision.id,
            "content_hash": revision.content_hash,
        }
    directory = services.paths.sessions / source.storage_key
    for role in ("media_edit_media", "media_edit_subtitles", "media_edit_word_timestamps"):
        path = directory / f"{role}.txt"
        path.write_text(role)
        services.artifacts.register(
            path,
            kind="txt",
            role=role,
            session_id=source.id,
            metadata=metadata,
        )
    with services.database.immediate_session() as db:
        with pytest.raises(ValueError, match="not derived from the active edited timeline"):
            services.session_forks.fork_in_session(
                db,
                source.id,
                checkpoint.id,
                allow_source_checkpoint=True,
            )


def _refresh(services, source, project, checkpoint, **changes):
    values = {
        "project_id": project["id"],
        "expected_revision": project["revision"],
        "expected_source_revision": source.revision,
        "checkpoint_artifact_id": checkpoint.id,
        **changes,
    }
    with services.database.immediate_session() as db:
        return update_project_source_in_session(db, **values, paths=services.paths)["project"]


def _preserved_rows(services):
    with services.database.session() as db:
        return {
            table.name: [dict(row) for row in db.execute(select(table)).mappings()]
            for table in Base.metadata.sorted_tables
            if table.name != TranslationProject.__tablename__
        }


def test_project_source_refresh_preserves_old_branch_work_and_changes_future_branches(source_case):
    services, source = source_case
    transcription = _text(services, source)
    with services.database.session() as db:
        write_setup(db, source.id, MultilingualSetup(target_languages=["ja"]))
    project = _project(services, source, transcription, branches=True)
    old_branch = project["branches"][0]
    old_sid = old_branch["session_id"]
    old_pin_id = old_branch["source_checkpoint_artifact_id"]
    plan = services.generation.create_plan(
        old_sid,
        source_revision_id=old_branch["source_checkpoint_revision_id"],
        segments=[{"text": "Existing speech", "start_ms": 0, "end_ms": 1000}],
        settings={"_source_artifact_id": old_pin_id},
    )
    with services.database.session() as db:
        directory = services.paths.sessions / db.get(SessionRecord, old_sid).storage_key
    output_file = directory / "kept-output.wav"
    output_file.write_bytes(b"existing generated audio remains untouched")
    output = services.artifacts.register(
        output_file,
        kind="wav",
        role="assembled_audio",
        session_id=old_sid,
        parent_ids=[old_pin_id],
    )
    with services.database.session() as db:
        save_generation_controls(
            db,
            old_sid,
            expected_revision=0,
            characters=[{"id": "c-narrator", "display_name": "Narrator"}],
            cast={"narrator": {"voice": "Retained narrator"}},
        )
        segment = db.scalar(
            select(GenerationSegment).where(
                GenerationSegment.plan_revision_id == plan["active_revision_id"],
            )
        )
        run = GenerationRun(
            session_id=old_sid,
            plan_revision_id=plan["active_revision_id"],
            status="completed",
        )
        db.add(run)
        db.flush()
        db.add(
            AudioTake(
                generation_segment_id=segment.id,
                generation_run_id=run.id,
                artifact_id=output.id,
                status="completed",
                is_active=True,
            )
        )
    correction = _text(services, source, "correction")
    with services.database.session() as db:
        project = get_project(db, project["id"])["project"]
    before = _preserved_rows(services)
    files_before = {
        artifact["relative_path"]: sha256_file(
            services.paths.managed_path(artifact["relative_path"])
        )
        for artifact in before[Artifact.__tablename__]
    }
    refreshed = _refresh(services, source, project, correction)
    assert refreshed["checkpoint_artifact_id"] == correction.id
    assert refreshed["checkpoint_role"] == "correction"
    assert refreshed["source_content_hash"] == correction.content_hash
    assert refreshed["revision"] == project["revision"] + 1
    assert refreshed["branches"] == project["branches"]
    assert _preserved_rows(services) == before
    assert {
        relative: sha256_file(services.paths.managed_path(relative)) for relative in files_before
    } == files_before
    assert (
        enrich_project_payload(services, {"project": refreshed})["project"]["source_status"][
            "source_changed"
        ]
        is False
    )
    with services.database.immediate_session() as db:
        added = create_branches_in_session(
            db,
            project["id"],
            refreshed["revision"],
            [{"target_language": "de"}],
            session_forks=services.session_forks,
            paths=services.paths,
            created_directories=[],
        )["project"]
    new_branch = next(branch for branch in added["branches"] if branch["target_language"] == "de")
    assert new_branch["source_checkpoint_role"] == "correction"
    assert new_branch["source_checkpoint_artifact_id"] != correction.id
    with services.database.session() as db:
        assert (
            db.get(Artifact, new_branch["source_checkpoint_artifact_id"]).session_id
            == new_branch["session_id"]
        )
        assert (
            db.get(SessionSetting, (old_sid, "translation")).value_json["source_artifact_id"]
            == old_pin_id
        )
        assert (
            db.get(SessionSetting, (new_branch["session_id"], "translation")).value_json[
                "source_artifact_id"
            ]
            == new_branch["source_checkpoint_artifact_id"]
        )
        assert (
            db.get(OutcomePlan, (new_branch["session_id"])).value_json["inputs"]["translation"]
            == "correction"
        )
        assert db.get(Artifact, output.id).state == "current"


@pytest.mark.parametrize(
    "defect",
    [
        "project_revision",
        "source_revision",
        "language",
        "ownership",
        "hash",
        "missing_file",
        "missing_project",
        "trashed_source",
    ],
)
def test_project_source_refresh_rejects_changed_or_invalid_inputs(source_case, defect):
    services, source = source_case
    transcription = _text(services, source)
    project = _project(services, source, transcription)
    correction = _text(
        services, source, "correction", language="pl" if defect == "language" else "en"
    )
    changes = {}
    if defect == "project_revision":
        changes["expected_revision"] = project["revision"] + 1
    elif defect == "source_revision":
        changes["expected_source_revision"] = source.revision + 1
    elif defect == "missing_project":
        changes["project_id"] = "missing"
    elif defect == "ownership":
        foreign = services.sessions.create("Foreign checkpoint owner", source_language="en")
        correction = _text(services, foreign, "correction")
    elif defect == "missing_file":
        services.paths.managed_path(correction.relative_path).unlink()
    elif defect in {"hash", "trashed_source"}:
        with services.database.session() as db:
            if defect == "hash":
                db.get(Artifact, correction.id).content_hash = "changed"
            else:
                db.get(SessionRecord, source.id).trashed_at = utcnow()
    with pytest.raises((KeyError, RevisionConflict)):
        _refresh(services, source, project, correction, **changes)
    with services.database.session() as db:
        retained = get_project(db, project["id"])["project"]
        assert retained["checkpoint_artifact_id"] == transcription.id
        assert retained["revision"] == project["revision"]


@pytest.mark.parametrize("defect", ["missing_render", "mismatched_render", "unrelated_checkpoint"])
def test_project_source_refresh_rejects_unready_media_checkpoint(source_case, defect):
    services, source = source_case
    transcription = _text(services, source)
    project = _project(services, source, transcription)
    correction = _text(services, source, "correction")
    with services.database.session() as db:
        plan = MediaEditPlan(session_id=source.id)
        db.add(plan)
        db.flush()
        revision = MediaEditPlanRevision(
            plan_id=plan.id,
            revision_number=1,
            content_hash="edit-hash",
            source_media_artifact_id=transcription.id,
            editorial_transcript_artifact_id=transcription.id,
            duration_ms=1000,
        )
        db.add(revision)
        db.flush()
        plan.active_revision_id = revision.id
        metadata = {
            "plan_id": plan.id,
            "media_edit_revision_id": revision.id,
            "content_hash": revision.content_hash,
        }
    if defect != "missing_render":
        directory = services.paths.sessions / source.storage_key
        for role in ("media_edit_media", "media_edit_subtitles", "media_edit_word_timestamps"):
            path = directory / f"{role}.txt"
            path.write_text(role)
            services.artifacts.register(
                path,
                kind="txt",
                role=role,
                session_id=source.id,
                metadata={**metadata, "content_hash": "wrong"}
                if defect == "mismatched_render"
                else metadata,
            )
    with pytest.raises(ValueError, match="render is unavailable|does not match|not derived"):
        _refresh(services, source, project, correction)
    with services.database.session() as db:
        retained = get_project(db, project["id"])["project"]
        assert retained["checkpoint_artifact_id"] == transcription.id
        assert retained["revision"] == project["revision"]


def test_project_source_refresh_http_replay_noop_and_openapi(source_case, source_app):
    services, source = source_case
    transcription = _text(services, source)
    project = _project(services, source, transcription)
    app, tokens = source_app
    client = app.test_client()
    headers = {
        "X-CSRF-Token": client.post(
            "/api/v1/auth/bootstrap",
            json={"token": tokens.issue()},
        ).get_json()["csrf_token"]
    }
    url = f"/api/v1/translation-projects/{project['id']}/source"
    payload = {
        "expected_revision": project["revision"],
        "expected_source_revision": source.revision,
        "checkpoint_artifact_id": transcription.id,
    }
    with services.database.session() as db:
        original_stamp = db.get(TranslationProject, project["id"]).updated_at
    unchanged = client.post(
        url, json=payload, headers={**headers, "Idempotency-Key": "pin-noop-key"}
    )
    assert unchanged.status_code == 200, unchanged.get_json()
    assert unchanged.get_json()["project"]["revision"] == project["revision"]
    assert "source_status" in unchanged.get_json()["project"]
    with services.database.session() as db:
        assert db.get(TranslationProject, project["id"]).updated_at == original_stamp
    correction = _text(services, source, "correction")
    payload["checkpoint_artifact_id"] = correction.id
    written = client.post(
        url, json=payload, headers={**headers, "Idempotency-Key": "pin-update-key"}
    )
    assert written.status_code == 200, written.get_json()
    updated = written.get_json()["project"]
    assert updated["revision"] == project["revision"] + 1
    with services.database.session() as db:
        updated_stamp = db.get(TranslationProject, project["id"]).updated_at
    replay = client.post(
        url, json=payload, headers={**headers, "Idempotency-Key": "pin-update-key"}
    )
    assert replay.status_code == 200, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json()["project"]["revision"] == updated["revision"]
    payload["expected_revision"] = updated["revision"]
    again = client.post(
        url, json=payload, headers={**headers, "Idempotency-Key": "pin-second-noop-key"}
    )
    assert again.status_code == 200, again.get_json()
    assert again.get_json()["project"]["revision"] == updated["revision"]
    with services.database.session() as db:
        assert db.get(SessionRecord, source.id).revision == source.revision
        assert db.get(TranslationProject, project["id"]).revision == updated["revision"]
        assert db.get(TranslationProject, project["id"]).updated_at == updated_stamp
    from pandrator.web.translation_project_routes import PROJECT_SCHEMAS, translation_project_paths

    operation = translation_project_paths()["/api/v1/translation-projects/{projectId}/source"][
        "post"
    ]
    assert operation["operationId"] == "updateTranslationProjectSource"
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "TranslationProjectSourceUpdateRequest"
    )
    assert "TranslationProjectSourceUpdateRequest" in PROJECT_SCHEMAS
