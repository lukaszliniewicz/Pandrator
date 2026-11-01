"""Actual cast PUT, scoped retained proofs, and atomic staged-file publication."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from pandrator.web import generation_controls as controls
from pandrator.web import models as m
from pandrator.web import project_cast_settings as writer
from pandrator.web import speech_inputs
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.project_speech_reference_capture import (
    capture_project_speech_reference,
    prepare_project_speech_reference,
    publish_project_speech_reference,
)
from pandrator.web.project_speech_references import (
    list_project_speech_references,
    put_project_speech_reference,
)
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_input_contracts import SpeechInputOwner, canonical_json
from tests.test_evidence_access_routes import body, headers
from tests.test_project_tts_retention import project as _narrator_project
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL
from tests.test_retained_preview_workspace import case as case
from tests.test_retained_preview_workspace import design as design
from tests.test_retained_preview_workspace import no_network as no_network
from tests.test_retained_preview_workspace import retained as retained
from tests.test_settings_workspace_access import put

narrator_project = _narrator_project


def forbidden(*_args, **_kwargs):
    raise AssertionError("Cast save crossed a forbidden source/file boundary")


@pytest.fixture
def project(narrator_project):
    p = narrator_project
    # Configure the current service through trusted fixture setup; cast PUT is
    # the HTTP operation under acceptance and must not inherit a narrator pin.
    with execution_actor_scope(None):
        p.r.f.services.workspace_settings.update(
            p.source["id"],
            "tts",
            0,
            {
                "service": "audio_cpp",
                "model": MODEL,
                "voice": "workshop-reference",
                "language": "en",
            },
        )
    p.path = f"/api/v1/sessions/{p.source['id']}/generation-controls"
    return p


def binding(p):
    return {"voice_id": p.r.voice["id"]}


def all_cast(p):
    choice = binding(p)
    return {
        "narrator": choice,
        "categories": {"male": choice},
        "characters": {"c-alice": choice},
        "source_speakers": {"Speaker 1": choice},
    }


def save(p, value=None, *, revision=0, key="retained-cast-save", client=None, auth=None):
    if value is None:
        value = {"characters": [{"id": "c-alice", "display_name": "Alice"}], "cast": all_cast(p)}
    return (client or p.r.f.users.alice).put(
        p.path,
        json={"expected_revision": revision, **value},
        headers=headers(auth or p.r.f.users.ah, key),
    )


def references(p):
    with p.r.f.case.database.snapshot_session() as session:
        return {
            row.slot_key: row
            for row in list_project_speech_references(
                session, p.source["id"], authorization_guard=lambda _s: None
            )
        }


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


def cast_receipts(p):
    with p.r.f.case.database.snapshot_session() as session:
        return session.scalar(
            select(func.count())
            .select_from(m.ApiIdempotency)
            .where(m.ApiIdempotency.operation_id == "updateGenerationControls")
        )


def delete_origin(p):
    with p.r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, p.r.sid))
        session.delete(session.get_one(m.Voice, p.r.voice["id"]))


def test_actual_put_deduplicates_four_slots_and_reads_bytes_only_outside_writer(
    project, monkeypatch
):
    p, r = project, project.r
    original_writer = r.f.case.database.immediate_session
    original_read = speech_inputs.read_verified_file
    original_prepare = writer.prepare_project_speech_reference
    depth, groups = 0, []

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
        groups.append(result)
        return result

    monkeypatch.setattr(r.f.case.database, "immediate_session", immediate)
    monkeypatch.setattr(speech_inputs, "read_verified_file", read)
    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    result = body(save(p))
    rows = references(p)
    assert set(rows) == {
        "cast:narrator",
        'cast:["categories","male"]',
        'cast:["characters","c-alice"]',
        'cast:["source_speakers","Speaker 1"]',
    }
    assert len(groups) == 1 and len(input_paths(p)) == 2
    assert len({canonical_json(row.pin) for row in rows.values()}) == 1
    assert all(
        row.owner
        == SpeechInputOwner(r.f.users.workspace_id, p.source["project_id"], p.source["id"])
        for row in rows.values()
    )
    assert result["revision"] == 1 and cast_receipts(p) == 1
    assert set(result["cast"]["narrator"]) == {
        "voice",
        "voice_id",
        "service",
        "model",
        "voice_description",
    }
    assert not any(
        part in str(result) for part in ("input_artifact_id", "binding_json", "base64", "api_key")
    )
    assert (
        save(
            p,
            {"cast": {"narrator": {**binding(p), "_project_speech_input": None}}},
            revision=1,
            key="public-cast-rejects-markers",
        ).status_code
        == 422
    )


def test_peer_unchanged_and_character_only_updates_survive_origin_deletion(project, monkeypatch):
    p, r = project, project.r
    first = body(save(p))
    old = references(p)
    delete_origin(p)
    statements = []
    event.listen(
        r.f.case.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input", forbidden
    )
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    monkeypatch.setattr(controls, "require_current_voice_library_read", forbidden)
    unchanged = body(
        save(p, revision=1, key="peer-unchanged-cast", client=r.f.users.bob, auth=r.f.users.bh)
    )
    assert unchanged["revision"] == 2 and references(p) == old
    edit = deepcopy(first["characters"])
    edit[0]["delivery"] = {"instruction": "Read with care."}
    changed = body(save(p, {"characters": edit}, revision=2, key="character-only-retained-cast"))
    assert changed["revision"] == 3 and references(p) == old
    assert not any("FROM voices" in sql or "FROM voice_samples" in sql for sql in statements)
    replay = save(p)
    assert body(replay) == first and replay.headers["Idempotency-Replayed"] == "true"


def test_project_credential_reuses_owned_slots_but_cannot_add_even_the_same_managed_id(
    project, monkeypatch
):
    p, r = project, project.r
    choice = binding(p)
    body(save(p, {"cast": {"narrator": choice}}))
    _, bearer = scoped_token(
        r.f.case, r.f.users.alice_id, {"app.read", "app.write"}, project=p.source["project_id"]
    )
    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input", forbidden
    )
    assert (
        body(save(p, {}, revision=1, key="project-token-reuse-cast", auth=bearer))["revision"] == 2
    )
    response = save(
        p,
        {"cast": {"narrator": choice, "categories": {"male": choice}}},
        revision=2,
        key="project-token-new-cast-slot",
        auth=bearer,
    )
    assert response.status_code == 404, response.get_json()
    assert set(references(p)) == {"cast:narrator"}


def test_server_proof_is_slot_scoped_and_requires_persisted_exact_metadata(project, monkeypatch):
    p, r = project, project.r
    body(save(p, {"cast": {"narrator": binding(p)}}))
    old = references(p)["cast:narrator"]
    monkeypatch.setattr(controls, "require_current_voice_library_read", forbidden)
    with r.f.case.database.immediate_session() as session:
        with pytest.raises(AssertionError, match="forbidden"):
            controls.save_generation_controls(
                session,
                p.source["id"],
                expected_revision=1,
                cast={"narrator": binding(p), "categories": {"male": binding(p)}},
                _retained_references={"cast:narrator": old},
            )
        with pytest.raises(RevisionConflict):
            controls.save_generation_controls(
                session,
                p.source["id"],
                expected_revision=1,
                cast={"narrator": binding(p)},
                _retained_references={"cast:narrator": replace(old, revision=old.revision + 1)},
            )
    assert body(r.f.users.alice.get(p.path))["revision"] == 1


def test_unchanged_same_selectors_keep_distinct_saved_versions(project):
    p, r = project, project.r
    selection = {"cast": {"narrator": binding(p), "categories": {"male": binding(p)}}}
    body(save(p, selection))
    old = references(p)
    with r.f.case.database.immediate_session() as session:
        session.get_one(m.VoiceSample, r.sid).transcript = "New reviewed source version."
        captured = capture_project_speech_reference(
            session,
            r.f.services.paths,
            p.source["id"],
            "cast:narrator",
            selector=binding(p),
            settings={"service": "audio_cpp", "model": MODEL, "voice": ""},
            authorization_guard=lambda _s: None,
            source_authorization_guard=lambda _s: None,
            refresh=True,
        )
    prepared = prepare_project_speech_reference(
        r.f.case.database, r.f.services.paths, captured, before_io=lambda: None
    )
    with r.f.case.database.immediate_session() as session:
        publish_project_speech_reference(
            session,
            r.f.services.paths,
            captured,
            prepared,
            settings=captured.settings,
            authorization_guard=lambda _s: None,
            source_authorization_guard=lambda _s: None,
        )
    distinct = references(p)
    assert distinct["cast:narrator"].pin != old["cast:narrator"].pin
    assert distinct['cast:["categories","male"]'].pin == old['cast:["categories","male"]'].pin
    body(save(p, selection, revision=1, key="keep-distinct-cast-versions"))
    assert references(p) == distinct


def test_null_survives_later_alias_and_removal_preserves_other_slots_and_history(
    project, monkeypatch
):
    p, r = project, project.r
    choice = {"voice": "future-reference"}
    selection = {"cast": {"narrator": choice, "categories": {"male": choice}}}
    body(save(p, selection))
    old = references(p)
    assert all(row.pin is None for row in old.values()) and input_paths(p) == []
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
        for slot in ("tts", "segment:" + str(uuid4())):
            put_project_speech_reference(
                session,
                p.source["id"],
                slot,
                selector=choice,
                binding=old["cast:narrator"].binding,
                pin=None,
                expected_revision=0,
                authorization_guard=lambda _s: None,
            )
    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input", forbidden
    )
    body(save(p, selection, revision=1, key="keep-explicit-null-cast"))
    body(save(p, {"cast": {"narrator": choice}}, revision=2, key="remove-one-cast-slot"))
    rows = references(p)
    assert rows["cast:narrator"] == old["cast:narrator"]
    assert 'cast:["categories","male"]' not in rows and "tts" in rows
    assert any(slot.startswith("segment:") for slot in rows) and input_paths(p) == []


def test_removing_reference_metadata_preserves_retained_files(project):
    p = project
    body(save(p))
    files = input_paths(p)
    body(save(p, {"cast": {}}, revision=1, key="clear-all-cast-slots"))
    assert references(p) == {}
    assert len(files) == 2 and all(
        p.r.f.services.paths.managed_path(path).is_file() for path in files
    )


@pytest.mark.parametrize("change", ["source", "settings", "ledger", "slot", "revoked"])
def test_late_changes_rollback_publication_and_clean_only_unpublished_preparation(
    project, monkeypatch, change
):
    p, r = project, project.r
    original = writer.prepare_project_speech_reference
    staged = []

    def prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        assert prepared is not None
        staged.extend([prepared.audio.relative_path, prepared.snapshot.relative_path])
        with execution_actor_scope(None):
            if change == "settings":
                r.f.services.workspace_settings.patch(p.source["id"], "tts", 1, {"max_attempts": 2})
            else:
                with r.f.case.database.immediate_session() as session:
                    if change == "source":
                        session.get_one(m.Voice, r.voice["id"]).revision += 1
                    elif change == "ledger":
                        controls.save_generation_controls(
                            session, p.source["id"], expected_revision=0, cast={}
                        )
                    elif change == "slot":
                        put_project_speech_reference(
                            session,
                            p.source["id"],
                            "cast:narrator",
                            selector=binding(p),
                            binding={
                                "adapter": "audio_cpp",
                                "model": MODEL,
                                "voice": "workshop-reference",
                                "display_name": "Concurrent",
                            },
                            pin=None,
                            expected_revision=0,
                            authorization_guard=lambda _s: None,
                        )
                    else:
                        session.delete(
                            session.get_one(
                                m.WorkspaceMembership, (r.f.users.workspace_id, r.f.users.alice_id)
                            )
                        )
        return prepared

    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    response = save(p)
    assert response.status_code in {403, 404, 409, 422}, response.get_json()
    assert input_paths(p) == [] and cast_receipts(p) == 0
    assert staged and all(not r.f.services.paths.managed_path(path).exists() for path in staged)
    assert r.path.is_file()
    with execution_actor_scope(None), r.f.case.database.snapshot_session() as session:
        value = controls.get_generation_controls(session, p.source["id"])
    assert value["revision"] == (1 if change == "ledger" else 0)
    assert len(references(p)) == (1 if change == "slot" else 0)


def test_exact_concurrent_retry_has_one_receipt_and_cleans_loser_with_independent_actor_context(
    project, monkeypatch
):
    p, r = project, project.r
    original = writer.prepare_project_speech_reference
    entered, losers, winners = False, [], []

    def prepare(*args, **kwargs):
        nonlocal entered
        prepared = original(*args, **kwargs)
        assert prepared is not None
        if not entered:
            entered = True
            losers.extend([prepared.audio.relative_path, prepared.snapshot.relative_path])
            with execution_actor_scope(None), r.f.case.app.app_context():
                winners.append(body(save(p)))
        return prepared

    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    response = save(p)
    assert body(response) == winners[0] and response.headers["Idempotency-Replayed"] == "true"
    assert cast_receipts(p) == 1 and len(input_paths(p)) == 2
    assert all(not r.f.services.paths.managed_path(path).exists() for path in losers)


def test_post_commit_error_preserves_graph_and_exact_replay_skips_all_capture(project, monkeypatch):
    p, r = project, project.r
    original_writer = r.f.case.database.immediate_session
    original_publish = writer.publish_project_speech_reference
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
            raise RuntimeError("synthetic error after cast commit")

    monkeypatch.setattr(writer, "publish_project_speech_reference", publish)
    monkeypatch.setattr(r.f.case.database, "immediate_session", late_error)
    with pytest.raises(RuntimeError, match="after cast commit"):
        save(p)
    files = input_paths(p)
    assert len(files) == 2 and all(
        r.f.services.paths.managed_path(path).is_file() for path in files
    )
    monkeypatch.setattr(writer, "capture_project_speech_reference", forbidden)
    assert save(p).headers["Idempotency-Replayed"] == "true"
    assert cast_receipts(p) == 1


@pytest.mark.parametrize("inactive", ["service", "model", "description", "prebuilt", "design"])
def test_inactive_selections_remain_public_metadata_without_retention(
    project, monkeypatch, inactive
):
    p = project
    choice = binding(p)
    if inactive == "service":
        choice["service"] = "gemini"
    elif inactive == "model":
        choice["model"] = "different-model"
    elif inactive == "description":
        choice["voice_description"] = "Warm narration"
    else:
        monkeypatch.setattr(
            writer, "resolve_capabilities", lambda _s: {"model": MODEL, "voice_mode": inactive}
        )
    monkeypatch.setattr(writer, "capture_project_speech_reference", forbidden)
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    result = body(save(p, {"cast": {"narrator": choice}}))
    assert result["cast"]["narrator"]["voice_id"] == choice["voice_id"]
    assert references(p) == {} and input_paths(p) == []


def test_inactive_unchanged_saved_choice_keeps_metadata_but_explicit_save_still_reads_library(
    project, monkeypatch
):
    p, r = project, project.r
    body(save(p, {"cast": {"narrator": binding(p)}}))
    old = references(p)
    with execution_actor_scope(None):
        r.f.services.workspace_settings.patch(
            p.source["id"],
            "tts",
            1,
            {"service": "gemini", "model": "gemini-3.8-flash-tts", "voice": "Kore"},
        )
    body(save(p, {"cast": {"narrator": binding(p)}}, revision=1, key="inactive-existing-cast"))
    assert references(p) == old
    monkeypatch.setattr(controls, "require_current_voice_library_read", forbidden)
    with pytest.raises(AssertionError, match="forbidden"):
        save(p, {"cast": {"narrator": binding(p)}}, revision=2, key="inactive-needs-normal-proof")


def test_metadata_list_is_ordered_guarded_and_does_not_check_graph_or_sources(project, monkeypatch):
    p, r = project, project.r
    body(save(p))
    statements = []
    event.listen(
        r.f.case.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    with r.f.case.database.immediate_session() as session:

        def deny(unit):
            assert not unit.autoflush
            raise PermissionError("List denied")

        statements.clear()
        with pytest.raises(PermissionError, match="List denied"):
            list_project_speech_references(session, p.source["id"], authorization_guard=deny)
        assert statements == []
        rows = list_project_speech_references(
            session, p.source["id"], authorization_guard=lambda _s: None
        )
        assert [row.slot_key for row in rows] == sorted(row.slot_key for row in rows)
        assert sum("FROM project_speech_references" in sql for sql in statements) == 1
        rows[0].binding["voice"] = "mutated"
    assert all(row.binding["voice"] == "workshop-reference" for row in references(p).values())


def test_generated_character_ids_are_frozen_between_phases_and_prepare_is_write_free(
    project, monkeypatch
):
    p, r = project, project.r
    with r.f.case.database.snapshot_session() as session:
        candidate = controls.prepare_generation_controls_update(
            session,
            p.source["id"],
            expected_revision=0,
            characters=[{"display_name": "New"}],
            cast={"narrator": binding(p)},
        )
        assert candidate["characters"][0]["id"].startswith("c-")
        assert not session.new and not session.dirty
    original = writer.prepare_project_speech_reference
    generated = []

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(
            controls,
            "uuid",
            SimpleNamespace(uuid4=lambda: pytest.fail("Character IDs regenerated in final phase")),
        )
        generated.append(True)
        return result

    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    result = body(
        save(p, {"characters": [{"display_name": "New"}], "cast": {"narrator": binding(p)}})
    )
    assert result["characters"][0]["id"].startswith("c-")
    assert len(generated) == 1


@pytest.mark.parametrize("denial", ["stale", "viewer", "foreign", "revoked"])
def test_denied_admission_never_prepares_bytes(project, monkeypatch, denial):
    p, r = project, project.r
    monkeypatch.setattr(writer, "prepare_project_speech_reference", forbidden)
    if denial == "stale":
        response = save(p, revision=9)
        assert response.status_code == 409
    elif denial == "foreign":
        private = body(
            r.f.users.owner.post(
                "/api/v1/sessions",
                json={"name": "Private owner source"},
                headers=headers(r.f.users.oh, "private-source-for-cast"),
            ),
            201,
        )
        response = r.f.users.alice.put(
            f"/api/v1/sessions/{private['id']}/generation-controls",
            json={"expected_revision": 0, "cast": {"narrator": binding(p)}},
            headers=headers(r.f.users.ah, "foreign-cast-denial"),
        )
        assert response.status_code == 404
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
        response = save(p)
        assert response.status_code == (403 if denial == "viewer" else 404), response.get_json()
    assert input_paths(p) == [] and references(p) == {} and cast_receipts(p) == 0


def test_selected_custom_profile_alias_is_an_active_compatible_cast_service(project):
    p, r = project, project.r
    body(
        put(
            r.f.users.owner,
            r.f.users.oh,
            "services.tts",
            {
                "provider_configs": [
                    {
                        "id": "external-speech",
                        "adapter": "audio_cpp",
                        "enabled": True,
                        "models": [MODEL],
                        "default_model": MODEL,
                        "api_base": "https://speech.invalid",
                        "connection_mode": "external",
                    }
                ]
            },
            revision=2,
            workspace=r.f.users.workspace_id,
        )
    )
    with execution_actor_scope(None):
        r.f.services.workspace_settings.patch(
            p.source["id"],
            "tts",
            1,
            {"service": "Custom", "openai_audio_endpoint": "external-speech"},
        )
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.Voice, r.voice["id"]).metadata_json = {
                "providers": {
                    "external-speech": {
                        "status": "ready",
                        "resource_kind": "linked_reference",
                        "voice_id": "workshop-reference",
                    }
                }
            }
    body(save(p, {"cast": {"narrator": {**binding(p), "service": "external-speech"}}}))
    row = references(p)["cast:narrator"]
    assert row.pin is not None and row.binding["voice"] == "workshop-reference"
    assert len(input_paths(p)) == 2


def test_retry_proves_recorded_ledger_owner_before_digest_or_response_lookup(project, monkeypatch):
    p, r = project, project.r
    body(save(p))
    private = body(
        r.f.users.owner.post(
            "/api/v1/sessions",
            json={"name": "Private receipt source"},
            headers=headers(r.f.users.oh, "private-cast-receipt-root"),
        ),
        201,
    )
    with execution_actor_scope(None), r.f.case.database.immediate_session() as session:
        private_ledger = controls.save_generation_controls(
            session, private["id"], expected_revision=0
        )
        receipt = session.scalar(
            select(m.ApiIdempotency).where(
                m.ApiIdempotency.operation_id == "updateGenerationControls"
            )
        )
        assert receipt is not None
        receipt.resource_id = private_ledger["id"]
    statements = []
    event.listen(
        r.f.case.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    monkeypatch.setattr(writer, "capture_project_speech_reference", forbidden)
    response = save(p)
    assert response.status_code == 404, response.get_json()
    assert not any(
        "api_idempotency.response_json" in sql or "api_idempotency.request_digest" in sql
        for sql in statements
    )


def test_pending_empty_retry_headers_preserve_in_progress_semantics(project, monkeypatch):
    p, r = project, project.r
    body(save(p))
    with r.f.case.database.immediate_session() as session:
        receipt = session.scalar(
            select(m.ApiIdempotency).where(
                m.ApiIdempotency.operation_id == "updateGenerationControls"
            )
        )
        assert receipt is not None
        receipt.state, receipt.resource_kind, receipt.resource_id = "in_progress", None, None
    monkeypatch.setattr(writer, "capture_project_speech_reference", forbidden)
    response = save(p)
    assert (
        response.status_code == 409
        and response.get_json()["error"]["code"] == "idempotency_in_progress"
    )
