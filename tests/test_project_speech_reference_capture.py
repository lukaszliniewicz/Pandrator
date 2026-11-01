"""Disposable, offline acceptance for saved-reference capture and publication."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest
from sqlalchemy import delete, event, func, select

from pandrator.web import project_speech_reference_capture as capture_module
from pandrator.web import speech_inputs
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.models import Artifact, SessionRecord, Voice, VoiceSample
from pandrator.web.project_speech_reference_capture import (
    ProjectSpeechReferenceCapture,
    capture_project_speech_reference,
    prepare_project_speech_reference,
    publish_project_speech_reference,
)
from pandrator.web.project_speech_references import (
    get_project_speech_reference,
    put_project_speech_reference,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.speech_reference_models import ProjectSpeechReference
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_input_contracts import (
    PreparedSpeechInput,
    SpeechInputError,
    SpeechInputOwner,
    canonical_json,
)
from tests.test_retained_speech_inputs import Harness
from tests.test_retained_speech_inputs import harness as harness


def allow(session) -> None:
    assert not session.autoflush


def forbidden(*_args, **_kwargs):
    raise AssertionError("This path must not read source material or prepare files.")


def session_id(h: Harness) -> str:
    assert h.owner.session_id is not None
    return h.owner.session_id


@pytest.fixture
def project(harness: Harness) -> Harness:
    h = harness
    with h.database.immediate_session() as session:
        record = SessionRecord(name="Saved speech", workflow_kind="audiobook")
        root = create_session_project(session, record, workspace_id=h.owner.workspace_id)
        h.owner = SpeechInputOwner(h.owner.workspace_id, root.id, record.id)
    return h


def capture(h: Harness, *, selector=None, settings=None, **kwargs) -> ProjectSpeechReferenceCapture:
    with h.database.snapshot_session() as session:
        return capture_project_speech_reference(
            session,
            h.paths,
            session_id(h),
            "tts",
            selector=selector if selector is not None else {"voice_id": h.voice_id},
            settings=h.settings if settings is None else settings,
            authorization_guard=kwargs.pop("authorization_guard", allow),
            source_authorization_guard=kwargs.pop("source_authorization_guard", allow),
            **kwargs,
        )


def prepare(h: Harness, captured: ProjectSpeechReferenceCapture) -> PreparedSpeechInput | None:
    return prepare_project_speech_reference(h.database, h.paths, captured, before_io=lambda: None)


def publish(h: Harness, captured: ProjectSpeechReferenceCapture, prepared=None, **kwargs):
    with h.database.immediate_session() as session:
        return publish_project_speech_reference(
            session,
            h.paths,
            captured,
            prepared,
            settings=kwargs.pop("settings", h.settings),
            authorization_guard=kwargs.pop("authorization_guard", allow),
            source_authorization_guard=kwargs.pop("source_authorization_guard", allow),
            **kwargs,
        )


def save(h: Harness, **kwargs):
    captured = capture(h, **kwargs)
    prepared = prepare(h, captured)
    stored = publish(h, captured, prepared)
    return captured, prepared, stored


def count(h: Harness) -> int:
    with h.database.snapshot_session() as session:
        return session.scalar(select(func.count()).select_from(Artifact)) or 0


@pytest.mark.parametrize("managed", [False, True])
def test_fresh_capture_and_publication_are_project_rooted_and_keep_settings_private(
    project: Harness, monkeypatch: pytest.MonkeyPatch, managed: bool
):
    h = project
    selector = {"voice_id": h.voice_id} if managed else {"voice": "chosen"}
    h.settings.update(
        audio_cpp_voice_ref={"data": "inherited-inline"},
        audio_cpp_voice_ref_hash="inherited-hash",
        audio_cpp_reference_text="Inherited text",
        provider_configs=[{"api_key": "private-test-key"}],
    )
    before = count(h)
    with monkeypatch.context() as block:
        block.setattr(speech_inputs, "read_verified_file", forbidden)
        captured = capture(h, selector=selector)
    assert count(h) == before
    assert captured.owner == h.owner and captured.expected_revision == 0
    assert captured.core_capture is not None and captured.core_capture.requires_files
    assert captured.settings == h.settings
    assert not any(key.startswith("audio_cpp_voice_ref") for key in captured.core_capture.settings)
    assert "audio_cpp_reference_text" not in captured.core_capture.settings
    assert captured.core_capture.settings["voice"] == captured.core_capture.settings["speaker"]
    assert captured.binding == {
        "adapter": "audio_cpp",
        "model": "synthetic",
        "voice": "chosen",
        "display_name": "Managed",
    }
    assert "private-test-key" not in repr(captured)
    settings_copy, selector_copy, binding_copy = (
        captured.settings,
        captured.selector,
        captured.binding,
    )
    settings_copy["provider_configs"][0]["api_key"] = "changed"
    selector_copy["voice"] = binding_copy["voice"] = "changed"
    assert captured.settings == h.settings and captured.binding["voice"] == "chosen"
    with monkeypatch.context() as block:
        block.setattr(h.database, "immediate_session", forbidden)
        prepared = prepare(h, captured)
    assert prepared is not None and count(h) == before
    with monkeypatch.context() as block:
        block.setattr(speech_inputs, "read_verified_file", forbidden)
        stored = publish(h, captured, prepared)
    assert stored.owner == h.owner and stored.pin is not None and stored.revision == 1
    assert count(h) == before + 2
    with h.database.snapshot_session() as session:
        row = session.get_one(Artifact, stored.pin["artifact_id"])
        assert (row.workspace_id, row.project_id, row.session_id) == (
            h.owner.workspace_id,
            h.owner.project_id,
            h.owner.session_id,
        )
        binding = session.get_one(ProjectSpeechReference, (h.owner.session_id, "tts")).binding_json
        assert set(binding) == {"adapter", "model", "voice", "display_name"}
        assert "private-test-key" not in canonical_json(binding)


@pytest.mark.parametrize("mutation", ["rename", "delete"])
def test_reuse_survives_origin_changes_without_source_queries_or_file_metadata(
    project: Harness, monkeypatch: pytest.MonkeyPatch, mutation: str
):
    h = project
    _, _, saved = save(h)
    with h.database.immediate_session() as session:
        if mutation == "delete":
            session.execute(delete(VoiceSample).where(VoiceSample.voice_id == h.voice_id))
            session.execute(delete(Voice).where(Voice.id == h.voice_id))
        else:
            voice = session.get_one(Voice, h.voice_id)
            voice.name = "Renamed"
            voice.metadata_json = {}
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    before = count(h)
    monkeypatch.setattr(capture_module, "resolve_binding", forbidden)
    monkeypatch.setattr(capture_module, "capture_speech_input", forbidden)
    monkeypatch.setattr(capture_module, "prepare_speech_input", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    reused = capture(h, source_authorization_guard=forbidden)
    assert reused.reusable == saved and reused.binding["voice"] == "chosen"
    assert prepare(h, reused) is None
    result = publish(h, reused, source_authorization_guard=forbidden)
    assert result == saved and count(h) == before
    assert not any("FROM voices" in q or "FROM voice_samples" in q for q in statements)
    copy = reused.reusable
    assert copy is not None and copy.pin is not None
    copy.binding["voice"] = "mutated"
    copy.pin["content_hash"] = "0" * 64
    assert reused.reusable == saved


def test_explicit_null_reuse_ignores_a_later_matching_alias(project: Harness, monkeypatch):
    h = project
    h.settings["voice"] = "builtin"
    selected = {"voice": "builtin"}
    captured, prepared, saved = save(h, selector=selected)
    assert captured.core_capture is not None and not captured.core_capture.requires_files
    assert prepared is None and saved.pin is None
    before = count(h)
    with h.database.immediate_session() as session:
        session.get_one(Voice, h.voice_id).metadata_json = {
            "providers": {
                "audio_cpp": {
                    "resource_kind": "linked_reference",
                    "status": "ready",
                    "voice_id": "builtin",
                }
            }
        }
    monkeypatch.setattr(capture_module, "capture_speech_input", forbidden)
    monkeypatch.setattr(capture_module, "resolve_binding", forbidden)
    monkeypatch.setattr(capture_module, "prepare_speech_input", forbidden)
    reused = capture(h, selector=selected, source_authorization_guard=forbidden)
    assert prepare(h, reused) is None
    assert publish(h, reused, source_authorization_guard=forbidden) == saved
    assert count(h) == before


@pytest.mark.parametrize("change", ["selector", "model", "refresh"])
def test_changed_selection_requires_source_guard_before_library_queries(project: Harness, change):
    h = project
    save(h)
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    options: dict[str, Any] = {}
    if change == "selector":
        options["selector"] = {"voice": "chosen"}
    elif change == "model":
        h.settings["model"] = "different"
    else:
        options["refresh"] = True
    denied = PermissionError("Source denied")

    def deny(session):
        assert not session.autoflush
        raise denied

    with pytest.raises(PermissionError) as error:
        capture(h, source_authorization_guard=deny, **options)
    assert error.value is denied
    assert not any("FROM voices" in q or "FROM voice_samples" in q for q in statements)


def test_refresh_captures_new_source_instead_of_reusing_old_pin(project: Harness):
    h = project
    _, _, old = save(h)
    with h.database.immediate_session() as session:
        session.get_one(VoiceSample, h.sample_id).transcript = "Changed reviewed wording."
    fresh = capture(h, refresh=True)
    assert fresh.reusable is None and fresh.core_capture is not None
    prepared = prepare(h, fresh)
    new = publish(h, fresh, prepared)
    assert new.revision == 2 and new.pin != old.pin


@pytest.mark.parametrize("stage", ["capture", "publish"])
def test_current_guard_denial_precedes_protected_selects_and_flush(project: Harness, stage):
    h = project
    captured = capture(h)
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    denied = OSError("Current authority denied")

    def deny(session):
        assert not session.autoflush
        raise denied

    with h.database.immediate_session() as session:
        session.add(Voice(workspace_id=h.owner.workspace_id, name="Pending"))
        statements.clear()
        with pytest.raises(OSError) as error:
            if stage == "capture":
                capture_project_speech_reference(
                    session,
                    h.paths,
                    session_id(h),
                    "tts",
                    selector={"voice": "chosen"},
                    settings=h.settings,
                    authorization_guard=deny,
                    source_authorization_guard=allow,
                )
            else:
                publish_project_speech_reference(
                    session,
                    h.paths,
                    captured,
                    None,
                    settings=h.settings,
                    authorization_guard=deny,
                    source_authorization_guard=allow,
                )
        assert error.value is denied and statements == []
        session.expunge_all()


@pytest.mark.parametrize("mutation", ["settings", "source", "revision", "root", "pin"])
def test_late_changes_deny_and_preserve_prepared_files_and_older_binding(
    project: Harness, mutation: str
):
    h = project
    _, _, old = save(h)
    assert old.pin is not None
    captured = capture(h, refresh=True)
    prepared = prepare(h, captured)
    assert prepared is not None
    if mutation == "settings":
        h.settings["speed"] = 1.5
    else:
        with h.database.immediate_session() as session:
            if mutation == "source":
                session.get_one(VoiceSample, h.sample_id).transcript = "Late source change"
            elif mutation == "revision":
                put_project_speech_reference(
                    session,
                    session_id(h),
                    "tts",
                    selector={"voice": "other"},
                    binding=old.binding,
                    pin=old.pin,
                    expected_revision=1,
                    authorization_guard=allow,
                )
            elif mutation == "root":
                session.get_one(SessionRecord, h.owner.session_id).trashed_at = session.get_one(
                    Artifact, h.artifact_id
                ).created_at
            else:
                assert old.pin is not None
                session.get_one(Artifact, old.pin["artifact_id"]).content_hash = "0" * 64
    before = count(h)
    with pytest.raises((RevisionConflict, SpeechInputError, AccessDenied)):
        publish(h, captured, prepared)
    assert count(h) == before
    assert h.paths.managed_path(prepared.snapshot.relative_path).exists()
    # Cleanup belongs to the caller and must preserve any committed borrowed files.
    speech_inputs.cleanup_unpublished_speech_input(h.database, prepared)
    with h.database.snapshot_session() as session:
        row = session.get_one(ProjectSpeechReference, (h.owner.session_id, "tts"))
        assert row.input_artifact_id == old.pin["artifact_id"]
        assert row.revision == (2 if mutation == "revision" else 1)


def test_no_reference_recapture_rejects_a_matching_source_appearing_before_publish(
    project: Harness,
):
    h = project
    h.settings["voice"] = "builtin"
    captured = capture(h, selector={"voice": "builtin"})
    assert prepare(h, captured) is None
    with h.database.immediate_session() as session:
        session.get_one(Voice, h.voice_id).metadata_json = {
            "providers": {
                "audio_cpp": {
                    "resource_kind": "linked_reference",
                    "status": "ready",
                    "voice_id": "builtin",
                }
            }
        }
    before = count(h)
    with pytest.raises(SpeechInputError, match="no-reference"):
        publish(h, captured)
    assert count(h) == before
    with h.database.snapshot_session() as session:
        assert (
            get_project_speech_reference(session, session_id(h), "tts", authorization_guard=allow)
            is None
        )


def test_unchanged_builtin_null_selection_creates_no_assets_and_reuses_without_revision_bump(
    project,
):
    h = project
    h.settings["voice"] = "builtin"
    before = count(h)
    _, prepared, saved = save(h, selector={"voice": "builtin"})
    assert prepared is None and saved.pin is None
    reused = capture(h, selector={"voice": "builtin"}, source_authorization_guard=forbidden)
    assert publish(h, reused, source_authorization_guard=forbidden).revision == 1
    assert count(h) == before


@pytest.mark.parametrize("disagreement", ["model", "service"])
def test_explicit_selector_identity_must_match_settings_even_on_reuse(project, disagreement):
    h = project
    selected = {"voice": "chosen", disagreement: "wrong"}
    with pytest.raises(SpeechInputError, match="disagrees"):
        capture(h, selector=selected, source_authorization_guard=forbidden)
    _, _, saved = save(h, selector={"voice": "chosen", disagreement: h.settings[disagreement]})
    h.settings[disagreement] = "other" if disagreement == "model" else "unsupported"
    with pytest.raises(SpeechInputError):
        capture(h, selector=saved.selector, source_authorization_guard=forbidden)


@pytest.mark.parametrize(
    "invalid",
    [
        "guard",
        "source_guard",
        "before_io",
        "slot",
        "selector",
        "description",
        "adapter",
        "model",
        "prepared",
    ],
)
def test_malformed_contract_values_deny(project, invalid):
    h = project
    if invalid == "before_io":
        with pytest.raises(TypeError):
            prepare_project_speech_reference(
                h.database, h.paths, capture(h), before_io=cast(Any, None)
            )
        return
    if invalid == "prepared":
        h.settings["voice"] = "builtin"
        captured = capture(h, selector={"voice": "builtin"})
        with pytest.raises(SpeechInputError):
            publish(h, captured, object())
        return
    options = {
        "session_id": h.owner.session_id,
        "slot_key": "tts",
        "selector": {"voice": "chosen"},
        "settings": h.settings,
        "authorization_guard": allow,
        "source_authorization_guard": allow,
    }
    if invalid == "guard":
        options["authorization_guard"] = None
    elif invalid == "source_guard":
        options["source_authorization_guard"] = None
    elif invalid == "slot":
        options["slot_key"] = "../tts"
    elif invalid == "selector":
        options["selector"] = {"voice": "chosen", "api_key": "secret"}
    elif invalid == "description":
        options["selector"] = {"voice_description": "A voice"}
    elif invalid == "adapter":
        h.settings["service"] = "other"
    elif invalid == "model":
        h.settings["model"] = ""
    with h.database.snapshot_session() as session:
        with pytest.raises((TypeError, ValueError)):
            capture_project_speech_reference(session, h.paths, **options)


def test_foreign_capture_owner_and_wrong_reusable_pin_are_denied(project):
    h = project
    captured, _, stored = save(h)
    reused = capture(h)
    foreign = replace(
        reused, owner=SpeechInputOwner(h.other.workspace_id, h.owner.project_id, h.owner.session_id)
    )
    with pytest.raises(SpeechInputError, match="owner"):
        publish(h, foreign)
    assert stored.pin is not None
    with h.database.immediate_session() as session:
        session.get_one(Artifact, stored.pin["artifact_id"]).content_hash = "0" * 64
    with pytest.raises(SpeechInputError, match="hash"):
        capture(h)
    assert captured.core_capture is not None


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("hint", [False, True])
def test_custom_audio_cpp_profile_registration_uses_the_selected_alias(project, managed, hint):
    h = project
    h.settings.update(
        service="Custom",
        preview_service_id="external-speech",
        preview_adapter="audio_cpp",
        openai_audio_endpoint="external-speech",
        provider_configs=[
            {"id": "external-speech", "api_base": "http://invalid.example", "adapter": "audio_cpp"}
        ],
    )
    if not hint:
        del h.settings["preview_adapter"]
    with h.database.immediate_session() as session:
        session.get_one(Voice, h.voice_id).metadata_json = {
            "providers": {
                "external-speech": {
                    "resource_kind": "linked_reference",
                    "status": "ready",
                    "voice_id": "chosen",
                }
            }
        }
    selector = {"voice_id": h.voice_id} if managed else {"voice": "chosen"}
    selector["service"] = "external-speech"
    captured, prepared, stored = save(h, selector=selector)
    assert prepared is not None and stored.pin is not None
    assert captured.binding["adapter"] == "audio_cpp" and captured.binding["voice"] == "chosen"
    reused = capture(h, selector=selector, source_authorization_guard=forbidden)
    assert publish(h, reused, source_authorization_guard=forbidden) == stored


@pytest.mark.parametrize("reference", [False, True])
def test_late_source_guard_denial_precedes_origin_queries_and_keeps_preparation(
    project, monkeypatch, reference
):
    h = project
    selected = {"voice_id": h.voice_id}
    if not reference:
        h.settings["voice"] = "builtin"
        selected = {"voice": "builtin"}
    captured = capture(h, selector=selected)
    prepared = prepare(h, captured)
    statements: list[str] = []
    event.listen(
        h.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    monkeypatch.setattr(capture_module, "capture_speech_input", forbidden)
    monkeypatch.setattr(capture_module, "publish_speech_input", forbidden)
    denied = PermissionError("Late source authority denied")

    def deny(session):
        assert not session.autoflush
        raise denied

    with pytest.raises(PermissionError) as error:
        publish(h, captured, prepared, source_authorization_guard=deny)
    assert error.value is denied
    assert not any("FROM voices" in q or "FROM voice_samples" in q for q in statements)
    if prepared is not None:
        assert h.paths.managed_path(prepared.audio.relative_path).exists()
        assert h.paths.managed_path(prepared.snapshot.relative_path).exists()
        speech_inputs.cleanup_unpublished_speech_input(h.database, prepared)


def test_caller_rollback_undoes_reference_and_artifacts_and_does_not_delete_files(project):
    h = project
    captured = capture(h)
    prepared = prepare(h, captured)
    assert prepared is not None
    before = count(h)
    with pytest.raises(RuntimeError, match="Caller rollback"):
        with h.database.immediate_session() as session:
            publish_project_speech_reference(
                session,
                h.paths,
                captured,
                prepared,
                settings=h.settings,
                authorization_guard=allow,
                source_authorization_guard=allow,
            )
            raise RuntimeError("Caller rollback")
    assert count(h) == before
    with h.database.snapshot_session() as session:
        assert (
            get_project_speech_reference(session, session_id(h), "tts", authorization_guard=allow)
            is None
        )
    assert h.paths.managed_path(prepared.snapshot.relative_path).exists()
    speech_inputs.cleanup_unpublished_speech_input(h.database, prepared)
    assert not h.paths.managed_path(prepared.snapshot.relative_path).exists()
