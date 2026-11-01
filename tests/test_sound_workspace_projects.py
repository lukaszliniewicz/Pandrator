"""Team ownership checks for immutable project sound plans and mix admission."""

from __future__ import annotations

import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from struct import pack
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from pandrator.runtime import DataPaths
from pandrator.web import sound_mix_admission, sound_mix_inputs, sound_mix_manifest
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, Job, Project, SessionRecord, SoundVariant, Workspace
from pandrator.web.sessions import SessionService
from pandrator.web.sound_mix_snapshot import prepare_sound_mix
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from pandrator.web.sound_project_service import ProjectSoundService
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class SoundHarness:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    project_sounds: ProjectSoundService
    sessions: dict[str, SessionRecord]
    workspaces: dict[str, str]
    variants: dict[str, str]
    variant_artifacts: dict[str, str]
    base_artifacts: dict[str, str]


def _write_pcm(path: Path, *, frames: int = 8000, value: int = 700) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(pack(f"<{frames}h", *([value] * frames)))


def _team_workspace(database: Database, name: str) -> str:
    with database.session() as db:
        workspace = Workspace(kind="team", name=name)
        db.add(workspace)
        db.flush()
        return workspace.id


def _new_variant(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    workspace_id: str,
    name: str,
    *,
    artifact_workspace_id: str | None = None,
) -> tuple[str, str]:
    path = paths.artifacts / f"{name}.wav"
    _write_pcm(path, frames=4000, value=1100)
    artifact = artifacts.register(
        path,
        kind="audio",
        role="sound.original",
        workspace_id=artifact_workspace_id or workspace_id,
    )
    with database.session() as db:
        variant = SoundVariant(
            workspace_id=workspace_id,
            artifact_id=artifact.id,
            state="ready",
            sample_rate=8000,
            channels=1,
            frame_count=4000,
            duration_seconds=0.5,
            provenance_json={"display_name": name},
        )
        db.add(variant)
        db.flush()
        return variant.id, artifact.id


@pytest.fixture
def sound_harness(tmp_path: Path) -> Iterator[SoundHarness]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    sessions_service = SessionService(database)
    workspaces = {
        "alpha": _team_workspace(database, "Alpha team"),
        "beta": _team_workspace(database, "Beta team"),
    }
    sessions: dict[str, SessionRecord] = {}
    base_artifacts: dict[str, str] = {}
    for key, workspace_id in workspaces.items():
        record = sessions_service.create(
            f"{key} voiceover",
            workflow_kind="voiceover",
            workspace_id=workspace_id,
        )
        sessions[key] = record
        base_path = paths.sessions / record.storage_key / "base.wav"
        _write_pcm(base_path)
        base = artifacts.register(
            base_path,
            kind="audio",
            role="assembled_audio",
            session_id=record.id,
            metadata={
                "audio_timeline": {
                    "version": 1,
                    "sample_rate_hz": 8000,
                    "total_frames": 8000,
                }
            },
        )
        base_artifacts[key] = base.id

    # Independent roots in Alpha cover same-workspace cloning.
    sessions["alpha_clone_target"] = sessions_service.create(
        "Alpha clone target",
        workflow_kind="voiceover",
        workspace_id=workspaces["alpha"],
    )
    sessions["alpha_empty_target"] = sessions_service.create(
        "Alpha empty target",
        workflow_kind="voiceover",
        workspace_id=workspaces["alpha"],
    )
    project_sounds = ProjectSoundService(database, artifacts)
    variants: dict[str, str] = {}
    variant_artifacts: dict[str, str] = {}
    for key, workspace_key in (("alpha", "alpha"), ("beta", "beta")):
        variant_id, artifact_id = _new_variant(
            database,
            paths,
            artifacts,
            workspaces[workspace_key],
            f"{key}-sound-original",
        )
        variants[key] = variant_id
        variant_artifacts[key] = artifact_id
    try:
        yield SoundHarness(
            database=database,
            paths=paths,
            artifacts=artifacts,
            project_sounds=project_sounds,
            sessions=sessions,
            workspaces=workspaces,
            variants=variants,
            variant_artifacts=variant_artifacts,
            base_artifacts=base_artifacts,
        )
    finally:
        database.dispose()


