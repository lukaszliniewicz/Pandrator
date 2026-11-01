"""Current annotation intent survives real copy, member review and generation."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator.web.projects import copy_publication
from pandrator.web.projects.copy_contracts import ProjectCopyError
from pandrator.web.projects.copy_performance_capture import capture_current_performance
from pandrator.web.speech_plan_context import plan_signature
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_project_null_speech_copy import capture_source, null_source
from tests.test_project_output_copy import snapshot_for
from tests.test_project_sound_copy import _copy
from tests.test_retained_preview_workspace import MODEL
from tests.test_settings_workspace_access import put
from tests.test_voice_lifecycle_idempotency import silent_wav


def annotation(direction):
    return {"decision": "steer", "delivery": {"instruction": direction}}


def command(client, csrf, root, path, values, *, method="post", status=200):
    return body(getattr(client, method)(root + path, json=values,
        headers=headers(csrf, str(uuid4()))), status)


@pytest.fixture
def performance(design):
    f, u = design, design.users
    source = null_source(f)
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=u.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        speech = f.services.generation.create_plan(source["id"], source_revision_id=None,
            settings={}, segments=[{"text": text, "language": "en"}
                for text in ("The first line.", "The second line.", "The final line.")])
    with f.case.database.immediate_session() as unit:
        segments = list(unit.scalars(select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == speech["active_revision_id"])
            .order_by(m.GenerationSegment.ordinal)))
        segments[0].speech_plan_json = {"parent_revision_id": str(uuid4()),
            "speech_xml": f'<segment id="{segments[0].id}">The first line.</segment>'}
        ids = [segment.id for segment in segments]
    root = f"/api/v1/sessions/{source['id']}/performance-plans"
    body(u.alice.put(f"/api/v1/sessions/{source['id']}/generation-controls",
        json={"expected_revision": 0, "characters": [{"id": "fred", "display_name": "Fred"}]},
        headers=headers(u.ah, "performance-copy-characters")))

    def create(**options):
        return command(u.alice, u.ah, root, "",
            {"expected_plan_revision_id": speech["active_revision_id"], **options}, status=201)

    adopted = create(annotation_format="xml", purpose="combined")
    items = [{"segment_id": key, "speech_xml":
        f'<segment id="{key}">' + (
            '<ins>calm</ins><dialogue><speaker ref="fred">The first line.</speaker></dialogue>'
            if index == 0 else ("The second line." if index == 1 else "The final line."))
        + '</segment>', "locked": index == 0} for index, key in enumerate(ids)]
    changed = command(u.alice, u.ah, root, "/" + adopted["id"],
        {"expected_version": adopted["version"], "items": items}, method="patch")
    command(u.alice, u.ah, root, "/" + adopted["id"] + "/adopt",
        {"expected_version": changed["version"], "enable": True})
    draft = create(mode="passive", annotation_format="pssml", batch_size=1,
        model_name="private-analysis-model", instructions="private-analysis-instructions")
    lease = command(u.alice, u.ah, root, "/" + draft["id"] + "/claim", {})
    command(u.alice, u.ah, root, "/" + draft["id"] + "/batches/" + lease["batch_id"] + "/submit",
        {"lease_token": lease["lease_token"], "items": [{"segment_id": ids[1],
            "annotation": annotation("automatic direction")} ]})
    draft = body(u.alice.get(root + "/" + draft["id"]))
    command(u.alice, u.ah, root, "/" + draft["id"],
        {"expected_version": draft["version"], "items": [{"segment_id": ids[1],
            "annotation": annotation("manual direction")} ]}, method="patch")
    speakers = create(mode="manual", purpose="speakers", annotation_format="xml")
    with f.case.database.immediate_session() as unit:
        completed = unit.scalar(select(m.PerformanceBatch).where(
            m.PerformanceBatch.performance_plan_id == draft["id"],
            m.PerformanceBatch.status == "completed"))
        assert completed is not None
        completed.usage_json = {"private_usage": "private-analysis-usage"}
        unit.get_one(m.PerformancePlan, draft["id"]).job_id = "private-analysis-job"
    return SimpleNamespace(f=f, u=u, source=source, speech=speech, ids=ids, root=root,
        adopted=adopted, draft=draft, speakers=speakers)


def test_registered_copy_member_edit_adopt_recopy_and_generation(performance, monkeypatch):
    p, f, u = performance, performance.f, performance.u
    body(put(u.owner, u.oh, "services.tts", {"provider_configs": [{
        "id": "audio_cpp", "enabled": True, "models": [MODEL], "default_model": MODEL,
        "api_base": "https://speech.invalid", "connection_mode": "external",
        "api_key": "synthetic-performance-copy-key", "credential_backend": "database",
    }]}, revision=1, workspace=u.workspace_id))
    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    frozen = json.loads(snapshot.state_json)["rows"]["performance_plans"]
    assert len(frozen) == 3
    assert all(row["version"] == 1 and row["job_id"] is None for row in frozen)
    assert all(row["settings_json"]["mode"] == "manual" for row in frozen)
    assert all(marker not in snapshot.state_json for marker in (
        "private-analysis-", "automatic direction", "performance_batches"))
    partial = next(row for row in frozen if row["id"] == p.draft["id"])
    assert set(partial["manual_annotations_json"]) == set(p.ids[:2])
    assert partial["manual_annotations_json"][p.ids[1]]["delivery"]["instruction"] == "manual direction"
    with f.case.database.snapshot_session() as unit:
        source_rows = deepcopy([dict(row) for row in unit.execute(select(m.PerformancePlan.__table__)
            .where(m.PerformancePlan.session_id == p.source["id"])).mappings()])
    copied, _preview = _copy(f, p.source, u.alice, u.ah, u.workspace_id, "copy-performance-intent")
    target = copied["source_session_id"]
    with f.case.database.snapshot_session() as unit:
        plans = list(unit.scalars(select(m.PerformancePlan).where(m.PerformancePlan.session_id == target)))
        assert len(plans) == 3
        assert unit.scalar(select(func.count()).select_from(m.PerformanceBatch)
            .where(m.PerformanceBatch.performance_plan_id.in_([row.id for row in plans]))) == 0
        segments = list(unit.scalars(select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == plans[0].plan_revision_id)
            .order_by(m.GenerationSegment.ordinal)))
        target_ids = [row.id for row in segments]
        draft = next(row for row in plans if row.settings_json["annotation_format"] == "pssml")
        draft_id, revision_id = draft.id, draft.plan_revision_id
        assert draft.base_signature == plan_signature(unit, revision_id)
        assert p.ids[0] not in draft.units_json[0]["speech_xml"]
        assert target_ids[0] in draft.units_json[0]["speech_xml"]
        assert 'ref="fred"' in draft.units_json[0]["speech_xml"]
        assert not (set(p.ids) & set(draft.manual_annotations_json))
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    base = f"/api/v1/sessions/{target}/performance-plans"
    assert u.alice.get(base).status_code == 404
    assert len(body(u.bob.get(base))["items"]) == 3
    changed = command(u.bob, u.bh, base, "/" + draft_id,
        {"expected_version": 1, "items": [{"segment_id": target_ids[1],
            "annotation": annotation("member direction")} ]}, method="patch")
    command(u.bob, u.bh, base, "/" + draft_id + "/adopt",
        {"expected_version": changed["version"], "accept_unanalysed": True, "enable": True})
    second, _ = _copy(f, {"project_id": copied["project_id"]}, u.bob, u.bh,
        u.workspace_id, "recopy-performance-intent")
    with f.case.database.snapshot_session() as unit:
        # The earlier adopted row is now historical, and the untouched speaker
        # draft has an old adopted baseline. Only the current adopted intent moves.
        twice = list(unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == second["source_session_id"])))
        assert len(twice) == 1 and twice[0].status == "adopted"
        assert twice[0].base_signature == plan_signature(unit, twice[0].plan_revision_id)
        signature = plan_signature(unit, revision_id)
    body(u.bob.post(f"/api/v1/sessions/{target}/generation-plan/review",
        json={"revision_id": revision_id, "content_signature": signature},
        headers=headers(u.bh, "review-performance-copy")))
    calls = []

    def post(url, **options):
        assert url == "https://speech.invalid/v1/audio/speech"
        calls.append(options["json"])
        response = requests.Response()
        response.status_code, response.url = 200, url
        response._content, response._content_consumed = silent_wav(), True
        response.headers["Content-Type"] = "audio/wav"
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests.Session, "post", lambda _session, url, **kw: post(url, **kw))
    admission = body(u.bob.post(f"/api/v1/sessions/{target}/generation-admissions", json={},
        headers=headers(u.bh, "generate-performance-copy")), 202)
    worker = Worker(f.services.jobs, "performance-copy-worker", f.services.workflow_handlers.handlers())
    assert worker.run_once()
    accepted = f.services.jobs.get(admission["id"])
    assert accepted.status == "succeeded", accepted.error_message
    assert worker.run_once()
    generated = f.services.jobs.get(accepted.result_json["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert len(calls) == 3
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.GenerationRun, accepted.result_json["generation_run_id"])
        chosen = run.settings_snapshot_json["performance_snapshot"]
        assert set(chosen["annotations"]) == set(target_ids)
        assert chosen["annotations"][target_ids[1]]["annotation"]["delivery"]["instruction"] == "member direction"
        assert source_rows == [dict(row) for row in unit.execute(select(m.PerformancePlan.__table__)
            .where(m.PerformancePlan.session_id == p.source["id"])).mappings()]
        audio = unit.scalar(select(m.AudioTake.artifact_id).where(m.AudioTake.generation_run_id == run.id))
        assert audio is not None
    assert u.bob.get(f"/api/v1/artifacts/{audio}/content").status_code == 200


@pytest.mark.parametrize("kind", ["stale_adopted", "leased", "limit"])
def test_unavailable_current_performance_blocks_before_units_or_annotation_bodies(performance, kind):
    p, f = performance, performance.f
    with f.case.database.immediate_session() as unit:
        if kind == "stale_adopted":
            unit.get_one(m.PerformancePlan, p.adopted["id"]).base_signature = "stale"
        elif kind == "leased":
            batch = unit.scalar(select(m.PerformanceBatch).where(
                m.PerformanceBatch.performance_plan_id == p.draft["id"],
                m.PerformanceBatch.status == "pending"))
            assert batch is not None
            batch.status, batch.lease_expires_at = "leased", m.utcnow() + timedelta(minutes=5)
    snapshot = capture_source(f, p.source) if kind == "limit" else None
    rows = json.loads(snapshot.state_json)["rows"] if snapshot else {
        "sessions": [{"id": p.source["id"]}],
        "generation_plans": [{"session_id": p.source["id"],
            "active_revision_id": p.speech["active_revision_id"]}],
    }
    statements = []

    def observe(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with f.case.database.snapshot_session() as unit:
            if kind == "limit":
                with pytest.raises(ProjectCopyError) as error:
                    capture_current_performance(unit, rows=rows, max_rows=1)
                assert error.value.code == "copy_limit_exceeded"
            else:
                saved, unavailable = capture_current_performance(unit, rows=rows, max_rows=100)
                assert saved == [] and unavailable == 1
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    assert not any("SELECT performance_plans.id, performance_plans.units_json" in sql
        or "performance_batches.annotations_json" in sql for sql in statements)


def test_stale_draft_and_historical_body_are_not_loaded_or_copied(performance):
    p, f = performance, performance.f
    with f.case.database.immediate_session() as unit:
        for status, signature in (("draft", "stale"), ("superseded", "historical")):
            unit.add(m.PerformancePlan(session_id=p.source["id"],
                plan_revision_id=p.speech["active_revision_id"], base_signature=signature,
                status=status, units_json=[{"unqualified": "old-private-body"}]))
    snapshot = capture_source(f, p.source)
    assert snapshot.ready and "old-private-body" not in snapshot.state_json
    assert len(json.loads(snapshot.state_json)["rows"]["performance_plans"]) == 3


def test_absent_performance_has_one_header_and_zero_bodies(design):
    f = design
    source = null_source(f)
    statements = []

    def observe(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with f.case.database.snapshot_session() as unit:
            assert capture_current_performance(unit, rows={"sessions": [source]}, max_rows=0) == ([], 0)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1
    assert not any(key in reads[0] for key in ("units_json", "annotations_json", "settings_json"))
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql in statements)


def test_format14_keeps_frozen_shape_and_rejects_new_family_before_staging():
    state = {"schema_version": 14, "binding_fields": {}, "unsupported_counts": {},
        "destination_connections": [], "rows": {family: [] for family in copy_publication._FIELDS
            if family != "performance_plans"}}
    snapshot = snapshot_for(state)
    original = snapshot.state_json, snapshot.fingerprint
    copy_publication._decode(snapshot)
    assert (snapshot.state_json, snapshot.fingerprint) == original
    forged = {**state, "rows": {**state["rows"], "performance_plans": []}}
    with pytest.raises(ProjectCopyError):
        copy_publication._decode(snapshot_for(forged))
