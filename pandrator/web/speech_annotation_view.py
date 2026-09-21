"""Read authored speech markup for the drawer without compiling or changing it."""

from sqlalchemy import select

from . import models as m
from .speech_annotation_records import record_markup


def annotation_xml_by_segment(session, revision_id, rows, run=None):
    """Keep display metadata separate from immutable source speech-plan fields."""
    if run is not None:
        snapshot = run.settings_snapshot_json or {}
        controls = (snapshot.get("generation_control_snapshot") or {}).get(
            "segments"
        ) or {}
        annotations = (snapshot.get("performance_snapshot") or {}).get(
            "annotations"
        ) or {}
        return {
            row.id: (controls.get(row.id) or {}).get("speech_xml")
            or record_markup(annotations.get(row.id))
            for row in rows
        }
    from .performance_plans import _annotations

    adopted = session.scalar(
        select(m.PerformancePlan).where(
            m.PerformancePlan.plan_revision_id == revision_id,
            m.PerformancePlan.status == "adopted",
        )
    )
    if adopted is None:
        return {}
    annotations = _annotations(session, adopted)
    return {row.id: record_markup(annotations.get(row.id)) for row in rows}
