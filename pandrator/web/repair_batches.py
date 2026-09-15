"""User-level second-pass batches over immutable generation checkpoints.

Internal candidates remain immutable and available to historical audio runs.
Grouping precedes pagination; full audio statistics are inspected only for
visible rows. Undo appends one restore revision and never rewrites history.

This covers both automatic second passes, which share the same guard
snapshot: early timing repair (one block split and regenerated) and passage
regroup (adjacent passages merged and regenerated as one group with the same
provider and no new word alignment).  Regroup batches are labelled as such
and never described as splitting or alignment.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from . import models as m
from .generation_review import revision_history
from .source_management import assert_session_idle
from .workspace import RevisionConflict, stable_hash

REPAIR_REASON = "early_timing_repair"
REGROUP_REASON = "passage_regroup"
MIXED_REASON = "mixed"
SECOND_PASS_REASONS = frozenset({REPAIR_REASON, REGROUP_REASON})
UNDO_REPAIR_REASON = "undo_timing_repairs"
UNDO_REGROUP_REASON = "undo_passage_regroup"
GUARD_KEY = "repair_batch_snapshot"
GUARD_VERSION = 1


def repair_state_hash(session, revision_id: str) -> str:
    """Guard editorial state and selected audio, including in-place changes."""
    revision = session.get(m.GenerationPlanRevision, revision_id)
    if revision is None:
        raise KeyError(revision_id)
    columns = [column for column in m.GenerationSegment.__table__.columns
               if column.name not in {"created_at", "updated_at"}]
    segments = [dict(row) for row in session.execute(
        select(*columns).where(m.GenerationSegment.plan_revision_id == revision_id)
        .order_by(m.GenerationSegment.ordinal, m.GenerationSegment.id)
    ).mappings()]
    take_columns = [column for column in m.AudioTake.__table__.columns
                    if column.name not in {"created_at", "updated_at"}]
    takes = [dict(row) for row in session.execute(
        select(*take_columns).join(m.GenerationSegment,
            m.GenerationSegment.id == m.AudioTake.generation_segment_id)
        .where(m.GenerationSegment.plan_revision_id == revision_id,
               m.AudioTake.is_active.is_(True))
        .order_by(m.AudioTake.generation_segment_id, m.AudioTake.id)
    ).mappings()]
    artifact_ids = {take["artifact_id"] for take in takes if take.get("artifact_id")}
    source_id = (revision.settings_json or {}).get("_source_artifact_id")
    if source_id:
        artifact_ids.add(source_id)
    artifacts = [dict(row) for row in session.execute(select(
        m.Artifact.id, m.Artifact.state, m.Artifact.content_hash,
    ).where(m.Artifact.id.in_(artifact_ids)).order_by(m.Artifact.id)).mappings()]
    return stable_hash({
        "revision_id": revision.id, "source_revision_id": revision.source_revision_id,
        "settings": revision.settings_json or {}, "segments": segments,
        "takes": takes, "artifacts": artifacts,
    })


def capture_repair_base(session, revision_id: str) -> dict[str, Any]:
    return {"schema_version": GUARD_VERSION, "base_revision_id": revision_id,
            "base_state_hash": repair_state_hash(session, revision_id)}


def record_accepted_repair(session, revision_id: str) -> None:
    """Record the accepted snapshot only after all replacement takes are saved."""
    session.flush()
    revision = session.get(m.GenerationPlanRevision, revision_id)
    operation = dict(revision.operation_json or {})
    guard = dict(operation.get(GUARD_KEY) or {})
    if guard.get("schema_version") != GUARD_VERSION:
        return
    guard["result_state_hash"] = repair_state_hash(session, revision_id)
    revision.operation_json = {**operation, GUARD_KEY: guard}


def _history_metadata(session, session_id: str):
    record = session.get(m.SessionRecord, session_id)
    if record is None or record.trashed_at is not None:
        raise KeyError(session_id)
    plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id))
    if plan is None:
        return None, [], {}
    revision = m.GenerationPlanRevision
    operation = revision.operation_json
    # Do not load every checkpoint's large lineage mappings or segment rows.
    rows = [dict(row) for row in session.execute(select(
        revision.id, revision.revision_number, revision.parent_revision_id,
        operation["reason"].as_string().label("reason"),
        operation["source_generation_run_id"].as_string().label("batch_id"),
        operation["repair_status"].as_string().label("repair_status"),
        operation["repair_reason"].as_string().label("repair_reason"),
        operation["source_block_ordinal"].as_integer().label("source_block_ordinal"),
        operation[GUARD_KEY].label("guard"),
    ).where(revision.plan_id == plan.id).order_by(revision.revision_number)).mappings()]
    root_ids = {row["batch_id"] for row in rows if row["reason"] in SECOND_PASS_REASONS and row["batch_id"]}
    roots = {}
    for offset in range(0, len(root_ids), 400):
        chunk = sorted(root_ids)[offset:offset + 400]
        for root in session.execute(select(
            m.GenerationRun.id, m.GenerationRun.plan_revision_id,
            m.GenerationRun.status, m.GenerationRun.job_id,
        ).where(m.GenerationRun.id.in_(chunk), m.GenerationRun.session_id == session_id)).mappings():
            roots[root["id"]] = dict(root)
    return plan, rows, roots


def _groups(plan, rows, roots):
    by_id = {row["id"]: row for row in rows}
    batches = defaultdict(list)
    entries = []
    for row in rows:
        root = roots.get(row["batch_id"])
        if row["reason"] in SECOND_PASS_REASONS and root and root["plan_revision_id"] in by_id:
            batches[row["batch_id"]].append(row)
        else:
            entries.append({"entry_id": row["id"], "row": row,
                            "sort_number": row["revision_number"], "batch": None})
    for batch_id, attempts in batches.items():
        root = roots[batch_id]
        applied = [row for row in attempts if row["repair_status"] == "applied"]
        result = applied[-1] if applied else by_id[root["plan_revision_id"]]
        # Explicitly selected internal checkpoints must never become invisible.
        representative = next((row for row in attempts if row["id"] == plan.active_revision_id), result)
        entries.append({
            "entry_id": f"repair-batch:{batch_id}", "row": representative,
            "sort_number": attempts[-1]["revision_number"],
            "batch": {"id": batch_id, "root": root, "attempts": attempts,
                      "result": result, "applied": applied},
        })
    return sorted(entries, key=lambda entry: entry["sort_number"], reverse=True)


def _batch_reason(batch) -> str:
    """Classify a batch without mislabelling regroup as splitting/alignment."""
    reasons = {row.get("reason") for row in batch["attempts"] if row.get("reason")}
    if reasons == {REGROUP_REASON}:
        return REGROUP_REASON
    if reasons == {REPAIR_REASON}:
        return REPAIR_REASON
    if not reasons:
        return REPAIR_REASON
    return MIXED_REASON


def _batch_summary_text(batch_reason: str, applied_count: int, attempt_count: int) -> str:
    if batch_reason == REGROUP_REASON:
        return f"Automatic passage regroup · {applied_count} accepted / {attempt_count} attempted"
    if batch_reason == MIXED_REASON:
        return f"Automatic second-pass repair · {applied_count} accepted / {attempt_count} attempted"
    return f"Automatic timing repair · {applied_count} accepted / {attempt_count} attempted"


def _batch_state(session, batch) -> str:
    root = batch["root"]
    job = session.get(m.Job, root["job_id"]) if root["job_id"] else None
    if root["status"] in {"queued", "running", "pausing", "cancel_requested"} or (
        job and job.status in {"queued", "running", "cancel_requested"}
    ):
        return "running"
    statuses = {row["repair_status"] for row in batch["attempts"]}
    stopped = root["status"] in {"canceled", "cancelled", "paused", "partial"} or bool(statuses & {"pending", "stopped"})
    failed = root["status"] == "failed" or "failed" in statuses
    if stopped or failed:
        return "partial" if batch["applied"] else "failed" if failed else "stopped"
    return "completed" if batch["applied"] else "no_changes"


def _batch_summary(session, session_id, active_id, batch, *, check_guard=True):
    result = batch["result"]
    base_id = batch["root"]["plan_revision_id"]
    guard = result.get("guard") or {}
    if not isinstance(guard, dict):
        guard = {}
    status = _batch_state(session, batch)
    reason = ""
    if not batch["applied"]:
        reason = "No automatic repairs were applied."
    elif status == "running":
        reason = "Finish or stop the repair run before undoing its changes."
    elif active_id != result["id"]:
        reason = "The active plan has changed. Restore the original as a separate copy to preserve later work."
    elif guard.get("schema_version") != GUARD_VERSION or guard.get("base_revision_id") != base_id or not guard.get("result_state_hash"):
        reason = "This older batch has no verified undo snapshot. Inspect or restore the original as a separate copy."
    elif check_guard:
        try:
            assert_session_idle(session, session_id)
        except RevisionConflict:
            reason = "Finish or stop active session work before undoing automatic repairs."
        if not reason and repair_state_hash(session, result["id"]) != guard["result_state_hash"]:
            reason = "The repaired text or selected audio changed. Restore the original as a separate copy to preserve later work."
        if not reason and repair_state_hash(session, base_id) != guard.get("base_state_hash"):
            reason = "The pre-repair plan or selected audio changed; the saved undo snapshot no longer matches."
    return {
        "id": batch["id"], "base_revision_id": base_id,
        "result_revision_id": result["id"], "attempt_count": len(batch["attempts"]),
        "applied_count": len(batch["applied"]),
        "rejected_count": sum(row["repair_status"] not in {"applied", "pending"} for row in batch["attempts"]),
        "status": status, "can_undo": not reason, "undo_disabled_reason": reason or None,
        "expected_revision_id": result["id"],
        "expected_state_hash": guard.get("result_state_hash"),
        "reason": _batch_reason(batch),
    }


def grouped_revision_history(database, session_id: str, *, limit=50, before_revision_number=None):
    limit = max(1, min(int(limit), 100))
    with database.session() as session:
        plan, rows, roots = _history_metadata(session, session_id)
        if plan is None:
            return {"items": [], "active_revision_id": None, "total": 0,
                    "checkpoint_total": 0, "next_before_revision_number": None}
        entries = _groups(plan, rows, roots)
        total = len(entries)
        if before_revision_number is not None:
            entries = [entry for entry in entries if entry["sort_number"] < before_revision_number]
        has_more = len(entries) > limit
        entries = entries[:limit]
        active_id = plan.active_revision_id
        summaries = {
            entry["entry_id"]: _batch_summary(session, session_id, active_id, entry["batch"])
            for entry in entries if entry["batch"]
        }
    # Keep reuse/identity semantics identical to the original history endpoint.
    raw = revision_history(database, session_id, limit=100,
                           revision_ids=list({entry["row"]["id"] for entry in entries}))
    details = {row["id"]: row for row in raw["items"]}
    items = []
    for entry in entries:
        item = dict(details.get(entry["row"]["id"], {}))
        if not item:
            continue  # Concurrent source removal: never fabricate an entry.
        item.update(entry_id=entry["entry_id"], history_revision_number=entry["sort_number"])
        if entry["batch"]:
            summary = summaries[entry["entry_id"]]
            item.update(repair_batch=summary, summary=_batch_summary_text(summary["reason"], summary["applied_count"], summary["attempt_count"]), origin="automatic")
            item["repair_status"] = None  # The batch has its own aggregate status.
            item["is_repair_checkpoint"] = item["id"] != summary["result_revision_id"]
            if item["is_repair_checkpoint"]:
                item["summary"] += " · selected checkpoint"
        items.append(item)
    return {"items": items, "active_revision_id": active_id, "total": total,
            "checkpoint_total": len(rows),
            "next_before_revision_number": entries[-1]["sort_number"] if entries and has_more else None}


def repair_batch_detail(database, session_id, batch_id, *, limit=50, before_revision_number=None):
    limit = max(1, min(int(limit), 100))
    with database.session() as session:
        plan, rows, roots = _history_metadata(session, session_id)
        entries = _groups(plan, rows, roots) if plan else []
        batch = next((entry["batch"] for entry in entries if entry["batch"] and entry["batch"]["id"] == batch_id), None)
        if batch is None:
            raise KeyError(batch_id)
        summary = _batch_summary(session, session_id, plan.active_revision_id, batch)
        attempts = list(reversed(batch["attempts"]))
        if before_revision_number is not None:
            attempts = [row for row in attempts if row["revision_number"] < before_revision_number]
        has_more = len(attempts) > limit
        attempts = attempts[:limit]
        return {"repair_batch": summary, "active_revision_id": plan.active_revision_id,
                "items": [{key: row[key] for key in ("id", "revision_number", "reason", "repair_status", "repair_reason", "source_block_ordinal")} for row in attempts],
                "next_before_revision_number": attempts[-1]["revision_number"] if attempts and has_more else None}


def undo_repair_batch_in_session(service, session, session_id, batch_id, *, expected_revision_id, expected_state_hash):
    plan, rows, roots = _history_metadata(session, session_id)
    entries = _groups(plan, rows, roots) if plan else []
    batch = next((entry["batch"] for entry in entries if entry["batch"] and entry["batch"]["id"] == batch_id), None)
    if batch is None:
        raise KeyError(batch_id)
    summary = _batch_summary(session, session_id, plan.active_revision_id, batch)
    if plan.active_revision_id != expected_revision_id or expected_revision_id != summary["result_revision_id"]:
        raise RevisionConflict("The active plan changed. No automatic repairs were undone.")
    if not summary["can_undo"]:
        raise RevisionConflict(summary["undo_disabled_reason"])
    if expected_state_hash != summary["expected_state_hash"]:
        raise RevisionConflict("The repair snapshot changed. Refresh history before undoing.")
    result = service.revise_topology_in_session(session, session_id, expected_revision_id, {
        "action": "restore", "target_revision_id": summary["base_revision_id"],
        "reason": UNDO_REGROUP_REASON if summary["reason"] == REGROUP_REASON else UNDO_REPAIR_REASON,
        "repair_batch_id": batch_id,
    })
    return {**result, "undone_repair_batch_id": batch_id,
            "restored_from_revision_id": summary["base_revision_id"]}
