"""Atomic sound-plan hooks for generation topology changes and session forks."""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.generation_topology import GenerationTopologyService
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    AudioTake,
    Base,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    SessionRecord,
    SoundVariant,
)
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from pandrator.web.sound_project_revisions import clone_sound_plan, rebase_sound_plan
from pandrator.web.sound_project_service import ProjectSoundService


@pytest.fixture
def topology_case(tmp_path: Path):
    database = Database(tmp_path / "sound.sqlite")
    Base.metadata.create_all(database.engine)
    with database.immediate_session() as session:
        record = SessionRecord(name="Topology sound plan", workflow_kind="voiceover")
        artifact = Artifact(kind="audio", role="sound.original", relative_path="sound.wav", content_hash="test-sound")
        session.add_all([record, artifact])
        session.flush()
        variant = SoundVariant(
            artifact_id=artifact.id,
            state="ready",
            provenance_json={"display_name": "Bell"},
            duration_seconds=0.25,
        )
        session.add(variant)
        session.flush()
        session_id, variant_id = record.id, variant.id
    paths = SimpleNamespace(managed_path=lambda relative: tmp_path / relative)
    service = ProjectSoundService(
        database, cast(ArtifactService, SimpleNamespace(paths=paths))
    )
    topology = GenerationTopologyService(database, JobQueue(database))
    try:
        yield database, service, topology, session_id, variant_id
    finally:
        database.dispose()


def _add_cues(database, service, session_id, variant_id, anchors):
    receipts = []
    with database.immediate_session() as session:
        for expected_revision, anchor in enumerate(anchors):
            receipts.append(
                service.add_in_session(
                    session,
                    session_id,
                    {
                        "expected_revision": expected_revision,
                        "variant_id": variant_id,
                        "anchor": anchor,
                    },
                )
            )
    return receipts


def _cues(service, session_id):
    return {
        row["logical_id"]: row
        for row in service.get_plan(session_id)["cues"]["items"]
    }


def _count(session, model, session_id):
    return int(
        session.scalar(
            select(func.count()).select_from(model).where(model.session_id == session_id)
        )
        or 0
    )


def test_missing_or_empty_plans_do_not_create_rebase_versions(topology_case):
    database, _service, topology, session_id, _variant_id = topology_case
    created = topology.create_plan(
        session_id,
        source_revision_id=None,
        segments=[{"text": "A plan without any sound cues."}],
    )
    with database.immediate_session() as session:
        target = SessionRecord(name="Empty sound fork", workflow_kind="voiceover")
        session.add(target)
        session.flush()
        target_id = target.id
        assert rebase_sound_plan(session, session_id, created["active_revision_id"], {}) is None
        clone = clone_sound_plan(session, session_id, target_id, {})
        assert clone["cue_count"] == 0 and clone["revision"] == 0
    with database.session() as session:
        assert _count(session, ProjectSoundPlan, session_id) == 0
        assert _count(session, ProjectSoundPlan, target_id) == 0

    with database.immediate_session() as session:
        session.add(
            ProjectSoundPlan(
                session_id=session_id,
                revision=1,
                speech_plan_revision_id=created["active_revision_id"],
            )
        )
    with database.immediate_session() as session:
        assert rebase_sound_plan(session, session_id, created["active_revision_id"], {}) is None
    with database.session() as session:
        assert _count(session, ProjectSoundPlan, session_id) == 1


def test_fresh_speech_plan_preserves_sound_pins_as_pending(topology_case):
    database, service, topology, session_id, variant_id = topology_case
    created = topology.create_plan(
        session_id, source_revision_id=None, segments=[{"text": "Original wording."}]
    )
    with database.session() as session:
        segment_id = session.scalar(select(GenerationSegment.id).where(
            GenerationSegment.plan_revision_id == created["active_revision_id"]
        ))
    receipts = _add_cues(database, service, session_id, variant_id, [
        {"kind": "after_segment", "segment_id": segment_id},
        {"kind": "source_time", "time_seconds": 1.25},
    ])
    replacement = topology.create_plan(
        session_id, source_revision_id=None, segments=[{"text": "Replacement wording."}]
    )
    plan = service.get_plan(session_id)
    cues = _cues(service, session_id)
    assert plan["speech_plan_revision_id"] == replacement["active_revision_id"]
    assert {row["variant_id"] for row in cues.values()} == {variant_id}
    assert cues[receipts[0]["cue"]["logical_id"]]["placement_status"] == "needs_placement"
    assert cues[receipts[1]["cue"]["logical_id"]]["placement_status"] == "ready"


