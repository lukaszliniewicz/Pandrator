"""Bounded pure contract checks for the current project sound-copy codec."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from pandrator.web.projects.copy_sound import (
    COPIED_SOUND_KEY,
    CUE_FIELDS,
    PLAN_FIELDS,
    SOUND_FAMILIES,
    VARIANT_FIELDS,
    freeze_sound_artifact,
    freeze_sound_cue,
    freeze_sound_variant,
    remap_sound_copy_rows,
    validate_sound_copy_graph,
)

_ERROR = "Invalid current sound-plan copy graph."
_MARKER = {COPIED_SOUND_KEY: {"version": 1}}
_DISPLAY_MARKER = {COPIED_SOUND_KEY: {"version": 1}, "display_name": "Chime"}


class _Poison:
    def __deepcopy__(self, _memo: dict[int, Any]) -> Any:
        raise AssertionError("discarded value was copied")

    def __iter__(self):
        raise AssertionError("discarded value was traversed")

    def __repr__(self) -> str:
        raise AssertionError("discarded value was formatted")


def _artifact(**changes: Any) -> dict[str, Any]:
    row = {
        "id": "artifact",
        "workspace_id": "workspace",
        "project_id": "project",
        "session_id": None,
        "kind": "audio",
        "role": "sound.original",
        "relative_path": "managed/sound.wav",
        "mime_type": "audio/wav",
        "size_bytes": 4096,
        "content_hash": "a" * 64,
        "state": "current",
        "settings_hash": None,
        "metadata_json": deepcopy(_MARKER),
    }
    row.update(changes)
    return row


def _variant(
    identifier: str = "variant", artifact_id: str = "artifact", **changes: Any
) -> dict[str, Any]:
    row = {
        "id": identifier,
        "workspace_id": "workspace",
        "item_id": None,
        "job_id": None,
        "artifact_id": artifact_id,
        "state": "ready",
        "fingerprint": None,
        "provenance_json": deepcopy(_DISPLAY_MARKER),
        "sample_rate": 44100,
        "channels": 2,
        "frame_count": 44100,
        "duration_seconds": 1.0,
    }
    row.update(changes)
    return row


def _plan(speech_revision_id: str | None = "speech-revision") -> dict[str, Any]:
    return {
        "id": "sound-plan",
        "session_id": "session",
        "revision": 3,
        "speech_plan_revision_id": speech_revision_id,
    }


def _cue(
    *,
    identifier: str = "cue",
    logical_id: str = "logical-cue",
    variant_id: str = "variant",
    anchor: dict[str, Any] | None = None,
    playback: dict[str, Any] | None = None,
    included: bool = False,
    placement_status: str = "ready",
) -> dict[str, Any]:
    return {
        "id": identifier,
        "logical_id": logical_id,
        "plan_id": "sound-plan",
        "variant_id": variant_id,
        "anchor_json": deepcopy(
            anchor if anchor is not None else {"kind": "source_time", "time_seconds": 2.5}
        ),
        "playback_json": deepcopy(playback if playback is not None else {"mode": "overlay"}),
        "included": included,
        "placement_status": placement_status,
    }


def _graph(
    *,
    workflow: str = "voiceover",
    anchor: dict[str, Any] | None = None,
    with_plan: bool = True,
    with_cue: bool = True,
    with_speech: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {
        "projects": [{"id": "project", "workspace_id": "workspace"}],
        "sessions": [{"id": "session", "project_id": "project", "workflow_kind": workflow}],
        "generation_plans": [],
        "generation_plan_revisions": [],
        "generation_segments": [],
        "artifacts": [],
        "artifact_edges": [],
        "project_sound_plans": [],
        "project_sound_cues": [],
        "sound_variants": [],
    }
    if with_speech:
        rows["generation_plans"] = [
            {
                "id": "generation-plan",
                "session_id": "session",
                "active_revision_id": "speech-revision",
            }
        ]
        rows["generation_plan_revisions"] = [
            {
                "id": "speech-revision",
                "plan_id": "generation-plan",
            }
        ]
        rows["generation_segments"] = [
            {
                "id": "segment",
                "plan_revision_id": "speech-revision",
            }
        ]
    if with_plan:
        rows["project_sound_plans"] = [_plan("speech-revision" if with_speech else None)]
    if with_cue and with_plan:
        rows["project_sound_cues"] = [
            freeze_sound_cue(
                _cue(anchor=anchor),
                workflow_kind=workflow,
            )
        ]
        rows["sound_variants"] = [_variant()]
        rows["artifacts"] = [_artifact()]
    return rows


def _assert_invalid(callable_: Any) -> None:
    with pytest.raises(ValueError) as error:
        callable_()
    assert str(error.value) == _ERROR


@pytest.mark.parametrize(
    ("anchor", "workflow", "status"),
    [
        ({"kind": "source_time", "time_seconds": 2.5}, "voiceover", "ready"),
        ({"kind": "before_segment", "segment_id": "segment"}, "audiobook", "ready"),
        ({"kind": "after_segment", "segment_id": "segment"}, "subtitles", "ready"),
        (
            {"kind": "after_word", "segment_id": "segment", "quote": "hello", "occurrence": 1},
            "voiceover",
            "needs_placement",
        ),
        (
            {"kind": "next_pause", "segment_id": "segment", "quote": "hello", "occurrence": 1},
            "subtitles",
            "needs_placement",
        ),
    ],
)
def test_freezes_timed_and_segment_anchors(
    anchor: dict[str, Any], workflow: str, status: str
) -> None:
    frozen = freeze_sound_cue(_cue(anchor=anchor, placement_status="ready"), workflow_kind=workflow)
    assert frozen["anchor_json"] == anchor
    assert frozen["placement_status"] == status


def test_graph_keeps_excluded_current_cue_and_allows_shared_variants_and_audio() -> None:
    rows = _graph()
    rows["sound_variants"].append(_variant("variant-two"))
    rows["project_sound_cues"].append(
        freeze_sound_cue(
            _cue(identifier="cue-two", logical_id="logical-two", variant_id="variant-two"),
            workflow_kind="voiceover",
        )
    )
    rows["project_sound_cues"].append(
        freeze_sound_cue(
            _cue(identifier="cue-three", logical_id="logical-three"),
            workflow_kind="voiceover",
        )
    )
    assert rows["project_sound_cues"][0]["included"] is False
    assert len(rows["artifacts"]) == 1
    validate_sound_copy_graph(rows, allow_sounds=True)


def test_empty_current_plan_and_no_plan_are_both_preserved() -> None:
    validate_sound_copy_graph(_graph(with_cue=False), allow_sounds=True)
    validate_sound_copy_graph(_graph(with_plan=False, with_cue=False), allow_sounds=True)


def test_no_sound_graph_skips_core_context_but_still_rejects_orphan_marker() -> None:
    rows: dict[str, list[dict[str, Any]]] = {
        "project_sound_plans": [],
        "project_sound_cues": [],
        "sound_variants": [],
    }
    validate_sound_copy_graph(rows, allow_sounds=True)

    rows["artifacts"] = [_artifact()]
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))

    rows["artifacts"] = []
    rows.pop("sound_variants")
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))


def test_freezer_sanitizes_anchor_provenance_and_fills_defaults_without_traversing_it() -> None:
    poison = _Poison()
    row = _cue(playback={})
    row["anchor_json"] = {
        "kind": "before_segment",
        "segment_id": "segment",
        "origin_text": poison,
        "resolution": poison,
    }
    frozen = freeze_sound_cue(row, workflow_kind="voiceover")
    assert frozen["anchor_json"] == {"kind": "before_segment", "segment_id": "segment"}
    assert frozen["playback_json"] == {
        "mode": "overlay",
        "offset_ms": 0,
        "gain_db": 0.0,
        "trim_start_seconds": 0.0,
        "fade_in_ms": 5,
        "fade_out_ms": 5,
    }
    assert row["anchor_json"]["origin_text"] is poison


def test_factory_projects_variant_and_artifact_without_reading_discarded_payloads() -> None:
    poison = _Poison()
    source_variant = _variant(
        item_id=poison,
        job_id=poison,
        fingerprint=poison,
        provenance_json=poison,
        prompt=poison,
    )
    frozen_variant = freeze_sound_variant(source_variant, display_name="A name" * 60)
    assert set(frozen_variant) == VARIANT_FIELDS
    assert frozen_variant["provenance_json"]["display_name"] == ("A name" * 60)[:255]
    assert (
        frozen_variant["item_id"]
        is frozen_variant["job_id"]
        is frozen_variant["fingerprint"]
        is None
    )

    source_artifact = _artifact(metadata_json=poison, settings_hash=poison, ancestry=poison)
    frozen_artifact = freeze_sound_artifact(source_artifact)
    assert set(frozen_artifact) == {
        "id",
        "workspace_id",
        "project_id",
        "session_id",
        "kind",
        "role",
        "relative_path",
        "mime_type",
        "size_bytes",
        "content_hash",
        "state",
        "settings_hash",
        "metadata_json",
    }
    assert frozen_artifact["metadata_json"] == _MARKER
    assert frozen_artifact["settings_hash"] is None


def test_graph_requires_already_frozen_canonical_cues() -> None:
    rows = _graph(anchor={"kind": "before_segment", "segment_id": "segment"})
    raw = deepcopy(rows["project_sound_cues"][0])
    raw["anchor_json"]["origin_note"] = _Poison()
    rows["project_sound_cues"][0] = raw
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))

    rows = _graph(
        anchor={
            "kind": "after_word",
            "segment_id": "segment",
            "quote": "hello",
            "occurrence": 1,
        }
    )
    rows["project_sound_cues"][0]["placement_status"] = "ready"
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))

    rows = _graph()
    rows["project_sound_cues"][0]["playback_json"] = {"mode": "overlay"}
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))


def test_source_time_and_playback_workflow_rules_are_strict() -> None:
    _assert_invalid(
        lambda: freeze_sound_cue(
            _cue(anchor={"kind": "source_time", "time_seconds": 1.0}),
            workflow_kind="audiobook",
        )
    )
    _assert_invalid(
        lambda: freeze_sound_cue(
            _cue(
                anchor={"kind": "source_time", "time_seconds": 1.0},
                playback={"mode": "insert"},
            ),
            workflow_kind="media_edit",
        )
    )
    _assert_invalid(
        lambda: freeze_sound_cue(
            _cue(
                anchor={"kind": "before_segment", "segment_id": "segment"},
                playback={"mode": "insert", "offset_ms": -1},
            ),
            workflow_kind="audiobook",
        )
    )
    _assert_invalid(
        lambda: freeze_sound_cue(
            _cue(anchor={"kind": "source_time", "time_seconds": True}),
            workflow_kind="voiceover",
        )
    )


@pytest.mark.parametrize(
    "defect",
    [
        "speech_pin",
        "foreign_segment",
        "foreign_workspace",
        "foreign_artifact",
        "nonready",
        "missing_audio",
    ],
)
def test_graph_rejects_invalid_pins_ownership_and_selected_audio(defect: str) -> None:
    rows = _graph(anchor={"kind": "before_segment", "segment_id": "segment"})
    if defect == "speech_pin":
        rows["project_sound_plans"][0]["speech_plan_revision_id"] = None
    elif defect == "foreign_segment":
        rows["project_sound_cues"][0]["anchor_json"]["segment_id"] = "foreign"
    elif defect == "foreign_workspace":
        rows["sound_variants"][0]["workspace_id"] = "other-workspace"
    elif defect == "foreign_artifact":
        rows["artifacts"][0]["project_id"] = "other-project"
    elif defect == "nonready":
        rows["sound_variants"][0]["state"] = "stale"
    else:
        rows["artifacts"] = []
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))


@pytest.mark.parametrize(
    "defect",
    ["duplicate_logical", "orphan_variant", "history_plan", "incoming_edge", "orphan_marker"],
)
def test_graph_rejects_forged_duplicate_or_orphan_rows(defect: str) -> None:
    rows = _graph()
    if defect == "duplicate_logical":
        rows["project_sound_cues"].append(
            freeze_sound_cue(
                _cue(identifier="cue-two", logical_id="logical-cue"),
                workflow_kind="voiceover",
            )
        )
    elif defect == "orphan_variant":
        rows["sound_variants"].append(_variant("variant-two"))
        rows["artifacts"].append(_artifact(id="artifact-two"))
    elif defect == "history_plan":
        rows["project_sound_plans"].append(
            {
                "id": "older-plan",
                "session_id": "session",
                "revision": 1,
                "speech_plan_revision_id": "speech-revision",
            }
        )
    elif defect == "incoming_edge":
        rows["artifact_edges"].append(
            {
                "parent_artifact_id": "ancestor",
                "child_artifact_id": "artifact",
                "relation": "derived_from",
            }
        )
    else:
        rows["artifacts"].append(
            _artifact(
                id="orphan-artifact",
                metadata_json=deepcopy(_MARKER),
            )
        )
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))


@pytest.mark.parametrize(
    "defect",
    [
        "bool_revision",
        "bool_sample_rate",
        "bool_included",
        "nonfinite_playback",
        "extra_cue_field",
        "bad_artifact_size",
    ],
)
def test_exact_types_fields_and_finite_json_are_enforced(defect: str) -> None:
    if defect == "bool_sample_rate":
        _assert_invalid(lambda: freeze_sound_variant(_variant(sample_rate=True), display_name=None))
        return
    if defect == "bad_artifact_size":
        _assert_invalid(lambda: freeze_sound_artifact(_artifact(size_bytes=True)))
        return
    rows = _graph()
    if defect == "bool_revision":
        rows["project_sound_plans"][0]["revision"] = True
    elif defect == "bool_included":
        rows["project_sound_cues"][0]["included"] = 1
    elif defect == "nonfinite_playback":
        rows["project_sound_cues"][0]["playback_json"]["gain_db"] = float("nan")
    else:
        rows["project_sound_cues"][0]["unexpected"] = "field"
    _assert_invalid(lambda: validate_sound_copy_graph(rows, allow_sounds=True))


def test_strict_default_rejects_sound_rows_and_reserved_artifact_markers() -> None:
    rows = {"project_sound_plans": [], "project_sound_cues": [], "sound_variants": []}
    validate_sound_copy_graph(rows)
    rows["project_sound_cues"] = [{}]
    _assert_invalid(lambda: validate_sound_copy_graph(rows))
    rows["project_sound_cues"] = []
    rows["artifacts"] = [_artifact()]
    _assert_invalid(lambda: validate_sound_copy_graph(rows))
    rows["artifacts"] = []
    rows.pop("sound_variants")
    validate_sound_copy_graph(rows)
    assert SOUND_FAMILIES == {"project_sound_plans", "project_sound_cues", "sound_variants"}
    assert PLAN_FIELDS == {"id", "session_id", "revision", "speech_plan_revision_id"}
    assert CUE_FIELDS == {
        "id",
        "logical_id",
        "plan_id",
        "variant_id",
        "anchor_json",
        "playback_json",
        "included",
        "placement_status",
    }
    assert VARIANT_FIELDS == {
        "id",
        "workspace_id",
        "item_id",
        "job_id",
        "artifact_id",
        "state",
        "fingerprint",
        "provenance_json",
        "sample_rate",
        "channels",
        "frame_count",
        "duration_seconds",
    }


def test_remap_rebinds_roots_segments_reusable_audio_and_placement_without_mutation() -> None:
    rows = _graph(
        anchor={"kind": "after_word", "segment_id": "segment", "quote": "hello", "occurrence": 1},
    )
    original = deepcopy(rows)
    identities = {
        "sound-plan": "new-sound-plan",
        "session": "new-session",
        "speech-revision": "new-speech-revision",
        "segment": "new-segment",
        "cue": "new-cue",
        "variant": "new-variant",
        "artifact": "new-artifact",
    }
    copied = remap_sound_copy_rows(rows, identities, destination_workspace_id="destination")
    assert rows == original
    assert copied["project_sound_plans"] == [
        {
            "id": "new-sound-plan",
            "session_id": "new-session",
            "revision": 1,
            "speech_plan_revision_id": "new-speech-revision",
        }
    ]
    cue = copied["project_sound_cues"][0]
    assert cue["id"] == cue["logical_id"] == "new-cue"
    assert cue["plan_id"] == "new-sound-plan"
    assert cue["variant_id"] == "new-variant"
    assert cue["anchor_json"]["segment_id"] == "new-segment"
    assert cue["placement_status"] == "needs_placement"
    assert copied["sound_variants"][0]["workspace_id"] == "destination"
    assert copied["sound_variants"][0]["artifact_id"] == "new-artifact"
    cue["anchor_json"]["quote"] = "changed"
    assert rows["project_sound_cues"][0]["anchor_json"]["quote"] == "hello"


@pytest.mark.parametrize("defect", ["missing", "invalid", "collision", "source_id_collision"])
def test_remap_rejects_missing_invalid_or_colliding_required_ids(defect: str) -> None:
    rows = _graph()
    identities = {
        "sound-plan": "new-sound-plan",
        "session": "new-session",
        "speech-revision": "new-speech-revision",
        "cue": "new-cue",
        "variant": "new-variant",
        "artifact": "new-artifact",
    }
    if defect == "missing":
        identities.pop("artifact")
    elif defect == "invalid":
        identities["cue"] = "x" * 37
    elif defect == "collision":
        identities["variant"] = "new-cue"
    else:
        identities["cue"] = "variant"
        identities["variant"] = "cue"
    _assert_invalid(
        lambda: remap_sound_copy_rows(
            rows,
            identities,
            destination_workspace_id="destination",
        )
    )
