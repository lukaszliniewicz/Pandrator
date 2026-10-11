"""Bounded sound resolution with disposable storage and mocked native tools."""

from __future__ import annotations

import hashlib
import threading
import uuid
import wave
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import event, func, select

from pandrator.logic.book_timing import AlignedBookText, BookWord
from pandrator.logic.dubbing.caption_alignment import SpeechSpan
from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    Job,
    OutputAssembly,
    SessionRecord,
    SoundVariant,
    utcnow,
)
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_project_commands import write_version
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from pandrator.web.sound_project_service import ProjectSoundService
from pandrator.web.sound_resolution_audio import _wav_window, resolve_boundary
from pandrator.web.sound_resolution_service import SoundResolutionService
from pandrator.web.sound_resolution_snapshot import WorkItem
from pandrator.web.workspace_settings import WorkspaceSettingsService


def _wav(path: Path, *, quiet: bool = True) -> None:
    data = np.zeros(32000, dtype="<i2")
    data[3200:12800] = 6000
    data[16000:20800] = 6000 if quiet else 5000
    if not quiet:
        data[:] = 5000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(data.tobytes())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    paths = DataPaths(tmp_path.resolve())
    upgrade_database(paths.database)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    service = SoundResolutionService(
        database,
        paths,
        artifacts,
        JobQueue(database),
        WorkspaceSettingsService(database),
        ProjectSoundService(database, artifacts),
    )
    monkeypatch.setattr(
        "pandrator.logic.dubbing.crispasr.resolve_executable",
        lambda *_args, **_kw: str(tmp_path / "fake-crispasr"),
    )
    with database.immediate_session() as session:
        record = SessionRecord(name="Book", workflow_kind="audiobook")
        session.add(record)
        session.flush()
        plan = GenerationPlan(session_id=record.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(plan_id=plan.id, revision_number=1, content_hash="speech")
        session.add(revision)
        session.flush()
        plan.active_revision_id = revision.id
        segment = GenerationSegment(
            plan_revision_id=revision.id,
            ordinal=0,
            text="display text",
            optimized_text="Héllo world",
            language="en",
        )
        session.add(segment)
        session.flush()
        source_path = paths.root / "take.wav"
        _wav(source_path)
        source = Artifact(
            session_id=record.id,
            kind="audio",
            role="generation.take",
            relative_path="take.wav",
            content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
            metadata_json={"synthesized_text": "Héllo world"},
        )
        sound_path = paths.root / "sound.wav"
        _wav(sound_path)
        variant_artifact = Artifact(
            kind="audio",
            role="sound.original",
            relative_path="sound.wav",
            content_hash=hashlib.sha256(sound_path.read_bytes()).hexdigest(),
        )
        session.add_all([source, variant_artifact])
        session.flush()
        take = AudioTake(
            generation_segment_id=segment.id,
            artifact_id=source.id,
            status="completed",
            is_active=True,
        )
        variant = SoundVariant(artifact_id=variant_artifact.id, state="ready", duration_seconds=1.0)
        session.add_all([take, variant])
        session.flush()
        ids = record.id, segment.id, variant.id, take.id, source.id
    yield service, ids
    database.dispose()


def _cue(service, ids, *, mode="insert", kind="after_word", quote="Héllo", revision=0):
    session_id, segment_id, variant_id, *_ = ids
    with service.database.immediate_session() as session:
        result = service.project_sounds.add_in_session(
            session,
            session_id,
            {
                "expected_revision": revision,
                "variant_id": variant_id,
                "anchor": {"kind": kind, "segment_id": segment_id, "quote": quote, "occurrence": 1},
                "playback": {"mode": mode},
            },
        )
    return result["cue"]["logical_id"]


def _activate(service, prepared):
    with service.database.immediate_session() as session:
        receipt = service.admit_in_session(session, prepared)
        job = session.get(Job, receipt["job_id"])
        job.status = "running"
        job.lease_owner = "fixture"
        job.lease_generation = 1
        job.lease_expires_at = utcnow() + timedelta(minutes=5)
    return receipt["job_id"]


def _run(service, prepared, job_id):
    token = current_job_lease.set({"job_id": job_id, "owner_id": "fixture", "lease_generation": 1})
    try:
        return service.run_resolution(
            {**prepared.payload, "_job_id": job_id, "_lease_generation": 1},
            lambda *_: None,
            threading.Event(),
        )
    finally:
        current_job_lease.reset(token)


def _native(monkeypatch, *, quiet=True):
    calls = []

    def align(item, _settings, workdir, _cancel):
        calls.append(item.take_id)
        _wav(workdir / "book-alignment.wav", quiet=quiet)
        return AlignedBookText(
            item.transcript,
            (BookWord("Héllo", 200, 800, 0, 5), BookWord("world", 1000, 1300, 6, 11)),
            "crispasr",
            {"duration_ms": 2000, "identity": item.identity},
        )

    monkeypatch.setattr("pandrator.web.sound_resolution_worker.align_take", align)
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_worker.normalize_take",
        lambda item, path, *_: _wav(path, quiet=quiet),
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.native_audio_cpp_guard", lambda *_args: nullcontext()
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.crispasr.run_vad_export",
        lambda _source, target, *_args, **_kw: target,
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.parse_vad_export",
        lambda *_args, **_kw: (
            (SpeechSpan(0, 300), SpeechSpan(500, 800)) if quiet else (SpeechSpan(0, 1500),)
        ),
    )
    return calls