def test_empty_sound_plan_follows_a_fresh_speech_revision(topology_case):
    database, service, topology, session_id, _variant_id = topology_case
    created = topology.create_plan(
        session_id, source_revision_id=None, segments=[{"text": "Original wording."}]
    )
    with database.immediate_session() as session:
        session.add(ProjectSoundPlan(
            session_id=session_id, revision=1,
            speech_plan_revision_id=created["active_revision_id"],
        ))
    replacement = topology.create_plan(
        session_id, source_revision_id=None, segments=[{"text": "Replacement wording."}]
    )
    plan = service.get_plan(session_id)
    assert plan["revision"] == 2
    assert plan["speech_plan_revision_id"] == replacement["active_revision_id"]
    assert plan["cues"]["items"] == []


def test_split_rebases_exact_outer_edges_and_untouched_segments(topology_case):
    database, service, topology, session_id, variant_id = topology_case
    created = topology.create_plan(
        session_id,
        source_revision_id=None,
        segments=[{"text": "A first sentence."}, {"text": "A second sentence."}],
    )
    with database.session() as session:
        source_segments = list(
            session.scalars(
                select(GenerationSegment)
                .where(GenerationSegment.plan_revision_id == created["active_revision_id"])
                .order_by(GenerationSegment.ordinal)
            )
        )
    first_id, untouched_id = [row.id for row in source_segments]
    with database.immediate_session() as session:
        sound_artifact_id = session.scalar(
            select(SoundVariant.artifact_id).where(SoundVariant.id == variant_id)
        )
        take = AudioTake(
            generation_segment_id=first_id,
            artifact_id=sound_artifact_id,
            kind="tts",
            status="completed",
            is_active=True,
            settings_hash="tts-identity",
        )
        session.add(take)
        session.flush()
        take_id = take.id
    receipts = _add_cues(
        database,
        service,
        session_id,
        variant_id,
        [
            {"kind": "before_segment", "segment_id": first_id},
            {"kind": "after_segment", "segment_id": first_id},
            {"kind": "before_segment", "segment_id": untouched_id},
            {"kind": "source_time", "time_seconds": 1.25},
            {
                "kind": "after_word",
                "segment_id": first_id,
                "quote": "first",
                "occurrence": 1,
                "start": 2,
                "end": 7,
            },
        ],
    )
    before_count = len(receipts)

    result = topology.revise_topology(
        session_id,
        created["active_revision_id"],
        {"action": "split", "segment_id": first_id, "cursor": 8},
    )

    current = _cues(service, session_id)
    child_ids = result["lineage"][first_id]
    assert len(child_ids) == 2
    assert current[receipts[0]["cue"]["logical_id"]]["anchor"]["segment_id"] == child_ids[0]
    assert current[receipts[1]["cue"]["logical_id"]]["anchor"]["segment_id"] == child_ids[1]
    assert (
        current[receipts[2]["cue"]["logical_id"]]["anchor"]["segment_id"]
        == result["lineage"][untouched_id][0]
    )
    timed = current[receipts[3]["cue"]["logical_id"]]
    assert timed["anchor"]["time_seconds"] == 1.25
    assert timed["placement_status"] == "ready"
    word = current[receipts[4]["cue"]["logical_id"]]
    assert word["placement_status"] == "needs_placement"
    assert "start" not in word["anchor"] and "end" not in word["anchor"]
    assert {cue["variant_id"] for cue in current.values()} == {variant_id}
    assert len(current) == before_count
    with database.session() as session:
        preserved_take = session.get(AudioTake, take_id)
        assert preserved_take is not None
        assert preserved_take.generation_segment_id == first_id
        assert preserved_take.status == "completed"
        assert preserved_take.settings_hash == "tts-identity"
        assert session.scalar(select(func.count()).select_from(AudioTake)) == 1
        assert _count(session, ProjectSoundPlan, session_id) == before_count + 1
        assert session.scalar(
            select(ProjectSoundPlan.revision).where(
                ProjectSoundPlan.session_id == session_id,
                ProjectSoundPlan.revision == 1,
            )
        ) == 1


