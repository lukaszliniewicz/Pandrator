"""Capture Phase 0 correctness and performance baselines on disposable data.

The command records known defects without failing merely because a future target
is not met. Use ``--include-browser`` to include the Playwright request-fan-out
characterization.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pydub import AudioSegment
from sqlalchemy import event, select
from sqlalchemy.orm import Session as OrmSession

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.audio_assembly import (
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
)
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.capabilities import probe_stable_capabilities
from pandrator.web.database import Database, upgrade_database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationRun,
    GenerationSegment,
    Job,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace import GenerationService, WorkspaceSettingsService


TARGET_BUDGETS = {
    "job_claim": {
        "max_successful_claimers_per_job": 1,
        "description": "One queued job is returned to exactly one concurrent worker.",
    },
    "resource_acquisition": {
        "max_uncaught_errors": 0,
        "description": "Expected resource contention is returned as unavailable, not raised.",
    },
    "workflow_snapshot": {
        "history_size_affects_current_state_rows": False,
        "description": "Current-state snapshots do not load complete artifact and job history.",
    },
    "generation_assembly": {
        "segment_count_affects_select_count": False,
        "description": "Take and artifact selection is batched for the complete assembly.",
    },
    "capabilities": {
        "slow_probe_calls_per_job_event": 0,
        "description": "Job events do not invoke hardware, cache, or FFmpeg discovery.",
    },
    "audio_composition": {
        "memory_growth_tracks_output_duration": False,
        "description": "Assembly memory remains bounded instead of retaining the complete output.",
    },
    "event_request_fanout": {
        "unrelated_resource_requests_per_event": 0,
        "description": "A job event invalidates only its affected resources.",
    },
}


def _round_ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


def _error_payload(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


def _normalize_sql(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip()


class QueryCounter:
    def __init__(self, database: Database):
        self.database = database
        self.statements: list[str] = []
        self.orm_loads: Counter[str] = Counter()

    def _record(
        self,
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.append(_normalize_sql(statement))

    def __enter__(self) -> "QueryCounter":
        event.listen(self.database.engine, "before_cursor_execute", self._record)
        event.listen(OrmSession, "loaded_as_persistent", self._record_orm_load)
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        event.remove(self.database.engine, "before_cursor_execute", self._record)
        event.remove(OrmSession, "loaded_as_persistent", self._record_orm_load)

    def _record_orm_load(self, _session: OrmSession, instance: object) -> None:
        self.orm_loads[type(instance).__name__] += 1

    def summary(self) -> dict[str, Any]:
        repeated = Counter(self.statements)
        return {
            "select_count": len(self.statements),
            "unique_select_shapes": len(repeated),
            "orm_objects_loaded": sum(self.orm_loads.values()),
            "orm_loads_by_type": dict(self.orm_loads.most_common()),
            "most_repeated": [
                {"count": count, "statement": statement[:320]}
                for statement, count in repeated.most_common(8)
            ],
        }


@contextmanager
def _disposable_database(root: Path) -> Iterator[tuple[DataPaths, Database]]:
    paths = DataPaths.from_value(root).ensure()
    upgrade_database(paths.database)
    database = Database(paths.database)
    try:
        yield paths, database
    finally:
        database.dispose()


def _run_concurrently(
    database_path: Path,
    contenders: int,
    callback: Callable[[JobQueue, int], Any],
    *,
    delay_statement: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    start = threading.Barrier(contenders)

    def run(index: int) -> dict[str, Any]:
        database = Database(database_path)
        delayed = False

        def delay_after_read(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            nonlocal delayed
            if delayed or delay_statement is None or not delay_statement(_normalize_sql(statement)):
                return
            delayed = True
            # Widen the read/write gap deterministically without modifying the
            # production queue implementation.
            time.sleep(0.02)

        if delay_statement is not None:
            event.listen(database.engine, "after_cursor_execute", delay_after_read)
        try:
            start.wait(timeout=10)
            return {"worker": index, "value": callback(JobQueue(database), index)}
        except BaseException as error:
            return {"worker": index, "error": _error_payload(error)}
        finally:
            if delay_statement is not None:
                event.remove(database.engine, "after_cursor_execute", delay_after_read)
            database.dispose()

    with ThreadPoolExecutor(max_workers=contenders) as executor:
        return list(executor.map(run, range(contenders)))


def benchmark_job_claims(
    database: Database,
    *,
    trials: int,
    contenders: int,
) -> dict[str, Any]:
    queue = JobQueue(database)
    duplicate_trials = 0
    no_winner_trials = 0
    error_trials = 0
    maximum_claimers = 0
    samples: list[dict[str, Any]] = []
    started = time.perf_counter()

    def is_candidate_read(statement: str) -> bool:
        upper = statement.upper()
        return (
            upper.startswith("SELECT")
            and " FROM JOBS " in f" {upper} "
            and "ORDER BY JOBS.CREATED_AT ASC" in upper
        )

    for trial in range(trials):
        queued = queue.enqueue(
            "phase0.claim",
            {"trial": trial},
            max_attempts=max(2, contenders + 1),
        )
        results = _run_concurrently(
            database.path,
            contenders,
            lambda candidate_queue, index: (
                claimed.id if (claimed := candidate_queue.claim(f"phase0-claim-{trial}-{index}")) else None
            ),
            delay_statement=is_candidate_read,
        )
        claimed_ids = [
            str(result["value"])
            for result in results
            if result.get("value") is not None
        ]
        claimers_for_job = sum(claimed_id == queued.id for claimed_id in claimed_ids)
        maximum_claimers = max(maximum_claimers, claimers_for_job)
        if claimers_for_job > 1:
            duplicate_trials += 1
        if claimers_for_job == 0:
            no_winner_trials += 1
        if any("error" in result for result in results):
            error_trials += 1
        if len(samples) < 3 and (claimers_for_job != 1 or any("error" in result for result in results)):
            samples.append(
                {
                    "trial": trial,
                    "queued_job_id": queued.id,
                    "claimers_for_job": claimers_for_job,
                    "results": results,
                }
            )

    return {
        "trials": trials,
        "contenders": contenders,
        "duplicate_trials": duplicate_trials,
        "no_winner_trials": no_winner_trials,
        "error_trials": error_trials,
        "maximum_claimers_for_one_job": maximum_claimers,
        "target_met": duplicate_trials == 0 and no_winner_trials == 0 and error_trials == 0,
        "elapsed_ms": _round_ms(time.perf_counter() - started),
        "samples": samples,
    }


def benchmark_resource_acquisition(
    database: Database,
    *,
    trials: int,
) -> dict[str, Any]:
    queue = JobQueue(database)
    error_trials = 0
    double_success_trials = 0
    no_winner_trials = 0
    samples: list[dict[str, Any]] = []
    started = time.perf_counter()

    def is_conflict_read(statement: str) -> bool:
        upper = statement.upper()
        return upper.startswith("SELECT") and "RESOURCE_CLAIMS" in upper

    for trial in range(trials):
        first = queue.enqueue("phase0.resource", {"trial": trial, "worker": 0})
        second = queue.enqueue("phase0.resource", {"trial": trial, "worker": 1})
        jobs = (first, second)
        claimed = (
            queue.claim(f"phase0-resource-{trial}-0"),
            queue.claim(f"phase0-resource-{trial}-1"),
        )
        resource_key = f"phase0-resource:{trial}"
        results = _run_concurrently(
            database.path,
            2,
            lambda candidate_queue, index: candidate_queue.acquire_resources(
                jobs[index].id,
                f"phase0-resource-{trial}-{index}",
                [resource_key],
                lease_generation=claimed[index].lease_generation,
            ),
            delay_statement=is_conflict_read,
        )
        successes = sum(result.get("value") is True for result in results)
        errors = [result for result in results if "error" in result]
        if successes > 1:
            double_success_trials += 1
        if successes == 0:
            no_winner_trials += 1
        if errors:
            error_trials += 1
        if len(samples) < 3 and (errors or successes != 1):
            samples.append(
                {
                    "trial": trial,
                    "resource_key": resource_key,
                    "successes": successes,
                    "results": results,
                }
            )

    return {
        "trials": trials,
        "contenders": 2,
        "error_trials": error_trials,
        "double_success_trials": double_success_trials,
        "no_winner_trials": no_winner_trials,
        "target_met": error_trials == 0 and double_success_trials == 0 and no_winner_trials == 0,
        "elapsed_ms": _round_ms(time.perf_counter() - started),
        "samples": samples,
    }


def benchmark_workflow_snapshot(
    database: Database,
    *,
    artifact_count: int,
    job_count: int,
) -> dict[str, Any]:
    record = SessionService(database).create("Phase 0 workflow baseline", workflow_kind="audiobook")
    roles = ("clean_text", "prepared_text", "tts_optimized", "export", "artifact")
    kinds = (
        "source.clean",
        "text.prepare",
        "text.optimize_tts",
        "audiobook.generate_audio",
        "export.create",
    )
    with database.session() as session:
        session.add_all(
            [
                Artifact(
                    session_id=record.id,
                    kind="text",
                    role=roles[index % len(roles)],
                    relative_path=f"phase0/workflow/artifact-{index}.txt",
                    size_bytes=32,
                    state="current",
                )
                for index in range(artifact_count)
            ]
        )
        session.add_all(
            [
                Job(
                    session_id=record.id,
                    kind=kinds[index % len(kinds)],
                    status="succeeded",
                    payload_json={"fixture": index},
                    result_json={},
                )
                for index in range(job_count)
            ]
        )

    started = time.perf_counter()
    with QueryCounter(database) as queries:
        snapshot = WorkflowService(database, JobQueue(database)).snapshot(record.id)
    elapsed = time.perf_counter() - started
    return {
        "fixture": {
            "artifacts": artifact_count,
            "jobs": job_count,
            "visible_stages": len(snapshot.get("stages") or []),
        },
        **queries.summary(),
        "elapsed_ms": _round_ms(elapsed),
        "response_json_bytes": len(json.dumps(snapshot, default=str).encode("utf-8")),
    }


def benchmark_generation_assembly(
    paths: DataPaths,
    database: Database,
    *,
    segment_count: int,
    run_scoped: bool = False,
) -> dict[str, Any]:
    jobs = JobQueue(database)
    settings = WorkspaceSettingsService(database)
    generation = GenerationService(database, jobs, settings)
    record = SessionService(database).create("Phase 0 assembly baseline", workflow_kind="audiobook")
    session_directory = paths.sessions / record.storage_key
    session_directory.mkdir(parents=True, exist_ok=True)
    plan = generation.create_plan(
        record.id,
        source_revision_id=None,
        segments=[
            {
                "text": f"Segment {index + 1}",
                "silence_after_ms": 5,
                "node_kind": "paragraph",
            }
            for index in range(segment_count)
        ],
    )
    output = settings.get(record.id, "output")
    settings.update(record.id, "output", output["revision"], {"format": "wav"})

    template_audio = session_directory / "phase0-template.wav"
    exported = AudioSegment.silent(duration=20, frame_rate=16000).export(template_audio, format="wav")
    exported.close()
    artifacts = ArtifactService(database, paths)
    with database.session() as session:
        segments = list(
            session.scalars(
                select(GenerationSegment)
                .where(GenerationSegment.plan_revision_id == plan["active_revision_id"])
                .order_by(GenerationSegment.ordinal)
            ).all()
        )
        generation_run_id = None
        if run_scoped:
            run = GenerationRun(
                session_id=record.id,
                plan_revision_id=plan["active_revision_id"],
                sequence_number=1,
                status="completed",
                settings_snapshot_json={},
            )
            session.add(run)
            session.flush()
            generation_run_id = run.id

    take_artifact_ids: list[str] = []
    for index in range(segment_count):
        path = session_directory / f"phase0-take-{index}.wav"
        shutil.copyfile(template_audio, path)
        artifact = artifacts.register(
            path,
            kind="audio",
            role="generation_take",
            session_id=record.id,
            calculate_hash=False,
        )
        take_artifact_ids.append(artifact.id)

    with database.session() as session:
        for segment, artifact_id in zip(segments, take_artifact_ids):
            managed = session.get(GenerationSegment, segment.id)
            managed.status = "completed"
            session.add(
                AudioTake(
                    generation_segment_id=segment.id,
                    generation_run_id=generation_run_id,
                    artifact_id=artifact_id,
                    kind="tts",
                    status="completed",
                    duration_ms=20,
                    is_active=True,
                )
            )

    assembly = generation.create_assembly(
        record.id,
        generation_run_id=generation_run_id,
    )
    started = time.perf_counter()
    with QueryCounter(database) as queries:
        result = WorkflowHandlers(database, paths).assemble_generation_output(
            {"output_assembly_id": assembly["id"]},
            lambda *_args: None,
            threading.Event(),
        )
    elapsed = time.perf_counter() - started
    return {
        "fixture": {
            "segments": segment_count,
            "take_duration_ms": 20,
            "silence_after_ms": 5,
            "run_scoped": run_scoped,
        },
        **queries.summary(),
        "selects_per_segment": round(len(queries.statements) / max(1, segment_count), 3),
        "elapsed_ms": _round_ms(elapsed),
        "output_duration_ms": result.get("duration_ms"),
    }


def benchmark_capability_endpoint(root: Path, *, runs: int) -> dict[str, Any]:
    from pandrator.web.api import create_app

    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(data_root=root, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    authenticated = client.post("/api/v1/auth/bootstrap", json={"token": token})
    if authenticated.status_code != 200:
        raise RuntimeError(f"Capability baseline could not authenticate: HTTP {authenticated.status_code}")

    durations: list[float] = []
    statuses: list[int] = []
    try:
        with mock.patch(
            "pandrator.web.capabilities.probe_stable_capabilities",
            wraps=probe_stable_capabilities,
        ) as wrapped:
            for _index in range(runs):
                started = time.perf_counter()
                response = client.get("/api/v1/capabilities")
                durations.append(_round_ms(time.perf_counter() - started))
                statuses.append(response.status_code)
            invocation_count = wrapped.call_count
    finally:
        app.extensions["pandrator"]["database"].dispose()

    return {
        "requests": runs,
        "probe_invocations": invocation_count,
        "probe_invocations_per_request": round(invocation_count / max(1, runs), 3),
        "warm_requests_without_probe": max(0, runs - invocation_count),
        "request_duration_ms": durations,
        "statuses": statuses,
        "all_requests_succeeded": all(status == 200 for status in statuses),
    }


def benchmark_audio_composition(segment_counts: list[int]) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    duration_observations: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pandrator-audio-baseline-") as directory:
        root = Path(directory)
        source = root / "source.wav"
        AudioSegment.silent(duration=20, frame_rate=16000).export(
            source,
            format="wav",
        ).close()
        for count in segment_counts:
            plan = build_audio_assembly_plan(
                [AudioAssemblyPart(source, 20) for _index in range(count)],
                output_format="wav",
                sample_rate_hz=16000,
                channels=1,
            )
            destination = root / f"assembled-{count}.wav"
            tracemalloc.start()
            started = time.perf_counter()
            result = assemble_audio_plan(plan, destination)
            elapsed = time.perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            observations.append(
                {
                    "segments": count,
                    "output_duration_ms": result.duration_ms,
                    "elapsed_ms": _round_ms(elapsed),
                    "python_peak_bytes": peak,
                    "python_peak_bytes_per_output_second": round(
                        peak / max(0.001, result.duration_ms / 1000),
                        3,
                    ),
                }
            )
        for duration_ms in (10000, 20000, 40000):
            duration_source = root / f"duration-{duration_ms}.wav"
            AudioSegment.silent(
                duration=duration_ms,
                frame_rate=16000,
            ).export(duration_source, format="wav").close()
            duration_plan = build_audio_assembly_plan(
                [AudioAssemblyPart(duration_source, duration_ms)],
                output_format="wav",
                sample_rate_hz=16000,
                channels=1,
            )
            tracemalloc.start()
            started = time.perf_counter()
            duration_result = assemble_audio_plan(
                duration_plan,
                root / f"duration-output-{duration_ms}.wav",
            )
            elapsed = time.perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            duration_observations.append(
                {
                    "segments": 1,
                    "output_duration_ms": duration_result.duration_ms,
                    "elapsed_ms": _round_ms(elapsed),
                    "python_peak_bytes": peak,
                }
            )
    segment_peaks = [int(item["python_peak_bytes"]) for item in observations]
    duration_peaks = [
        int(item["python_peak_bytes"])
        for item in duration_observations
    ]
    smallest_duration_peak = min(duration_peaks, default=0)
    largest_duration_peak = max(duration_peaks, default=0)
    return {
        "segment_duration_ms": 20,
        "segment_scaling": observations,
        "duration_scaling": duration_observations,
        "duration_peak_growth_ratio": round(
            largest_duration_peak / max(1, smallest_duration_peak),
            3,
        ),
        "target_met": (
            not duration_peaks
            or largest_duration_peak
            <= max(
                2 * smallest_duration_peak,
                smallest_duration_peak + 1024 * 1024,
            )
        ),
        "segment_metadata_peak_bytes": max(segment_peaks, default=0),
        "note": (
            "Duration scaling holds the plan at one segment; segment scaling "
            "captures the expected O(segment-count) metadata. Tracemalloc "
            "reports Python allocations, and compatible PCM takes are "
            "stream-copied without a native decoder."
        ),
    }


def benchmark_event_request_fanout() -> dict[str, Any]:
    npm = shutil.which("npm")
    if not npm:
        return {"available": False, "error": "npm was not found on PATH."}
    environment = os.environ.copy()
    environment["PANDRATOR_PHASE0_BASELINE"] = "1"
    environment["CI"] = "1"
    command = [
        npm,
        "run",
        "test:e2e",
        "--",
        "phase0-baseline.spec.ts",
        "--project=chromium",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT / "web",
            env=environment,
            capture_output=True,
            text=True,
            timeout=360,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": True, "error": _error_payload(error)}
    output = f"{completed.stdout}\n{completed.stderr}"
    marker = "PHASE0_EVENT_FANOUT="
    payload = None
    for line in output.splitlines():
        if marker in line:
            try:
                payload = json.loads(line.split(marker, 1)[1])
            except json.JSONDecodeError:
                continue
    result: dict[str, Any] = {
        "available": True,
        "command": command[1:],
        "return_code": completed.returncode,
    }
    if payload is not None:
        result["observation"] = payload
    else:
        result["error"] = "Playwright completed without emitting the baseline marker."
        result["output_tail"] = output[-3000:]
    return result


def capture_baseline(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pandrator-phase0-") as directory:
        root = Path(directory)
        with _disposable_database(root / "backend") as (paths, database):
            observations: dict[str, Any] = {
                "job_claim": benchmark_job_claims(
                    database,
                    trials=args.claim_trials,
                    contenders=args.claim_contenders,
                ),
                "resource_acquisition": benchmark_resource_acquisition(
                    database,
                    trials=args.resource_trials,
                ),
                "workflow_snapshot": benchmark_workflow_snapshot(
                    database,
                    artifact_count=args.history_artifacts,
                    job_count=args.history_jobs,
                ),
                "generation_assembly": benchmark_generation_assembly(
                    paths,
                    database,
                    segment_count=args.assembly_segments,
                ),
                "audio_composition": benchmark_audio_composition(args.audio_segments),
            }
        if not args.skip_capabilities:
            observations["capabilities"] = benchmark_capability_endpoint(
                root / "capabilities",
                runs=args.capability_runs,
            )
        if args.include_browser:
            observations["event_request_fanout"] = benchmark_event_request_fanout()

    return {
        "schema_version": 1,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "target_budgets": TARGET_BUDGETS,
        "observations": observations,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the JSON result to this path as well as stdout.")
    parser.add_argument("--claim-trials", type=int, default=30)
    parser.add_argument("--claim-contenders", type=int, default=6)
    parser.add_argument("--resource-trials", type=int, default=30)
    parser.add_argument("--history-artifacts", type=int, default=250)
    parser.add_argument("--history-jobs", type=int, default=1000)
    parser.add_argument("--assembly-segments", type=int, default=50)
    parser.add_argument(
        "--audio-segments",
        type=lambda value: [max(1, int(item)) for item in value.split(",")],
        default=[100, 500, 1000, 2000],
        help="Comma-separated composition sizes.",
    )
    parser.add_argument("--capability-runs", type=int, default=2)
    parser.add_argument("--skip-capabilities", action="store_true")
    parser.add_argument("--include-browser", action="store_true")
    args = parser.parse_args()
    for name in (
        "claim_trials",
        "claim_contenders",
        "resource_trials",
        "history_artifacts",
        "history_jobs",
        "assembly_segments",
        "capability_runs",
    ):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    result = capture_baseline(args)
    serialized = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
