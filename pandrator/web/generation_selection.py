"""Shared automatic generation selection for preview and start.

Preview and ``prepare_start`` must use identical predicates and bulk access so
the optimism hash binds the same plan, row/take state, and resolved settings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select

REASON_KEYS = (
    "missing_audio",
    "edited_or_failed",
    "settings_changed",
    "voice_changed",
    "performance_changed",
    "identity_unknown",
    "requested",
)

_REUSE_TO_REASON = {
    "generation_settings_changed": "settings_changed",
    "voice_reference_changed": "voice_changed",
    "performance_changed": "performance_changed",
    "audio_identity_unknown": "identity_unknown",
}

_NONTERMINAL_RUN_STATUSES = ("queued", "running", "pausing", "paused")


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def resolve_mode(*, stale_only: bool = False, missing_only: bool = False) -> str:
    if stale_only and missing_only:
        raise ValueError(
            "Stale-only and missing-only generation are mutually exclusive."
        )
    if missing_only:
        return "missing"
    if stale_only:
        return "stale"
    return "all"


def _settings_summary(resolved_snapshot: dict[str, Any]) -> dict[str, str]:
    tts = dict(resolved_snapshot.get("tts") or {})
    return {
        "service": str(tts.get("service") or tts.get("tts_service") or ""),
        "model": str(tts.get("model") or tts.get("xtts_model") or ""),
        "voice": str(tts.get("voice") or tts.get("speaker") or ""),
    }


def _actual_identity_fingerprint(artifact_metadata: Any) -> str:
    from .generation_audio_identity import IDENTITY_KEY

    actual = (
        artifact_metadata.get(IDENTITY_KEY)
        if isinstance(artifact_metadata, dict)
        else None
    )
    if not isinstance(actual, dict):
        return "none"
    return _stable_hash(actual)


def collect_row_state(session: Any, bound_revision_id: str) -> dict[str, Any]:
    """Capture lightweight plain-data row/take/artifact/run state.

    No audio-identity work happens here, so the write transaction can recheck
    this cheaply without recompiling per-row identities under the lock.
    Voice/cast drift is covered there by the existing snapshot guard plus a
    fresh resolved-settings comparison.
    """
    from .models import Artifact, AudioTake, GenerationRun, GenerationSegment

    segment_rows = list(
        session.execute(
            select(
                GenerationSegment.id,
                GenerationSegment.ordinal,
                GenerationSegment.status,
                GenerationSegment.revision,
            )
            .where(
                GenerationSegment.plan_revision_id == bound_revision_id,
                GenerationSegment.removed.is_(False),
            )
            .order_by(GenerationSegment.ordinal)
        )
    )
    segments: dict[str, dict[str, Any]] = {
        row[0]: {"ordinal": row[1], "status": row[2], "revision": row[3]}
        for row in segment_rows
    }
    actives: dict[str, list[dict[str, Any]]] = {}
    segment_ids = list(segments)
    for offset in range(0, len(segment_ids), 500):
        chunk = segment_ids[offset : offset + 500]
        if not chunk:
            continue
        for row in session.execute(
            select(
                AudioTake.id,
                AudioTake.generation_segment_id,
                AudioTake.status,
                AudioTake.revision,
                AudioTake.artifact_id,
                AudioTake.created_at,
            ).where(
                AudioTake.generation_segment_id.in_(chunk),
                AudioTake.is_active.is_(True),
            )
        ):
            actives.setdefault(row[1], []).append(
                {
                    "id": row[0],
                    "status": row[2],
                    "revision": row[3],
                    "artifact_id": row[4],
                    "created_at": str(row[5]),
                }
            )
    for takes in actives.values():
        takes.sort(key=lambda item: item["id"])
    artifacts: dict[str, dict[str, Any]] = {}
    artifact_ids = list(
        {
            take["artifact_id"]
            for takes in actives.values()
            for take in takes
            if take["artifact_id"]
        }
    )
    for offset in range(0, len(artifact_ids), 500):
        chunk = artifact_ids[offset : offset + 500]
        if not chunk:
            continue
        for row in session.execute(
            select(Artifact.id, Artifact.state, Artifact.content_hash).where(
                Artifact.id.in_(chunk)
            )
        ):
            artifacts[row[0]] = {"state": row[1], "content_hash": row[2]}
    active_runs = [
        [run_id, status]
        for run_id, status in session.execute(
            select(GenerationRun.id, GenerationRun.status)
            .where(
                GenerationRun.plan_revision_id == bound_revision_id,
                GenerationRun.status.in_(_NONTERMINAL_RUN_STATUSES),
            )
            .order_by(GenerationRun.id)
        )
    ]
    return {
        "segments": segments,
        "actives": actives,
        "artifacts": artifacts,
        "active_runs": active_runs,
    }


def _live_artifact(
    take: dict[str, Any] | None, artifacts: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if take is None or not take["artifact_id"]:
        return None
    artifact = artifacts.get(take["artifact_id"])
    if artifact is None or artifact["state"] == "deleted":
        return None
    return artifact


def _resolve_takes(
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Resolve latest/candidate/recorded takes from captured plain state.

    The candidate mirrors the legacy stale-only per-segment query: the
    latest-created active ``completed`` take with a live artifact. A newer
    failed (or artifact-deleted) active take never hides an older completed
    one. The recorded take is the latest active take with a live artifact in
    ``completed`` or ``stale`` status: a text/voice edit marks takes stale
    without deleting the recording.
    """
    latest_by_id: dict[str, Any] = {}
    candidate_by_id: dict[str, Any] = {}
    recorded_by_id: dict[str, Any] = {}
    for segment_id, takes in state["actives"].items():
        if segment_id not in state["segments"] or not takes:
            continue
        latest_by_id[segment_id] = max(
            takes, key=lambda item: (item["created_at"], item["id"])
        )
        available = [
            item
            for item in takes
            if item["status"] == "completed"
            and _live_artifact(item, state["artifacts"]) is not None
        ]
        if available:
            candidate_by_id[segment_id] = max(
                available, key=lambda item: (item["created_at"], item["id"])
            )
        recorded = [
            item
            for item in takes
            if item["status"] in {"completed", "stale"}
            and _live_artifact(item, state["artifacts"]) is not None
        ]
        if recorded:
            recorded_by_id[segment_id] = max(
                recorded, key=lambda item: (item["created_at"], item["id"])
            )
    return latest_by_id, candidate_by_id, recorded_by_id