def test_merge_maps_external_edges_and_leaves_internal_edges_pending(topology_case):
    database, service, topology, session_id, variant_id = topology_case
    created = topology.create_plan(
        session_id,
        source_revision_id=None,
        segments=[
            {"text": "The left sentence."},
            {"text": "The right sentence."},
            {"text": "The untouched sentence."},
        ],
    )
    with database.session() as session:
        segments = list(
            session.scalars(
                select(GenerationSegment)
                .where(GenerationSegment.plan_revision_id == created["active_revision_id"])
                .order_by(GenerationSegment.ordinal)
            )
        )
    left, right, untouched = [row.id for row in segments]
    receipts = _add_cues(
        database,
        service,
        session_id,
        variant_id,
        [
            {"kind": "before_segment", "segment_id": left},
            {"kind": "after_segment", "segment_id": left},
            {"kind": "before_segment", "segment_id": right},
            {"kind": "after_segment", "segment_id": right},
            {"kind": "before_segment", "segment_id": untouched},
        ],
    )

    result = topology.revise_topology(
        session_id,
        created["active_revision_id"],
        {"action": "merge", "left_segment_id": left, "right_segment_id": right},
    )

    current = _cues(service, session_id)
    merged_id = result["lineage"][left][0]
    assert current[receipts[0]["cue"]["logical_id"]]["anchor"]["segment_id"] == merged_id
    assert current[receipts[3]["cue"]["logical_id"]]["anchor"]["segment_id"] == merged_id
    for index in (1, 2):
        cue = current[receipts[index]["cue"]["logical_id"]]
        assert cue["placement_status"] == "needs_placement"
    assert (
        current[receipts[4]["cue"]["logical_id"]]["anchor"]["segment_id"]
        == result["lineage"][untouched][0]
    )


def test_resegment_draft_does_not_rebase_until_restore_adopts_it(topology_case):
    database, service, topology, session_id, variant_id = topology_case
    with database.immediate_session() as session:
        session.get(SessionRecord, session_id).workflow_kind = "audiobook"
    created = topology.create_plan(
        session_id,
        source_revision_id=None,
        segments=[{"text": "A reasonable sentence for a range split."}],
    )
    with database.session() as session:
        source_id = session.scalar(
            select(GenerationSegment.id).where(
                GenerationSegment.plan_revision_id == created["active_revision_id"]
            )
        )
    receipt = _add_cues(
        database,
        service,
        session_id,
        variant_id,
        [{"kind": "before_segment", "segment_id": source_id}],
    )[0]
    with database.immediate_session() as session:
        draft = topology.revise_topology_in_session(
            session,
            session_id,
            created["active_revision_id"],
            {"action": "resegment", "segment_ids": [source_id], "boundaries": [18]},
        )
    assert draft["is_draft"] is True
    assert draft["active_plan_revision_id"] == created["active_revision_id"]
    assert service.get_plan(session_id)["revision"] == 1

    adopted = topology.revise_topology(
        session_id,
        created["active_revision_id"],
        {"action": "restore", "target_revision_id": draft["plan_revision_id"]},
    )
    cue = _cues(service, session_id)[receipt["cue"]["logical_id"]]
    assert adopted["active_plan_revision_id"] == adopted["plan_revision_id"]
    assert cue["placement_status"] == "needs_placement"
    assert cue["anchor"]["segment_id"] == source_id


def test_topology_hook_failure_rolls_back_speech_and_sound_versions(topology_case, monkeypatch):
    database, service, topology, session_id, variant_id = topology_case
    created = topology.create_plan(
        session_id,
        source_revision_id=None,
        segments=[{"text": "A sentence to split."}],
    )
    with database.session() as session:
        source_id = session.scalar(
            select(GenerationSegment.id).where(
                GenerationSegment.plan_revision_id == created["active_revision_id"]
            )
        )
    _add_cues(
        database,
        service,
        session_id,
        variant_id,
        [{"kind": "before_segment", "segment_id": source_id}],
    )
    import pandrator.web.sound_project_revisions as revisions

    original = revisions.rebase_sound_plan

    def rebase_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("rollback after sound version staging")

    monkeypatch.setattr(revisions, "rebase_sound_plan", rebase_then_fail)
    with pytest.raises(RuntimeError, match="rollback after sound version"):
        topology.revise_topology(
            session_id,
            created["active_revision_id"],
            {"action": "split", "segment_id": source_id, "cursor": 10},
        )
    with database.session() as session:
        assert (
            session.scalar(
                select(GenerationPlan.active_revision_id).where(
                    GenerationPlan.session_id == session_id
                )
            )
            == created["active_revision_id"]
        )
        assert session.scalar(
            select(func.count(GenerationPlanRevision.id))
            .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
            .where(GenerationPlan.session_id == session_id)
        ) == 1
        assert _count(session, ProjectSoundPlan, session_id) == 1
        assert session.scalar(
            select(func.count(ProjectSoundCue.id))
            .join(ProjectSoundPlan, ProjectSoundPlan.id == ProjectSoundCue.plan_id)
            .where(ProjectSoundPlan.session_id == session_id)
        ) == 1


