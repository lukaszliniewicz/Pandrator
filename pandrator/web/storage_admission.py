"""Durable estimated-byte admission; this does not preallocate OS storage."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .database import Database
from .models import Job, StorageReservation

_MIN_HEADROOM_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class StorageJobLease:
    job_id: str
    owner_id: str
    generation: int


@dataclass(frozen=True)
class StorageRequirement:
    path: Path
    estimated_bytes: int
    label: str = "output"


class StoragePressure(Exception):
    code = "storage_pressure"

    def __init__(
        self, *, label: str, estimated_bytes: int, free_bytes: int,
        reserved_bytes: int, headroom_bytes: int,
    ) -> None:
        self.label = label
        self.estimated_bytes = estimated_bytes
        self.free_bytes = free_bytes
        self.reserved_bytes = reserved_bytes
        self.headroom_bytes = headroom_bytes
        super().__init__(self.code)


class StorageLeaseLost(Exception):
    """The supplied job lease no longer authorizes a reservation."""


@dataclass(frozen=True)
class _DeviceRequirement:
    ancestor: Path
    filesystem_key: str
    estimated_bytes: int
    label: str


def _group_requirements(requirements: Sequence[StorageRequirement]) -> list[_DeviceRequirement]:
    # Validate the complete input before touching the filesystem or database.
    for requirement in requirements:
        if isinstance(requirement.estimated_bytes, bool) or not isinstance(
            requirement.estimated_bytes, int
        ):
            raise TypeError("estimated_bytes must be an integer")
        if requirement.estimated_bytes < 0:
            raise ValueError("estimated_bytes must be nonnegative")
    groups: dict[str, _DeviceRequirement] = {}
    for requirement in requirements:
        if requirement.estimated_bytes == 0:
            continue
        ancestor = requirement.path
        while True:
            try:
                device = os.stat(ancestor).st_dev
                break
            except (FileNotFoundError, NotADirectoryError):
                parent = ancestor.parent
                if parent == ancestor:
                    raise
                ancestor = parent
        key = f"dev:{device:x}"
        previous = groups.get(key)
        groups[key] = _DeviceRequirement(
            ancestor=previous.ancestor if previous else ancestor,
            filesystem_key=key,
            estimated_bytes=requirement.estimated_bytes + (
                previous.estimated_bytes if previous else 0
            ),
            label=previous.label if previous else requirement.label,
        )
    return list(groups.values())


def _fact(group: _DeviceRequirement, free: int, reserved: int) -> dict[str, int | str]:
    headroom = max(_MIN_HEADROOM_BYTES, (group.estimated_bytes + 19) // 20)
    if free < group.estimated_bytes + reserved + headroom:
        raise StoragePressure(
            label=group.label, estimated_bytes=group.estimated_bytes, free_bytes=free,
            reserved_bytes=reserved, headroom_bytes=headroom,
        )
    return {
        "filesystem_key": group.filesystem_key,
        "label": group.label,
        "estimated_bytes": group.estimated_bytes,
        "free_bytes": free,
        "reserved_bytes": reserved,
        "headroom_bytes": headroom,
    }


class StorageAdmission:
    def __init__(self, database: Database) -> None:
        self._database = database

    def reserve(
        self, lease: StorageJobLease | None, requirements: Sequence[StorageRequirement],
        *, preserve_existing: bool = False,
    ) -> list[dict[str, int | str]]:
        if not isinstance(preserve_existing, bool):
            raise TypeError("preserve_existing must be a boolean")
        groups = _group_requirements(requirements)
        if not groups:
            return []
        # All stat/disk probes precede the writer, including retries of its SQL unit.
        measured = [(group, shutil.disk_usage(group.ancestor).free) for group in groups]
        if lease is None:
            return [_fact(group, free, 0) for group, free in measured]

        def acquire(unit: Session) -> list[dict[str, int | str]]:
            now = datetime.now(UTC)  # BEGIN IMMEDIATE has already succeeded.
            if unit.scalar(select(Job.id).where(
                Job.id == lease.job_id,
                Job.status == "running",
                Job.lease_owner == lease.owner_id,
                Job.lease_generation == lease.generation,
                Job.lease_expires_at > now,
            )) is None:
                raise StorageLeaseLost()
            facts = []
            for group, free in measured:
                reserved = int(unit.scalar(
                    select(func.coalesce(func.sum(StorageReservation.reserved_bytes), 0))
                    .join(Job, Job.id == StorageReservation.job_id)
                    .where(
                        StorageReservation.filesystem_key == group.filesystem_key,
                        StorageReservation.job_id != lease.job_id,
                        Job.status.in_(("running", "cancel_requested")),
                        Job.lease_owner == StorageReservation.lease_owner,
                        Job.lease_generation == StorageReservation.lease_generation,
                        Job.lease_expires_at > now,
                    )
                ) or 0)
                facts.append(_fact(group, free, reserved))
                statement = insert(StorageReservation).values(
                    job_id=lease.job_id, filesystem_key=group.filesystem_key,
                    lease_owner=lease.owner_id, lease_generation=lease.generation,
                    reserved_bytes=group.estimated_bytes, created_at=now, updated_at=now,
                )
                unit.execute(statement.on_conflict_do_update(
                    index_elements=["job_id", "filesystem_key"],
                    set_={
                        "lease_owner": lease.owner_id,
                        "lease_generation": lease.generation,
                        # Nested output work must not release its parent's envelope.
                        # Admission still checks only the newly requested work above:
                        # some of the parent estimate may already be materialized.
                        "reserved_bytes": case(
                            (and_(
                                StorageReservation.lease_owner == lease.owner_id,
                                StorageReservation.lease_generation == lease.generation,
                            ), func.max(
                                StorageReservation.reserved_bytes, group.estimated_bytes,
                            )),
                            else_=group.estimated_bytes,
                        ) if preserve_existing else group.estimated_bytes,
                        "updated_at": now,
                    },
                ))
            return facts

        return self._database.run_immediate(acquire)

    def release(self, lease: StorageJobLease) -> None:
        def remove(unit: Session) -> None:
            unit.execute(delete(StorageReservation).where(
                StorageReservation.job_id == lease.job_id,
                StorageReservation.lease_owner == lease.owner_id,
                StorageReservation.lease_generation == lease.generation,
            ))

        self._database.run_immediate(remove)
