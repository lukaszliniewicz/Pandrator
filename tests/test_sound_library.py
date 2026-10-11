"""Focused persistence, query, and candidate-lifecycle checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, select

from pandrator.logic.audio_cpp_generation_models import get_model as get_audio_cpp_generation_model
from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, Job, SessionRecord, SoundItem, SoundVariant
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_library_queries import MAX_PAGE_SIZE, _provenance
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from pandrator.web.sound_service import SoundService


@pytest.fixture
def domain(tmp_path: Path):
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(paths.database))
    command.upgrade(config, "head")
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    sounds = SoundService(database, paths, artifacts, JobQueue(database))
    try:
        yield database, paths, artifacts, sounds
    finally:
        database.dispose()


def _ready_candidate(database, paths, artifacts, *, name="sample", job=None):
    audio_path = paths.root / "sounds" / "audio" / f"{name}.wav"
    audio_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    audio_path.write_bytes(b"managed audio bytes: " + name.encode())
    artifact = artifacts.register(
        audio_path,
        kind="audio",
        role="sound.original",
        session_id=None,
        calculate_hash=True,
        metadata={"operation": "test"},
    )
    with database.immediate_session() as session:
        candidate = SoundVariant(
            artifact_id=artifact.id,
            job_id=job.id if job is not None else None,
            state="ready",
            provenance_json={"provider": "test", "prompt": "wind", "secret": "omit"},
            sample_rate=44100,
            channels=2,
            frame_count=8,
            duration_seconds=0.1,
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    return candidate_id, artifact.id, audio_path


def test_migration_adds_global_records_without_touching_existing_rows(tmp_path: Path):
    database_path = tmp_path / "migration.sqlite3"
    migration_dir = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migration_dir))
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
    command.upgrade(config, "0054_session_interface_mode")

    database = Database(database_path)
    with database.session() as session:
        existing = SessionRecord(name="Existing before sound library")
        session.add(existing)
        session.flush()
        existing_id = existing.id
    database.dispose()

    # The historical 0001 migration creates the current Base metadata, so
    # remove later-phase tables to represent a real 0054 database before
    # exercising migration 0055 itself.
    baseline_engine = create_engine(sqlite_url(database_path))
    with baseline_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
    with baseline_engine.begin() as connection:
        for table in (
            "project_sound_cues",
            "project_sound_plans",
            "sound_variants",
            "sound_items",
        ):
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
    with baseline_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.commit()
    baseline_engine.dispose()

    command.upgrade(config, "0055_sound_library")
    engine = Database(database_path)
    try:
        with engine.session() as session:
            row = session.get(SessionRecord, existing_id)
            assert row is not None
            assert row.name == "Existing before sound library"
            item = SoundItem(name="global item", tags_json=["weather"])
            candidate = SoundVariant(state="discarded", provenance_json={})
            session.add_all([item, candidate])
            session.flush()
            candidate.item_id = item.id
            item.preferred_variant_id = candidate.id

        with engine.session() as session:
            session.delete(session.get(SessionRecord, existing_id))
        with engine.session() as session:
            assert session.get(SoundItem, item.id) is not None
            assert session.get(SoundVariant, candidate.id) is not None
    finally:
        engine.dispose()

    command.downgrade(config, "0054_session_interface_mode")
    inspection = create_engine(sqlite_url(database_path))
    try:
        inspector = inspect(inspection)
        assert not inspector.has_table("sound_items")
        assert not inspector.has_table("sound_variants")
    finally:
        inspection.dispose()


def test_save_attach_update_revision_and_soft_discard(domain):
    database, paths, artifacts, sounds = domain
    first, _first_artifact, first_path = _ready_candidate(
        database, paths, artifacts, name="first"
    )
    with database.immediate_session() as session:
        created = sounds.save_in_session(
            session,
            {"candidate_id": first, "name": "Rain", "tags": ["weather"]},
        )
    item_id = created["item_id"]
    assert created["item"]["revision"] == 1
    assert created["item"]["preferred_variant"]["id"] == first

    second, _second_artifact, _second_path = _ready_candidate(
        database, paths, artifacts, name="second"
    )
    with database.immediate_session() as session:
        attached = sounds.save_in_session(
            session,
            {"candidate_id": second, "item_id": item_id, "expected_revision": 1},
        )
    assert attached["item"]["revision"] == 2
    assert attached["item"]["preferred_variant"]["id"] == first

    third, _third_artifact, _third_path = _ready_candidate(
        database, paths, artifacts, name="third"
    )
    with database.immediate_session() as session:
        with pytest.raises(ValueError, match="update_sound"):
            sounds.save_in_session(
                session,
                {
                    "candidate_id": third,
                    "item_id": item_id,
                    "expected_revision": 2,
                    "tags": [],
                },
            )
    with database.session() as session:
        assert session.get(SoundItem, item_id).revision == 2

    with database.immediate_session() as session:
        reused = sounds.save_in_session(
            session,
            {"candidate_id": second, "name": "ignored on reuse"},
        )
    assert reused["reused"] is True
    assert reused["item"]["revision"] == 2

    with database.immediate_session() as session:
        updated = sounds.update_in_session(
            session,
            item_id,
            {"expected_revision": 2, "tags": [], "archived": True},
        )
    assert updated["tags"] == []
    assert updated["archived"] is True
    assert updated["revision"] == 3

    with database.immediate_session() as session:
        no_op = sounds.update_in_session(
            session,
            item_id,
            {"expected_revision": 3, "tags": [], "archived": True},
        )
    assert no_op["revision"] == 3
    with database.immediate_session() as session:
        with pytest.raises(RevisionConflict):
            sounds.update_in_session(session, item_id, {"expected_revision": 2, "name": "stale"})

    failed_job = Job(
        kind="sound.generate",
        status="failed",
        payload_json={},
        resource_keys_json=[],
        error_code="test",
        error_message="failed generation",
    )
    with database.immediate_session() as session:
        session.add(failed_job)
        session.flush()
        failed_job_id = failed_job.id
    discarded, artifact_id, discarded_path = _ready_candidate(
        database, paths, artifacts, name="discarded", job=failed_job
    )
    with database.immediate_session() as session:
        result = sounds.discard_in_session(session, discarded)
    assert result["status"] == "discarded"
    assert discarded_path.is_file()
    with database.session() as session:
        assert session.get(Artifact, artifact_id).state == "current"
        assert session.get(Job, failed_job_id).status == "failed"
        assert session.get(SoundVariant, discarded).state == "discarded"
    assert first_path.is_file()


def test_candidate_pinned_by_project_cue_cannot_be_discarded(domain):
    database, paths, artifacts, sounds = domain
    candidate_id, artifact_id, audio_path = _ready_candidate(
        database, paths, artifacts, name="project-pinned"
    )
    with database.immediate_session() as session:
        owner = SessionRecord(name="Sound cue owner")
        session.add(owner)
        session.flush()
        plan = ProjectSoundPlan(session_id=owner.id, revision=1)
        session.add(plan)
        session.flush()
        cue = ProjectSoundCue(
            logical_id="cue-1",
            plan_id=plan.id,
            variant_id=candidate_id,
            anchor_json={"kind": "segment", "id": "segment-1"},
            playback_json={"gain": 1.0},
            included=False,
            placement_status="needs_placement",
        )
        session.add(cue)

    with database.immediate_session() as session:
        with pytest.raises(ValueError, match="project sound cue"):
            sounds.discard_in_session(session, candidate_id)
    with database.session() as session:
        cue = session.scalar(
            select(ProjectSoundCue).where(ProjectSoundCue.variant_id == candidate_id)
        )
        candidate = session.get(SoundVariant, candidate_id)
        assert cue is not None and cue.included is False
        assert candidate is not None and candidate.state == "ready"
        assert session.get(Artifact, artifact_id).state == "current"
    assert audio_path.is_file()


def test_queued_candidate_status_comes_from_its_durable_job(domain):
    database, _paths, _artifacts, sounds = domain
    expected = {"queued": "queued", "running": "running", "failed": "failed", "canceled": "canceled"}
    candidate_ids = {}
    with database.immediate_session() as session:
        for status in expected:
            job = Job(
                kind="sound.generate",
                status=status,
                payload_json={},
                resource_keys_json=[],
            )
            session.add(job)
            session.flush()
            candidate = SoundVariant(
                job_id=job.id,
                state="queued",
                provenance_json={"operation": "gen"},
            )
            session.add(candidate)
            session.flush()
            candidate_ids[status] = candidate.id

    statuses = {item["id"]: item["status"] for item in sounds.list_candidates(limit=20)["items"]}
    assert {status: statuses[candidate_id] for status, candidate_id in candidate_ids.items()} == expected


def test_paged_queries_are_bounded_exact_tagged_and_compact(domain):
    database, paths, artifacts, sounds = domain
    for index in range(5):
        with database.immediate_session() as session:
            item = SoundItem(
                name=f"Sound {index}",
                tags_json=["exact" if index % 2 == 0 else "other"],
                revision=1,
                archived=False,
            )
            session.add(item)
            session.flush()
            if index == 0:
                session.add(
                    SoundVariant(
                        item_id=item.id,
                        state="discarded",
                        provenance_json={"provider": "local", "secret": "omit"},
                    )
                )

    queries: list[str] = []

    def count_query(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(database.engine, "before_cursor_execute", count_query)
    try:
        first_page = sounds.list_sounds(limit=2, tag="exact")
        second_page = sounds.list_sounds(limit=2, tag="exact", cursor=first_page["next_cursor"])
        all_ids = [item["id"] for item in first_page["items"] + second_page["items"]]
        assert len(all_ids) == 3
        assert first_page["next_cursor"] is not None
        assert second_page["next_cursor"] is None

        item_id = first_page["items"][0]["id"]
        detailed = sounds.get_sound(item_id, limit=1)
        assert set(detailed["variants"]) == {"items", "next_cursor"}
        candidate_page = sounds.list_candidates(limit=MAX_PAGE_SIZE)
        assert candidate_page["items"][0]["provenance"] == {"provider": "local"}
    finally:
        event.remove(database.engine, "before_cursor_execute", count_query)
    assert len(queries) == 5  # one per list page; item detail uses two.

    with database.snapshot_session() as session:
        with pytest.raises(ValueError, match="cursor"):
            from pandrator.web.sound_library_queries import list_candidates

            list_candidates(session, cursor="not a cursor")
        with pytest.raises(ValueError, match="at most 200"):
            from pandrator.web.sound_library_queries import list_sounds

            list_sounds(session, query="x" * 201)


def test_detail_provenance_is_allowlisted_while_lists_stay_compact(domain, monkeypatch):
    database, paths, artifacts, sounds = domain
    candidate_id, _artifact_id, _audio_path = _ready_candidate(
        database, paths, artifacts, name="provenance"
    )
    detailed = {
        "provider": "audio_cpp", "model_id": "sfx", "prompt": "bell",
        "seed": 7, "backend": "cpu", "duration_seconds": 4.0,
        "threads": 4, "num_inference_steps": 100, "guidance_scale": 7.0,
        "runtime_identity": {
            "model_sha256": "model-hash", "model_revision": "revision-1",
            "runtime_version": "1.2", "runtime_sha256": "runtime-hash",
            "minimum_runtime_version": "1.0", "path": "/private/runtime",
        },
        "secret": "private",
    }
    with database.immediate_session() as session:
        session.get(SoundVariant, candidate_id).provenance_json = detailed
        saved = sounds.save_in_session(
            session, {"candidate_id": candidate_id, "name": "Bell"}
        )
    item_id = saved["item_id"]
    queries = []

    def count_query(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    def no_audio_probe(_path):
        pytest.fail("Sound-library reads must not probe audio files")

    event.listen(database.engine, "before_cursor_execute", count_query)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_bytes", no_audio_probe)
            patch.setattr(Path, "is_file", no_audio_probe)
            candidate = sounds.get_candidate(candidate_id)
            item = sounds.get_sound(item_id, limit=1)
            listed = sounds.list_sounds(limit=1)
            candidates = sounds.list_candidates(limit=1)
    finally:
        event.remove(database.engine, "before_cursor_execute", count_query)
    expected = {key: value for key, value in detailed.items() if key not in {"secret"}}
    expected["runtime_identity"] = {
        key: value for key, value in detailed["runtime_identity"].items() if key != "path"
    }
    assert candidate["provenance"] == expected
    assert item["preferred_variant"]["provenance"] == expected
    assert item["variants"]["items"][0]["provenance"] == expected
    assert "threads" not in listed["items"][0]["preferred_variant"]["provenance"]
    assert "runtime_identity" not in candidates["items"][0]["provenance"]
    assert len(queries) == 5
    invalid = _provenance({
        "threads": True, "num_inference_steps": -2, "guidance_scale": float("nan"),
        "runtime_identity": {"path": "/private", "model_sha256": 12},
    }, include_detail=True)
    assert invalid == {}


def test_music_provenance_keeps_bounded_controls_and_safe_model_assets_detail_only():
    model = get_audio_cpp_generation_model("minimax_music3_q4_0")
    runtime_root = "/private/audio_cpp/cache"
    model_assets = [
        {"path": asset.path, "size_bytes": asset.size_bytes, "sha256": asset.sha256}
        for asset in model.files
    ]
    model_assets.append(
        {
            "path": f"{runtime_root}/models/private.gguf",
            "size_bytes": 1,
            "sha256": "f" * 64,
        }
    )
    stored = {
        "provider": "audio_cpp",
        "model_id": model.id,
        "operation": "gen",
        "kind": "music",
        "prompt": "An instrumental passage",
        "lyrics": "l" * 16005,
        "parameters": {
            "num_inference_steps": 30,
            "tags": "jazz/swing",
            "negative_prompt": "No vocals / bright drums",
            "path": "/private/model.gguf",
            "api_key": "private",
            "nested": {"unsafe": True},
            "not_finite": float("nan"),
            "sampler": "weights.gguf",
        },
        "runtime_identity": {
            "runtime_version": "0.9.1",
            "model_revision": model.revision,
            "model_assets_revision": model.revision,
            "model_assets": model_assets,
            "runtime_path": runtime_root,
        },
    }

    compact = _provenance(stored)
    detail = _provenance(stored, include_detail=True)

    assert compact["kind"] == "music"
    assert "lyrics" not in compact
    assert "parameters" not in compact
    assert "runtime_identity" not in compact
    assert detail["lyrics"] == "l" * 16000
    assert detail["parameters"] == {
        "num_inference_steps": 30,
        "tags": "jazz/swing",
        "negative_prompt": "No vocals / bright drums",
    }
    manifest = detail["runtime_identity"]["asset_manifest"]
    assert len(model.files) == len(manifest) == 13
    assert manifest == [
        {
            "filename": asset.path,
            "bytes": asset.size_bytes,
            "sha256": asset.sha256,
            "revision": model.revision,
        }
        for asset in model.files
    ]
    assert runtime_root not in str(detail)
    assert "private.gguf" not in str(detail)

    malformed = _provenance(
        {
            "kind": "voice",
            "lyrics": 12,
            "parameters": [{"unsafe": True}],
            "runtime_identity": {"model_assets": [{"path": "/private/model.gguf"}]},
        },
        include_detail=True,
    )
    assert malformed == {}
    for malformed_kind in ([], {"kind": "music"}, None, 12):
        assert _provenance({"kind": malformed_kind}, include_detail=True) == {}