def test_insert_uses_spoken_text_and_preserves_immutable_plan(setup, monkeypatch):
    service, ids = setup
    cue_id = _cue(service, ids)
    calls = _native(monkeypatch)
    prepared = service.prepare(ids[0], expected_revision=1)
    assert (
        prepared.snapshot.items[0].transcript_hash
        == hashlib.sha256("Héllo world".encode()).hexdigest()
    )
    assert (
        prepared.snapshot.items[0].transcript_hash
        != hashlib.sha256("display text".encode()).hexdigest()
    )
    assert "Héllo world" not in str(prepared.payload)
    assert {"service:stt", "service:tts:audio_cpp", f"session:{ids[0]}"} <= set(
        prepared.resource_keys
    )
    job_id = _activate(service, prepared)
    result = _run(service, prepared, job_id)
    assert result["revision"] == 2 and result["resolved"] == 1 and calls == [ids[3]]
    with service.database.snapshot_session() as session:
        old = session.scalar(
            select(ProjectSoundPlan).where(
                ProjectSoundPlan.session_id == ids[0], ProjectSoundPlan.revision == 1
            )
        )
        new = session.scalar(
            select(ProjectSoundPlan).where(
                ProjectSoundPlan.session_id == ids[0], ProjectSoundPlan.revision == 2
            )
        )
        first = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == old.id))
        second = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == new.id))
        assert first.placement_status == "needs_placement" and "resolution" not in first.anchor_json
        assert second.logical_id == cue_id and second.placement_status == "ready"
        assert second.anchor_json["resolution"]["take_id"] == ids[3]
        assert second.anchor_json["resolution"]["take_content_hash"]
        assert (
            session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.role == "sound_alignment")
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(AudioTake)) == 1
        assert session.scalar(select(func.count()).select_from(OutputAssembly)) == 0


def test_cache_reuse_and_overlay_alignment_only(setup, monkeypatch):
    service, ids = setup
    _cue(service, ids, mode="overlay")
    calls = _native(monkeypatch, quiet=False)
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.crispasr.run_vad_export",
        lambda *_args, **_kw: pytest.fail("after_word overlay must not run VAD"),
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_worker.normalize_take",
        lambda *_args: pytest.fail("after_word overlay must not normalize cached audio"),
    )
    first = service.prepare(ids[0], expected_revision=1)
    _run(service, first, _activate(service, first))
    second = service.prepare(ids[0], expected_revision=2, cue_ids=list(first.snapshot.cue_ids))
    _run(service, second, _activate(service, second))
    assert len(calls) == 1
    with service.database.snapshot_session() as session:
        latest = session.scalar(
            select(ProjectSoundPlan)
            .where(ProjectSoundPlan.session_id == ids[0])
            .order_by(ProjectSoundPlan.revision.desc())
        )
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == latest.id))
        assert cue.anchor_json["resolution"]["evidence"] == {"alignment_only_overlay": True}


