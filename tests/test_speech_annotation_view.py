"""Projection of adopted and frozen speech annotation display data."""

from types import SimpleNamespace
from typing import Any, cast

from pandrator.web.speech_annotation_view import (
    annotation_view_by_segment,
    annotation_xml_by_segment,
)


def test_frozen_annotation_unwraps_record_and_copies_legacy_data():
    row = SimpleNamespace(id="segment-1")
    annotation = {
        "decision": "steer",
        "delivery": {"instruction": "Quietly"},
        "spans": [{"start": 0, "end": 2, "delivery": {"emotion": "warm"}}],
        "events": [{"kind": "laugh", "start": 2}],
        "_speech_xml": None,
    }
    snapshot = {
        "performance_snapshot": {
            "annotations": {row.id: {"text_hash": "frozen", "annotation": annotation}}
        }
    }
    run = SimpleNamespace(settings_snapshot_json=snapshot)
    view = annotation_view_by_segment(None, "revision-1", [row], run)
    assert view[row.id] == {
        "speech_annotation": {
            "decision": "steer",
            "delivery": {"instruction": "Quietly"},
            "spans": [{"start": 0, "end": 2, "delivery": {"emotion": "warm"}}],
            "events": [{"kind": "laugh", "start": 2}],
        }
    }
    displayed = cast(dict[str, Any], view[row.id]["speech_annotation"])
    displayed["spans"][0]["delivery"]["emotion"] = "changed"
    assert annotation["spans"][0]["delivery"]["emotion"] == "warm"


def test_frozen_xml_precedence_and_nested_wrapper_xml():
    row = SimpleNamespace(id="segment-1")
    nested_xml = '<segment id="segment-1">Hello</segment>'
    control_xml = '<segment id="segment-1"><narrator>Hello</narrator></segment>'
    snapshot = {
        "performance_snapshot": {
            "annotations": {
                row.id: {
                    "text_hash": "frozen",
                    "annotation": {"decision": "steer", "_speech_xml": nested_xml},
                }
            }
        }
    }
    run = SimpleNamespace(settings_snapshot_json=snapshot)
    assert annotation_view_by_segment(None, "revision-1", [row], run)[row.id] == {
        "speech_annotation_xml": nested_xml
    }
    assert annotation_xml_by_segment(None, "revision-1", [row], run)[row.id] == nested_xml

    snapshot["generation_control_snapshot"] = {
        "segments": {row.id: {"speech_xml": control_xml}}
    }
    assert annotation_view_by_segment(None, "revision-1", [row], run)[row.id] == {
        "speech_annotation_xml": control_xml
    }
    assert annotation_xml_by_segment(None, "revision-1", [row], run)[row.id] == control_xml