def compute_selection(
    session: Any,
    bound_revision_id: str,
    resolved_snapshot: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Compute one automatic selection with bulk queries only.

    Audio-identity inspection (``AudioIdentityContext``) runs only in stale
    mode, the single mode whose predicate depends on compatibility. Missing
    and full-regenerate modes never pay for per-row identity work. The
    returned ``row_state`` is the lightweight captured state the write
    transaction rechecks without recompiling identities.
    """
    if mode not in {"missing", "stale", "all"}:
        raise ValueError(f"Unknown generation selection mode: {mode}.")
    state = collect_row_state(session, bound_revision_id)
    latest_by_id, candidate_by_id, recorded_by_id = _resolve_takes(state)

    reuse_by_id: dict[str, str] = {}
    expected_fp_by_id: dict[str, str] = {}
    actual_fp_by_id: dict[str, str] = {}
    if mode == "stale":
        from .generation_audio_identity import (
            AudioIdentityContext,
            take_reuse_reason,
        )
        from .models import Artifact, AudioTake, GenerationSegment

        context = AudioIdentityContext(session, resolved_snapshot)
        segment_ids = list(state["segments"])
        orm_segments: dict[str, Any] = {}
        for offset in range(0, len(segment_ids), 500):
            chunk = segment_ids[offset : offset + 500]
            if not chunk:
                continue
            for segment in session.scalars(
                select(GenerationSegment).where(GenerationSegment.id.in_(chunk))
            ):
                orm_segments[segment.id] = segment
        take_ids = [take["id"] for takes in state["actives"].values() for take in takes]
        orm_takes: dict[str, Any] = {}
        for offset in range(0, len(take_ids), 500):
            chunk = take_ids[offset : offset + 500]
            if not chunk:
                continue
            for take in session.scalars(
                select(AudioTake).where(AudioTake.id.in_(chunk))
            ):
                orm_takes[take.id] = take
        artifact_ids = list(state["artifacts"])
        orm_artifacts: dict[str, Any] = {}
        for offset in range(0, len(artifact_ids), 500):
            chunk = artifact_ids[offset : offset + 500]
            if not chunk:
                continue
            for artifact in session.scalars(
                select(Artifact).where(Artifact.id.in_(chunk))
            ):
                orm_artifacts[artifact.id] = artifact
        for segment_id in state["segments"]:
            segment = orm_segments.get(segment_id)
            candidate = candidate_by_id.get(segment_id)
            if segment is None:
                reuse_by_id[segment_id] = "audio_unavailable"
                expected_fp_by_id[segment_id] = "none"
                continue
            expected = context.for_segment(segment)
            expected_fp_by_id[segment_id] = _stable_hash(expected)
            orm_take = orm_takes.get(candidate["id"]) if candidate else None
            orm_artifact = (
                orm_artifacts.get(candidate["artifact_id"])
                if candidate and candidate["artifact_id"]
                else None
            )
            reuse_by_id[segment_id] = take_reuse_reason(
                segment, orm_take, orm_artifact, expected
            )
            actual_fp_by_id[segment_id] = _actual_identity_fingerprint(
                orm_artifact.metadata_json if orm_artifact is not None else None
            )

    reasons: dict[str, int] = {key: 0 for key in REASON_KEYS}
    generate_ids: list[str] = []
    preserve_take_ids: dict[str, str] = {}
    replace_count = 0
    missing_count = 0
    first_generate_ordinal: int | None = None
    hash_rows: list[list[Any]] = []
    ordered = sorted(state["segments"].items(), key=lambda item: item[1]["ordinal"])
    for segment_id, meta in ordered:
        candidate = candidate_by_id.get(segment_id)
        latest = latest_by_id.get(segment_id)
        recorded = recorded_by_id.get(segment_id)
        candidate_artifact = (
            state["artifacts"].get(candidate["artifact_id"])
            if candidate and candidate["artifact_id"]
            else None
        )
        has_completed_audio = candidate is not None
        has_recording = recorded is not None
        if mode == "all":
            generate = True
        elif mode == "stale":
            generate = reuse_by_id[segment_id] != "reusable"
        else:
            generate = meta["status"] != "completed" or not has_completed_audio
        row: list[Any] = [
            segment_id,
            meta["ordinal"],
            meta["status"],
            meta["revision"],
            candidate["id"] if candidate is not None else None,
            candidate["revision"] if candidate is not None else None,
            candidate["status"] if candidate is not None else None,
            candidate["artifact_id"] if candidate is not None else None,
            candidate_artifact["state"] if candidate_artifact is not None else None,
            candidate_artifact["content_hash"]
            if candidate_artifact is not None
            else None,
            latest["id"] if latest is not None else None,
            latest["status"] if latest is not None else None,
            latest["artifact_id"] if latest is not None else None,
            recorded["id"] if recorded is not None else None,
            recorded["status"] if recorded is not None else None,
            len(state["actives"].get(segment_id, [])),
            generate,
        ]
        if mode == "stale":
            row.extend(
                [
                    reuse_by_id[segment_id],
                    expected_fp_by_id[segment_id],
                    actual_fp_by_id[segment_id],
                ]
            )
        hash_rows.append(row)
        if generate:
            generate_ids.append(segment_id)
            if first_generate_ordinal is None:
                first_generate_ordinal = meta["ordinal"]
            if has_recording:
                replace_count += 1
            else:
                missing_count += 1
            if mode == "all":
                # A full regenerate is an explicit request: never attribute a
                # reusable row to a staleness bucket, and never falsely claim
                # its identity is unknown.
                reasons["requested"] += 1
            elif not has_recording:
                reasons["missing_audio"] += 1
            elif meta["status"] != "completed" or not has_completed_audio:
                reasons["edited_or_failed"] += 1
            else:
                reasons[
                    _REUSE_TO_REASON.get(reuse_by_id[segment_id], "identity_unknown")
                ] += 1
        elif has_completed_audio and candidate is not None:
            preserve_take_ids[segment_id] = candidate["id"]

    total_count = len(ordered)
    generate_count = len(generate_ids)
    preserve_count = total_count - generate_count
    selection_hash = _stable_hash(
        {
            "plan_revision_id": bound_revision_id,
            "mode": mode,
            "settings_hash": _stable_hash(resolved_snapshot),
            "active_runs": state["active_runs"],
            "rows": hash_rows,
        }
    )
    blocked_reason: str | None = None
    if generate_count == 0:
        if mode == "missing":
            blocked_reason = "There are no missing speech blocks to generate."
        elif mode == "stale":
            blocked_reason = "There are no missing or stale speech blocks to generate."
        else:
            blocked_reason = "There are no active speech blocks to generate."
    return {
        "mode": mode,
        "speech_plan_revision_id": bound_revision_id,
        "selection_hash": selection_hash,
        "total_count": total_count,
        "generate_count": generate_count,
        "preserve_count": preserve_count,
        "replace_count": replace_count,
        "missing_count": missing_count,
        "generate_segment_ids": generate_ids,
        "preserve_take_ids": preserve_take_ids,
        "reasons": reasons,
        "first_generate_ordinal": first_generate_ordinal,
        "blocked_reason": blocked_reason,
        "settings_summary": _settings_summary(resolved_snapshot),
        "row_state": state,
    }
