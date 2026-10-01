"""Deferred multilingual intent and explicit project activation."""

from __future__ import annotations

import asyncio
import tempfile
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    Document,
    DocumentRevision,
    Job,
    OutcomePlan,
    OutcomePlanHistory,
    SessionRecord,
    SessionSetting,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.multilingual_setup import MultilingualSetup
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import build_runtime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def case():
    with tempfile.TemporaryDirectory() as root:
        prepare_web_test_data_root(root)
        tokens = BootstrapTokenStore()
        app = create_app(data_root=root, testing=True, bootstrap_tokens=tokens)
        client = app.test_client()
        csrf = client.post("/api/v1/auth/bootstrap", json={"token": tokens.issue()}).get_json()["csrf_token"]
        services = app.extensions["pandrator"]
        yield client, {"X-CSRF-Token": csrf}, services
        services["database"].dispose()


def _source(client, headers, *, setup=None, name="Languages", **extra):
    body = {
        "name": name, "workflow_kind": "subtitles", "source_language": "auto",
        "included_stages": ["transcribe", "correct", "translate", "generate_audio", "export"],
        **extra,
    }
    if setup is not None:
        body["multilingual_setup"] = setup
    response = client.post("/api/v1/sessions", json=body, headers={**headers, "Idempotency-Key": f"create-{name}-key"})
    return response


def _reviewed_correction(services, session_id: str, *, language="en", reviewed=True):
    database, paths, artifacts = (services[key] for key in ("database", "paths", "artifacts"))
    with database.session() as db:
        record = db.get(SessionRecord, session_id)
        directory = paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "correction.srt"
    file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    checkpoint = artifacts.register(file, kind="srt", role="correction", session_id=session_id, metadata={"language": language})
    with database.session() as db:
        document = Document(session_id=session_id, stage="correction", language=language)
        db.add(document)
        db.flush()
        revision = DocumentRevision(
            document_id=document.id, revision_number=1,
            content_hash=checkpoint.content_hash, reviewed=reviewed,
        )
        db.add(revision)
        db.flush()
        document.active_revision_id = revision.id
        artifact = db.get(Artifact, checkpoint.id)
        artifact.metadata_json = {**artifact.metadata_json, "revision_id": revision.id}
    return checkpoint


def test_create_validation_persists_deferred_plan_without_jobs(case):
    client, headers, services = case
    setup = {"target_languages": ["PL", "de_DE"]}
    response = _source(client, headers, setup=setup)
    assert response.status_code == 201, response.get_json()
    created = response.get_json()
    assert created["included_stages_json"] == ["transcribe", "correct", "export"]
    with services["database"].session() as db:
        setting = db.get(SessionSetting, (created["id"], "multilingual_setup"))
        assert setting.value_json == {
            "target_languages": ["pl", "de-de"],
            "generate_voiceover": False,
            "keep_source_subtitles": True,
        }
        assert db.scalars(select(Job).where(Job.session_id == created["id"])).all() == []
    state = client.get(f"/api/v1/sessions/{created['id']}/translation-project").get_json()
    assert state["setup_state"] == "awaiting_correction"
    assert state["setup"] == setting.value_json
    assert state["correction_checkpoint_artifact_id"] is None
    assert state["setup_blocked_reason"] is None


@pytest.mark.parametrize("change", [
    {"workflow_kind": "audiobook"},
    {"target_language": "fr"},
    {"source_language": "pl"},
    {"included_stages": ["transcribe", "export"]},
    {"multilingual_setup": {"target_languages": ["pl", "PL"]}},
    {"multilingual_setup": {"target_languages": ["auto"]}},
])
def test_invalid_create_does_not_replace_existing(case, change):
    client, headers, services = case
    original = _source(client, headers, name="Same").get_json()
    body = {
        "name": "Same", "workflow_kind": "subtitles", "source_language": "en",
        "included_stages": ["correct"], "overwrite_session_id": original["id"],
        "multilingual_setup": {"target_languages": ["pl"]}, **change,
    }
    response = client.post("/api/v1/sessions", json=body, headers={**headers, "Idempotency-Key": "invalid-replacement-key"})
    assert response.status_code == 422, response.get_json()
    with services["database"].session() as db:
        assert db.get(SessionRecord, original["id"]).trashed_at is None
        assert len(db.scalars(select(SessionRecord)).all()) == 1


