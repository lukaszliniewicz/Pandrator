"""Duration estimates for speech generation, from measured take timestamps."""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.logic.tts_service_identity import normalize_service_id

from .model_base import as_utc
from .models import AudioTake, GenerationRun, Project, SessionRecord

#: How many finished runs of the same route are inspected for a pace.
HISTORY_RUNS = 3
#: A take is a block; more than this many is a different job, not a pace.
HISTORY_TAKE_LIMIT = 2_000
#: Completed runs this recent are the only ones considered.
_HISTORY_RUN_SCAN = 20


def _settings_pace_key(settings: dict[str, Any]) -> tuple[str, str, str] | None:
    """The route attributes that change how long one block takes."""

    tts = settings.get("tts") if isinstance(settings.get("tts"), dict) else settings
    if not isinstance(tts, dict):
        return None
    service = normalize_service_id(tts.get("service") or tts.get("tts_service"))
    model = str(tts.get("model") or tts.get("xtts_model") or "").strip().lower()
    try:
        concurrency = str(int(tts.get("tts_concurrent_requests") or 1))
    except (TypeError, ValueError):
        concurrency = "1"
    return service, model, concurrency


def _seconds_per_block(moments: list[datetime], concurrency: int) -> float | None:
    """A robust per-block pace from take timestamps.

    Parallel synthesis commits a wave of takes within milliseconds and then
    waits for the next wave, so single gaps are mostly near zero. The pace is
    the median over windows wider than a wave, which still ignores a pause in
    the middle of a run.
    """

    if len(moments) < 3:
        return None
    window = max(4, 2 * max(1, concurrency))
    if len(moments) <= window:
        span = (moments[-1] - moments[0]).total_seconds()
        return span / (len(moments) - 1) if span > 0 else None
    rates = [
        (moments[index + window] - moments[index]).total_seconds() / window
        for index in range(len(moments) - window)
    ]
    rates = [rate for rate in rates if rate > 0]
    return statistics.median(rates) if rates else None


def _run_pace(
    session: Session, run_ids: list[str], concurrency: int
) -> dict[str, Any] | None:
    """Seconds per block of the most recent run that has enough takes."""

    for run_id in run_ids:
        stamps = list(
            session.scalars(
                select(AudioTake.created_at)
                .where(
                    AudioTake.generation_run_id == run_id,
                    AudioTake.created_at.is_not(None),
                )
                .order_by(AudioTake.created_at.asc(), AudioTake.id.asc())
                .limit(HISTORY_TAKE_LIMIT)
            ).all()
        )
        moments: list[datetime] = sorted(as_utc(value) for value in stamps if value is not None)
        pace = _seconds_per_block(moments, concurrency)
        if pace is None:
            continue
        return {
            "run_id": run_id,
            "blocks": len(moments),
            "seconds_per_block": pace,
        }
    return None


def plan_time_estimate(
    session: Session,
    *,
    workspace_id: str,
    settings: dict[str, Any],
    blocks: int | None,
) -> dict[str, Any]:
    """Estimate the synthesis time the plan describes, or say why it cannot.

    The pace comes from the same workspace's most recent completed runs on the
    same service, model and concurrency, so it already reflects parallel
    requests and is not divided again. ``blocks`` is every active block in the
    plan, so audio a run would keep makes this an upper bound.
    """

    unknown = {"estimated_seconds": None, "estimate_basis": "no_history"}
    if blocks is None or blocks < 1:
        return unknown
    wanted = _settings_pace_key(settings)
    if wanted is None:
        return unknown
    recent = list(
        session.execute(
            select(
                GenerationRun.id,
                GenerationRun.settings_snapshot_json,
                GenerationRun.created_at,
            )
            .join(SessionRecord, SessionRecord.id == GenerationRun.session_id)
            .join(Project, Project.id == SessionRecord.project_id)
            .where(
                Project.workspace_id == workspace_id,
                GenerationRun.status == "completed",
                GenerationRun.settings_snapshot_json.is_not(None),
            )
            .order_by(GenerationRun.created_at.desc())
            .limit(_HISTORY_RUN_SCAN)
        ).all()
    )
    matching = [
        str(row.id)
        for row in recent
        if _settings_pace_key(row.settings_snapshot_json or {}) == wanted
    ][:HISTORY_RUNS]
    if not matching:
        return unknown
    pace = _run_pace(session, matching, int(wanted[2]))
    if pace is None:
        return unknown
    seconds = round(float(pace["seconds_per_block"]) * blocks, 1)
    return {
        "estimated_seconds": seconds,
        "estimate_basis": (
            f"{blocks} blocks at {float(pace['seconds_per_block']):.2f} s per block, "
            f"measured over {pace['blocks']} takes in run {pace['run_id']}"
        ),
    }


def block_count(session: Session, session_id: str) -> int | None:
    """How many speech blocks the session's active generation plan holds."""

    from .models import GenerationPlan, GenerationSegment

    plan = session.scalar(
        select(GenerationPlan).where(GenerationPlan.session_id == session_id)
    )
    if plan is None or not plan.active_revision_id:
        return None
    total = session.scalar(
        select(func.count())
        .select_from(GenerationSegment)
        .where(
            GenerationSegment.plan_revision_id == plan.active_revision_id,
            GenerationSegment.removed.is_(False),
        )
    )
    return int(total) if total else None
