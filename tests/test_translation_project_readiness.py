"""Projects expose independent readiness without modifying pinned language work."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    AppSetting,
    Artifact,
    Document,
    DocumentRevision,
    GenerationRun,
    Job,
    SessionRecord,
    SessionSetting,
    utcnow,
)
from pandrator.web.output_settings_snapshot import build_output_settings_snapshot
from pandrator.web.project_readiness import enrich_project_payload
from pandrator.web.translation_projects import get_project
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def project_case(tmp_path):
    prepare_web_test_data_root(tmp_path)
    tokens = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    client = app.test_client()
    headers = {
        "X-CSRF-Token": client.post(
            "/api/v1/auth/bootstrap", json={"token": tokens.issue()}
        ).get_json()["csrf_token"]
    }
    services = app.extensions["pandrator"]["services"]
    source = services.sessions.create(
        "Languages",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "translate", "export"],
    )
    directory = services.paths.sessions / source.storage_key
    directory.mkdir(parents=True)
    file = directory / "correction.srt"
    file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n")
    checkpoint = services.artifacts.register(
        file, kind="srt", role="correction", session_id=source.id, metadata={"language": "en"}
    )
    response = client.post(
        f"/api/v1/sessions/{source.id}/translation-project",
        json={"checkpoint_artifact_id": checkpoint.id, "expected_revision": source.revision},
        headers={**headers, "Idempotency-Key": "readiness-project-key"},
    )
    assert response.status_code == 200, response.get_json()
    yield services, client, headers, source, checkpoint, response.get_json()["project"]
    services.database.dispose()


def _branches(case, *, voiceover=False, carry=False):
    services, client, headers, source, _, project = case
    if voiceover:
        with services.database.session() as db:
            db.add(
                SessionSetting(
                    session_id=source.id,
                    section="multilingual_setup",
                    value_json={"target_languages": ["ja", "pl", "de"], "generate_voiceover": True},
                )
            )
    response = client.post(
        f"/api/v1/translation-projects/{project['id']}/branches",
        json={
            "expected_revision": project["revision"],
            "targets": [
                {"target_language": language, "carry_source_subtitle_settings": carry}
                for language in ("ja", "pl", "de")
            ],
        },
        headers={**headers, "Idempotency-Key": "readiness-branches-key"},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["project"]["branches"]


def _read(case):
    return case[1].get(f"/api/v1/translation-projects/{case[-1]['id']}").get_json()["project"]


def _translation(services, branch, *, reviewed=False):
    with services.database.session() as db:
        record = db.get(SessionRecord, branch["session_id"])
        directory = services.paths.sessions / record.storage_key
    file = directory / "translation.srt"
    file.write_text("1\n00:00:00,000 --> 00:00:01,000\nTranslated.\n")
    artifact = services.artifacts.register(
        file,
        kind="srt",
        role="translation",
        session_id=branch["session_id"],
        parent_ids=[branch["source_checkpoint_artifact_id"]],
        metadata={"language": branch["target_language"]},
    )
    with services.database.session() as db:
        document = Document(
            session_id=branch["session_id"], stage="translation", language=branch["target_language"]
        )
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
            "language": branch["target_language"],
            "revision_id": revision.id,
        }
        revision_id = revision.id
    return artifact, revision_id


def test_readiness_profiles_provenance_independence_and_no_writes(project_case):
    services = project_case[0]
    branches = _branches(project_case)
    assert [b["target_language"] for b in branches] == ["ja", "pl", "de"]
    assert [
        b["settings"]["subtitles"]["subtitle_profiles"]["target"]["language"] for b in branches
    ] == ["ja", "pl", "de"]
    for branch in branches:
        assert branch["settings"]["subtitles"]["origin"] == "automatic_language_defaults"
        assert branch["readiness"]["voice"]["status"] == "not_requested"
        assert branch["readiness"]["generation"]["status"] == "not_requested"
        assert branch["created_at"]
    with services.database.session() as db:
        payload = get_project(db, project_case[-1]["id"])
        before = deepcopy(payload)
        counts = {
            model.__name__: db.scalar(select(func.count()).select_from(model))
            for model in (Artifact, Job, SessionSetting)
        }
    first = enrich_project_payload(services, payload)
    assert payload == before
    assert first["project"]["source_status"]["source_changed"] is False
    with services.database.session() as db:
        assert counts == {
            model.__name__: db.scalar(select(func.count()).select_from(model))
            for model in (Artifact, Job, SessionSetting)
        }
    sid = branches[0]["session_id"]
    settings = services.workspace_settings.get(sid, "subtitles")
    services.workspace_settings.patch(
        sid, "subtitles", settings["revision"], {"language_defaults": False, "max_cps": 4}
    )
    changed = _read(project_case)["branches"]
    assert changed[0]["settings"]["subtitles"]["origin"] == "custom_override"
    assert changed[1]["settings"]["subtitles"]["origin"] == "automatic_language_defaults"
    assert changed[1]["settings"]["subtitles"]["effective"]["max_cps"] != 4


def test_source_drift_preserves_checkpoint_and_historical_mode_unknown(project_case):
    services, _, _, source, checkpoint, _ = project_case
    branches = _branches(project_case)
    with services.database.session() as db:
        pinned = db.get(Artifact, checkpoint.id)
        pinned.state = "stale"
        clone = db.get(Artifact, branches[0]["source_checkpoint_artifact_id"])
        clone.metadata_json = {
            k: v
            for k, v in clone.metadata_json.items()
            if k != "translation_project_settings_origin"
        }
        directory = services.paths.sessions / source.storage_key
    newer = directory / "new-correction.srt"
    newer.write_text("1\n00:00:00,000 --> 00:00:01,000\nChanged.\n")
    current = services.artifacts.register(
        newer, kind="srt", role="correction", session_id=source.id, metadata={"language": "en"}
    )
    project = _read(project_case)
    assert project["source_status"]["source_changed"] is True
    assert project["source_status"]["pinned_checkpoint"]["artifact_id"] == checkpoint.id
    assert project["source_status"]["current_correction"]["artifact_id"] == current.id
    assert (
        project["branches"][0]["source_checkpoint_artifact_id"]
        == branches[0]["source_checkpoint_artifact_id"]
    )
    assert project["branches"][0]["settings"]["subtitles"]["origin"] == "unknown_historical"
    assert (
        project["branches"][1]["settings"]["subtitles"]["origin"] == "automatic_language_defaults"
    )


def test_produced_translation_is_separate_from_review_and_subtitle_export(project_case):
    services = project_case[0]
    branch = _branches(project_case)[0]
    artifact, revision_id = _translation(services, branch)
    state = _read(project_case)["branches"][0]["readiness"]
    assert state["translation"]["status"] == "completed"
    assert state["translation"]["artifact_id"] == artifact.id
    assert state["review"]["status"] == "needs_review"
    assert state["review"]["revision_id"] == revision_id
    assert state["exports"]["subtitles"]["status"] == "ready"
    with services.database.session() as db:
        db.get(DocumentRevision, revision_id).reviewed = True
    assert _read(project_case)["branches"][0]["readiness"]["review"]["status"] == "completed"
    captured = services.workflows.resolve_stage(
        branch["session_id"],
        "export",
        {
            "export_mode": "subtitles",
            "subtitle_mode": "translation",
            "subtitle_format": "srt",
            "subtitle_selection": "translation",
        },
    )
    with services.database.session() as db:
        source = db.get(Artifact, artifact.id)
        directory = services.paths.managed_path(source.relative_path).parent
    snapshot = build_output_settings_snapshot(
        captured.payload["settings"], captured.payload["resolved_settings_snapshot"]
    )
    wrong_file = directory / "english-source-export.srt"
    wrong_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n")
    wrong = services.artifacts.register(
        wrong_file,
        kind="srt",
        role="export_subtitle_source",
        session_id=branch["session_id"],
        parent_ids=[branch["source_checkpoint_artifact_id"]],
        metadata={"language": "en", "output_settings": snapshot},
    )
    wrong_readiness = _read(project_case)["branches"][0]["readiness"]["exports"]["subtitles"]
    assert wrong_readiness["status"] == "stale"
    assert wrong_readiness["artifact_ids"] == [wrong.id]
    assert wrong_readiness["matching_artifact_ids"] == []
    file = directory / "export.srt"
    file.write_text("exported")
    exported = services.artifacts.register(
        file,
        kind="srt",
        role="export_subtitle_translation",
        session_id=branch["session_id"],
        parent_ids=[artifact.id],
        metadata={
            "output_settings": build_output_settings_snapshot(
                captured.payload["settings"], captured.payload["resolved_settings_snapshot"]
            )
        },
    )
    status = _read(project_case)["branches"][0]["readiness"]["exports"]["subtitles"]
    assert status["status"] == "completed"
    assert status["matching_artifact_ids"] == [exported.id]
    snapshot = services.workspace_settings.get(branch["session_id"], "subtitles")
    services.workspace_settings.patch(
        branch["session_id"],
        "subtitles",
        snapshot["revision"],
        {"max_cps": 4, "language_defaults": False},
    )
    stale = _read(project_case)["branches"][0]["readiness"]["exports"]["subtitles"]
    assert stale["status"] == "stale"
    assert set(stale["artifact_ids"]) == {exported.id, wrong.id}
    assert stale["matching_artifact_ids"] == []


def test_copied_effective_limits_and_target_settings_mismatch(project_case):
    services, _, _, source, _, _ = project_case
    with services.database.session() as db:
        db.add(
            AppSetting(
                key="defaults.subtitles", value_json={"max_chars_per_line": 42, "max_cps": 15}
            )
        )
    branches = _branches(project_case, carry=True)
    assert branches[0]["settings"]["subtitles"]["origin"] == "copied_source"
    assert branches[0]["settings"]["subtitles"]["effective"]["max_cps"] == 15
    assert branches[0]["settings"]["creation_origin"]["source_session_id"] == source.id
    snapshot = services.workspace_settings.get(branches[0]["session_id"], "translation")
    services.workspace_settings.patch(
        branches[0]["session_id"], "translation", snapshot["revision"], {"target_language": "ko"}
    )
    changed = _read(project_case)["branches"]
    assert changed[0]["target_language"] == "ja"
    assert changed[0]["effective_target_language"] == "ko"
    assert changed[0]["target_language_matches_settings"] is False
    assert changed[0]["readiness"]["translation"]["status"] == "blocked"
    assert "target_language_settings_mismatch" in changed[0]["readiness"]["translation"]["reasons"]
    assert changed[1]["target_language_matches_settings"] is True


def test_active_failed_jobs_and_http_replay_are_enriched(project_case):
    services = project_case[0]
    branches = _branches(project_case)
    with services.database.session() as db:
        active = Job(
            session_id=branches[0]["session_id"],
            kind="dubbing.translate",
            status="running",
            progress=0.4,
            progress_detail="Batch 2",
        )
        failed = Job(
            session_id=branches[1]["session_id"],
            kind="dubbing.translate",
            status="failed",
            error_code="provider_error",
            error_message="Backend failed",
        )
        db.add_all([active, failed])
        db.flush()
        active_id, failed_id = active.id, failed.id
    state = _read(project_case)["branches"]
    assert state[0]["readiness"]["translation"]["status"] == "running"
    assert state[0]["readiness"]["translation"]["active_job_id"] == active_id
    assert state[0]["readiness"]["translation"]["progress"] == 0.4
    assert state[1]["readiness"]["translation"]["status"] == "blocked"
    assert state[1]["readiness"]["translation"]["job_id"] == failed_id
    assert state[1]["readiness"]["translation"]["failure"]["message"] == "Backend failed"
    services, client, headers, source, checkpoint, _ = project_case
    replay = client.post(
        f"/api/v1/sessions/{source.id}/translation-project",
        json={"checkpoint_artifact_id": checkpoint.id, "expected_revision": source.revision},
        headers={**headers, "Idempotency-Key": "readiness-project-key"},
    )
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert "source_status" in replay.get_json()["project"]


def test_voice_language_coverage_and_secret_redaction(project_case):
    services = project_case[0]
    branches = _branches(project_case, voiceover=True)
    with services.database.session() as db:
        for branch, service, model in zip(
            branches,
            ("Silero", "Future service", "Silero"),
            ("v3_en", "future-model", "v3_de"),
            strict=True,
        ):
            setting = db.get(SessionSetting, (branch["session_id"], "tts"))
            setting.value_json = {
                **setting.value_json,
                "service": service,
                "model": model,
                "silero_model": model,
                "voice": "narrator",
                "api_key": "secret-never-return",
                "external_server_url": "https://private.example/?token=secret",
            }
    state = _read(project_case)
    ja, pl, de = [b["readiness"] for b in state["branches"]]
    assert ja["voice"]["status"] == "blocked"
    assert ja["generation"]["status"] == "blocked"
    assert "does not support 'ja'" in ja["voice"]["reasons"][0]
    assert ja["voice"]["language_support"]["decision"] == "unsupported"
    assert pl["voice"]["coverage_unverified"] is True
    assert pl["voice"]["status"] == "ready"
    assert de["voice"]["language_support"]["decision"] == "supported"
    assert "secret-never-return" not in str(state)
    assert "private.example" not in str(state)
    assert ja["exports"]["subtitles"]["status"] != "not_requested"


def test_stale_speech_plan_and_generation_run_identity(project_case):
    services = project_case[0]
    branch = _branches(project_case, voiceover=True)[0]
    artifact, revision_id = _translation(services, branch, reviewed=True)
    plan = services.generation.create_plan(
        branch["session_id"],
        source_revision_id=revision_id,
        segments=[{"text": "Translated.", "start_ms": 0, "end_ms": 1000}],
        settings={
            "_source_artifact_id": artifact.id,
            "_source_content_hash": artifact.content_hash,
        },
    )
    with services.database.session() as db:
        run = GenerationRun(
            session_id=branch["session_id"],
            plan_revision_id=plan["active_revision_id"],
            status="failed",
        )
        db.add(run)
        db.flush()
        run_id = run.id
        translated = db.get(Artifact, artifact.id)
        translated.content_hash = "changed-upstream-hash"
    state = _read(project_case)["branches"][0]["readiness"]
    assert state["speech_plan"]["status"] == "stale"
    assert state["speech_plan"]["revision_id"] == plan["active_revision_id"]
    assert state["speech_plan"]["content_signature"]
    assert state["generation"]["generation_run_id"] == run_id
    assert state["exports"]["subtitles"]["generation_run_id"] is None


def test_trashed_voiceover_branch_does_not_break_other_readiness(project_case):
    services = project_case[0]
    branches = _branches(project_case, voiceover=True)
    with services.database.session() as db:
        db.get(SessionRecord, branches[0]["session_id"]).trashed_at = utcnow()
    project = _read(project_case)
    assert project["branches"][0]["readiness"]["generation"]["status"] == "blocked"
    assert project["branches"][0]["readiness"]["generation"]["session_unavailable"] is True
    assert "session_unavailable" not in project["branches"][1]["readiness"]["generation"]


def test_flat_subtitle_export_stays_ready_when_configured_voiceover_is_blocked(project_case):
    services = project_case[0]
    branch = _branches(project_case, voiceover=True)[0]
    translated, revision_id = _translation(services, branch, reviewed=True)
    plan = services.generation.create_plan(
        branch["session_id"],
        source_revision_id=revision_id,
        segments=[{"text": "Translated.", "start_ms": 0, "end_ms": 1000}],
        settings={
            "_source_artifact_id": translated.id,
            "_source_content_hash": translated.content_hash,
        },
    )
    with services.database.session() as db:
        run = GenerationRun(
            session_id=branch["session_id"],
            plan_revision_id=plan["active_revision_id"],
            status="failed",
        )
        db.add(run)
        db.flush()
        run_id = run.id
    override = {
        "export_mode": "subtitles",
        "subtitle_mode": "translation",
        "subtitle_format": "srt",
        "subtitle_selection": "translation",
    }
    with pytest.raises(ValueError, match="Only a completed generation run"):
        services.workflows.resolve_stage(
            branch["session_id"], "export", {"generation_run_id": run_id}
        )
    captured = services.workflows.resolve_stage(branch["session_id"], "export", override)
    assert captured.source_artifact_id == translated.id
    assert captured.source_content_hash == translated.content_hash
    assert {key: captured.payload["settings"][key] for key in override} == override
    assert len(captured.payload["settings_hash"]) == 64
    assert (
        len(
            build_output_settings_snapshot(
                captured.payload["settings"], captured.payload["resolved_settings_snapshot"]
            )["settings_hash"]
        )
        == 64
    )
    with patch.object(
        services.workflows, "resolve_stage", wraps=services.workflows.resolve_stage
    ) as resolve:
        readiness = _read(project_case)["branches"][0]["readiness"]["exports"]
    assert readiness["configured"]["status"] == "blocked"
    assert "Only a completed generation run" in readiness["configured"]["reasons"][0]
    assert readiness["subtitles"]["status"] == "ready"
    assert readiness["subtitles"]["source_artifact_id"] == captured.source_artifact_id
    assert any(
        call.args == (branch["session_id"], "export", override) for call in resolve.call_args_list
    )