def test_missing_take_pending_and_stale_plan_rejected(setup):
    service, ids = setup
    cue_id = _cue(service, ids)
    with service.database.immediate_session() as session:
        session.get(AudioTake, ids[3]).is_active = False
    prepared = service.prepare(ids[0], expected_revision=1)
    assert prepared.snapshot.items[0].reason == "missing_active_take"
    _run(service, prepared, _activate(service, prepared))
    with service.database.snapshot_session() as session:
        latest = session.scalar(
            select(ProjectSoundPlan)
            .where(ProjectSoundPlan.session_id == ids[0])
            .order_by(ProjectSoundPlan.revision.desc())
        )
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == latest.id))
        assert cue.logical_id == cue_id and cue.placement_status == "needs_placement"
    with service.database.immediate_session() as session:
        service.project_sounds.remove_in_session(session, ids[0], cue_id, 2)
    with pytest.raises(RevisionConflict):
        service.prepare(ids[0], expected_revision=2)


def test_cancel_and_changed_selection_do_not_write_version(setup):
    service, ids = setup
    _cue(service, ids)
    prepared = service.prepare(ids[0], expected_revision=1)
    job_id = _activate(service, prepared)
    with service.database.immediate_session() as session:
        session.get(AudioTake, ids[3]).is_active = False
    with pytest.raises(RevisionConflict):
        _run(service, prepared, job_id)
    with service.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.max(ProjectSoundPlan.revision)).where(
                    ProjectSoundPlan.session_id == ids[0]
                )
            )
            == 1
        )


def test_insert_without_quiet_remains_pending(setup, monkeypatch):
    service, ids = setup
    _cue(service, ids)
    _native(monkeypatch, quiet=False)
    prepared = service.prepare(ids[0], expected_revision=1)
    receipt = _run(service, prepared, _activate(service, prepared))
    assert receipt["pending"] == 1 and receipt["resolved"] == 0
    with service.database.snapshot_session() as session:
        latest = session.scalar(
            select(ProjectSoundPlan)
            .where(ProjectSoundPlan.session_id == ids[0])
            .order_by(ProjectSoundPlan.revision.desc())
        )
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == latest.id))
        assert cue.anchor_json["resolution"]["reason"] == "no_safe_quiet_gap"


def test_next_pause_uses_forward_gap(setup, monkeypatch):
    service, ids = setup
    _cue(service, ids, kind="next_pause")
    _native(monkeypatch)
    prepared = service.prepare(ids[0], expected_revision=1)
    receipt = _run(service, prepared, _activate(service, prepared))
    assert receipt["resolved"] == 1
    with service.database.snapshot_session() as session:
        latest = session.scalar(
            select(ProjectSoundPlan)
            .where(ProjectSoundPlan.session_id == ids[0])
            .order_by(ProjectSoundPlan.revision.desc())
        )
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == latest.id))
        assert cue.anchor_json["resolution"]["take_frame"] > 12800


def test_next_pause_overlay_without_gap_stays_pending(setup, monkeypatch):
    service, ids = setup
    _cue(service, ids, mode="overlay", kind="next_pause")
    _native(monkeypatch, quiet=False)
    prepared = service.prepare(ids[0], expected_revision=1)
    receipt = _run(service, prepared, _activate(service, prepared))
    assert receipt["pending"] == 1 and receipt["resolved"] == 0
    assert receipt["cues"][0]["reason"] == "no_safe_quiet_gap"


def test_admission_limit_and_default_batch(setup):
    service, ids = setup
    for revision in range(21):
        _cue(service, ids, revision=revision)
    prepared = service.prepare(ids[0], expected_revision=21)
    assert len(prepared.snapshot.cue_ids) == 20 and prepared.snapshot.remaining_count == 1
    with pytest.raises(ValueError, match="twenty"):
        service.prepare(ids[0], expected_revision=21, cue_ids=[str(index) for index in range(21)])