@pytest.fixture
def fork_case(tmp_path: Path):
    app = create_app(
        data_root=tmp_path / "app-data",
        testing=True,
        background_maintenance=False,
    )
    try:
        yield app.extensions["pandrator"]
    finally:
        app.extensions["pandrator"]["services"].close()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _fork_source(services):
    record = services["sessions"].create("Sound fork source", workflow_kind="voiceover")
    source_dir = services["paths"].sessions / record.storage_key
    source_dir.mkdir(parents=True, exist_ok=True)
    upload_path = source_dir / "source.mp4"
    upload_path.write_bytes(b"disposable source media")
    upload = services["artifacts"].register(
        upload_path, kind="source", role="upload", session_id=record.id
    )
    checkpoint_path = source_dir / "correction.srt"
    checkpoint_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello from the checkpoint.\n",
        encoding="utf-8",
    )
    checkpoint = services["artifacts"].register(
        checkpoint_path,
        kind="srt",
        role="correction",
        session_id=record.id,
        parent_ids=[upload.id],
        metadata={"language": "en"},
    )
    services["workflow_handlers"]._store_srt_document(
        record.id,
        checkpoint,
        "correction",
        language="en",
        parent_artifact=upload,
    )
    sound_path = services["paths"].artifacts / "sound-library" / "bell.wav"
    sound_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(sound_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 800)
    sound_artifact = services["artifacts"].register(
        sound_path, kind="audio", role="sound.original"
    )
    with services["database"].immediate_session() as session:
        variant = SoundVariant(
            artifact_id=sound_artifact.id,
            state="ready",
            provenance_json={"display_name": "Bell"},
            duration_seconds=0.1,
        )
        session.add(variant)
        session.flush()
        variant_id = variant.id
    plan = services["generation"].create_plan(
        record.id,
        source_revision_id=None,
        segments=[{"text": "Hello from speech."}],
    )
    with services["database"].session() as session:
        segment_id = session.scalar(
            select(GenerationSegment.id).where(
                GenerationSegment.plan_revision_id == plan["active_revision_id"]
            )
        )
    with services["database"].immediate_session() as session:
        services["project_sounds"].add_in_session(
            session,
            record.id,
            {
                "expected_revision": 0,
                "variant_id": variant_id,
                "anchor": {
                    "kind": "after_word",
                    "segment_id": segment_id,
                    "quote": "Hello",
                    "occurrence": 1,
                    "start": 0,
                    "end": 5,
                },
            },
        )
        services["project_sounds"].add_in_session(
            session,
            record.id,
            {
                "expected_revision": 1,
                "variant_id": variant_id,
                "anchor": {"kind": "source_time", "time_seconds": 0.25},
            },
        )
    return record.id, checkpoint.id, variant_id, segment_id, sound_artifact.id


def test_fork_clones_sound_pins_and_rolls_back_failed_clone(fork_case, monkeypatch):
    services = fork_case
    database = services["database"]
    source_id, checkpoint_id, variant_id, segment_id, artifact_id = _fork_source(services)
    artifact_tree_before = _tree_hashes(services["paths"].artifacts)

    with database.immediate_session() as session:
        fork = services["session_forks"].fork_in_session(
            session,
            source_id,
            checkpoint_id,
            name="Sound fork",
            carry_media_assets=True,
        )
    target_id = fork.record.id
    cloned = _cues(services["project_sounds"], target_id)
    assert len(cloned) == 2
    assert {cue["variant_id"] for cue in cloned.values()} == {variant_id}
    word = next(cue for cue in cloned.values() if cue["anchor"]["kind"] == "after_word")
    timed = next(cue for cue in cloned.values() if cue["anchor"]["kind"] == "source_time")
    assert word["placement_status"] == "needs_placement"
    assert word["anchor"]["segment_id"] == segment_id
    assert word["anchor"]["origin_session_id"] == source_id
    assert timed["placement_status"] == "ready"
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.id == artifact_id)
        ) == 1
        assert session.scalar(select(func.count()).select_from(SoundVariant)) == 1
        assert session.scalar(select(func.count()).select_from(ProjectSoundPlan).where(
            ProjectSoundPlan.session_id == target_id
        )) == 1
    assert _tree_hashes(services["paths"].artifacts) == artifact_tree_before

    import pandrator.web.sound_project_revisions as revisions

    original = revisions.clone_sound_plan

    def clone_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("rollback after fork sound version staging")

    monkeypatch.setattr(revisions, "clone_sound_plan", clone_then_fail)
    session_tree_before = _tree_hashes(services["paths"].sessions)
    artifacts_before = _tree_hashes(services["paths"].artifacts)
    with pytest.raises(RuntimeError, match="rollback after fork sound version"):
        with database.immediate_session() as session:
            services["session_forks"].fork_in_session(
                session,
                source_id,
                checkpoint_id,
                name="Failing sound fork",
                carry_media_assets=False,
            )
    assert _tree_hashes(services["paths"].sessions) == session_tree_before
    assert _tree_hashes(services["paths"].artifacts) == artifacts_before
    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(ProjectSoundPlan)
        ) == 3
        assert session.scalar(select(func.count()).select_from(SoundVariant)) == 1
