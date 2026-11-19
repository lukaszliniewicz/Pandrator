"""Resource waiters must not starve independent durable work."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import event

from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import Job, ResourceClaim, utcnow
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def queue(tmp_path):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    try:
        yield JobQueue(database), database
    finally:
        database.dispose()


def hold(queue, keys):
    job = queue.enqueue("hold", resource_keys=keys, max_attempts=2)
    claimed = queue.claim("holder")
    assert claimed is not None and claimed.id == job.id
    assert queue.acquire_resources(
        job.id, "holder", keys, lease_generation=claimed.lease_generation
    )
    return claimed


def test_scan_passes_more_than_one_page_of_waiters_without_spending_attempts(queue):
    jobs, database = queue
    owner = hold(jobs, ["service:tts:xtts"])
    # Exercise a legacy alias held by a still-running older worker.
    with database.immediate_session() as unit:
        claim = unit.get(ResourceClaim, "service:tts:xtts")
        assert claim is not None
        claim.resource_key = "service:tts:XTTS"
        waiters = [
            jobs.enqueue_in_session(unit, "blocked", resource_keys=["service:tts:xtts"])
            for _ in range(150)
        ]
    independent = jobs.enqueue("render", resource_keys=["session:independent"])
    chosen = jobs.claim("other-worker")
    assert chosen is not None and chosen.id == independent.id
    with database.snapshot_session() as unit:
        stored_waiters = []
        for waiter in waiters:
            stored = unit.get(Job, waiter.id)
            assert stored is not None
            assert (stored.status, stored.attempts, stored.available_at) == ("queued", 0, None)
            stored_waiters.append(stored)
        # The queue is first in, first out by (created_at, id). Waiters enqueued
        # within one clock tick share created_at (about 15 ms on Windows), so the
        # first in line is the lowest id among them, not necessarily waiters[0].
        first = min(stored_waiters, key=lambda job: (job.created_at, job.id)).id
    jobs.release_resources(owner.id, "holder", lease_generation=owner.lease_generation)
    resumed = jobs.claim("other-worker")
    assert resumed is not None and resumed.id == first


def test_all_blocked_poll_does_not_take_writer_lock(queue):
    jobs, database = queue
    hold(jobs, ["gpu:0"])
    waiter = jobs.enqueue("blocked", resource_keys=["gpu:0"])
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        assert jobs.claim("idle-worker") is None
    finally:
        event.remove(database.engine, "before_cursor_execute", observe)
    assert "BEGIN IMMEDIATE" not in statements
    assert jobs.get(waiter.id).attempts == 0


def test_empty_queue_retains_one_read_and_no_writer_or_resource_scan(queue):
    jobs, database = queue
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        assert jobs.claim("idle-worker") is None
    finally:
        event.remove(database.engine, "before_cursor_execute", observe)
    reads = [statement for statement in statements if statement.startswith("SELECT")]
    assert len(reads) == 1
    assert "RESOURCE_CLAIMS" not in reads[0]
    assert "BEGIN IMMEDIATE" not in statements


@pytest.mark.parametrize("held_key,requested_key", [
    ("gpu:cuda", "gpu:vulkan"), ("gpu:0", "gpu:default"),
    ("gpu:metal", "gpu:0"), ("service:tts:Audio.cpp", "service:tts:audio_cpp"),
])
def test_aliases_cannot_acquire_one_device_or_service_twice(queue, held_key, requested_key):
    jobs, _database = queue
    holder = jobs.enqueue("first", resource_keys=[held_key])
    contender = jobs.enqueue("second", resource_keys=[requested_key])
    first, second = jobs.claim("first"), jobs.claim("second")
    assert first is not None and second is not None
    assert jobs.acquire_resources(holder.id, "first", [held_key], lease_generation=first.lease_generation)
    assert not jobs.acquire_resources(
        contender.id, "second", [requested_key], lease_generation=second.lease_generation,
    )
    assert jobs.defer_for_resources(contender.id, "second", lease_generation=second.lease_generation, retry_delay_seconds=0)
    assert jobs.claim("second") is None


def test_expired_claim_does_not_hide_queued_work(queue):
    jobs, database = queue
    hold(jobs, ["gpu:0"])
    with database.immediate_session() as unit:
        claim = unit.get(ResourceClaim, "gpu:0")
        assert claim is not None
        claim.expires_at = utcnow() - timedelta(seconds=1)
    waiter = jobs.enqueue("next", resource_keys=["gpu:0"])
    chosen = jobs.claim("next-worker")
    assert chosen is not None and chosen.id == waiter.id
    assert jobs.acquire_resources(
        chosen.id, "next-worker", chosen.resource_keys_json,
        lease_generation=chosen.lease_generation,
    )


def test_reclaim_preserves_generation_fence_even_with_own_old_claim(queue):
    jobs, database = queue
    owner = hold(jobs, ["gpu:0"])
    with database.immediate_session() as unit:
        job = unit.get(Job, owner.id)
        assert job is not None
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
    replacement = jobs.claim("replacement")
    assert replacement is not None and replacement.id == owner.id
    assert replacement.lease_generation == owner.lease_generation + 1
    assert not jobs.heartbeat(owner.id, "holder", lease_generation=owner.lease_generation)
    assert jobs.acquire_resources(
        owner.id, "replacement", replacement.resource_keys_json,
        lease_generation=replacement.lease_generation,
    )


def test_session_serialization_is_retained_for_disjoint_resources(queue):
    jobs, database = queue
    record = SessionService(database).create("One session")
    jobs.enqueue("first", session_id=record.id, resource_keys=["first:resource"])
    waiter = jobs.enqueue("second", session_id=record.id, resource_keys=["second:resource"])
    independent = jobs.enqueue("independent")
    assert jobs.claim("first-worker") is not None
    chosen = jobs.claim("second-worker")
    assert chosen is not None and chosen.id == independent.id
    assert jobs.get(waiter.id).attempts == 0


def test_independent_workers_overlap_cloud_and_local_work_behind_waiter(queue):
    jobs, database = queue
    cloud = jobs.enqueue("cloud", resource_keys=["service:tts:openai"])
    blocked = jobs.enqueue("cloud", resource_keys=["service:tts:openai"])
    local = jobs.enqueue("local", resource_keys=["service:tts:xtts", "gpu:0"])
    cloud_started, local_started, finish = Event(), Event(), Event()
    other_database = Database(database.path)

    def cloud_handler(_payload, _progress, _cancel):
        cloud_started.set()
        assert finish.wait(10)
        return {"cloud": True}

    def local_handler(_payload, _progress, _cancel):
        assert cloud_started.is_set() and not finish.is_set()
        local_started.set()
        return {"local": True}

    first = Worker(jobs, "cloud-worker", {"cloud": cloud_handler})
    second = Worker(JobQueue(other_database), "local-worker", {"local": local_handler})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            cloud_future = executor.submit(first.run_once)
            try:
                assert cloud_started.wait(10)
                local_future = executor.submit(second.run_once)
                assert local_future.result(timeout=10)
                assert local_started.is_set()
                assert jobs.get(cloud.id).status == "running"
                assert jobs.get(local.id).status == "succeeded"
                assert jobs.get(blocked.id).attempts == 0
            finally:
                finish.set()
            assert cloud_future.result(timeout=10)
        assert jobs.get(cloud.id).status == "succeeded"
    finally:
        finish.set()
        other_database.dispose()


def test_kind_filtered_claim_takes_only_its_kinds_in_queue_order(queue):
    jobs, _database = queue
    generation = jobs.enqueue("generation.run")
    first_export = jobs.enqueue("export.create")
    second_export = jobs.enqueue("export.variant")
    lane_kinds = {"export.create", "export.variant"}
    chosen = jobs.claim("lane", kinds=lane_kinds)
    assert chosen is not None and chosen.id == first_export.id
    assert jobs.claim("main").id == generation.id
    assert jobs.claim("lane", kinds=lane_kinds).id == second_export.id
    assert jobs.claim("lane", kinds=lane_kinds) is None


def test_delivery_lane_exports_beside_a_long_generation(queue):
    jobs, database = queue
    sessions = SessionService(database)
    generating = sessions.create("Generating").id
    other = sessions.create("Other language").id
    generation = jobs.enqueue(
        "generation.run", session_id=generating, resource_keys=[f"session:{generating}"]
    )
    export = jobs.enqueue("export.create", session_id=other, resource_keys=[f"session:{other}"])
    same_project = jobs.enqueue(
        "export.create", session_id=generating, resource_keys=[f"session:{generating}"]
    )
    started, finish = Event(), Event()
    other_database = Database(database.path)

    def generate(_payload, _progress, _cancel):
        started.set()
        assert finish.wait(10)
        return {}

    main = Worker(jobs, "main", {"generation.run": generate})
    lane = Worker(
        JobQueue(other_database), "main.delivery", {"export.create": lambda *_: {}},
        kinds={"export.create"},
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(main.run_once)
            try:
                assert started.wait(10)
                assert lane.run_once()
                assert jobs.get(export.id).status == "succeeded"
                assert jobs.get(generation.id).status == "running"
                # One project's jobs still run one at a time.
                assert not lane.run_once()
                assert (jobs.get(same_project.id).status, jobs.get(same_project.id).attempts) == (
                    "queued", 0,
                )
            finally:
                finish.set()
            assert running.result(timeout=10)
        assert lane.run_once()
        assert jobs.get(same_project.id).status == "succeeded"
    finally:
        finish.set()
        other_database.dispose()


def test_delivery_lane_never_overtakes_older_work_in_its_session(queue):
    jobs, database = queue
    sessions = SessionService(database)
    busy = sessions.create("Busy elsewhere").id
    project = sessions.create("Assemble then export").id
    lane_kinds = {"export.create"}
    jobs.enqueue("generation.run", session_id=busy)
    assemble = jobs.enqueue("generation.assemble", session_id=project)
    export = jobs.enqueue("export.create", session_id=project)
    assert jobs.claim("main").session_id == busy
    # The export must wait for the assembly queued before it.
    assert jobs.claim("main.delivery", kinds=lane_kinds) is None
    claimed = jobs.claim("second")
    assert claimed is not None and claimed.id == assemble.id
    jobs.complete(claimed.id, "second", {}, lease_generation=claimed.lease_generation)
    chosen = jobs.claim("main.delivery", kinds=lane_kinds)
    assert chosen is not None and chosen.id == export.id


def test_delivery_lane_waits_for_older_work_with_a_lapsed_lease(queue):
    jobs, database = queue
    project = SessionService(database).create("Assembly lease lapsed").id
    assemble = jobs.enqueue(
        "generation.assemble", session_id=project, max_attempts=2,
        resource_keys=[f"session:{project}"],
    )
    export = jobs.enqueue("export.create", session_id=project)
    claimed = jobs.claim("crashed")
    assert claimed is not None and claimed.id == assemble.id
    assert jobs.acquire_resources(
        assemble.id, "crashed", [f"session:{project}"], lease_generation=claimed.lease_generation,
    )
    expired = utcnow() - timedelta(seconds=1)
    with database.immediate_session() as unit:
        unit.get(Job, assemble.id).lease_expires_at = expired
        unit.get(ResourceClaim, f"session:{project}").expires_at = expired
    # The assembly will be reclaimed; the export must not run before it.
    assert jobs.claim("main.delivery", kinds={"export.create"}) is None
    assert jobs.get(assemble.id).status == "running"
    reclaimed = jobs.claim("main")
    assert reclaimed is not None and reclaimed.id == assemble.id
    jobs.complete(assemble.id, "main", {}, lease_generation=reclaimed.lease_generation)
    chosen = jobs.claim("main.delivery", kinds={"export.create"})
    assert chosen is not None and chosen.id == export.id


def test_worker_delivery_lane_handles_every_delivery_kind(tmp_path):
    from pandrator.web.cli import _delivery_lane
    from pandrator.web.comparison_exports import COMPARISON_EXPORT_JOB_KIND
    from pandrator.web.job_handler_domains import DELIVERY_LANE_JOB_KINDS
    from pandrator.web.tts_providers import TtsProviderRegistry

    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    providers = TtsProviderRegistry()
    try:
        lane = _delivery_lane(database, paths, JobQueue(database), providers, "worker")
        assert lane is not None and lane.worker_id == "worker.delivery"
        assert lane.kinds == DELIVERY_LANE_JOB_KINDS == frozenset(lane.handlers)
        assert COMPARISON_EXPORT_JOB_KIND in DELIVERY_LANE_JOB_KINDS
        assert "subtitle.evidence" in DELIVERY_LANE_JOB_KINDS
    finally:
        providers.close()
        database.dispose()


def test_jobs_released_together_show_only_the_one_that_really_started(queue):
    """Field test 15:20: three queued Vertex jobs looked like they all started."""

    jobs, database = queue
    sessions = SessionService(database)
    held = sessions.create("Dutch running").id
    holder = jobs.enqueue(
        "generation.run",
        session_id=held,
        resource_keys=[f"session:{held}", "service:tts:vertex_ai"],
        max_attempts=2,
    )
    targets = []
    for name in ("German", "Japanese", "Hungarian"):
        session_id = sessions.create(name).id
        targets.append(
            jobs.enqueue(
                "generation.run",
                session_id=session_id,
                resource_keys=[f"session:{session_id}", "service:tts:vertex_ai"],
                max_attempts=2,
            )
        )
    running = jobs.claim("holder")
    assert running is not None and running.id == holder.id
    assert jobs.acquire_resources(
        holder.id, "holder", list(running.resource_keys_json or []),
        lease_generation=running.lease_generation,
    )
    # The running job is cancelled but keeps its lease and claim until it stops.
    jobs.request_cancel(holder.id)

    # Nothing is claimed while the cancelled job still holds the Vertex lock.
    assert [jobs.claim("main") for _ in range(3)] == [None, None, None]
    for job in targets:
        stored = jobs.get(job.id)
        assert (stored.status, stored.started_at, stored.attempts) == ("queued", None, 0)
    assert jobs.get(holder.id).status == "cancel_requested"

    # When it finishes, exactly one waiter starts; the rest keep no start time.
    jobs.release_resources(holder.id, "holder", lease_generation=running.lease_generation)
    started = jobs.claim("main")
    assert started is not None and started.id == targets[0].id
    assert started.started_at is not None
    for job in targets[1:]:
        stored = jobs.get(job.id)
        assert (stored.status, stored.started_at) == ("queued", None)


def test_a_deferred_claim_does_not_claim_to_have_started(queue):
    jobs, _database = queue
    job = jobs.enqueue("generation.run", resource_keys=["gpu:default"], max_attempts=2)
    claimed = jobs.claim("main")
    assert claimed is not None and claimed.started_at is not None
    assert jobs.defer_for_resources(job.id, "main", lease_generation=claimed.lease_generation)
    stored = jobs.get(job.id)
    assert (stored.status, stored.started_at) == ("queued", None)


def test_service_display_name_and_alias_share_one_resource_key(queue):
    jobs, database = queue
    from pandrator.web.resource_keys import canonical_resource_key

    assert (
        canonical_resource_key("service:tts:Google Vertex AI")
        == canonical_resource_key("service:tts:vertex_ai")
        == "service:tts:vertex_ai"
    )
    holder = jobs.enqueue(
        "generation.run", resource_keys=["service:tts:Google Vertex AI"], max_attempts=2
    )
    contender = jobs.enqueue(
        "generation.run", resource_keys=["service:tts:vertex_ai"], max_attempts=2
    )
    first = jobs.claim("first")
    second = jobs.claim("second")
    assert first is not None and second is not None
    assert jobs.acquire_resources(
        holder.id, "first", ["service:tts:Google Vertex AI"],
        lease_generation=first.lease_generation,
    )
    assert not jobs.acquire_resources(
        contender.id, "second", ["service:tts:vertex_ai"],
        lease_generation=second.lease_generation,
    )
    # A row written before the alias existed still blocks the same service.
    with database.immediate_session() as unit:
        claim = unit.get(ResourceClaim, "service:tts:vertex_ai")
        assert claim is not None
        claim.resource_key = "service:tts:google_vertex_ai"
    assert not jobs.acquire_resources(
        contender.id, "second", ["service:tts:vertex_ai"],
        lease_generation=second.lease_generation,
    )
