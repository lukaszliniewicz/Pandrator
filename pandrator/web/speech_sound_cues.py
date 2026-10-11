"""DB-backed proposal for sound markers in one speech segment."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pandrator.logic.speech_performance import Anchor, anchor_range
from pandrator.logic.speech_sound_markup import (
    MAX_SOUND_MARKERS,
    SoundMarker,
    preceding_lexical_anchor,
)

from .sound_project_commands import (
    assert_revision,
    latest_cues,
    normalize_placement,
    require_variant,
)
from .sound_project_queries import current_header
from .sound_project_schemas import SoundAnchor, SoundPlayback


@dataclass(frozen=True, slots=True)
class SoundProposal:
    revision: int
    plan: Any
    active_revision_id: str | None
    current: list[dict[str, Any]]
    candidate: list[dict[str, Any]]
    represented_ids: frozenset[str]
    changes: dict[str, list[str]]

    @property
    def changed(self) -> bool:
        return self.current != self.candidate


def marker_for_cue(cue: dict[str, Any], segment_id: str, text: str) -> SoundMarker | None:
    anchor = cue["anchor_json"]
    if anchor.get("segment_id") != segment_id:
        return None
    kind = anchor.get("kind")
    if kind == "before_segment":
        placement, offset = "before", 0
    elif kind == "after_segment":
        placement, offset = "after", len(text)
    elif kind in {"after_word", "next_pause"}:
        try:
            start, end = anchor_range(
                text, Anchor(quote=anchor["quote"], occurrence=anchor["occurrence"])
            )
            if anchor.get("start", start) != start or anchor.get("end", end) != end:
                return None
            lexical = preceding_lexical_anchor(text, end)
            if lexical["start"] != start or lexical["end"] != end:
                return None
        except (ValueError, KeyError, TypeError):
            return None
        placement, offset = ("after" if kind == "after_word" else "next_pause"), end
    else:
        return None
    return SoundMarker(cue["logical_id"], cue["variant_id"], placement, offset)


def visible_markers(cues: list[dict[str, Any]], segment_id: str, text: str) -> list[SoundMarker]:
    markers = (
        marker_for_cue(cue, segment_id, text)
        for cue in sorted(cues, key=lambda item: item["logical_id"])
    )
    return [marker for marker in markers if marker is not None][:MAX_SOUND_MARKERS]


def _anchor_for_marker(marker: SoundMarker, segment_id: str, text: str) -> SoundAnchor:
    if marker.placement == "before":
        if marker.offset != 0:
            raise ValueError("A before marker is allowed only at segment start.")
        return SoundAnchor(kind="before_segment", segment_id=segment_id)
    if marker.placement == "after" and marker.offset == len(text):
        return SoundAnchor(kind="after_segment", segment_id=segment_id)
    if marker.placement == "after" and marker.offset == 0:
        raise ValueError("An after marker needs preceding speech.")
    if marker.placement not in {"after", "next_pause"}:
        raise ValueError("Invalid sound marker placement.")
    token = preceding_lexical_anchor(text, marker.offset)
    return SoundAnchor(
        kind="after_word" if marker.placement == "after" else "next_pause",
        segment_id=segment_id,
        **token,
    )


def load_source_state(session, session_id: str) -> tuple[Any, Any, str | None, list[dict[str, Any]]]:
    """Read the current sound header and cues once for a caller-local batch."""

    record, plan, active = current_header(session, session_id)
    return record, plan, active, latest_cues(session, plan)


def propose(
    session,
    session_id: str,
    segment_id: str,
    speech_revision_id: str,
    text: str,
    markers: tuple[SoundMarker, ...] | None,
    expected_revision: int | None,
    *,
    source_state: tuple[Any, Any, str | None, list[dict[str, Any]]] | None = None,
) -> SoundProposal:
    record, plan, active, loaded_cues = (
        source_state if source_state is not None else load_source_state(session, session_id)
    )
    if record.id != session_id:
        raise ValueError("Sound proposal source state belongs to another session.")
    if active != speech_revision_id or (plan is not None and plan.speech_plan_revision_id != active):
        raise ValueError("Sound cues need rebase to the active speech revision.")
    revision = plan.revision if plan else 0
    current = loaded_cues
    represented = visible_markers(current, segment_id, text)
    represented_ids = frozenset(marker.cue for marker in represented)
    if markers is None:
        return SoundProposal(revision, plan, active, current, current, represented_ids, {
            "added": [], "updated": [], "removed": [],
        })
    if expected_revision is None:
        raise ValueError("expected_sound_plan_revision is required with speech_xml.")
    assert_revision(plan, expected_revision)
    by_id = {cue["logical_id"]: cue for cue in current}
    updated: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    for marker in markers:
        old = by_id.get(marker.cue)
        if old is not None and marker.cue not in represented_ids:
            raise ValueError("A sound marker cannot edit a cue outside this representable segment.")
        require_variant(session, marker.variant)
        if old is not None:
            previous_marker = marker_for_cue(old, segment_id, text)
            anchor = (
                deepcopy(old["anchor_json"])
                if previous_marker is not None
                and previous_marker.offset == marker.offset
                and previous_marker.placement == marker.placement
                else _anchor_for_marker(marker, segment_id, text).model_dump(exclude_none=True)
            )
            anchor_changed = anchor != old["anchor_json"]
            proposal = deepcopy(old)
            proposal["variant_id"] = marker.variant
            proposal["anchor_json"] = anchor
            if anchor_changed:
                _, _, status = normalize_placement(
                    session,
                    SoundAnchor.model_validate(anchor),
                    SoundPlayback.model_validate(proposal["playback_json"]),
                    record.workflow_kind,
                    active,
                )
                proposal["placement_status"] = status
                # A resolved word placement must be resolved again after movement.
                proposal["anchor_json"] = {
                    key: value for key, value in anchor.items() if not key.startswith("origin_")
                }
            updated.append(proposal)
        else:
            anchor = _anchor_for_marker(marker, segment_id, text)
            anchor_json, playback_json, status = normalize_placement(
                session, anchor, SoundPlayback(), record.workflow_kind, active
            )
            additions.append({
                "logical_id": marker.cue,
                "variant_id": marker.variant,
                "anchor_json": anchor_json,
                "playback_json": playback_json,
                "included": True,
                "placement_status": status,
            })
    candidate = [
        deepcopy(cue) for cue in current if cue["logical_id"] not in represented_ids
    ] + updated + additions
    candidate.sort(key=lambda cue: cue["logical_id"])
    before = {cue["logical_id"]: cue for cue in current}
    after = {cue["logical_id"]: cue for cue in candidate}
    changes = {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "updated": sorted(key for key in before.keys() & after.keys() if before[key] != after[key]),
    }
    return SoundProposal(revision, plan, active, current, candidate, represented_ids, changes)