def test_update_revision_disable_and_project_guard(case):
    client, headers, services = case
    source = _source(client, headers, name="Update").get_json()
    url = f"/api/v1/sessions/{source['id']}"
    setup = {"target_languages": ["pl"]}
    changed = client.patch(url, json={"multilingual_setup": setup}, headers={**headers, "If-Match": "1"})
    assert changed.status_code == 200, changed.get_json()
    stale = client.patch(url, json={"multilingual_setup": None}, headers={**headers, "If-Match": "1"})
    assert stale.status_code == 409
    disabled = client.patch(url, json={"multilingual_setup": None}, headers={**headers, "If-Match": "2"})
    assert disabled.status_code == 200
    with services["database"].session() as db:
        assert db.get(SessionSetting, (source["id"], "multilingual_setup")) is None
    # An active project source cannot alter its saved plan.
    added = client.patch(url, json={"multilingual_setup": setup}, headers={**headers, "If-Match": "3"})
    checkpoint = _reviewed_correction(services, source["id"])
    project = client.post(
        f"{url}/translation-project",
        json={"checkpoint_artifact_id": checkpoint.id, "expected_revision": added.get_json()["revision"]},
        headers={**headers, "Idempotency-Key": "project-guard-key"},
    )
    assert project.status_code == 200, project.get_json()
    guarded = client.patch(url, json={"multilingual_setup": None}, headers={**headers, "If-Match": "4"})
    assert guarded.status_code in {409, 422}


def test_update_revalidates_effective_source_before_mutation(case):
    client, headers, services = case
    source = _source(client, headers, name="Effective", setup={"target_languages": ["pl"]}).get_json()
    url = f"/api/v1/sessions/{source['id']}"
    for change in (
        {"workflow_kind": "audiobook"},
        {"source_language": "pl"},
        {"target_language": "de"},
        {"included_stages": ["translate", "export"]},
    ):
        response = client.patch(url, json=change, headers={**headers, "If-Match": "1"})
        assert response.status_code in {409, 422}, response.get_json()
        with services["database"].session() as db:
            current = db.get(SessionRecord, source["id"])
            assert current.revision == 1
            assert current.workflow_kind == "subtitles"
            assert current.source_language == "auto"
            assert current.target_language is None
            assert current.included_stages_json == ["transcribe", "correct", "export"]


def test_standalone_fork_does_not_inherit_source_plan(case):
    client, headers, services = case
    source = _source(client, headers, name="Standalone", setup={"target_languages": ["pl"]}).get_json()
    checkpoint = _reviewed_correction(services, source["id"])
    response = client.post(
        f"/api/v1/sessions/{source['id']}/forks",
        json={"checkpoint_artifact_id": checkpoint.id,
              "expected_revision": source["revision"], "name": "Standalone child"},
        headers={**headers, "Idempotency-Key": "standalone-fork-key"},
    )
    assert response.status_code == 201, response.get_json()
    sid = response.get_json()["id"]
    with services["database"].session() as db:
        assert db.get(SessionSetting, (sid, "multilingual_setup")) is None
    assert client.get(f"/api/v1/sessions/{sid}/translation-project").get_json()[
        "setup_state"] == "none"