def test_cache_publication_rolls_back_with_plan_write(setup, monkeypatch):
    service, ids = setup
    _cue(service, ids)
    _native(monkeypatch)
    prepared = service.prepare(ids[0], expected_revision=1)
    job_id = _activate(service, prepared)
    original_immediate = service.database.immediate_session
    original_registration = service.artifacts.prepare_registration
    writer_open = False

    @contextmanager
    def tracked_writer():
        nonlocal writer_open
        with original_immediate() as session:
            writer_open = True
            try:
                yield session
            finally:
                writer_open = False

    def checked_registration(*args, **kwargs):
        assert not writer_open, "cache hashing must finish before SQLite writer"
        return original_registration(*args, **kwargs)

    monkeypatch.setattr(service.database, "immediate_session", tracked_writer)
    monkeypatch.setattr(service.artifacts, "prepare_registration", checked_registration)
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_worker.write_version",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("fixture rollback")),
    )
    with pytest.raises(RuntimeError, match="fixture rollback"):
        _run(service, prepared, job_id)
    with service.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.max(ProjectSoundPlan.revision)).where(
                    ProjectSoundPlan.session_id == ids[0]
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.role == "sound_alignment")
            )
            == 0
        )
    assert not list((service.paths.root / "sounds" / "alignment").glob("*.json"))


def test_repeated_decomposed_grapheme_owns_second_word(tmp_path, monkeypatch):
    source = tmp_path / "spoken.wav"
    _wav(source, quiet=False)
    text = "e\u0301 e\u0301"
    aligned = AlignedBookText(
        text,
        (BookWord("e\u0301", 200, 500, 0, 2), BookWord("e\u0301", 900, 1200, 3, 5)),
        "fixture",
        {"duration_ms": 2000},
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.native_audio_cpp_guard", lambda *_args: nullcontext()
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.crispasr.resolve_executable",
        lambda *_args: str(tmp_path / "fake-crispasr"),
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.crispasr.run_vad_export",
        lambda *_args, **_kw: pytest.fail("after_word overlay must not run VAD"),
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.parse_vad_export",
        lambda *_args, **_kw: (SpeechSpan(0, 2000),),
    )
    item = WorkItem(
        "cue",
        {"kind": "after_word", "quote": "e\u0301", "occurrence": 2},
        {"mode": "overlay"},
        None,
        "take",
        "artifact",
        "hash",
        "transcript",
        text,
        source,
        "fr",
        {},
    )
    result = resolve_boundary(item, aligned, source, tmp_path, {}, threading.Event())
    assert result["status"] == "ready"
    assert result["take_frame"] == 19200
    assert result["evidence"] == {"alignment_only_overlay": True}
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.crispasr.run_vad_export",
        lambda _source, target, *_args, **_kw: target,
    )
    monkeypatch.setattr(
        "pandrator.web.sound_resolution_audio.parse_vad_export", lambda *_args, **_kw: ()
    )
    insert = WorkItem(
        "cue",
        item.anchor,
        {"mode": "insert"},
        None,
        "take",
        "artifact",
        "hash",
        "transcript",
        text,
        source,
        "fr",
        {},
    )
    pending = resolve_boundary(insert, aligned, source, tmp_path, {}, threading.Event())
    assert pending["status"] == "needs_placement"
    assert pending["reason"] == "non_speech_evidence_unavailable"


def test_wav_analysis_reads_only_bounded_window(tmp_path, monkeypatch):
    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * (120 * 16000))
    original_open = wave.open
    read_sizes = []

    class ObservedWave:
        def __init__(self, reader):
            self.reader = reader

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.reader.close()

        def __getattr__(self, name):
            return getattr(self.reader, name)

        def readframes(self, count):
            read_sizes.append(count)
            return self.reader.readframes(count)

    def observed_open(filename, mode="rb"):
        reader = original_open(filename, mode)
        return ObservedWave(reader) if mode == "rb" and Path(filename) == path else reader

    monkeypatch.setattr("pandrator.web.sound_resolution_audio.wave.open", observed_open)
    first, samples, rate, total = _wav_window(path, 10 * 16000)
    assert (first, rate, total) == (round(9.7 * 16000), 16000, 120 * 16000)
    assert len(samples) == round(2.8 * 16000)
    assert read_sizes == [len(samples)]
    assert read_sizes[0] <= 6 * rate


