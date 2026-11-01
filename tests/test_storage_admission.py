"""Cross-engine capacity accounting follows current job leases atomically."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from pandrator.web import storage_admission as core
from pandrator.web.database import SCHEMA_HEAD, Database, sqlite_url, upgrade_database
from pandrator.web.models import Base, Job, StorageReservation, Workspace
from pandrator.web.storage_admission import (
    StorageAdmission,
    StorageJobLease,
    StorageLeaseLost,
    StoragePressure,
    StorageRequirement,
)

MIB = 1024 * 1024


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    value = Database(tmp_path / "storage.sqlite3")
    Base.metadata.create_all(value.engine)
    with value.session() as unit:
        unit.add(Workspace(id="workspace", kind="team", name="Synthetic workspace"))
    try:
        yield value
    finally:
        value.dispose()


def _job(database: Database, job_id: str, *, seconds: float = 60) -> StorageJobLease:
    lease = StorageJobLease(job_id, f"owner-{job_id}", 1)
    with database.session() as unit:
        unit.add(Job(
            id=job_id, kind="synthetic", workspace_id="workspace", status="running",
            lease_owner=lease.owner_id, lease_generation=lease.generation,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        ))
    return lease


def _fake_device(monkeypatch: pytest.MonkeyPatch, *, free: int = 100 * MIB) -> None:
    monkeypatch.setattr(core, "os", SimpleNamespace(stat=lambda _path: SimpleNamespace(st_dev=7)))
    monkeypatch.setattr(core, "shutil", SimpleNamespace(
        disk_usage=lambda _path: SimpleNamespace(free=free),
    ))


def _rows(database: Database) -> list[tuple[str, str, int, int]]:
    with database.snapshot_session() as unit:
        return [
            (row.job_id, row.lease_owner, row.lease_generation, row.reserved_bytes)
            for row in unit.scalars(select(StorageReservation)).all()
        ]


def test_independent_engines_compete_for_one_device(database, monkeypatch, tmp_path):
    _fake_device(monkeypatch)
    first, second = _job(database, "first"), _job(database, "second")
    other = Database(database.path)
    barrier = threading.Barrier(2)

    def acquire(db, lease):
        barrier.wait(timeout=5)
        try:
            return StorageAdmission(db).reserve(lease, [StorageRequirement(tmp_path, 60 * MIB)])
        except StoragePressure as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            tasks = [workers.submit(acquire, database, first), workers.submit(acquire, other, second)]
            results = [task.result(timeout=10) for task in tasks]
        assert sum(isinstance(result, StoragePressure) for result in results) == 1
        assert sum(isinstance(result, list) for result in results) == 1
        assert len(_rows(database)) == 1
        pressure = next(result for result in results if isinstance(result, StoragePressure))
        assert pressure.reserved_bytes == 60 * MIB
        assert str(pressure) == pressure.code == "storage_pressure"
    finally:
        other.dispose()


def test_same_job_adjustment_replaces_estimate_and_excludes_its_old_reservation(
    database, monkeypatch, tmp_path,
):
    _fake_device(monkeypatch)
    lease = _job(database, "job")
    admission = StorageAdmission(database)
    admission.reserve(lease, [StorageRequirement(tmp_path, 60 * MIB)])
    facts = admission.reserve(lease, [StorageRequirement(tmp_path, 75 * MIB)])
    assert facts[0]["reserved_bytes"] == 0
    assert _rows(database) == [(lease.job_id, lease.owner_id, 1, 75 * MIB)]
    admission.reserve(lease, [StorageRequirement(tmp_path, 30 * MIB)])
    assert _rows(database) == [(lease.job_id, lease.owner_id, 1, 30 * MIB)]
    admission.release(lease)
    assert _rows(database) == []


@pytest.mark.parametrize("requested", [30, 60, 75])
def test_preserve_existing_retains_same_lease_grouped_floor(
    database, monkeypatch, tmp_path, requested,
):
    _fake_device(monkeypatch)
    lease = _job(database, "job")
    admission = StorageAdmission(database)
    admission.reserve(lease, [StorageRequirement(tmp_path, 60 * MIB)])
    # Only the newly requested bytes must fit: parent work may already exist.
    _fake_device(monkeypatch, free=(requested + 16) * MIB)
    facts = admission.reserve(lease, [
        StorageRequirement(tmp_path, (requested - 10) * MIB),
        StorageRequirement(tmp_path / "nested", 10 * MIB),
    ], preserve_existing=True)
    assert facts[0]["estimated_bytes"] == requested * MIB
    assert facts[0]["reserved_bytes"] == 0
    assert _rows(database) == [(lease.job_id, lease.owner_id, 1, max(60, requested) * MIB)]


@pytest.mark.parametrize("changed", ["owner", "generation", "both"])
def test_preserve_existing_does_not_inherit_obsolete_lease_floor(
    database, monkeypatch, tmp_path, changed,
):
    _fake_device(monkeypatch)
    old = _job(database, "job")
    admission = StorageAdmission(database)
    admission.reserve(old, [StorageRequirement(tmp_path, 60 * MIB)])
    successor = StorageJobLease(
        old.job_id, "successor" if changed != "generation" else old.owner_id,
        2 if changed != "owner" else old.generation,
    )
    with database.session() as unit:
        job = unit.get(Job, old.job_id)
        assert job is not None
        job.lease_owner, job.lease_generation = successor.owner_id, successor.generation
    admission.reserve(successor, [StorageRequirement(tmp_path, 20 * MIB)], preserve_existing=True)
    assert _rows(database) == [(
        old.job_id, successor.owner_id, successor.generation, 20 * MIB,
    )]


def test_preserve_existing_failed_growth_keeps_old_reservation(database, monkeypatch, tmp_path):
    _fake_device(monkeypatch)
    lease = _job(database, "job")
    admission = StorageAdmission(database)
    admission.reserve(lease, [StorageRequirement(tmp_path, 60 * MIB)])
    with pytest.raises(StoragePressure):
        admission.reserve(lease, [StorageRequirement(tmp_path, 85 * MIB)], preserve_existing=True)
    assert _rows(database) == [(lease.job_id, lease.owner_id, 1, 60 * MIB)]


@pytest.mark.parametrize("revocation", ["expired", "owner", "generation", "cancel_requested"])
def test_preserve_existing_cannot_bypass_lease_fences(
    database, monkeypatch, tmp_path, revocation,
):
    _fake_device(monkeypatch)
    lease = _job(database, "job")
    admission = StorageAdmission(database)
    admission.reserve(lease, [StorageRequirement(tmp_path, 60 * MIB)])
    with database.session() as unit:
        job = unit.get(Job, lease.job_id)
        assert job is not None
        if revocation == "expired":
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif revocation == "owner":
            job.lease_owner = "successor"
        elif revocation == "generation":
            job.lease_generation += 1
        else:
            job.status = revocation
    with pytest.raises(StorageLeaseLost):
        admission.reserve(lease, [StorageRequirement(tmp_path, 20 * MIB)], preserve_existing=True)
    assert _rows(database) == [(lease.job_id, lease.owner_id, 1, 60 * MIB)]


@pytest.mark.parametrize("invalid", [None, 0, 1, "true"])
def test_invalid_preserve_existing_rejected_before_io(database, monkeypatch, invalid):
    monkeypatch.setattr(core, "os", SimpleNamespace(stat=lambda _path: pytest.fail("stat")))
    monkeypatch.setattr(database, "run_immediate", lambda _callback: pytest.fail("writer"))
    with pytest.raises(TypeError, match="preserve_existing must be a boolean"):
        StorageAdmission(database).reserve(None, [], preserve_existing=invalid)


@pytest.mark.parametrize("stale_reason", ["expired", "generation", "owner", "finished"])
def test_stale_reservations_do_not_compete(database, monkeypatch, tmp_path, stale_reason):
    _fake_device(monkeypatch)
    stale, current = _job(database, "stale"), _job(database, "current")
    admission = StorageAdmission(database)
    admission.reserve(stale, [StorageRequirement(tmp_path, 80 * MIB)])
    with database.session() as unit:
        job = unit.get(Job, stale.job_id)
        assert job is not None
        if stale_reason == "expired":
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif stale_reason == "generation":
            job.lease_generation += 1
        elif stale_reason == "owner":
            job.lease_owner = "successor"
        else:
            job.status = "completed"
    facts = admission.reserve(current, [StorageRequirement(tmp_path, 80 * MIB)])
    assert facts[0]["reserved_bytes"] == 0
    assert len(_rows(database)) == 2


def test_stale_release_cannot_remove_successors_reservation(database, monkeypatch, tmp_path):
    _fake_device(monkeypatch)
    old = _job(database, "job")
    admission = StorageAdmission(database)
    admission.reserve(old, [StorageRequirement(tmp_path, 20 * MIB)])
    successor = StorageJobLease(old.job_id, "new-owner", 2)
    with database.session() as unit:
        job = unit.get(Job, old.job_id)
        assert job is not None
        job.lease_owner, job.lease_generation = successor.owner_id, successor.generation
    admission.reserve(successor, [StorageRequirement(tmp_path, 30 * MIB)])
    admission.release(old)
    assert _rows(database) == [(old.job_id, successor.owner_id, 2, 30 * MIB)]
    with pytest.raises(StorageLeaseLost):
        admission.reserve(old, [StorageRequirement(tmp_path, 10 * MIB)])


def test_stopping_job_keeps_reserved_space_until_its_effect_cleanup(database, monkeypatch, tmp_path):
    _fake_device(monkeypatch)
    stopping, next_job = _job(database, "stopping"), _job(database, "next")
    admission = StorageAdmission(database)
    admission.reserve(stopping, [StorageRequirement(tmp_path, 80 * MIB)])
    with database.session() as unit:
        job = unit.get(Job, stopping.job_id)
        assert job is not None
        job.status = "cancel_requested"
    with pytest.raises(StoragePressure):
        admission.reserve(next_job, [StorageRequirement(tmp_path, 20 * MIB)])
    with pytest.raises(StorageLeaseLost):
        admission.reserve(stopping, [StorageRequirement(tmp_path, MIB)])
    admission.release(stopping)
    assert admission.reserve(next_job, [StorageRequirement(tmp_path, 80 * MIB)])


def test_lease_expiring_while_waiting_for_writer_is_rejected(database, monkeypatch, tmp_path):
    _fake_device(monkeypatch)
    lease = _job(database, "expiring", seconds=0.15)
    attempted = threading.Event()

    def before_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement == "BEGIN IMMEDIATE":
            attempted.set()

    event.listen(database.engine, "before_cursor_execute", before_sql)
    blocker = sqlite3.connect(database.path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            task = workers.submit(
                StorageAdmission(database).reserve, lease, [StorageRequirement(tmp_path, MIB)],
            )
            assert attempted.wait(5)
            # Holding the independent writer past expiry forces the post-BEGIN proof.
            assert not threading.Event().wait(0.25)
            blocker.rollback()
            with pytest.raises(StorageLeaseLost):
                task.result(timeout=5)
        assert _rows(database) == []
    finally:
        blocker.close()
        event.remove(database.engine, "before_cursor_execute", before_sql)


def test_same_device_estimates_sum_and_use_first_positive_label(database, monkeypatch, tmp_path):
    _fake_device(monkeypatch)
    lease = _job(database, "job")
    facts = StorageAdmission(database).reserve(lease, [
        StorageRequirement(tmp_path, 0, "zero"),
        StorageRequirement(tmp_path / "one", 30 * MIB, "first"),
        StorageRequirement(tmp_path / "two", 40 * MIB, "second"),
    ])
    assert facts == [{
        "filesystem_key": "dev:7", "label": "first", "estimated_bytes": 70 * MIB,
        "free_bytes": 100 * MIB, "reserved_bytes": 0, "headroom_bytes": 16 * MIB,
    }]
    with pytest.raises(StoragePressure) as failure:
        StorageAdmission(database).reserve(lease, [
            StorageRequirement(tmp_path, 45 * MIB), StorageRequirement(tmp_path, 45 * MIB),
        ])
    assert failure.value.estimated_bytes == 90 * MIB
    assert _rows(database)[0][3] == 70 * MIB


def test_distinct_device_failure_rolls_back_every_update(database, monkeypatch, tmp_path):
    first_path, second_path = tmp_path / "first", tmp_path / "second"
    monkeypatch.setattr(core, "os", SimpleNamespace(stat=lambda path: SimpleNamespace(
        st_dev=1 if path == first_path else 2,
    )))
    monkeypatch.setattr(core, "shutil", SimpleNamespace(disk_usage=lambda path: SimpleNamespace(
        free=100 * MIB if path == first_path else 10 * MIB,
    )))
    lease = _job(database, "job")
    admission = StorageAdmission(database)
    admission.reserve(lease, [StorageRequirement(first_path, 20 * MIB)])
    with pytest.raises(StoragePressure):
        admission.reserve(lease, [
            StorageRequirement(first_path, 30 * MIB), StorageRequirement(second_path, MIB),
        ])
    assert _rows(database) == [(lease.job_id, lease.owner_id, 1, 20 * MIB)]


def test_filesystem_probes_resolve_ancestor_and_precede_writer(database, monkeypatch, tmp_path):
    lease = _job(database, "job")
    probes = []
    writer_active = False
    real_stat = core.os.stat

    def stat(path):
        assert not writer_active
        probes.append(("stat", path))
        return real_stat(path)

    def disk_usage(path):
        assert not writer_active
        probes.append(("free", path))
        return SimpleNamespace(free=100 * MIB)

    def started(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal writer_active
        if statement == "BEGIN IMMEDIATE":
            writer_active = True

    def finished(_connection):
        nonlocal writer_active
        writer_active = False

    monkeypatch.setattr(core, "os", SimpleNamespace(stat=stat))
    monkeypatch.setattr(core, "shutil", SimpleNamespace(disk_usage=disk_usage))
    event.listen(database.engine, "before_cursor_execute", started)
    event.listen(database.engine, "commit", finished)
    event.listen(database.engine, "rollback", finished)
    try:
        StorageAdmission(database).reserve(lease, [StorageRequirement(tmp_path / "missing" / "out", MIB)])
    finally:
        event.remove(database.engine, "before_cursor_execute", started)
        event.remove(database.engine, "commit", finished)
        event.remove(database.engine, "rollback", finished)
    assert probes[-1] == ("free", tmp_path)
    assert ("stat", tmp_path) in probes


@pytest.mark.parametrize("requirements", [[], [StorageRequirement(Path("unused"), 0)]])
def test_zero_budget_has_no_filesystem_or_sql_operations(database, monkeypatch, requirements):
    monkeypatch.setattr(core, "os", SimpleNamespace(stat=lambda _path: pytest.fail("stat")))
    monkeypatch.setattr(core, "shutil", SimpleNamespace(disk_usage=lambda _path: pytest.fail("free")))
    monkeypatch.setattr(database, "run_immediate", lambda _callback: pytest.fail("writer"))
    assert StorageAdmission(database).reserve(StorageJobLease("unused", "unused", 0), requirements) == []
    assert StorageAdmission(database).reserve(None, requirements) == []


@pytest.mark.parametrize("invalid,expected", [
    (-1, ValueError), (True, TypeError), (False, TypeError), (1.5, TypeError), ("1", TypeError),
])
def test_invalid_bytes_rejected_before_any_io(database, monkeypatch, invalid, expected):
    monkeypatch.setattr(core, "os", SimpleNamespace(stat=lambda _path: pytest.fail("stat")))
    monkeypatch.setattr(database, "run_immediate", lambda _callback: pytest.fail("writer"))
    with pytest.raises(expected):
        StorageAdmission(database).reserve(None, [
            StorageRequirement(Path("first"), MIB), StorageRequirement(Path("invalid"), invalid),
        ])


def test_legacy_check_uses_integer_margin_without_sql(database, monkeypatch, tmp_path):
    estimate = 400 * MIB + 1
    margin = (estimate + 19) // 20
    _fake_device(monkeypatch, free=estimate + margin)
    monkeypatch.setattr(database, "run_immediate", lambda _callback: pytest.fail("writer"))
    facts = StorageAdmission(database).reserve(None, [StorageRequirement(tmp_path, estimate)])
    assert facts[0]["headroom_bytes"] == margin
    assert facts[0]["reserved_bytes"] == 0
    _fake_device(monkeypatch, free=estimate + margin - 1)
    with pytest.raises(StoragePressure):
        StorageAdmission(database).reserve(None, [StorageRequirement(tmp_path, estimate)])


def test_table_enforces_nonnegative_bytes_and_job_delete_cascades(database, monkeypatch, tmp_path):
    _fake_device(monkeypatch)
    lease = _job(database, "job")
    StorageAdmission(database).reserve(lease, [StorageRequirement(tmp_path, MIB)])
    with pytest.raises(IntegrityError):
        with database.session() as unit:
            unit.execute(text("UPDATE storage_reservations SET reserved_bytes = -1"))
    with database.session() as unit:
        job = unit.get(Job, lease.job_id)
        assert job is not None
        unit.delete(job)
    assert _rows(database) == []


def _migration_config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(core.__file__).with_name("migrations")))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def test_fresh_upgrade_creates_reservations_at_new_head(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    upgrade_database(path)
    database = Database(path)
    try:
        with database.snapshot_session() as unit:
            assert unit.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_HEAD
            assert unit.scalar(text("SELECT count(*) FROM storage_reservations")) == 0
        inspector = inspect(database.engine)
        assert inspector.get_pk_constraint("storage_reservations")["constrained_columns"] == [
            "job_id", "filesystem_key",
        ]
        foreign_key = inspector.get_foreign_keys("storage_reservations")[0]
        assert foreign_key["referred_table"] == "jobs"
        assert foreign_key.get("options", {}).get("ondelete") == "CASCADE"
    finally:
        database.dispose()


@pytest.mark.parametrize("live_status", ["running", "cancel_requested"])
def test_upgrade_from_0085_preserves_jobs_and_downgrade_protects_live_rows(
    tmp_path, monkeypatch, live_status,
):
    path = tmp_path / "predecessor.sqlite3"
    config = _migration_config(path)
    command.upgrade(config, "0085_project_speech_references")
    database = Database(path)
    try:
        assert "storage_reservations" not in inspect(database.engine).get_table_names()
        with database.session() as unit:
            unit.add(Workspace(id="workspace", kind="team", name="Synthetic workspace"))
        lease = _job(database, "preserved")
        upgrade_database(path)
        with database.snapshot_session() as unit:
            job = unit.get(Job, lease.job_id)
            assert job is not None
            assert job.status == "running" and job.lease_owner == lease.owner_id
        _fake_device(monkeypatch)
        StorageAdmission(database).reserve(lease, [StorageRequirement(tmp_path, MIB)])
        with database.session() as unit:
            job = unit.get(Job, lease.job_id)
            assert job is not None
            job.status = live_status
        with pytest.raises(RuntimeError, match="live storage reservations"):
            command.downgrade(config, "0085_project_speech_references")
        assert len(_rows(database)) == 1
        with database.session() as unit:
            job = unit.get(Job, lease.job_id)
            assert job is not None
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        command.downgrade(config, "0085_project_speech_references")
        assert "storage_reservations" not in inspect(database.engine).get_table_names()
        with database.snapshot_session() as unit:
            assert unit.get(Job, lease.job_id) is not None
    finally:
        database.dispose()
