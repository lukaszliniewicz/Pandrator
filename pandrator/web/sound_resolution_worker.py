"""Fenced execution of exact spoken-word sound placement."""

from __future__ import annotations

import tempfile
import threading
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from pandrator.logic.book_timing import AlignedBookText
from pandrator.logic.sound_boundary_refinement import BOUNDARY_REFINEMENT_VERSION

from .artifacts import sha256_file
from .job_execution import current_job_lease
from .models import Job, utcnow
from .settings_policy import RevisionConflict
from .sound_project_commands import latest_cues, write_version
from .sound_project_queries import current_header
from .sound_resolution_audio import align_take, normalize_take, resolve_boundary
from .sound_resolution_cache import find_cache, publish_cache, stage_cache
from .sound_resolution_snapshot import (
    REFINEMENT_SETTINGS,
    SoundResolutionSnapshot,
    WorkItem,
    capture,
    digest,
)


def _lease(session: Session, payload: dict[str, Any], cancel_event: threading.Event) -> Job:
    identity = current_job_lease.get()
    job_id = payload.get("_job_id")
    job = session.get(Job, job_id, populate_existing=True) if isinstance(job_id, str) else None
    if (
        identity is None
        or identity.get("job_id") != job_id
        or identity.get("lease_generation") != payload.get("_lease_generation")
        or job is None
        or job.kind != "sound.resolve"
        or job.session_id != payload.get("session_id")
        or job.payload_json
        != {key: value for key, value in payload.items() if not key.startswith("_")}
        or job.lease_owner != identity.get("owner_id")
        or job.lease_generation != identity.get("lease_generation")
        or job.status != "running"
        or job.lease_expires_at is None
        or (
            job.lease_expires_at.replace(tzinfo=UTC)
            if job.lease_expires_at.tzinfo is None
            else job.lease_expires_at.astimezone(UTC)
        )
        <= utcnow()
        or cancel_event.is_set()
    ):
        raise RuntimeError("Sound resolution lease or cancellation changed.")
    return job


def _pins(service, session: Session, payload: dict[str, Any]) -> SoundResolutionSnapshot:
    pinned = payload.get("pins")
    if not isinstance(pinned, dict) or digest(pinned) != payload.get("pin_hash"):
        raise RuntimeError("Sound resolution payload is invalid.")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or session_id != pinned.get("session_id"):
        raise RuntimeError("Sound resolution session pin is invalid.")
    snapshot = capture(
        session,
        session_id,
        pinned["sound_plan_revision"],
        pinned["cue_ids"],
        service.settings,
        service.paths,
    )
    current = {**snapshot.pins, "remaining_count": pinned["remaining_count"]}
    if digest(current) != payload["pin_hash"]:
        raise RevisionConflict("Sound resolution inputs changed; submit a new request.")
    return snapshot


def _verify_take(item: WorkItem) -> None:
    if item.reason is not None:
        return
    try:
        valid = (
            item.path is not None
            and item.take_hash is not None
            and item.path.is_file()
            and sha256_file(item.path) == item.take_hash
        )
    except OSError:
        valid = False
    if not valid:
        raise RuntimeError("Selected audio take content changed; submit sound resolution again.")