def test_lease_generation_change_blocks_publication(setup):
    service, ids = setup
    _cue(service, ids)
    prepared = service.prepare(ids[0], expected_revision=1)
    job_id = _activate(service, prepared)
    with service.database.immediate_session() as session:
        session.get(Job, job_id).lease_generation = 2
    with pytest.raises(RuntimeError, match="lease"):
        _run(service, prepared, job_id)
    with service.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.max(ProjectSoundPlan.revision)).where(
                    ProjectSoundPlan.session_id == ids[0]
                )
            )
            == 1
        )


def test_ambiguous_take_and_mismatched_spoken_transcript_stay_pending(setup):
    service, ids = setup
    _cue(service, ids)
    with service.database.immediate_session() as session:
        second = AudioTake(
            generation_segment_id=ids[1], artifact_id=ids[4], status="completed", is_active=True
        )
        session.add(second)
        session.flush()
        second_id = second.id
    ambiguous = service.prepare(ids[0], expected_revision=1)
    assert ambiguous.snapshot.items[0].reason == "ambiguous_active_take"
    assert set(ambiguous.payload["pins"]["items"][0]["active_take_ids"]) == {ids[3], second_id}
    with service.database.immediate_session() as session:
        session.get(AudioTake, second_id).is_active = False
        session.get(Artifact, ids[4]).metadata_json = {"synthesized_text": "Other spoken words"}
    mismatch = service.prepare(ids[0], expected_revision=1)
    assert mismatch.snapshot.items[0].reason == "spoken_transcript_mismatch"


def test_replaced_active_take_reselects_ready_cue_and_pins_new_artifact(setup, monkeypatch):
    service, ids = setup
    cue_id = _cue(service, ids)
    _native(monkeypatch)
    first = service.prepare(ids[0], expected_revision=1)
    _run(service, first, _activate(service, first))
    with pytest.raises(ValueError, match="no word cues"):
        service.prepare(ids[0], expected_revision=2)
    with service.database.immediate_session() as session:
        old_artifact = session.get(Artifact, ids[4])
        (service.paths.root / "replacement-take.wav").write_bytes(
            (service.paths.root / old_artifact.relative_path).read_bytes()
        )
        replacement = Artifact(
            session_id=ids[0], kind="audio", role="generation.take",
            relative_path="replacement-take.wav",
            content_hash=old_artifact.content_hash,
            metadata_json=dict(old_artifact.metadata_json),
        )
        session.add(replacement)
        session.flush()
        session.get(AudioTake, ids[3]).is_active = False
        take = AudioTake(
            generation_segment_id=ids[1], artifact_id=replacement.id,
            status="completed", is_active=True,
        )
        session.add(take)
        session.flush()
        replacement_id, take_id = replacement.id, take.id
    refreshed = service.prepare(ids[0], expected_revision=2)
    assert refreshed.snapshot.cue_ids == (cue_id,)
    assert refreshed.snapshot.items[0].take_id == take_id
    assert refreshed.snapshot.items[0].artifact_id == replacement_id
    _run(service, refreshed, _activate(service, refreshed))
    with service.database.snapshot_session() as session:
        latest = session.scalar(select(ProjectSoundPlan).order_by(ProjectSoundPlan.revision.desc()))
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.plan_id == latest.id))
        assert cue.anchor_json["resolution"]["artifact_id"] == replacement_id
    with pytest.raises(ValueError, match="no word cues"):
        service.prepare(ids[0], expected_revision=3)


