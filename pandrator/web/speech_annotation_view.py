"""Read authored speech markup for the drawer without compiling or changing it."""

from sqlalchemy import select

from . import models as m
from .performance_annotations import plan_annotations
from .speech_annotation_records import record_annotation, record_markup


def _annotation_view(record, *, frozen_xml=None):
    xml = frozen_xml or record_markup(record)
    if xml:
        return {"speech_annotation_xml": xml}
    annotation = record_annotation(record)
    return {"speech_annotation": annotation} if annotation is not None else {}


def annotation_view_by_segment(session, revision_id, rows, run=None):
    """Project adopted or frozen annotation data for full segment display."""
    if run is not None:
        snapshot = run.settings_snapshot_json or {}
        controls = (snapshot.get("generation_control_snapshot") or {}).get(
            "segments"
        ) or {}
        annotations = (snapshot.get("performance_snapshot") or {}).get(
            "annotations"
        ) or {}
        return {
            row.id: _annotation_view(
                (annotations.get(row.id) or {}).get("annotation"),
                frozen_xml=(controls.get(row.id) or {}).get("speech_xml"),
            )
            for row in rows
        }
    adopted = session.scalar(
        select(m.PerformancePlan).where(
            m.PerformancePlan.plan_revision_id == revision_id,
            m.PerformancePlan.status == "adopted",
        )
    )
    if adopted is None:
        return {}
    annotations = plan_annotations(session, adopted)
    return {row.id: _annotation_view(annotations.get(row.id)) for row in rows}


def annotation_xml_by_segment(session, revision_id, rows, run=None) -> dict[str, str | None]:
    """Compatibility projection for callers that only consume canonical XML."""
    views = annotation_view_by_segment(session, revision_id, rows, run)
    if not views:
        return {}
    result = {}
    for row in rows:
        xml = views[row.id].get("speech_annotation_xml")
        result[row.id] = xml if isinstance(xml, str) else None
    return result