def _pending(item: WorkItem, reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": "needs_placement", "reason": reason, "evidence": evidence or {}}


def _resolution(item: WorkItem, measured: dict[str, Any]) -> dict[str, Any]:
    identity = item.identity or {}
    safe_identity = {
        "hash": digest(identity),
        "engine": identity.get("engine"),
        "model": identity.get("model"),
    }
    result = {
        "status": measured["status"],
        "reason": measured.get("reason"),
        "evidence": measured.get("evidence", {}),
        "take_id": item.take_id,
        "artifact_id": item.artifact_id,
        "take_content_hash": item.take_hash,
        "transcript_hash": item.transcript_hash,
        "alignment_identity": safe_identity,
        "refinement_version": BOUNDARY_REFINEMENT_VERSION,
        "refinement_settings": REFINEMENT_SETTINGS,
    }
    for key in (
        "take_frame",
        "take_sample_rate",
        "gap_start_frame",
        "gap_end_frame",
        "shift_frames",
    ):
        if key in measured:
            result[key] = measured[key]
    return result


def run_resolution(
    service, payload: dict[str, Any], progress, cancel_event: threading.Event
) -> dict[str, Any]:
    with service.database.snapshot_session() as session:
        snapshot = _pins(service, session, payload)
        _lease(session, payload, cancel_event)
        cached: dict[str, AlignedBookText] = {}
        for item in snapshot.items:
            if item.reason is None and item.take_id is not None and item.take_id not in cached:
                found = find_cache(session, service.paths, snapshot.session_id, item)
                if found is not None:
                    cached[item.take_id] = found
    for item in snapshot.items:
        _verify_take(item)
    results: dict[str, dict[str, Any]] = {}
    staged: dict[str, tuple[WorkItem, Path]] = {}
    alignments = dict(cached)
    service.paths.temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="sound-resolution-", dir=service.paths.temporary
    ) as temporary:
        workroot = Path(temporary)
        for index, item in enumerate(snapshot.items):
            if cancel_event.is_set():
                raise RuntimeError("Sound resolution was canceled.")
            progress(0.05 + 0.8 * index / len(snapshot.items), "Resolving word cues")
            if item.reason is not None:
                results[item.cue_id] = _pending(item, item.reason)
                continue
            assert item.take_id is not None
            take_dir = workroot / item.take_id
            take_dir.mkdir(exist_ok=True)
            aligned = alignments.get(item.take_id)
            if aligned is None:
                try:
                    aligned = align_take(item, snapshot.settings, take_dir, cancel_event)
                except Exception:
                    if cancel_event.is_set():
                        raise RuntimeError("Sound resolution was canceled.") from None
                    raise RuntimeError(
                        "Forced alignment failed; check the configured STT aligner and model, then retry."
                    ) from None
                if (
                    aligned.text != item.transcript
                    or aligned.diagnostics.get("identity") != item.identity
                ):
                    raise RuntimeError("Alignment identity changed; submit sound resolution again.")
                alignments[item.take_id] = aligned
                staged[item.take_id] = (item, stage_cache(service.paths, item, aligned, take_dir))
            normalized = take_dir / "book-alignment.wav"
            direct_overlay = (
                item.anchor.get("kind") == "after_word" and item.playback.get("mode") == "overlay"
            )
            if not direct_overlay and not normalized.is_file():
                try:
                    normalize_take(item, normalized, snapshot.settings, cancel_event)
                except Exception:
                    if cancel_event.is_set():
                        raise RuntimeError("Sound resolution was canceled.") from None
                    raise RuntimeError(
                        "Audio normalization failed; check the selected take and FFmpeg runtime."
                    ) from None
            try:
                results[item.cue_id] = resolve_boundary(
                    item, aligned, normalized, take_dir, snapshot.settings, cancel_event
                )
            except Exception:
                if cancel_event.is_set():
                    raise RuntimeError("Sound resolution was canceled.") from None
                raise RuntimeError(
                    "Boundary analysis failed; check the CrispASR VAD runtime and selected audio take."
                ) from None
        # Check files before opening SQLite's writer. The final DB pin/lease
        # fence below prevents any stale plan or selection from being published.
        for item in snapshot.items:
            _verify_take(item)
        registrations = {
            take_id: service.artifacts.prepare_registration(path)
            for take_id, (_item, path) in staged.items()
        }
        with service.database.immediate_session() as session:
            job = _lease(session, payload, cancel_event)
            _pins(service, session, payload)
            for take_id, (item, path) in staged.items():
                publish_cache(
                    session,
                    service.paths,
                    service.artifacts,
                    snapshot.session_id,
                    item,
                    path,
                    registrations[take_id],
                )
            _lease(session, payload, cancel_event)
            _pins(service, session, payload)
            _record, old, active = current_header(session, snapshot.session_id)
            if old is None or old.id != snapshot.plan_id:
                raise RevisionConflict("Sound plan changed before publication.")
            cues = latest_cues(session, old)
            for cue in cues:
                measured = results.get(cue["logical_id"])
                if measured is None:
                    continue
                item = next(item for item in snapshot.items if item.cue_id == cue["logical_id"])
                cue["anchor_json"] = {
                    **cue["anchor_json"],
                    "resolution": _resolution(item, measured),
                }
                cue["placement_status"] = measured["status"]
            plan = write_version(session, snapshot.session_id, old, active, cues)
            receipt = {
                "job_id": job.id,
                "status": "completed",
                "revision": plan.revision,
                "resolved": sum(result["status"] == "ready" for result in results.values()),
                "pending": sum(result["status"] != "ready" for result in results.values()),
                "remaining_count": snapshot.remaining_count,
                "cues": [
                    {
                        "logical_id": cue_id,
                        "status": results[cue_id]["status"],
                        "reason": results[cue_id].get("reason"),
                    }
                    for cue_id in snapshot.cue_ids
                ],
            }
            _lease(session, payload, cancel_event)
            return receipt