def test_legacy_ready_resolution_without_artifact_id_refreshes_once(setup, monkeypatch):
    service, ids = setup
    _native(monkeypatch)
    cue_id = str(uuid.uuid4())
    with service.database.immediate_session() as session:
        write_version(session, ids[0], None, session.get(GenerationSegment, ids[1]).plan_revision_id, [{
            "logical_id": cue_id, "variant_id": ids[2],
            "anchor_json": {
                "kind": "after_word", "segment_id": ids[1], "quote": "Héllo", "occurrence": 1,
                "resolution": {
                    "status": "ready", "take_id": ids[3],
                    "take_content_hash": session.get(Artifact, ids[4]).content_hash,
                    "transcript_hash": hashlib.sha256("Héllo world".encode("utf-8")).hexdigest(),
                },
            },
            "playback_json": {"mode": "insert", "offset_ms": 0},
            "included": True, "placement_status": "ready",
        }])
    prepared = service.prepare(ids[0], expected_revision=1)
    assert prepared.snapshot.cue_ids == (cue_id,)
    _run(service, prepared, _activate(service, prepared))
    with pytest.raises(ValueError, match="no word cues"):
        service.prepare(ids[0], expected_revision=2)


def test_current_ready_cues_do_not_starve_later_pending_and_lookup_is_batched(setup):
    service, ids = setup
    ready_ids = [str(uuid.UUID(int=index + 1)) for index in range(25)]
    pending_id = str(uuid.UUID(int=100))
    with service.database.immediate_session() as session:
        revision_id = session.get(GenerationSegment, ids[1]).plan_revision_id
        source_hash = session.get(Artifact, ids[4]).content_hash
        transcript_hash = hashlib.sha256("Héllo world".encode("utf-8")).hexdigest()
        cues = []
        for cue_id in [*ready_ids, pending_id]:
            pending = cue_id == pending_id
            anchor = {"kind": "after_word", "segment_id": ids[1],
                      "quote": "Héllo", "occurrence": 1}
            if not pending:
                anchor["resolution"] = {
                    "status": "ready", "take_id": ids[3], "artifact_id": ids[4],
                    "take_content_hash": source_hash, "transcript_hash": transcript_hash,
                }
            cues.append({
                "logical_id": cue_id, "variant_id": ids[2], "anchor_json": anchor,
                "playback_json": {"mode": "insert", "offset_ms": 0},
                "included": True,
                "placement_status": "needs_placement" if pending else "ready",
            })
        write_version(session, ids[0], None, revision_id, cues)
    take_selects = []

    def count_take_select(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "audio_takes" in statement:
            take_selects.append(statement)

    event.listen(service.database.engine, "before_cursor_execute", count_take_select)
    try:
        prepared = service.prepare(ids[0], expected_revision=1)
    finally:
        event.remove(service.database.engine, "before_cursor_execute", count_take_select)
    assert prepared.snapshot.cue_ids == (pending_id,)
    assert prepared.snapshot.remaining_count == 0
    assert len(take_selects) <= 2
    with service.database.immediate_session() as session:
        session.get(Artifact, ids[4]).state = "superseded"
    stale = service.prepare(ids[0], expected_revision=1)
    assert stale.snapshot.cue_ids == (pending_id, *ready_ids[:19])
    assert stale.snapshot.remaining_count == 6


@pytest.mark.parametrize(("failure", "reason"), [
    ("missing", "missing_active_take"),
    ("ambiguous", "ambiguous_active_take"),
    ("failed", "active_take_unavailable"),
    ("artifact", "active_take_unavailable"),
    ("transcript", "spoken_transcript_mismatch"),
])
def test_stale_ready_cue_is_selected_then_reports_existing_reason(
    setup, monkeypatch, failure, reason
):
    service, ids = setup
    cue_id = _cue(service, ids)
    _native(monkeypatch)
    first = service.prepare(ids[0], expected_revision=1)
    _run(service, first, _activate(service, first))
    with service.database.immediate_session() as session:
        take = session.get(AudioTake, ids[3])
        artifact = session.get(Artifact, ids[4])
        if failure == "missing":
            take.is_active = False
        elif failure == "ambiguous":
            session.add(AudioTake(
                generation_segment_id=ids[1], artifact_id=ids[4],
                status="completed", is_active=True,
            ))
        elif failure == "failed":
            take.status = "failed"
        elif failure == "artifact":
            artifact.state = "superseded"
        else:
            artifact.metadata_json = {"synthesized_text": "different transcript"}
    prepared = service.prepare(ids[0], expected_revision=2)
    assert prepared.snapshot.cue_ids == (cue_id,)
    assert prepared.snapshot.items[0].reason == reason
