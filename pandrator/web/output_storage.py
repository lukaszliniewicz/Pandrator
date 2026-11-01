"""Storage estimates and job-scoped admission for materialized outputs."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol

from .database import Database
from .job_execution import current_job_lease
from .storage_admission import (
    StorageAdmission,
    StorageJobLease,
    StoragePressure,
    StorageRequirement,
)

_active_scope: ContextVar[bool] = ContextVar("output_storage_scope", default=False)
_reserved_lease: ContextVar[StorageJobLease | None] = ContextVar(
    "output_storage_reserved_lease", default=None
)
_HEADER_BYTES = 64 * 1024


class OutputStorageContext(Protocol):
    @property
    def database(self) -> Database: ...


def estimated_pcm_bytes(
    duration_ms: float,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
    sample_width: int = 3,
) -> int:
    """Decoded PCM estimate, including container/metadata allowance."""
    if not math.isfinite(duration_ms) or duration_ms < 0:
        raise ValueError("Invalid output duration.")
    if min(sample_rate, channels, sample_width) < 1:
        raise ValueError("Invalid PCM format.")
    return math.ceil(duration_ms * sample_rate / 1000) * channels * sample_width + _HEADER_BYTES


def estimated_video_bytes(
    source_bytes: int,
    duration_ms: float,
    *,
    transcode: bool = False,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
) -> int:
    """Estimate encoded bytes; a quality-driven encode has no fixed size cap.

    Remuxes retain input payload size. For possible reencoding, use the larger
    of twice the input or a 0.25-bit-per-pixel/frame planning estimate. Unknown
    dimensions use 1080p/25 fps. These are estimates, not codec output bounds.
    """
    if source_bytes < 0 or not math.isfinite(duration_ms) or duration_ms < 0:
        raise ValueError("Invalid video storage estimate.")
    if not transcode:
        return source_bytes + _HEADER_BYTES
    frame_rate = fps if fps is not None and math.isfinite(fps) and fps > 0 else 25.0
    pixels = max(1, width or 1920) * max(1, height or 1080)
    encoded = math.ceil(pixels * frame_rate * duration_ms / 1000 / 32)
    return max(2 * source_bytes, encoded) + _HEADER_BYTES


def estimated_assembly_bytes(
    duration_ms: float,
    sample_rate: int,
    channels: int,
    *,
    copies: int = 4,
) -> int:
    return copies * max(
        estimated_pcm_bytes(
            duration_ms, sample_rate=sample_rate, channels=channels, sample_width=2
        ),
        # Supported lossy encoding can exceed low-rate mono PCM.
        math.ceil(duration_ms * 64) + _HEADER_BYTES,
    )


def reserve_output_space(
    context: OutputStorageContext | Database,
    path: Path,
    estimated_bytes: int,
    *,
    label: str = "output",
    preserve_existing: bool = False,
) -> None:
    """Reserve additional working bytes on this destination's filesystem."""
    if not isinstance(preserve_existing, bool):
        raise TypeError("preserve_existing must be a boolean")
    if type(estimated_bytes) is int and estimated_bytes == 0:
        return
    running = current_job_lease.get() if _active_scope.get() else None
    lease = (
        StorageJobLease(running["job_id"], running["owner_id"], running["lease_generation"])
        if running is not None
        else None
    )
    database = context if isinstance(context, Database) else context.database
    facts = StorageAdmission(database).reserve(
        lease,
        [
            StorageRequirement(path, estimated_bytes, label),
            StorageRequirement(database.path, _HEADER_BYTES, "database metadata"),
        ],
        preserve_existing=preserve_existing,
    )
    if lease is not None and facts:
        _reserved_lease.set(lease)


@contextmanager
def storage_effect_scope(database: Database) -> Iterator[None]:
    """Retire only reservations actually acquired by this worker invocation."""
    active_token = _active_scope.set(True)
    lease_token = _reserved_lease.set(None)
    try:
        yield
    finally:
        lease = _reserved_lease.get()
        try:
            if lease is not None:
                try:
                    StorageAdmission(database).release(lease)
                except Exception:
                    # A terminal/expired job stops contributing to admission
                    # even if its metadata could not be retired immediately.
                    logging.getLogger(__name__).warning(
                        "Could not retire storage reservation for job %s",
                        lease.job_id,
                        exc_info=True,
                    )
        finally:
            _reserved_lease.reset(lease_token)
            _active_scope.reset(active_token)


def storage_wait_detail(error: StoragePressure) -> str:
    def size(value: int) -> str:
        return f"{value / (1024**2):.1f} MiB"

    return (
        f"Waiting for storage ({error.label}): estimated additional space {size(error.estimated_bytes)}, "
        f"free {size(error.free_bytes)}, reserved by other work {size(error.reserved_bytes)}, "
        f"headroom {size(error.headroom_bytes)}. Completed inputs and recordings are retained. "
        "Free space on the affected filesystem; the same job checks again in 60 seconds."
    )