def _cue_payload(variant_id: str, *, revision: int = 0) -> dict[str, Any]:
    return {
        "expected_revision": revision,
        "variant_id": variant_id,
        "anchor": {"kind": "source_time", "time_seconds": 0},
    }


def _add_cue(sound: SoundHarness, session_id: str, variant_id: str) -> dict[str, Any]:
    with sound.database.immediate_session() as db:
        return sound.project_sounds.add_in_session(db, session_id, _cue_payload(variant_id))


def _sound_plan_rows(
    sound: SoundHarness, session_id: str
) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    with sound.database.session() as db:
        plans = tuple(
            sorted(
                (plan.id, plan.revision, plan.speech_plan_revision_id)
                for plan in db.scalars(
                    select(ProjectSoundPlan).where(ProjectSoundPlan.session_id == session_id)
                )
            )
        )
        plan_ids = [row[0] for row in plans]
        cues = tuple(
            sorted(
                (
                    cue.id,
                    cue.logical_id,
                    cue.plan_id,
                    cue.variant_id,
                    cue.anchor_json,
                    cue.playback_json,
                    cue.included,
                )
                for cue in db.scalars(
                    select(ProjectSoundCue).where(ProjectSoundCue.plan_id.in_(plan_ids))
                )
            )
        )
    return plans, cues


def _publication_counts(sound: SoundHarness) -> tuple[int, int]:
    with sound.database.session() as db:
        jobs = db.scalar(select(func.count()).select_from(Job))
        artifacts = db.scalar(select(func.count()).select_from(Artifact))
    assert jobs is not None and artifacts is not None
    return jobs, artifacts


def test_add_and_update_reject_foreign_variants_without_new_plan_versions(
    sound_harness: SoundHarness,
) -> None:
    sound = sound_harness
    source_id = sound.sessions["alpha"].id
    empty_id = sound.sessions["alpha_empty_target"].id
    added = _add_cue(sound, source_id, sound.variants["alpha"])
    assert added["revision"] == 1
    assert added["cue"]["playback"]["mode"] == "overlay"
    assert added["cue"]["variant_id"] == sound.variants["alpha"]

    source_before = _sound_plan_rows(sound, source_id)
    with sound.database.immediate_session() as db:
        with pytest.raises(ValueError, match="workspace"):
            sound.project_sounds.update_in_session(
                db,
                source_id,
                added["cue"]["logical_id"],
                {"expected_revision": 1, "variant_id": sound.variants["beta"]},
            )
    assert _sound_plan_rows(sound, source_id) == source_before

    empty_before = _sound_plan_rows(sound, empty_id)
    assert empty_before == ((), ())
    with sound.database.immediate_session() as db:
        with pytest.raises(ValueError, match="workspace"):
            sound.project_sounds.add_in_session(db, empty_id, _cue_payload(sound.variants["beta"]))
    assert _sound_plan_rows(sound, empty_id) == empty_before


def test_clone_rejects_foreign_workspace_before_plan_and_same_workspace_clone_keeps_variant(
    sound_harness: SoundHarness,
) -> None:
    sound = sound_harness
    source_id = sound.sessions["alpha"].id
    foreign_target_id = sound.sessions["beta"].id
    same_workspace_target_id = sound.sessions["alpha_clone_target"].id
    added = _add_cue(sound, source_id, sound.variants["alpha"])

    foreign_before = _sound_plan_rows(sound, foreign_target_id)
    with sound.database.immediate_session() as db:
        with pytest.raises(ValueError, match="workspace"):
            sound.project_sounds.clone_in_session(
                db, source_id, foreign_target_id, {}, carry_media_assets=True
            )
    assert _sound_plan_rows(sound, foreign_target_id) == foreign_before == ((), ())

    with sound.database.immediate_session() as db:
        cloned = sound.project_sounds.clone_in_session(
            db, source_id, same_workspace_target_id, {}, carry_media_assets=True
        )
    assert cloned["cue_count"] == 1
    assert cloned["revision"] == 1
    _plans, cues = _sound_plan_rows(sound, same_workspace_target_id)
    assert len(cues) == 1
    assert cues[0][3] == added["cue"]["variant_id"] == sound.variants["alpha"]
    with sound.database.session() as db:
        source_root = db.get(Project, sound.sessions["alpha"].project_id)
        target_root = db.get(Project, sound.sessions["alpha_clone_target"].project_id)
        variant = db.get(SoundVariant, cues[0][3])
        assert source_root is not None and target_root is not None
        assert source_root.id != target_root.id
        assert source_root.workspace_id == target_root.workspace_id == sound.workspaces["alpha"]
        assert variant is not None and variant.workspace_id == sound.workspaces["alpha"]


