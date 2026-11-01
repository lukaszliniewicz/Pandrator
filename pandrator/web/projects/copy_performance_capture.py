"""Bounded current performance intent inside an authorized copy snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..speech_plan_context import plan_signature
from .copy_contracts import project_copy_error
from .copy_performance import MAX_PERFORMANCE_PLANS, freeze_performance_plan_rows

_CHUNK = 500
_SETTINGS = (
    "purpose", "annotation_format", "context_before", "context_after",
    "context_max_chars", "batch_size", "allow_vocalizations", "character_dictionary",
    "base_adopted_plan_id", "base_adopted_plan_version",
)


def _chunks(values):
    for start in range(0, len(values), _CHUNK):
        yield values[start:start + _CHUNK]


def capture_current_performance(
    session: Session, *, rows: Mapping[str, Any], max_rows: int,
    allow_performance: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Capture matching drafts/adopted intent; exclude stale draft history.

    The caller proves project authority in this same unit. Headers and limits
    precede units/annotation bodies; no job, usage or response payload is loaded.
    """
    headers = list(session.execute(
        select(m.PerformancePlan.id, m.PerformancePlan.session_id,
            m.PerformancePlan.plan_revision_id, m.PerformancePlan.base_signature,
            m.PerformancePlan.status, m.PerformancePlan.version)
        .join(m.GenerationPlan, and_(
            m.GenerationPlan.session_id == m.PerformancePlan.session_id,
            m.GenerationPlan.active_revision_id == m.PerformancePlan.plan_revision_id))
        .where(m.PerformancePlan.session_id.in_([row["id"] for row in rows["sessions"]]),
            m.PerformancePlan.status != "superseded")
        .order_by(m.PerformancePlan.created_at, m.PerformancePlan.id)
        .limit(MAX_PERFORMANCE_PLANS + 1)
    ).mappings())
    if len(headers) > MAX_PERFORMANCE_PLANS:
        raise project_copy_error("copy_limit_exceeded")
    if not headers:
        return [], 0
    if not allow_performance:
        return [], len(headers)
    selected = {row["session_id"]: row["active_revision_id"] for row in rows["generation_plans"]}
    signatures: dict[str, str] = {}
    adopted: dict[str, Any] = {}
    candidates = []
    unavailable = 0
    for header in headers:
        sid, revision = header["session_id"], header["plan_revision_id"]
        if header["status"] not in {"draft", "adopted"} or selected.get(sid) != revision:
            unavailable += 1
            continue
        if header["status"] == "adopted":
            if sid in adopted:
                raise project_copy_error("copy_graph_invalid")
            adopted[sid] = header
        if revision not in signatures:
            signatures[revision] = plan_signature(session, revision)
        if header["base_signature"] != signatures[revision]:
            unavailable += header["status"] == "adopted"
            continue
        candidates.append(header)
    if unavailable:
        return [], unavailable
    if not candidates:
        return [], 0

    identifiers = [row["id"] for row in candidates]
    # A passive analysis lease may exist without an active job. It is never
    # transferred into the destination or mistaken for a reviewed annotation.
    leased = set()
    for chunk in _chunks(identifiers):
        leased.update(session.scalars(select(m.PerformanceBatch.performance_plan_id)
            .where(m.PerformanceBatch.performance_plan_id.in_(chunk),
                m.PerformanceBatch.status == "leased",
                m.PerformanceBatch.lease_expires_at > m.utcnow()).distinct()))
    if leased:
        return [], len(leased)

    settings: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    setting_columns = [m.PerformancePlan.settings_json[key].label(key) for key in _SETTINGS]
    presence_columns = [func.json_type(m.PerformancePlan.settings_json, "$." + key)
        .label("_present_" + key) for key in _SETTINGS]
    for chunk in _chunks(identifiers):
        projected = session.execute(select(m.PerformancePlan.id,
            func.json_type(m.PerformancePlan.settings_json).label("_settings_type"),
            func.json_type(m.PerformancePlan.units_json).label("_units_type"),
            func.json_array_length(m.PerformancePlan.units_json).label("_unit_count"),
            *setting_columns, *presence_columns).where(m.PerformancePlan.id.in_(chunk),
                func.json_valid(m.PerformancePlan.settings_json),
                func.json_valid(m.PerformancePlan.units_json)))
        for row in projected.mappings():
            if row["_settings_type"] != "object" or row["_units_type"] != "array":
                raise project_copy_error("copy_graph_invalid")
            # SQLite JSON extraction returns booleans as integer 0/1. Rebuild
            # only actual JSON booleans; numeric lookalikes still fail strict
            # schema validation in the finite codec.
            settings[row["id"]] = {
                key: True if row["_present_" + key] == "true"
                    else False if row["_present_" + key] == "false" else row[key]
                for key in _SETTINGS if row["_present_" + key] is not None
            }
            counts[row["id"]] = row["_unit_count"]
    if set(counts) != set(identifiers):
        raise project_copy_error("copy_graph_invalid")
    candidates = [header for header in candidates if _current_baseline(header, settings, adopted)]
    identifiers = [row["id"] for row in candidates]
    if sum(counts[key] + 1 for key in identifiers) > max_rows:
        raise project_copy_error("copy_limit_exceeded")
    if not identifiers:
        return [], 0
    bodies: dict[str, Any] = {}
    annotations: dict[str, dict[str, Any]] = {key: {} for key in identifiers}
    completed_count = 0
    for chunk in _chunks(identifiers):
        # Only accepted annotations are selected. Response/usage/lease bodies
        # and incomplete automatic results are deliberately outside this query.
        batches = list(session.execute(select(m.PerformanceBatch.performance_plan_id,
            m.PerformanceBatch.annotations_json)
            .where(m.PerformanceBatch.performance_plan_id.in_(chunk),
                m.PerformanceBatch.status == "completed")
            .order_by(m.PerformanceBatch.performance_plan_id, m.PerformanceBatch.ordinal)
            .limit(max_rows + 1)))
        completed_count += len(batches)
        if completed_count > max_rows:
            raise project_copy_error("copy_limit_exceeded")
        for plan_id, accepted in batches:
            if type(accepted) is not dict:
                raise project_copy_error("copy_graph_invalid")
            annotations[plan_id].update(accepted)
        for row in session.execute(select(m.PerformancePlan.id, m.PerformancePlan.units_json,
            m.PerformancePlan.manual_annotations_json).where(m.PerformancePlan.id.in_(chunk))).mappings():
            bodies[row["id"]] = row
    if set(bodies) != set(identifiers):
        raise project_copy_error("copy_graph_invalid")
    values = []
    baselines = {}
    for header in candidates:
        identifier, sid = header["id"], header["session_id"]
        body = bodies[identifier]
        manual = body["manual_annotations_json"]
        if type(manual) is not dict:
            raise project_copy_error("copy_graph_invalid")
        annotations[identifier].update(manual)
        current = adopted.get(sid)
        values.append({**header, "settings_json": settings[identifier], "units_json": body["units_json"]})
        baselines[identifier] = current["id"] if current is not None and header["status"] == "draft" else None
    try:
        return freeze_performance_plan_rows(values, rows=rows, annotations=annotations,
            base_adopted_plan_ids=baselines), 0
    except (ValueError, TypeError, KeyError) as error:
        raise project_copy_error("copy_graph_invalid") from error


def _current_baseline(header, settings, adopted) -> bool:
    value = settings[header["id"]]
    if header["status"] != "draft" or "base_adopted_plan_id" not in value:
        return True
    current = adopted.get(header["session_id"])
    return (value["base_adopted_plan_id"], value.get("base_adopted_plan_version")) == (
        (current["id"], current["version"]) if current is not None else (None, None)
    )