def test_readiness_and_atomic_planned_branches(case):
    client, headers, services = case
    setup = {"target_languages": ["pl", "de"], "generate_voiceover": False, "keep_source_subtitles": True}
    source = _source(client, headers, setup=setup, name="Planned", workflow_kind="media_edit").get_json()
    url = f"/api/v1/sessions/{source['id']}/translation-project"
    unreviewed = _reviewed_correction(services, source["id"], reviewed=False)
    ready = client.get(url).get_json()
    assert ready["setup_state"] == "ready"
    with services["database"].session() as db:
        revision_id = db.get(Artifact, unreviewed.id).metadata_json["revision_id"]
        document = db.get(Document, db.get(DocumentRevision, revision_id).document_id)
        document.active_revision_id = None
    blocked = client.get(url).get_json()
    assert blocked["setup_state"] == "blocked"
    assert "revision changed" in blocked["setup_blocked_reason"]
    with services["database"].session() as db:
        document = db.get(Document, db.get(DocumentRevision, revision_id).document_id)
        document.active_revision_id = revision_id
    ready = client.get(url).get_json()
    assert ready["setup_state"] == "ready"
    assert ready["setup_blocked_reason"] is None
    assert ready["correction_checkpoint_artifact_id"] == unreviewed.id
    payload = {"checkpoint_artifact_id": unreviewed.id, "expected_revision": source["revision"], "create_planned_branches": True}
    write_headers = {**headers, "Idempotency-Key": "planned-project-key"}
    response = client.post(url, json=payload, headers=write_headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["setup_state"] == "active"
    branches = response.get_json()["project"]["branches"]
    assert [branch["target_language"] for branch in branches] == ["pl", "de"]
    assert all(branch["workflow_kind"] == "subtitles" for branch in branches)
    assert client.post(url, json=payload, headers=write_headers).headers["Idempotency-Replayed"] == "true"
    assert client.get(url).get_json()["setup_state"] == "active"
    branch_url = f"/api/v1/sessions/{branches[0]['session_id']}"
    change_branch = client.patch(
        branch_url, json={"multilingual_setup": None},
        headers={**headers, "If-Match": "1"},
    )
    assert change_branch.status_code in {409, 422}
    with services["database"].session() as db:
        assert len(db.scalars(select(Job)).all()) == 0
        for branch in branches:
            sid, language = branch["session_id"], branch["target_language"]
            record = db.get(SessionRecord, sid)
            assert record.included_stages_json == ["translate", "export"]
            assert db.get(SessionSetting, (sid, "multilingual_setup")) is None
            assert db.get(SessionSetting, (sid, "translation")).value_json["target_language"] == language
            for section in ("tts", "output"):
                assert db.get(SessionSetting, (sid, section)).value_json["language"] == language
            output = db.get(SessionSetting, (sid, "output")).value_json
            assert output["subtitle_selection"] == "dual"
            assert output["audio_mode"] == "preserve"
            assert output["export_mode"] == "subtitles"
            assert output["subtitle_mode"] == "none"
            outcome = db.get(OutcomePlan, sid).value_json
            assert outcome["export"]["target_language"] == language
            assert outcome["export"]["subtitles"] == "dual"
            assert outcome["export"]["audio"] == "preserve"
            assert outcome["export"]["mode"] == "subtitles"
            assert outcome["export"]["subtitle_mode"] == "none"
            assert outcome["export"]["audio_mode"] == "preserve"
            assert outcome["inputs"]["generation"] == "translation"
            for section in ("tts", "output"):
                settings = client.get(f"/api/v1/sessions/{sid}/settings/{section}")
                assert settings.status_code == 200, settings.get_json()
                assert settings.get_json()["effective"]["language"] == language
            resolved = client.post(
                f"/api/v1/sessions/{sid}/settings/resolve",
                json={"sections": ["tts", "output"]}, headers=headers,
            )
            assert resolved.status_code == 200, resolved.get_json()
            assert resolved.get_json()["value"]["tts"]["language"] == language
            assert resolved.get_json()["value"]["output"]["language"] == language


def test_planned_second_fork_failure_rolls_back_project_and_files(case):
    client, headers, services = case
    source = _source(client, headers, setup={"target_languages": ["pl", "de"]}, name="Rollback").get_json()
    checkpoint = _reviewed_correction(services, source["id"])
    before = {p.name for p in services["paths"].sessions.iterdir()}
    forks = services["session_forks"]
    original = forks.fork_in_session
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("Injected second fork failure")
        return original(*args, **kwargs)

    with patch.object(forks, "fork_in_session", side_effect=fail_second):
        response = client.post(
            f"/api/v1/sessions/{source['id']}/translation-project",
            json={"checkpoint_artifact_id": checkpoint.id, "expected_revision": source["revision"], "create_planned_branches": True},
            headers={**headers, "Idempotency-Key": "planned-failure-key"},
        )
    assert response.status_code == 422, response.get_json()
    assert {p.name for p in services["paths"].sessions.iterdir()} == before
    with services["database"].session() as db:
        assert db.scalars(select(TranslationProject)).all() == []
        assert db.scalars(select(TranslationProjectBranch)).all() == []
        assert len(db.scalars(select(SessionRecord)).all()) == 1


def test_setup_dto_is_strict():
    assert MultilingualSetup(target_languages=["PT_br"]).target_languages == ["pt-br"]
    with pytest.raises(ValueError):
        MultilingualSetup(target_languages=["pt"], generate_voiceover="yes")


def test_voiceover_create_setup_seeds_correction_only_outcome(case):
    client, headers, services = case
    response = _source(
        client, headers,
        name="NativeVoiceoverSource", workflow_kind="voiceover",
        setup={"target_languages": ["fr"], "generate_voiceover": True},
    )
    assert response.status_code == 201, response.get_json()
    sid = response.get_json()["id"]
    outcome = client.get(f"/api/v1/sessions/{sid}/outcome-plan").get_json()
    keys = {step["key"] for step in outcome["pipeline"]}
    assert "correct" in keys
    assert "translate" not in keys
    assert "generate_audio" not in keys
    assert "apply_rvc" not in keys
    assert outcome["value"]["deliverables"]["voiceover"] is False
    assert outcome["value"]["export"]["audio"] == "preserve"
    with services["database"].session() as db:
        assert db.get(OutcomePlan, sid).revision == 1
        assert db.get(SessionRecord, sid).revision == 1


def test_enabling_existing_voiceover_reconciles_outcome_once(case):
    client, headers, services = case
    source = _source(client, headers, name="EnableVoiceover", workflow_kind="voiceover").get_json()
    sid = source["id"]
    initial = client.get(f"/api/v1/sessions/{sid}/outcome-plan").get_json()
    assert "generate_audio" in {step["key"] for step in initial["pipeline"]}
    response = client.patch(
        f"/api/v1/sessions/{sid}",
        json={"multilingual_setup": {"target_languages": ["fr"]}},
        headers={**headers, "If-Match": "1"},
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["revision"] == 2
    updated = client.get(f"/api/v1/sessions/{sid}/outcome-plan").get_json()
    keys = {step["key"] for step in updated["pipeline"]}
    assert "translate" not in keys
    assert "generate_audio" not in keys
    assert updated["revision"] == initial["revision"] + 1
    with services["database"].session() as db:
        history = db.scalars(select(OutcomePlanHistory).where(OutcomePlanHistory.session_id == sid)).all()
        assert len(history) == 1
        assert history[0].value_json["deliverables"]["voiceover"] is True
    preferences = client.patch(
        f"/api/v1/sessions/{sid}",
        json={"multilingual_setup": {"target_languages": ["fr", "it"]}},
        headers={**headers, "If-Match": "2"},
    )
    assert preferences.status_code == 200, preferences.get_json()
    assert preferences.get_json()["revision"] == 3
    assert client.get(f"/api/v1/sessions/{sid}/outcome-plan").get_json()[
        "revision"] == updated["revision"]


def test_voiceover_plan_sets_target_language_and_dubbing_mode(case):
    client, headers, services = case
    source = _source(
        client, headers, name="Voiceover",
        setup={"target_languages": ["fr"], "generate_voiceover": True,
               "keep_source_subtitles": False},
    ).get_json()
    checkpoint = _reviewed_correction(services, source["id"])
    response = client.post(
        f"/api/v1/sessions/{source['id']}/translation-project",
        json={"checkpoint_artifact_id": checkpoint.id,
              "expected_revision": source["revision"], "create_planned_branches": True},
        headers={**headers, "Idempotency-Key": "voiceover-plan-key"},
    )
    assert response.status_code == 200, response.get_json()
    branch = response.get_json()["project"]["branches"][0]
    assert branch["workflow_kind"] == "voiceover"
    with services["database"].session() as db:
        sid = branch["session_id"]
        assert db.get(SessionRecord, sid).included_stages_json == ["translate", "generate_audio", "export"]
        output = db.get(SessionSetting, (sid, "output")).value_json
        assert output["subtitle_selection"] == "translation"
        assert output["audio_mode"] == "dubbing_only"
        assert output["export_mode"] == "media"
        assert output["language"] == "fr"
        outcome = db.get(OutcomePlan, sid).value_json
        assert outcome["deliverables"]["voiceover"] is True
        assert outcome["transformations"]["generate_audio"] is True
        assert outcome["export"]["subtitles"] == "translation"
        assert outcome["export"]["audio"] == "generated"
        assert outcome["export"]["target_language"] == "fr"
        assert outcome["export"]["mode"] == "media"
        assert outcome["export"]["subtitle_mode"] == output["subtitle_mode"]
        assert outcome["export"]["audio_mode"] == "dubbing_only"
    for section in ("tts", "output"):
        settings = client.get(f"/api/v1/sessions/{sid}/settings/{section}")
        assert settings.get_json()["effective"]["language"] == "fr"
    resolved = client.post(
        f"/api/v1/sessions/{sid}/settings/resolve",
        json={"sections": ["tts", "output"]}, headers=headers,
    )
    assert resolved.status_code == 200, resolved.get_json()
    assert resolved.get_json()["value"]["tts"]["language"] == "fr"
    assert resolved.get_json()["value"]["output"]["language"] == "fr"


def test_known_correction_language_rechecked_and_no_plan_rejected(case):
    client, headers, services = case
    source = _source(client, headers, name="LanguageConflict",
                     setup={"target_languages": ["pl"]}).get_json()
    checkpoint = _reviewed_correction(services, source["id"], language="pl")
    url = f"/api/v1/sessions/{source['id']}/translation-project"
    blocked = client.get(url).get_json()
    assert blocked["setup_state"] == "blocked"
    assert blocked["correction_checkpoint_artifact_id"] == checkpoint.id
    assert "repeats" in blocked["setup_blocked_reason"]
    response = client.post(
        url,
        json={"checkpoint_artifact_id": checkpoint.id,
              "expected_revision": source["revision"], "create_planned_branches": True},
        headers={**headers, "Idempotency-Key": "source-target-key"},
    )
    assert response.status_code == 422
    with services["database"].session() as db:
        assert db.scalars(select(TranslationProject)).all() == []
    plain = _source(client, headers, name="NoPlan").get_json()
    plain_cp = _reviewed_correction(services, plain["id"])
    response = client.post(
        f"/api/v1/sessions/{plain['id']}/translation-project",
        json={"checkpoint_artifact_id": plain_cp.id,
              "expected_revision": plain["revision"], "create_planned_branches": True},
        headers={**headers, "Idempotency-Key": "missing-plan-key"},
    )
    assert response.status_code == 422


def test_native_mcp_setup_lifecycle_with_disposable_api(case, tmp_path):
    http, headers, services = case

    def request_json(path, *, method="GET", body=None, idempotency_key=None,
                     if_match_revision=None, **_kwargs):
        if path == "/api/v1/sessions" and method == "POST":
            setup = body.get("multilingual_setup")
            if setup is not None:
                assert setup["target_languages"] in (["pl"], ["it"])
        request_headers = dict(headers)
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        if if_match_revision is not None:
            request_headers["If-Match"] = str(if_match_revision)
        response = http.open(path, method=method, json=body, headers=request_headers)
        data = response.get_json()
        if response.status_code >= 400:
            error = data["error"]
            raise PandratorMcpError(error["code"], error["message"])
        return data

    application = object.__new__(ApplicationClient)
    application._request_json = request_json
    runtime = build_runtime(McpSettings(
        target_name="unconfigured", configuration_path=tmp_path / "absent.json",
    ))
    runtime.startup_error = None
    runtime.application = application

    async def exercise():
        async with Client(build_server(runtime), raise_exceptions=True) as mcp:
            tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}

            async def call(name, arguments):
                Draft202012Validator(tools[name].input_schema).validate(arguments)
                result = await mcp.call_tool(name, arguments)
                assert not result.is_error
                return result.structured_content["result"]

            created = await call("pandrator_create_session", {
                "name": "Native languages", "workflow_kind": "subtitles",
                "source_language": "auto", "included_stages": ["correct", "translate"],
                "multilingual_setup": {"target_languages": ["PL"]},
                "idempotency_key": "native-language-create-key",
            })
            sid = created["id"]
            pending = await call("pandrator_get_translation_project", {"session_id": sid})
            assert pending["setup_state"] == "awaiting_correction"
            disabled = await call("pandrator_update_session", {
                "session_id": sid, "expected_revision": created["revision"],
                "multilingual_setup": None,
                "idempotency_key": "native-language-disable-key",
            })
            assert (await call("pandrator_get_translation_project", {"session_id": sid}))[
                "setup_state"] == "none"
            updated = await call("pandrator_update_session", {
                "session_id": sid, "expected_revision": disabled["revision"],
                "multilingual_setup": {"target_languages": ["pl", "de"]},
                "idempotency_key": "native-language-update-key",
            })
            assert updated["revision"] == 3
            renamed = await call("pandrator_update_session", {
                "session_id": sid, "expected_revision": updated["revision"],
                "name": "Native languages revised",
                "idempotency_key": "native-language-rename-key",
            })
            assert renamed["revision"] == 4
            checkpoint = _reviewed_correction(services, sid)
            ready = await call("pandrator_get_translation_project", {"session_id": sid})
            assert ready["setup_state"] == "ready"
            project = await call("pandrator_create_translation_project", {
                "session_id": sid, "checkpoint_artifact_id": checkpoint.id,
                "expected_revision": renamed["revision"], "create_planned_branches": True,
                "idempotency_key": "native-language-project-key",
            })
            assert len(project["project"]["branches"]) == 2
            assert (await call("pandrator_get_translation_project", {"session_id": sid}))[
                "setup_state"] == "active"
            direct_voiceover = await call("pandrator_create_session", {
                "name": "Native voiceover setup", "workflow_kind": "voiceover",
                "source_language": "en", "included_stages": ["correct", "translate", "generate_audio"],
                "multilingual_setup": {"target_languages": ["it"], "generate_voiceover": True},
                "idempotency_key": "native-voiceover-create-key",
            })
            direct_workflow = await call(
                "pandrator_get_workflow", {"session_id": direct_voiceover["id"]},
            )
            included = {stage["key"] for stage in direct_workflow["stages"] if stage["included"]}
            assert "correct" in included
            assert "translate" not in included
            assert "generate_audio" not in included
            plain_voiceover = await call("pandrator_create_session", {
                "name": "Native voiceover existing", "workflow_kind": "voiceover",
                "source_language": "en", "included_stages": ["correct", "translate", "generate_audio"],
                "idempotency_key": "native-voiceover-plain-key",
            })
            enabled = await call("pandrator_update_session", {
                "session_id": plain_voiceover["id"],
                "expected_revision": plain_voiceover["revision"],
                "multilingual_setup": {"target_languages": ["it"], "generate_voiceover": True},
                "idempotency_key": "native-voiceover-enable-key",
            })
            assert enabled["revision"] == plain_voiceover["revision"] + 1
            patched_workflow = await call(
                "pandrator_get_workflow", {"session_id": plain_voiceover["id"]},
            )
            patched_included = {stage["key"] for stage in patched_workflow["stages"] if stage["included"]}
            assert "correct" in patched_included
            assert "translate" not in patched_included
            assert "generate_audio" not in patched_included

    asyncio.run(exercise())
