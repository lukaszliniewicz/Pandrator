"""Focused persistence and contract checks for project sound plans."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    Base,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
    SoundVariant,
)
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan  # noqa: F401
from pandrator.web.sound_project_schemas import SoundAnchor, SoundPlayback
from pandrator.web.sound_project_service import ProjectSoundService


@pytest.fixture
def sound_project():
    with tempfile.TemporaryDirectory() as temporary:
        database = Database(Path(temporary) / "project.sqlite")
        Base.metadata.create_all(database.engine)
        paths = SimpleNamespace(managed_path=lambda relative: Path(temporary) / relative)
        service = ProjectSoundService(database, cast(ArtifactService, SimpleNamespace(paths=paths)))
        with database.immediate_session() as session:
            record = SessionRecord(name="Project", workflow_kind="audiobook")
            target = SessionRecord(name="Fork", workflow_kind="audiobook")
            session.add_all([record, target])
            session.flush()
            speech_plan = GenerationPlan(session_id=record.id)
            session.add(speech_plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=speech_plan.id, revision_number=1, content_hash="first"
            )
            session.add(revision)
            session.flush()
            speech_plan.active_revision_id = revision.id
            segment = GenerationSegment(plan_revision_id=revision.id, ordinal=0, text="Hello world")
            artifact = Artifact(kind="audio", relative_path="sound.wav", content_hash="sha256-test")
            session.add_all([segment, artifact])
            session.flush()
            variant = SoundVariant(
                artifact_id=artifact.id,
                state="ready",
                provenance_json={"display_name": "Bell"},
                duration_seconds=1.0,
            )
            session.add(variant)
            session.flush()
            ids = record.id, target.id, revision.id, segment.id, variant.id
        yield database, service, ids
        database.dispose()


def _create(variant_id: str, segment_id: str, revision: int = 0):
    return {
        "expected_revision": revision,
        "variant_id": variant_id,
        "anchor": {"kind": "before_segment", "segment_id": segment_id},
    }


def test_lazy_page_budget_and_immutable_versions(sound_project):
    database, service, (project_id, _target, _speech, segment_id, variant_id) = sound_project
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", count)
    try:
        empty = service.get_plan(project_id)
        assert empty["revision"] == 0 and empty["cues"]["items"] == []
        assert len(statements) <= 3
        statements.clear()
        with database.immediate_session() as session:
            added = service.add_in_session(session, project_id, _create(variant_id, segment_id))
        assert added["revision"] == 1
        first_id = added["plan_id"]
        with database.immediate_session() as session:
            changed = service.update_in_session(
                session,
                project_id,
                added["cue"]["logical_id"],
                {"expected_revision": 1, "included": False},
            )
        assert changed["revision"] == 2
        with database.session() as session:
            assert (
                service.snapshot_in_session(session, project_id, revision=1)["cues"][0]["included"]
                is True
            )
        statements.clear()
        page = service.get_plan(project_id, limit=1)
        assert len(statements) <= 3
        assert page["cues"]["items"][0]["sound"]["content_hash"] == "sha256-test"
        with database.immediate_session() as session:
            with pytest.raises(RevisionConflict):
                service.remove_in_session(session, project_id, added["cue"]["logical_id"], 1)
        with database.immediate_session() as session:
            removed = service.remove_in_session(session, project_id, added["cue"]["logical_id"], 2)
        assert removed["revision"] == 3
        with database.immediate_session() as session:
            restored = service.restore_in_session(
                session, project_id, {"expected_revision": 3, "source_plan_id": first_id}
            )
        assert restored["revision"] == 4
        assert service.get_plan(project_id)["cues"]["items"][0]["included"] is True
        with database.session() as session:
            assert (
                session.scalar(
                    select(ProjectSoundPlan).where(ProjectSoundPlan.id == first_id)
                ).revision
                == 1
            )
            assert (
                session.scalar(
                    select(ProjectSoundCue).where(ProjectSoundCue.plan_id == first_id)
                ).included
                is True
            )
    finally:
        event.remove(database.engine, "before_cursor_execute", count)


def test_anchor_playback_and_global_variant_guards(sound_project):
    database, service, (project_id, _target, _speech, segment_id, variant_id) = sound_project
    with pytest.raises(ValidationError):
        SoundAnchor.model_validate({"kind": "source_time", "time_seconds": -1})
    with pytest.raises(ValidationError):
        SoundAnchor.model_validate(
            {"kind": "after_word", "segment_id": segment_id, "quote": " ", "occurrence": 1}
        )
    with pytest.raises(ValidationError):
        SoundPlayback.model_validate({"mode": "insert", "offset_ms": -1})
    with pytest.raises(ValidationError):
        SoundAnchor.model_validate({"kind": "next_pause", "segment_id": segment_id})
    with database.immediate_session() as session:
        word = service.add_in_session(
            session,
            project_id,
            {
                "expected_revision": 0,
                "variant_id": variant_id,
                "anchor": {
                    "kind": "after_word",
                    "segment_id": segment_id,
                    "quote": "Hello",
                    "occurrence": 1,
                },
            },
        )
    assert word["cue"]["placement_status"] == "needs_placement"
    with database.immediate_session() as session:
        pause = service.add_in_session(
            session,
            project_id,
            {
                "expected_revision": 1,
                "variant_id": variant_id,
                "anchor": {
                    "kind": "next_pause",
                    "segment_id": segment_id,
                    "quote": "Hello",
                    "occurrence": 1,
                },
            },
        )
    assert pause["cue"]["placement_status"] == "needs_placement"
    with database.immediate_session() as session:
        variant = session.get(SoundVariant, variant_id)
        session.get(Artifact, variant.artifact_id).session_id = project_id
    with database.immediate_session() as session:
        with pytest.raises(ValueError, match="global audio artifact"):
            service.add_in_session(session, project_id, _create(variant_id, segment_id, revision=2))
    assert service.get_plan(project_id)["revision"] == 2


def test_rejects_non_audio_or_stale_variant_and_retains_global_pin(sound_project):
    database, service, (project_id, _target, _speech, segment_id, variant_id) = sound_project
    with database.immediate_session() as session:
        variant = session.get(SoundVariant, variant_id)
        artifact = session.get(Artifact, variant.artifact_id)
        artifact.kind = "document"
    with database.immediate_session() as session:
        with pytest.raises(ValueError, match="audio artifact"):
            service.add_in_session(session, project_id, _create(variant_id, segment_id))
    with database.immediate_session() as session:
        variant = session.get(SoundVariant, variant_id)
        artifact = session.get(Artifact, variant.artifact_id)
        artifact.kind = "audio"
        artifact.state = "stale"
    with database.immediate_session() as session:
        with pytest.raises(ValueError, match="current global audio"):
            service.add_in_session(session, project_id, _create(variant_id, segment_id))
    with database.immediate_session() as session:
        variant = session.get(SoundVariant, variant_id)
        session.get(Artifact, variant.artifact_id).state = "current"
        service.add_in_session(session, project_id, _create(variant_id, segment_id))
    with pytest.raises(IntegrityError):
        with database.immediate_session() as session:
            session.delete(session.get(SoundVariant, variant_id))
    with pytest.raises(IntegrityError):
        with database.immediate_session() as session:
            variant = session.get(SoundVariant, variant_id)
            session.delete(session.get(Artifact, variant.artifact_id))
    with database.session() as session:
        assert session.get(SoundVariant, variant_id) is not None
        assert session.get(Artifact, session.get(SoundVariant, variant_id).artifact_id) is not None
        assert session.scalar(
            select(ProjectSoundCue).where(ProjectSoundCue.variant_id == variant_id)
        )


def test_cursor_paging_and_segment_filter(sound_project):
    database, service, (project_id, _target, speech_id, segment_id, variant_id) = sound_project
    with database.immediate_session() as session:
        other = GenerationSegment(plan_revision_id=speech_id, ordinal=1, text="Second")
        session.add(other)
        session.flush()
        other_id = other.id
        first = service.add_in_session(session, project_id, _create(variant_id, segment_id))
        second = service.add_in_session(session, project_id, _create(variant_id, other_id, 1))
    first_page = service.get_plan(project_id, limit=1)
    assert first_page["cues"]["next_cursor"] is not None
    second_page = service.get_plan(project_id, cursor=first_page["cues"]["next_cursor"], limit=1)
    assert second_page["cues"]["next_cursor"] is None
    assert {
        first_page["cues"]["items"][0]["logical_id"],
        second_page["cues"]["items"][0]["logical_id"],
    } == {first["cue"]["logical_id"], second["cue"]["logical_id"]}
    filtered = service.get_plan(project_id, segment_ids=[segment_id])
    assert [cue["logical_id"] for cue in filtered["cues"]["items"]] == [first["cue"]["logical_id"]]
    with pytest.raises(ValueError, match="cursor"):
        service.get_plan(project_id, cursor="x" * 36)


def test_restore_old_basis_marks_pending_and_remains_editable(sound_project):
    database, service, (project_id, _target, _speech, segment_id, variant_id) = sound_project
    with database.immediate_session() as session:
        first = service.add_in_session(session, project_id, _create(variant_id, segment_id))
        old_plan_id = first["plan_id"]
        speech_plan = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == project_id)
        )
        next_revision = GenerationPlanRevision(
            plan_id=speech_plan.id, revision_number=2, content_hash="second"
        )
        session.add(next_revision)
        session.flush()
        speech_plan.active_revision_id = next_revision.id
        next_revision_id = next_revision.id
    with database.immediate_session() as session:
        service.rebase_in_session(session, project_id, next_revision_id, {})
    with database.immediate_session() as session:
        restored = service.restore_in_session(
            session, project_id, {"expected_revision": 2, "source_plan_id": old_plan_id}
        )
    assert restored["speech_plan_revision_id"] == next_revision_id
    assert restored["revision"] == 3
    pending = service.get_plan(project_id)["cues"]["items"][0]
    assert pending["placement_status"] == "needs_placement"
    assert pending["anchor"]["segment_id"] == segment_id
    with database.immediate_session() as session:
        edited = service.update_in_session(
            session, project_id, pending["logical_id"], {"expected_revision": 3, "included": False}
        )
    assert edited["revision"] == 4
    assert edited["cue"]["placement_status"] == "needs_placement"
    with database.session() as session:
        assert (
            session.get(ProjectSoundPlan, old_plan_id).speech_plan_revision_id != next_revision_id
        )


def test_rebase_and_clone_preserve_pins_without_guessing_segments(sound_project):
    database, service, (project_id, target_id, speech_id, segment_id, variant_id) = sound_project
    with database.immediate_session() as session:
        added = service.add_in_session(session, project_id, _create(variant_id, segment_id))
        source_plan = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == project_id)
        )
        next_revision = GenerationPlanRevision(
            plan_id=source_plan.id, revision_number=2, content_hash="second"
        )
        session.add(next_revision)
        session.flush()
        source_plan.active_revision_id = next_revision.id
        next_segment = GenerationSegment(
            plan_revision_id=next_revision.id, ordinal=0, text="Changed"
        )
        session.add(next_segment)
        session.flush()
        next_revision_id, next_segment_id = next_revision.id, next_segment.id
    with database.immediate_session() as session:
        receipt = service.rebase_in_session(session, project_id, next_revision_id, {})
    assert receipt["revision"] == 2
    page = service.get_plan(project_id)
    assert page["cues"]["items"][0]["placement_status"] == "needs_placement"
    assert page["cues"]["items"][0]["anchor"]["segment_id"] == segment_id
    with database.immediate_session() as session:
        receipt = service.rebase_in_session(
            session, project_id, next_revision_id, {(segment_id, "before_segment"): next_segment_id}
        )
    assert receipt["revision"] == 3
    assert (
        service.get_plan(project_id)["cues"]["items"][0]["anchor"]["segment_id"] == next_segment_id
    )
    with database.immediate_session() as session:
        clone = service.clone_in_session(
            session, project_id, target_id, {next_segment_id: "untrusted"}
        )
    assert clone["revision"] == 1
    cloned_cue = service.get_plan(target_id)["cues"]["items"][0]
    assert cloned_cue["variant_id"] == added["cue"]["variant_id"]
    assert cloned_cue["placement_status"] == "needs_placement"
    assert cloned_cue["anchor"]["origin_session_id"] == project_id


def test_source_time_uses_overlay_and_clone_media_policy(sound_project):
    database, service, (project_id, target_id, _speech_id, _segment_id, variant_id) = sound_project
    with database.immediate_session() as session:
        session.get(SessionRecord, project_id).workflow_kind = "voiceover"
        session.get(SessionRecord, target_id).workflow_kind = "voiceover"
    with database.immediate_session() as session:
        with pytest.raises(ValueError, match="overlay"):
            service.add_in_session(
                session,
                project_id,
                {
                    "expected_revision": 0,
                    "variant_id": variant_id,
                    "anchor": {"kind": "source_time", "time_seconds": 1.25},
                    "playback": {"mode": "insert"},
                },
            )
    writes = []

    def capture_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture_write)
    try:
        with database.immediate_session() as session:
            added = service.add_in_session(
                session,
                project_id,
                {
                    "expected_revision": 0,
                    "variant_id": variant_id,
                    "anchor": {"kind": "source_time", "time_seconds": 1.25},
                },
            )
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_write)
    assert writes and all("project_sound_" in statement for statement in writes)
    assert added["cue"]["playback"]["mode"] == "overlay"
    assert added["cue"]["placement_status"] == "ready"
    with database.immediate_session() as session:
        service.clone_in_session(session, project_id, target_id, {}, carry_media_assets=False)
    assert service.get_plan(target_id)["cues"]["items"][0]["placement_status"] == "needs_placement"