def test_tampered_foreign_variant_is_rejected_before_plan_projection_or_audio_reads(
    sound_harness: SoundHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sound = sound_harness
    session_id = sound.sessions["alpha"].id
    added = _add_cue(sound, session_id, sound.variants["alpha"])
    with sound.database.session() as db:
        cue = db.scalar(
            select(ProjectSoundCue).where(ProjectSoundCue.logical_id == added["cue"]["logical_id"])
        )
        assert cue is not None
        cue.variant_id = sound.variants["beta"]

    def fail_if_audio_is_read(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("sound ownership must be checked before reading input audio")

    monkeypatch.setattr("pandrator.web.sound_project_service.cue_summary", fail_if_audio_is_read)
    with pytest.raises(ValueError, match="workspace"):
        sound.project_sounds.get_plan(session_id)

    monkeypatch.setattr(sound_mix_manifest, "preferred_pcm_format", fail_if_audio_is_read)
    monkeypatch.setattr(sound_mix_manifest, "run_media_process", fail_if_audio_is_read)
    monkeypatch.setattr("pandrator.web.sound_mix_snapshot.sha256_file", fail_if_audio_is_read)
    monkeypatch.setattr(
        "pandrator.web.sound_mix_snapshot._verify_input_files", fail_if_audio_is_read
    )
    with sound.database.snapshot_session() as db:
        with pytest.raises(ValueError, match="workspace"):
            sound_mix_inputs._load_snapshot(db, session_id, sound.base_artifacts["alpha"], None)
    with pytest.raises(ValueError, match="workspace"):
        prepare_sound_mix(
            sound.database,
            sound.paths,
            sound.artifacts,
            session_id,
            sound.base_artifacts["alpha"],
        )


def test_mix_admission_revalidates_owner_before_job_or_artifact_publication(
    sound_harness: SoundHarness,
) -> None:
    sound = sound_harness
    session_id = sound.sessions["alpha"].id
    added = _add_cue(sound, session_id, sound.variants["alpha"])
    prepared = prepare_sound_mix(
        sound.database,
        sound.paths,
        sound.artifacts,
        session_id,
        sound.base_artifacts["alpha"],
    )
    assert prepared.has_cues
    before_publication = _publication_counts(sound)
    before_plan = _sound_plan_rows(sound, session_id)

    with sound.database.immediate_session() as db:
        cue = db.scalar(
            select(ProjectSoundCue).where(ProjectSoundCue.logical_id == added["cue"]["logical_id"])
        )
        assert cue is not None
        cue.variant_id = sound.variants["beta"]
        db.flush()
        with pytest.raises(ValueError, match="workspace"):
            sound_mix_admission.admit_in_session(db, JobQueue(sound.database), prepared)

    after_publication = _publication_counts(sound)
    after_plan = _sound_plan_rows(sound, session_id)
    assert after_publication == before_publication
    assert len(after_plan[0]) == len(before_plan[0])
    assert len(after_plan[1]) == len(before_plan[1])


def test_variant_with_foreign_backing_artifact_is_rejected_atomically(
    sound_harness: SoundHarness,
) -> None:
    sound = sound_harness
    # Keep the consumer's defense against rows predating library scope checks.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(sound.database.path))
    command.downgrade(config, "0077_media_scope_consistency")
    session_id = sound.sessions["alpha_empty_target"].id
    with sound.database.session() as db:
        malformed = SoundVariant(
            workspace_id=sound.workspaces["alpha"],
            artifact_id=sound.variant_artifacts["beta"],
            state="ready",
            sample_rate=8000,
            channels=1,
            frame_count=4000,
            duration_seconds=0.5,
        )
        db.add(malformed)
        db.flush()
        malformed_id = malformed.id

    before = _sound_plan_rows(sound, session_id)
    with sound.database.immediate_session() as db:
        with pytest.raises(ValueError, match="workspace"):
            sound.project_sounds.add_in_session(db, session_id, _cue_payload(malformed_id))
    assert _sound_plan_rows(sound, session_id) == before == ((), ())
