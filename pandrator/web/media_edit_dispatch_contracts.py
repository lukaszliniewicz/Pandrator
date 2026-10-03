"""Pure validation and normalization for passive media-edit results."""

from __future__ import annotations

from typing import Any

from .dispatch import DispatchError


def normalize_media_edit_dispatch_result(
    input_packet: dict[str, Any],
    result: object,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise DispatchError(
            "invalid_model_response", "Media-edit result must be an object.", 422
        )
    if result.get("kind") != "media_edit":
        raise DispatchError(
            "result_kind_mismatch", "This batch requires a media_edit result.", 422
        )
    cuts = result.get("cuts")
    if not isinstance(cuts, list):
        raise DispatchError(
            "invalid_model_response", "Media-edit result cuts must be a list.", 422
        )
    if len(cuts) > 1000:
        raise DispatchError(
            "invalid_model_response",
            "Media-edit results may contain at most 1000 cuts.",
            422,
        )
    valid_cues = list(input_packet.get("cues") or [])
    cue_positions = {
        str(cue.get("id")): index for index, cue in enumerate(valid_cues)
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in cuts:
        if not isinstance(item, dict):
            raise DispatchError(
                "invalid_model_response",
                "Every media-edit cut must be an object.",
                422,
            )
        start_at_media_start = bool(item.get("start_at_media_start", False))
        end_at_media_end = bool(item.get("end_at_media_end", False))
        start_id = item.get("start_cue_id")
        end_id = item.get("end_cue_id")
        reason = item.get("reason")
        if (start_id is not None) == start_at_media_start:
            raise DispatchError(
                "invalid_model_response",
                "Exactly one start_cue_id or start_at_media_start=true is required.",
                422,
            )
        if (end_id is not None) == end_at_media_end:
            raise DispatchError(
                "invalid_model_response",
                "Exactly one end_cue_id or end_at_media_end=true is required.",
                422,
            )
        if (not start_at_media_start and not isinstance(start_id, str)) or (
            not end_at_media_end and not isinstance(end_id, str)
        ):
            raise DispatchError(
                "invalid_model_response", "Cut cue IDs must be strings.", 422
            )
        if not isinstance(reason, str):
            raise DispatchError(
                "invalid_model_response", "Cut reason must be a string.", 422
            )
        start_id = start_id.strip() if isinstance(start_id, str) else ""
        end_id = end_id.strip() if isinstance(end_id, str) else ""
        reason = reason.strip()
        if not start_at_media_start and start_id not in cue_positions:
            raise DispatchError(
                "invalid_model_response", "Cut references an unknown cue ID.", 422
            )
        if not end_at_media_end and end_id not in cue_positions:
            raise DispatchError(
                "invalid_model_response", "Cut references an unknown cue ID.", 422
            )
        start_position = -1 if start_at_media_start else cue_positions[start_id]
        end_position = (
            len(valid_cues) if end_at_media_end else cue_positions[end_id]
        )
        if start_position > end_position:
            raise DispatchError(
                "invalid_model_response", "Cut cue IDs are out of order.", 422
            )
        if not reason:
            raise DispatchError(
                "invalid_model_response", "Cut reasons must not be empty.", 422
            )
        if len(reason) > 500:
            raise DispatchError(
                "invalid_model_response",
                "Cut reasons must be at most 500 characters.",
                422,
            )
        pair = (
            "__media_start__" if start_at_media_start else start_id,
            "__media_end__" if end_at_media_end else end_id,
        )
        if pair in seen:
            raise DispatchError(
                "invalid_model_response", "Cut cue-ID pairs must be unique.", 422
            )
        seen.add(pair)
        normalized.append(
            {
                "start_cue_id": None if start_at_media_start else start_id,
                "start_at_media_start": start_at_media_start,
                "end_cue_id": None if end_at_media_end else end_id,
                "end_at_media_end": end_at_media_end,
                "reason": reason,
            }
        )
    return {"kind": "media_edit", "cuts": normalized}
