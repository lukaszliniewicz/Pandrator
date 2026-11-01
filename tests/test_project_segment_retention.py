"""Actual segment PATCH retains owned inputs across edits, copies and retries."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web import project_segment_settings as writer
from pandrator.web import speech_inputs
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.project_speech_references import (
    StoredProjectSpeechReference,
    get_project_speech_reference,
    put_project_speech_reference,
)
from tests.test_evidence_access_routes import body, headers
from tests.test_project_cast_retention import narrator_project as narrator_project
from tests.test_project_cast_retention import project as _cast_project
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL
from tests.test_retained_preview_workspace import case as case
from tests.test_retained_preview_workspace import design as design
from tests.test_retained_preview_workspace import no_network as no_network
from tests.test_retained_preview_workspace import retained as retained

cast_project = _cast_project


def forbidden(*_args, **_kwargs):
    raise AssertionError("Segment editor crossed a protected source or file boundary")


@pytest.fixture
def project(cast_project):
    p = cast_project
    with execution_actor_scope(None):
        p.plan = p.r.f.services.generation.create_plan(
            p.source["id"],
            source_revision_id=None,
            settings={},
            segments=[{"text": "First original sentence."}, {"text": "Second original sentence."}],
        )
    with p.r.f.case.database.snapshot_session() as session:
        p.ids = list(
            session.scalars(
                select(m.GenerationSegment.id)
                .where(m.GenerationSegment.plan_revision_id == p.plan["active_revision_id"])
                .order_by(m.GenerationSegment.ordinal)
            )
        )
    return p


def patch(p, changes, *, index=0, identifier=None, revision=1, key=None, client=None, auth=None):
    request_headers = {**(auth or p.r.f.users.ah), "If-Match": f'"{revision}"'}
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return (client or p.r.f.users.alice).patch(
        f"/api/v1/generation-segments/{identifier or p.ids[index]}",
        json=changes,
        headers=request_headers,
    )


def batch(p, updates, *, key=None, client=None, auth=None):
    request_headers = dict(auth or p.r.f.users.ah)
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return (client or p.r.f.users.alice).patch(
        f"/api/v1/sessions/{p.source['id']}/generation-segments",
        json={"updates": updates},
        headers=request_headers,
    )


def reference(p, identifier=None):
    with p.r.f.case.database.snapshot_session() as session:
        return get_project_speech_reference(
            session,
            p.source["id"],
            "segment:" + (identifier or p.ids[0]),
            authorization_guard=lambda _s: None,
        )


def physical(p, identifier=None):
    with p.r.f.case.database.snapshot_session() as session:
        row = session.get_one(m.GenerationSegment, identifier or p.ids[0])
        return SimpleNamespace(
            **{
                column.name: getattr(row, column.name)
                for column in m.GenerationSegment.__table__.columns
            }
        )


def required_reference(p, identifier=None) -> StoredProjectSpeechReference:
    value = reference(p, identifier)
    assert value is not None
    return value


def input_paths(p):
    with p.r.f.case.database.snapshot_session() as session:
        return list(
            session.scalars(
                select(m.Artifact.relative_path).where(
                    m.Artifact.session_id == p.source["id"],
                    m.Artifact.role.in_(["speech_input", "speech_reference_audio"]),
                )
            )
        )


def receipts(p):
    with p.r.f.case.database.snapshot_session() as session:
        return session.scalar(
            select(func.count())
            .select_from(m.ApiIdempotency)
            .where(
                m.ApiIdempotency.operation_id.in_(
                    ["updateGenerationSegment", "updateGenerationSegments"]
                )
            )
        )


def test_actual_single_independent_fields_safe_projection_and_optional_retry(project):
    p, r = project, project.r
    first = patch(p, {"voice_id": r.voice["id"], "voice": "fallback-alias"})
    value = body(first)
    selected = reference(p)
    assert selected is not None and selected.pin is not None
    assert value["voice_id"] == r.voice["id"] and value["voice"] == "fallback-alias"
    assert value["retained_voice"]["binding"]["voice_id"] == r.voice["id"]
    assert value["retained_voice"]["display_name"] == r.voice["name"]
    assert not any(
        key in str(value)
        for key in ("input_artifact_id", "source_fingerprint", "base64", "api_key")
    )
    assert first.headers["ETag"] == '"2"' and receipts(p) == 0
    body(patch(p, {"voice": None}, revision=2))
    assert physical(p).voice_id == r.voice["id"] and physical(p).voice is None
    assert reference(p) == selected
    cleared = body(patch(p, {"voice_id": None}, revision=3))
    assert physical(p).voice_id is None and physical(p).voice is None
    assert reference(p) is None and "retained_voice" not in cleared
    assert len(input_paths(p)) == 2


def test_alias_null_pin_never_changes_after_later_alias_and_omitted_fields_stay_physical(
    project, monkeypatch
):
    p, r = project, project.r
    first = body(patch(p, {"voice": "future-reference"}))
    assert first["retained_voice"]["binding"]["voice"] == "future-reference"
    second = body(
        batch(p, [{"id": p.ids[1], "revision": 1, "changes": {"voice": "future-reference"}}])
    )
    assert second["items"][0]["retained_voice"]["binding"]["voice"] == "future-reference"
    assert required_reference(p, p.ids[1]).pin is None
    selected = reference(p)
    assert selected is not None and selected.pin is None
    with r.f.case.database.immediate_session() as session:
        session.get_one(m.Voice, r.voice["id"]).metadata_json = {
            "providers": {
                "audio_cpp": {
                    "status": "ready",
                    "resource_kind": "linked_reference",
                    "voice_id": "future-reference",
                }
            }
        }
    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input", forbidden
    )
    preserved = body(patch(p, {"voice_id": None}, revision=2))
    assert preserved["retained_voice"]["binding"]["voice"] == "future-reference"
    assert (
        physical(p).voice == "future-reference"
        and reference(p) == selected
        and input_paths(p) == []
    )
    body(patch(p, {"marked": True}, revision=3))
    assert reference(p) == selected
    body(patch(p, {"voice_id": None, "voice": None}, revision=4))
    assert reference(p) is None


def test_origin_deletion_peer_edit_and_exact_reselection_keep_null_fk_without_library(
    project, monkeypatch
):
    p, r = project, project.r
    body(patch(p, {"voice_id": r.voice["id"]}, key="segment-original-managed"))
    selected = reference(p)
    with r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, r.sid))
        session.delete(session.get_one(m.Voice, r.voice["id"]))
    assert physical(p).voice_id is None
    statements = []
    event.listen(
        r.f.case.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    monkeypatch.setattr("pandrator.web.project_speech_reference_capture.resolve_binding", forbidden)
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    edit = body(
        patch(
            p,
            {"text": "Peer edit after deletion."},
            revision=2,
            client=r.f.users.bob,
            auth=r.f.users.bh,
        )
    )
    assert edit["retained_voice"]["binding"]["voice_id"] == r.voice["id"]
    result = body(
        patch(p, {"voice_id": r.voice["id"]}, revision=3, key="reselect-owned-deleted-id")
    )
    assert result["voice_id"] is None and physical(p).voice_id is None
    assert reference(p) == selected
    assert not any(
        "voices.metadata_json" in sql or "voice_samples.transcript" in sql for sql in statements
    )


def test_batch_unique_staging_and_no_byte_read_in_writer_request_order(project, monkeypatch):
    p, r = project, project.r
    original_writer, original_read = (
        r.f.case.database.immediate_session,
        speech_inputs.read_verified_file,
    )
    original_prepare = writer.prepare_project_speech_reference
    depth, preparations = 0, []

    @contextmanager
    def immediate(**kwargs):
        nonlocal depth
        with original_writer(**kwargs) as session:
            depth += 1
            try:
                yield session
            finally:
                depth -= 1

    def read(*args, **kwargs):
        assert depth == 0
        return original_read(*args, **kwargs)

    def prepare(*args, **kwargs):
        assert depth == 0
        result = original_prepare(*args, **kwargs)
        preparations.append(result)
        return result

    monkeypatch.setattr(r.f.case.database, "immediate_session", immediate)
    monkeypatch.setattr(speech_inputs, "read_verified_file", read)
    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    updates = [
        {"id": identifier, "revision": 1, "changes": {"voice_id": r.voice["id"]}}
        for identifier in reversed(p.ids)
    ]
    value = body(batch(p, updates, key="unique-segment-batch-input"))
    assert [item["id"] for item in value["items"]] == list(reversed(p.ids))
    rows = [required_reference(p, identifier) for identifier in p.ids]
    assert rows[0].pin == rows[1].pin and len(preparations) == 1 and len(input_paths(p)) == 2


@pytest.mark.parametrize(
    "invalid", ["revision", "foreign", "duplicate", "blank_text", "foreign_voice"]
)
def test_entire_mixed_batch_validates_before_bytes_or_any_edits(project, monkeypatch, invalid):
    p, r = project, project.r
    updates = [
        {"id": identifier, "revision": 1, "changes": {"voice_id": r.voice["id"]}}
        for identifier in p.ids
    ]
    if invalid == "revision":
        updates[1]["revision"] = 99
    elif invalid == "foreign":
        updates[1]["id"] = str(uuid4())
    elif invalid == "duplicate":
        updates[1]["id"] = p.ids[0]
    elif invalid == "blank_text":
        updates[1]["changes"]["text"] = "   "
    else:
        updates[1]["changes"]["voice_id"] = str(uuid4())
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    response = batch(p, updates, key="all-segments-validate-first")
    assert response.status_code in {404, 409, 422}, response.get_json()
    assert all(physical(p, identifier).revision == 1 for identifier in p.ids)
    assert input_paths(p) == [] and receipts(p) == 0


def review(p):
    with p.r.f.case.database.immediate_session() as session:
        session.add(
            m.SpeechPlanReview(
                revision_id=p.plan["active_revision_id"], content_hash="synthetic-review"
            )
        )


@pytest.mark.parametrize("frozen", ["reviewed", "generated"])
def test_reviewed_edit_copy_keeps_unedited_selection_and_original_slots_and_exact_retry(
    project, frozen
):
    p, r = project, project.r
    updates = [
        {"id": identifier, "revision": 1, "changes": {"voice_id": r.voice["id"]}}
        for identifier in p.ids
    ]
    body(batch(p, updates))
    source_refs = [required_reference(p, identifier) for identifier in p.ids]
    if frozen == "reviewed":
        review(p)
    else:
        with r.f.case.database.immediate_session() as session:
            session.add(
                m.GenerationRun(
                    session_id=p.source["id"],
                    plan_revision_id=p.plan["active_revision_id"],
                    sequence_number=1,
                    status="completed",
                    settings_snapshot_json={},
                )
            )
    response = patch(
        p, {"voice_id": None, "voice": "new-alias"}, revision=2, key="retained-segment-copy-edit"
    )
    value = body(response)
    assert value["id"] != p.ids[0] and value["previous_segment_id"] == p.ids[0]
    assert (
        reference(p, p.ids[0]) == source_refs[0] and required_reference(p, value["id"]).pin is None
    )
    with r.f.case.database.snapshot_session() as session:
        active = session.scalar(
            select(m.GenerationPlan.active_revision_id).where(
                m.GenerationPlan.session_id == p.source["id"]
            )
        )
        sibling = session.scalar(
            select(m.GenerationSegment.id).where(
                m.GenerationSegment.plan_revision_id == active, m.GenerationSegment.ordinal == 1
            )
        )
    assert required_reference(p, sibling).pin == source_refs[1].pin
    retry = patch(
        p, {"voice_id": None, "voice": "new-alias"}, revision=2, key="retained-segment-copy-edit"
    )
    assert body(retry) == value and retry.headers["Idempotency-Replayed"] == "true"
    assert physical(p, p.ids[0]).voice_id == r.voice["id"]


def test_project_credential_reuses_and_clears_owned_choices_but_cannot_acquire_alias(
    project, monkeypatch
):
    p, r = project, project.r
    body(patch(p, {"voice_id": r.voice["id"]}))
    _, bearer = scoped_token(
        r.f.case, r.f.users.alice_id, {"app.read", "app.write"}, project=p.source["project_id"]
    )
    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input", forbidden
    )
    body(
        patch(p, {"voice_id": r.voice["id"]}, revision=2, auth=bearer, key="project-segment-owned")
    )
    denied = patch(
        p,
        {"voice_id": None, "voice": "new-alias"},
        revision=3,
        auth=bearer,
        key="project-segment-source-deny",
    )
    assert denied.status_code == 404
    body(
        patch(
            p,
            {"voice_id": None, "voice": None},
            revision=3,
            auth=bearer,
            key="project-segment-owned-clear",
        )
    )
    assert reference(p) is None


@pytest.mark.parametrize("change", ["source", "settings", "fk", "slot", "plan", "role"])
def test_late_source_settings_fk_slot_plan_or_role_change_rolls_back_and_cleans_staging(
    project, monkeypatch, change
):
    p, r = project, project.r
    if change == "fk":
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.GenerationSegment, p.ids[0]).voice_id = r.voice["id"]
    original_prepare = writer.prepare_project_speech_reference
    staged = []

    def prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        assert result is not None
        staged.extend([result.audio.relative_path, result.snapshot.relative_path])
        with execution_actor_scope(None):
            if change == "settings":
                r.f.services.workspace_settings.patch(p.source["id"], "tts", 1, {"max_attempts": 2})
            else:
                with r.f.case.database.immediate_session() as session:
                    if change == "source":
                        session.get_one(m.Voice, r.voice["id"]).revision += 1
                    elif change == "fk":
                        session.get_one(m.GenerationSegment, p.ids[0]).voice_id = None
                    elif change == "slot":
                        put_project_speech_reference(
                            session,
                            p.source["id"],
                            "segment:" + p.ids[0],
                            selector={"voice": "late"},
                            binding={
                                "adapter": "audio_cpp",
                                "model": MODEL,
                                "voice": "late",
                                "display_name": "Late",
                            },
                            pin=None,
                            expected_revision=0,
                            authorization_guard=lambda _s: None,
                        )
                    elif change == "plan":
                        plan = session.scalar(
                            select(m.GenerationPlan).where(
                                m.GenerationPlan.session_id == p.source["id"]
                            )
                        )
                        new = m.GenerationPlanRevision(
                            plan_id=plan.id, revision_number=2, content_hash="late"
                        )
                        session.add(new)
                        session.flush()
                        plan.active_revision_id = new.id
                    else:
                        session.delete(
                            session.get_one(
                                m.WorkspaceMembership, (r.f.users.workspace_id, r.f.users.alice_id)
                            )
                        )
        return result

    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    response = patch(p, {"voice_id": r.voice["id"]}, key="late-segment-state-change")
    assert response.status_code in {403, 404, 409, 422}, response.get_json()
    assert physical(p).revision == 1 and input_paths(p) == [] and receipts(p) == 0
    assert staged and all(not r.f.services.paths.managed_path(path).exists() for path in staged)
    assert r.path.is_file()


def test_concurrent_exact_retry_cleans_loser_with_independent_actor_context(project, monkeypatch):
    p, r = project, project.r
    original = writer.prepare_project_speech_reference
    entered, losing, winner = False, [], []

    def prepare(*args, **kwargs):
        nonlocal entered
        result = original(*args, **kwargs)
        assert result is not None
        if not entered:
            entered = True
            losing.extend([result.audio.relative_path, result.snapshot.relative_path])
            with execution_actor_scope(None), r.f.case.app.app_context():
                winner.append(
                    body(patch(p, {"voice_id": r.voice["id"]}, key="concurrent-segment-save"))
                )
        return result

    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    response = patch(p, {"voice_id": r.voice["id"]}, key="concurrent-segment-save")
    assert body(response) == winner[0] and response.headers["Idempotency-Replayed"] == "true"
    assert receipts(p) == 1 and len(input_paths(p)) == 2
    assert all(not r.f.services.paths.managed_path(path).exists() for path in losing)


def test_post_commit_error_preserves_durable_files_and_replays_without_source(project, monkeypatch):
    p, r = project, project.r
    original_writer, original_publish = (
        r.f.case.database.immediate_session,
        writer.publish_project_speech_reference,
    )
    published, failed = False, False

    def publish(*args, **kwargs):
        nonlocal published
        result = original_publish(*args, **kwargs)
        published = True
        return result

    @contextmanager
    def late_error(**kwargs):
        nonlocal failed
        with original_writer(**kwargs) as session:
            yield session
        if published and not failed:
            failed = True
            raise RuntimeError("synthetic failure after segment commit")

    monkeypatch.setattr(writer, "publish_project_speech_reference", publish)
    monkeypatch.setattr(r.f.case.database, "immediate_session", late_error)
    with pytest.raises(RuntimeError, match="after segment commit"):
        patch(p, {"voice_id": r.voice["id"]}, key="segment-after-commit")
    assert physical(p).revision == 2 and required_reference(p).pin is not None
    assert len(input_paths(p)) == 2 and all(
        r.f.services.paths.managed_path(path).is_file() for path in input_paths(p)
    )
    monkeypatch.setattr(writer, "capture_project_speech_reference", forbidden)
    assert (
        patch(p, {"voice_id": r.voice["id"]}, key="segment-after-commit").headers[
            "Idempotency-Replayed"
        ]
        == "true"
    )


def test_retired_reference_allows_text_copy_and_clear_without_inference_revival(
    project, monkeypatch
):
    p, r = project, project.r
    body(patch(p, {"voice_id": r.voice["id"]}))
    saved = required_reference(p)
    assert saved.pin is not None
    with r.f.case.database.immediate_session() as session:
        session.get_one(m.Artifact, saved.pin["artifact_id"]).state = "deleted"
    review(p)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    value = body(patch(p, {"text": "Editing retired selection."}, revision=2))
    assert (
        required_reference(p, value["id"]).pin == saved.pin
        and required_reference(p).pin == saved.pin
    )
    result = body(
        patch(
            p, {"voice_id": None, "voice": None}, identifier=value["id"], revision=value["revision"]
        )
    )
    assert reference(p, result["id"]) is None and required_reference(p).pin == saved.pin


def test_annotated_text_error_after_private_staging_rolls_back_all_publication(
    project, monkeypatch
):
    p, r = project, project.r
    with r.f.case.database.immediate_session() as session:
        row = session.get_one(m.GenerationSegment, p.ids[0])
        row.optimized_text = row.text
        row.speech_plan_json = {
            "speech_xml": f'<segment id="{row.id}"><narrator>{row.text}</narrator></segment>'
        }
    original = writer.prepare_project_speech_reference
    staged = []

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result is not None
        staged.extend([result.audio.relative_path, result.snapshot.relative_path])
        return result

    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    response = patch(
        p,
        {"voice_id": r.voice["id"], "text": "A plain change to annotated content."},
        key="annotated-segment-denial",
    )
    assert response.status_code in {409, 422}, response.get_json()
    assert physical(p).revision == 1 and reference(p) is None and receipts(p) == 0
    assert staged and all(not r.f.services.paths.managed_path(path).exists() for path in staged)


def test_default_legacy_voice_mutation_clears_stale_metadata_and_noncanonical_ids_stay_supported(
    project, monkeypatch
):
    p, r = project, project.r
    body(patch(p, {"voice_id": r.voice["id"]}))
    with execution_actor_scope(None):
        r.f.services.generation.update_segment(p.ids[0], 2, {"voice_id": None, "voice": None})
    assert reference(p) is None
    with r.f.case.database.immediate_session() as session:
        session.add(
            m.GenerationSegment(
                id="legacy-segment-id",
                plan_revision_id=p.plan["active_revision_id"],
                ordinal=9,
                text="Legacy",
            )
        )
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    value = body(patch(p, {"voice_id": r.voice["id"]}, identifier="legacy-segment-id"))
    assert value["voice_id"] == r.voice["id"] and "retained_voice" not in value


@pytest.mark.parametrize("denial", ["viewer", "foreign", "stale", "revoked"])
def test_current_denial_happens_before_material_preparation(project, monkeypatch, denial):
    p, r = project, project.r
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    if denial == "stale":
        response = patch(p, {"voice_id": r.voice["id"]}, revision=99)
        assert response.status_code == 409
    elif denial == "foreign":
        foreign = body(
            r.f.users.owner.post(
                "/api/v1/sessions",
                json={"name": "Private"},
                headers=headers(r.f.users.oh, "foreign-segment-session"),
            ),
            201,
        )
        other = r.f.services.generation.create_plan(
            foreign["id"], source_revision_id=None, settings={}, segments=[{"text": "Private body"}]
        )
        with r.f.case.database.snapshot_session() as session:
            other_id = session.scalar(
                select(m.GenerationSegment.id).where(
                    m.GenerationSegment.plan_revision_id == other["active_revision_id"]
                )
            )
        statements = []
        event.listen(
            r.f.case.database.engine,
            "before_cursor_execute",
            lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
        )
        response = batch(
            p, [{"id": other_id, "revision": 1, "changes": {"voice_id": r.voice["id"]}}]
        )
        assert response.status_code == 404
        assert not any(
            "generation_segments.text" in sql and "generation_plans.session_id" not in sql
            for sql in statements
        )
    else:
        with r.f.case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (r.f.users.workspace_id, r.f.users.alice_id))
            )
            if denial == "viewer":
                session.add(
                    m.ProjectMembership(
                        project_id=p.source["project_id"], user_id=r.f.users.alice_id, role="viewer"
                    )
                )
        response = patch(p, {"voice_id": r.voice["id"]})
        assert response.status_code == (403 if denial == "viewer" else 404), response.get_json()
    assert input_paths(p) == [] and receipts(p) == 0


def test_empty_single_preserves_legacy_noop_and_empty_batch_entry_is_rejected(project, monkeypatch):
    p = project
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    assert body(patch(p, {}))["revision"] == 2
    response = batch(p, [{"id": p.ids[0], "revision": 2, "changes": {}}])
    assert response.status_code == 422 and physical(p).revision == 2


def test_typed_plan_rejects_missing_or_mismatched_source_decisions_before_copy(
    project, monkeypatch
):
    p, r = project, project.r
    with execution_actor_scope(None), r.f.case.database.immediate_session() as session:
        planned = writer._prepare_write(
            session,
            r.f.case.database,
            r.f.services.paths,
            r.f.services.workspace_settings,
            p.source["id"],
            [{"id": p.ids[0], "revision": 1, "changes": {"marked": True}}],
        )
        monkeypatch.setattr(
            "pandrator.web.speech_plan_workspace.prepare_segment_edit_targets", forbidden
        )
        with pytest.raises(ValueError, match="decisions do not match"):
            r.f.services.generation.update_segment_in_session(
                session, p.ids[0], 1, {"marked": True}, _speech_write=replace(planned, decisions=())
            )
        with pytest.raises(ValueError, match="does not match these updates"):
            r.f.services.generation.update_segment_in_session(
                session, p.ids[0], 1, {"marked": False}, _speech_write=planned
            )
    assert physical(p).revision == 1


def test_inactive_exact_owned_reselection_keeps_deleted_fk_null_without_source_access(
    project, monkeypatch
):
    p, r = project, project.r
    body(patch(p, {"voice_id": r.voice["id"]}))
    selected = required_reference(p)
    with execution_actor_scope(None):
        r.f.services.workspace_settings.patch(
            p.source["id"],
            "tts",
            1,
            {"service": "gemini", "model": "gemini-3.8-flash-tts", "voice": "Kore"},
        )
        with r.f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.VoiceSample, r.sid))
            session.delete(session.get_one(m.Voice, r.voice["id"]))
    monkeypatch.setattr(writer, "require_current_voice_library_read", forbidden)
    result = body(patch(p, {"voice_id": r.voice["id"]}, revision=2))
    assert result["voice_id"] is None and reference(p) == selected


def test_retry_proves_recorded_original_segment_before_response_or_digest_lookup(
    project, monkeypatch
):
    p, r = project, project.r
    body(patch(p, {"voice_id": r.voice["id"]}, key="segment-receipt-owner"))
    foreign = body(
        r.f.users.owner.post(
            "/api/v1/sessions",
            json={"name": "Private receipt source"},
            headers=headers(r.f.users.oh, "foreign-segment-receipt-source"),
        ),
        201,
    )
    other = r.f.services.generation.create_plan(
        foreign["id"],
        source_revision_id=None,
        settings={},
        segments=[{"text": "Private receipt segment"}],
    )
    with r.f.case.database.immediate_session() as session:
        foreign_id = session.scalar(
            select(m.GenerationSegment.id).where(
                m.GenerationSegment.plan_revision_id == other["active_revision_id"]
            )
        )
        receipt = session.scalar(
            select(m.ApiIdempotency).where(
                m.ApiIdempotency.operation_id == "updateGenerationSegment"
            )
        )
        assert receipt is not None
        receipt.resource_id = foreign_id
    statements = []
    event.listen(
        r.f.case.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    monkeypatch.setattr(writer, "capture_project_speech_reference", forbidden)
    response = patch(p, {"voice_id": r.voice["id"]}, key="segment-receipt-owner")
    assert response.status_code == 404
    assert not any(
        "api_idempotency.response_json" in sql or "api_idempotency.request_digest" in sql
        for sql in statements
    )
