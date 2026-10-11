"""Selected speech-take comparison admission and rendering acceptance."""

from __future__ import annotations

import json
import struct
import threading
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService, sha256_file
from pandrator.web.auth import Principal
from pandrator.web.comparison_export_render import (
    ComparisonExportContext,
    render_comparison_export,
)
from pandrator.web.comparison_export_routes import register_comparison_export_routes
from pandrator.web.comparison_exports import (
    prepare_comparison_export,
)
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.generation_audio_versions import (
    audio_version_take_ids,
    initialize_audio_version,
    record_audio_version_take,
)
from pandrator.web.idempotency import IdempotencyService
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    SessionRecord,
    utcnow,
)
from pandrator.web.settings_policy import stable_hash
from tests.web_test_support import prepare_web_test_data_root


def _write_pcm(path: Path, values: list[int], sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(values)}h", *values))


def _read_pcm(path: Path) -> tuple[int, list[int]]:
    with wave.open(str(path), "rb") as reader:
        values = struct.unpack(
            f"<{reader.getnframes() * reader.getnchannels()}h",
            reader.readframes(reader.getnframes()),
        )
        return reader.getframerate(), list(values)


class ComparisonRig:
    def __init__(self, root: Path):
        self.paths: DataPaths = prepare_web_test_data_root(root)
        self.database = Database(self.paths.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.jobs = JobQueue(self.database)
        with self.database.session() as session:
            self.record = SessionRecord(name="Comparison export fixture")
            session.add(self.record)
            session.flush()
            self.session_id = self.record.id
            self.storage_key = self.record.storage_key
            plan = GenerationPlan(session_id=self.session_id)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                content_hash=stable_hash({"fixture": self.session_id}),
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
            self.plan_revision_id = revision.id
            self.segment_ids = []
            for ordinal in range(2):
                segment = GenerationSegment(
                    plan_revision_id=revision.id,
                    ordinal=ordinal,
                    text=f"Fixture segment {ordinal + 1}",
                    source_segment_ids_json=[ordinal + 1],
                    speech_plan_json={"delivery": {"pace": "steady"}},
                    speaker=f"Speaker {ordinal + 1}",
                    voice_id=None,
                    voice=f"Voice {ordinal + 1}",
                    language="en",
                )
                session.add(segment)
                session.flush()
                self.segment_ids.append(segment.id)
        self.run_ids: list[str] = []
        self.take_ids: list[str] = []
        self.artifact_ids: list[str] = []

    def add_run(
        self,
        sequence: int,
        *,
        status: str = "completed",
        operation: str = "generate",
        source_run_id: str | None = None,
        job_id: str | None = None,
    ) -> str:
        settings = {"tts": {"voice": f"fixture-{sequence}"}}
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session_id,
                plan_revision_id=self.plan_revision_id,
                sequence_number=sequence,
                status=status,
                operation=operation,
                source_generation_run_id=source_run_id,
                job_id=job_id,
                settings_snapshot_json=settings,
                settings_hash=stable_hash(settings),
            )
            session.add(run)
            session.flush()
            run_id = run.id
        self.run_ids.append(run_id)
        return run_id

    def add_take(
        self,
        segment_id: str,
        run_id: str,
        values: list[int],
        *,
        active: bool,
        status: str = "completed",
        filename: str | None = None,
    ) -> tuple[str, str, Path]:
        path = self.paths.artifacts / (filename or f"take-{len(self.take_ids)}.wav")
        _write_pcm(path, values)
        artifact = self.artifacts.register(
            path,
            kind="audio",
            role="generation_take",
            session_id=self.session_id,
        )
        with self.database.session() as session:
            take = AudioTake(
                generation_segment_id=segment_id,
                generation_run_id=run_id,
                artifact_id=artifact.id,
                status=status,
                kind="tts",
                settings_hash=stable_hash({"take": filename or str(len(self.take_ids))}),
                duration_ms=1,
                is_active=active,
            )
            session.add(take)
            session.flush()
            take_id = take.id
        self.take_ids.append(take_id)
        self.artifact_ids.append(artifact.id)
        return take_id, artifact.id, path

    def request(self, clips: list[dict], *, gap_ms: int = 2, output_format: str = "wav") -> dict:
        return {"clips": clips, "gap_ms": gap_ms, "output_format": output_format}

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def rig(tmp_path: Path):
    value = ComparisonRig(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_regular_cutoff_selects_ordered_runs_and_preserves_pcm_gain(rig: ComparisonRig) -> None:
    first_run = rig.add_run(1)
    selected_run = rig.add_run(2)
    old_take, _old_artifact, _ = rig.add_take(
        rig.segment_ids[0], first_run, [100] * 8, active=False
    )
    current_take, _current_artifact, _ = rig.add_take(
        rig.segment_ids[0], selected_run, [300] * 8, active=True
    )
    second_take, _second_artifact, _ = rig.add_take(
        rig.segment_ids[1], first_run, [-200] * 8, active=True
    )
    request = rig.request(
        [
            {"generation_run_id": selected_run, "segment_id": rig.segment_ids[0]},
            {"generation_run_id": selected_run, "segment_id": rig.segment_ids[1]},
        ]
    )
    prepared = prepare_comparison_export(
        rig.database, rig.paths, rig.session_id, request
    )
    assert [item["take_id"] for item in prepared.clips] == [current_take, second_take]
    assert old_take != current_take
    payload = prepared.job_payload()
    result = render_comparison_export(
        ComparisonExportContext(rig.database, rig.paths, rig.artifacts),
        payload,
        lambda *_args: None,
        threading.Event(),
    )
    output, output_path = rig.artifacts.resolve(result["output_artifact_id"])
    rate, values = _read_pcm(output_path)
    assert rate == 8000
    assert values == [300] * 8 + [0] * 16 + [-200] * 8
    assert len(values) == 32
    assert result["duration_ms"] == 4
    manifest_artifact, manifest_path = rig.artifacts.resolve(result["manifest_artifact_id"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["audio_source"] == "speech_take"
    assert manifest["sound_mix_included"] is False
    assert manifest["sound_mix_exclusion"]
    assert manifest["output_hash"] == sha256_file(output_path) == output.content_hash
    assert manifest["clips"][0]["start_ms"] == 0
    assert manifest["clips"][0]["end_ms"] == 1
    assert manifest["clips"][1]["start_ms"] == 3
    assert manifest["clips"][1]["end_ms"] == 4
    assert manifest_artifact.kind == "json"
    assert result["artifact_ids"] == {
        "audio": output.id,
        "manifest": manifest_artifact.id,
    }
    with rig.database.session() as session:
        assert session.get(AudioTake, old_take).is_active is False
        assert session.get(AudioTake, current_take).is_active is True
        assert session.get(AudioTake, second_take).is_active is True


def test_explicit_historical_take_and_recast_side_are_exact(rig: ComparisonRig) -> None:
    root_run = rig.add_run(1)
    selected_run = rig.add_run(2)
    historical_take, _artifact_id, _path = rig.add_take(
        rig.segment_ids[0], root_run, [111, 111, 111], active=False
    )
    rig.add_take(rig.segment_ids[0], selected_run, [222, 222, 222], active=True)
    rig.add_take(rig.segment_ids[1], root_run, [-111, -111], active=True)
    explicit = prepare_comparison_export(
        rig.database,
        rig.paths,
        rig.session_id,
        rig.request(
            [
                {
                    "generation_run_id": selected_run,
                    "segment_id": rig.segment_ids[0],
                    "take_id": historical_take,
                }
            ]
        ),
    )
    assert explicit.clips[0]["take_id"] == historical_take

    running_job = Job(session_id=rig.session_id, kind="generation.run", status="running")
    with rig.database.session() as session:
        session.add(running_job)
        session.flush()
        recast = GenerationRun(
            session_id=rig.session_id,
            plan_revision_id=rig.plan_revision_id,
            sequence_number=3,
            status="running",
            operation="regenerate",
            source_generation_run_id=root_run,
            job_id=running_job.id,
            settings_snapshot_json={"tts": {"voice": "recast"}},
            settings_hash=stable_hash({"tts": {"voice": "recast"}}),
        )
        session.add(recast)
        session.flush()
        baseline = {
            rig.segment_ids[0]: historical_take,
            rig.segment_ids[1]: next(
                take_id
                for take_id in rig.take_ids
                if session.get(AudioTake, take_id).generation_segment_id == rig.segment_ids[1]
            ),
        }
        initialize_audio_version(
            session,
            recast,
            baseline,
            [rig.segment_ids[0]],
            [{"key": "speaker:1", "label": "Speaker 1"}],
        )
        recast_id = recast.id
        pinned_before = audio_version_take_ids(recast, "before")[rig.segment_ids[0]]
        session.flush()
    recast_prepared = prepare_comparison_export(
        rig.database,
        rig.paths,
        rig.session_id,
        rig.request(
            [
                {
                    "generation_run_id": recast_id,
                    "segment_id": rig.segment_ids[0],
                    "audio_version_side": "before",
                    "take_id": pinned_before,
                }
            ]
        ),
    )
    assert recast_prepared.clips[0]["take_id"] == pinned_before
    with pytest.raises(ValueError, match="Wait for recast generation"):
        prepare_comparison_export(
            rig.database,
            rig.paths,
            rig.session_id,
            rig.request(
                [
                    {
                        "generation_run_id": recast_id,
                        "segment_id": rig.segment_ids[0],
                        "audio_version_side": "after",
                    }
                ]
            ),
        )
    with pytest.raises(ValueError, match="pinned side"):
        prepare_comparison_export(
            rig.database,
            rig.paths,
            rig.session_id,
            rig.request(
                [
                    {
                        "generation_run_id": recast_id,
                        "segment_id": rig.segment_ids[0],
                        "audio_version_side": "before",
                        "take_id": historical_take,
                    }
                ]
            ),
        )

    after_take, _after_artifact, _after_path = rig.add_take(
        rig.segment_ids[0], recast_id, [333] * 8, active=False
    )
    with rig.database.session() as session:
        recast = session.get(GenerationRun, recast_id)
        job = session.get(Job, recast.job_id)
        generated = session.get(AudioTake, after_take)
        record_audio_version_take(session, recast_id, generated, applied=False)
        recast.status = "partial"
        job.status = "succeeded"
    with rig.database.session() as session:
        pinned_after = audio_version_take_ids(
            session.get(GenerationRun, recast_id), "after"
        )[rig.segment_ids[0]]
    assert pinned_after == after_take
    after = prepare_comparison_export(
        rig.database,
        rig.paths,
        rig.session_id,
        rig.request(
            [
                {
                    "generation_run_id": recast_id,
                    "segment_id": rig.segment_ids[0],
                    "audio_version_side": "after",
                    "take_id": pinned_after,
                }
            ]
        ),
    )
    assert after.clips[0]["take_id"] == after_take
    with pytest.raises(ValueError, match="pinned side"):
        prepare_comparison_export(
            rig.database,
            rig.paths,
            rig.session_id,
            rig.request(
                [
                    {
                        "generation_run_id": recast_id,
                        "segment_id": rig.segment_ids[0],
                        "audio_version_side": "after",
                        "take_id": historical_take,
                    }
                ]
            ),
        )


@pytest.mark.parametrize("mutation", ["deleted", "missing", "hash_changed", "cross_session"])
def test_unavailable_or_foreign_source_artifacts_reject_before_enqueue(
    rig: ComparisonRig,
    mutation: str,
) -> None:
    selected_run = rig.add_run(1)
    take_id, artifact_id, path = rig.add_take(
        rig.segment_ids[0], selected_run, [100, 100], active=True
    )
    if mutation == "deleted":
        with rig.database.session() as session:
            session.get(Artifact, artifact_id).state = "deleted"
    elif mutation == "missing":
        path.unlink()
    elif mutation == "hash_changed":
        _write_pcm(path, [999, 999])
    else:
        with rig.database.session() as session:
            foreign_session = SessionRecord(name="Another session")
            session.add(foreign_session)
            session.flush()
            session.get(Artifact, artifact_id).session_id = foreign_session.id
    with pytest.raises((KeyError, ValueError)):
        prepare_comparison_export(
            rig.database,
            rig.paths,
            rig.session_id,
            rig.request(
                [{"generation_run_id": selected_run, "segment_id": rig.segment_ids[0]}]
            ),
        )


def test_same_idempotency_key_replays_one_job_and_small_receipt(rig: ComparisonRig) -> None:
    run_id = rig.add_run(1)
    rig.add_take(rig.segment_ids[0], run_id, [100, 100], active=True)
    request_body = rig.request(
        [{"generation_run_id": run_id, "segment_id": rig.segment_ids[0]}]
    )
    redactor = SecretRedactor(rig.database, rig.paths)
    services = SimpleNamespace(
        database=rig.database,
        paths=rig.paths,
        jobs=rig.jobs,
        idempotency=IdempotencyService(rig.database, redactor),
    )
    principal = Principal(
        subject="comparison-export-test",
        kind="automation_client",
        scopes=frozenset({"app.run"}),
        token_id=None,
        network_zone="local",
        target_instance_id="comparison-fixture",
    )

    class Guards:
        @staticmethod
        def principal():
            return principal

        @staticmethod
        def require_scope(_scope):
            return lambda function: function

        @staticmethod
        def error_response(code, message, status, details=None):
            return jsonify(
                {"error": {"code": code, "message": message, "details": details}}
            ), status

    app = Flask("comparison-export-test")
    register_comparison_export_routes(
        app,
        SimpleNamespace(services=services, guards=Guards(), static_dir=rig.paths.root),
    )
    client = app.test_client()
    uri = f"/api/v1/sessions/{rig.session_id}/comparison-exports"
    headers = {"Idempotency-Key": "comparison-export-replay"}
    first = client.post(uri, json=request_body, headers=headers)
    replay = client.post(uri, json=request_body, headers=headers)
    assert first.status_code == 202, first.get_json()
    assert replay.status_code == 202, replay.get_json()
    assert len(first.data) < 4096
    assert first.get_json() == replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    with rig.database.session() as session:
        jobs = list(
            session.scalars(
                select(Job).where(Job.kind == "export.comparison")
            )
        )
        assert len(jobs) == 1
        assert jobs[0].session_id == rig.session_id
        assert jobs[0].resource_keys_json == [f"session:{rig.session_id}"]
        assert jobs[0].max_attempts == 1
        assert session.get(AudioTake, rig.take_ids[0]).is_active is True


def test_worker_rejects_a_source_hash_changed_after_queue(rig: ComparisonRig) -> None:
    run_id = rig.add_run(1)
    _take_id, _artifact_id, path = rig.add_take(
        rig.segment_ids[0], run_id, [100, 100, 100], active=True
    )
    prepared = prepare_comparison_export(
        rig.database,
        rig.paths,
        rig.session_id,
        rig.request(
            [{"generation_run_id": run_id, "segment_id": rig.segment_ids[0]}]
        ),
    )
    _write_pcm(path, [101, 101, 101])
    with pytest.raises(ValueError, match="changed after it was selected"):
        render_comparison_export(
            ComparisonExportContext(rig.database, rig.paths, rig.artifacts),
            prepared.job_payload(),
            lambda *_args: None,
            threading.Event(),
        )


@pytest.mark.parametrize("lease_state", ["current", "expired", "stale_generation"])
def test_queued_worker_fences_sqlite_loaded_lease(
    rig: ComparisonRig, lease_state: str
) -> None:
    run_id = rig.add_run(1)
    rig.add_take(rig.segment_ids[0], run_id, [100] * 8, active=True)
    prepared = prepare_comparison_export(
        rig.database,
        rig.paths,
        rig.session_id,
        rig.request([{"generation_run_id": run_id, "segment_id": rig.segment_ids[0]}]),
    )
    queued = rig.jobs.enqueue(
        "export.comparison", prepared.job_payload(), session_id=rig.session_id
    )
    claimed = rig.jobs.claim("comparison-fixture-worker")
    assert claimed is not None and claimed.id == queued.id
    with rig.database.session() as session:
        job = session.get(Job, claimed.id)
        assert job is not None and job.lease_expires_at is not None
        assert job.lease_expires_at.tzinfo is None
        if lease_state == "expired":
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
        elif lease_state == "stale_generation":
            job.lease_generation += 1
    payload = {
        **prepared.job_payload(),
        "_job_id": claimed.id,
        "_lease_generation": claimed.lease_generation,
    }
    token = current_job_lease.set(
        {
            "job_id": claimed.id,
            "owner_id": "comparison-fixture-worker",
            "lease_generation": claimed.lease_generation,
        }
    )
    try:
        if lease_state == "current":
            result = render_comparison_export(
                ComparisonExportContext(rig.database, rig.paths, rig.artifacts),
                payload,
                lambda *_args: None,
                threading.Event(),
            )
            assert rig.artifacts.resolve(result["output_artifact_id"])[1].is_file()
            assert rig.artifacts.resolve(result["manifest_artifact_id"])[1].is_file()
        else:
            with pytest.raises(InterruptedError, match="lease is no longer current"):
                render_comparison_export(
                    ComparisonExportContext(rig.database, rig.paths, rig.artifacts),
                    payload,
                    lambda *_args: None,
                    threading.Event(),
                )
            with rig.database.session() as session:
                assert len(list(session.scalars(select(Artifact)))) == 1
    finally:
        current_job_lease.reset(token)
