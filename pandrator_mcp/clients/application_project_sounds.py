"""Application HTTP bindings for project sound-plan cues and jobs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests
from .application_sound_validation import (
    bounded_page,
    identifier,
    revision,
)
from .application_sound_validation import (
    idempotency_key as _idempotency_key,
)

_PROJECT_CUE_UPDATE_FIELDS = frozenset({"variant_id", "anchor", "playback", "included"})


class ApplicationProjectSoundMethods(ApplicationRequests):
    """Fixed-route client methods for project sound plans and their jobs."""

    def get_project_sound_plan(
        self,
        session_id: str,
        *,
        segment_ids: list[str] | None = None,
        cursor: str | None = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        parameters: list[tuple[str, Any]] = [("limit", bounded_page(limit))]
        if segment_ids is not None:
            if len(segment_ids) > 100 or any(
                not isinstance(value, str) or not value or len(value) > 36 for value in segment_ids
            ):
                raise ValueError("segment_ids must contain at most 100 valid IDs.")
            parameters.extend(("segment_id", value) for value in segment_ids)
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 36:
                raise ValueError("cursor must be a sound-plan cue ID.")
            parameters.append(("cursor", cursor))
        return self._request_json(
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}/sound-plan",
            parameters=parameters,
        )

    def get_project_sound_mix_status(
        self,
        session_id: str,
        *,
        base_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        path = (
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}"
            "/sound-plan/mix"
        )
        if base_artifact_id is None:
            return self._request_json(path)
        return self._request_json(
            path,
            parameters={"base_artifact_id": identifier(base_artifact_id, "base_artifact_id")},
        )

    def render_project_sound_plan(
        self,
        session_id: str,
        *,
        expected_revision: int,
        base_artifact_id: str,
        timeline_artifact_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "expected_revision": revision(expected_revision),
            "base_artifact_id": identifier(base_artifact_id, "base_artifact_id"),
        }
        if timeline_artifact_id is not None:
            body["timeline_artifact_id"] = identifier(timeline_artifact_id, "timeline_artifact_id")
        return self._request_json(
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}/sound-plan/render",
            method="POST",
            body=body,
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=16 * 1024,
        )

    def resolve_project_sound_plan(
        self,
        session_id: str,
        *,
        expected_revision: int,
        cue_ids: list[str] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"expected_revision": revision(expected_revision, minimum=1)}
        if cue_ids is not None:
            if not 1 <= len(cue_ids) <= 20 or any(
                not isinstance(value, str) or not value or len(value) > 36 for value in cue_ids
            ):
                raise ValueError("cue_ids must contain 1-20 valid cue IDs when provided.")
            body["cue_ids"] = cue_ids
        return self._request_json(
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}/sound-plan/resolve",
            method="POST",
            body=body,
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=8 * 1024,
        )

    def add_project_sound_cue(
        self,
        session_id: str,
        *,
        expected_revision: int,
        variant_id: str,
        anchor: dict[str, Any],
        playback: dict[str, Any] | None = None,
        included: bool | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "expected_revision": revision(expected_revision),
            "variant_id": identifier(variant_id, "variant_id"),
            "anchor": self._validate_anchor(anchor),
        }
        if playback is not None:
            body["playback"] = self._validate_playback(playback)
        if included is not None:
            if type(included) is not bool:
                raise ValueError("included must be a boolean.")
            body["included"] = included
        return self._request_json(
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}/sound-plan/cues",
            method="POST",
            body=body,
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=32 * 1024,
        )

    def update_project_sound_cue(
        self,
        session_id: str,
        logical_cue_id: str,
        *,
        expected_revision: int,
        changes: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(changes, Mapping) or not changes:
            raise ValueError("Cue updates require at least one change.")
        if set(changes) - _PROJECT_CUE_UPDATE_FIELDS:
            raise ValueError("Cue updates contain unsupported fields.")
        body: dict[str, Any] = {"expected_revision": revision(expected_revision, minimum=1)}
        for field, value in changes.items():
            if value is None:
                raise ValueError(f"{field} must be non-null when provided.")
            if field == "variant_id":
                value = identifier(value, field)
            elif field == "anchor":
                value = self._validate_anchor(value)
            elif field == "playback":
                value = self._validate_playback(value)
            elif field == "included" and type(value) is not bool:
                raise ValueError("included must be a boolean.")
            body[field] = value
        return self._request_json(
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}/sound-plan/cues/{quote(identifier(logical_cue_id, 'logical_cue_id'), safe='')}",
            method="PATCH",
            body=body,
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=32 * 1024,
        )

    def remove_project_sound_cue(
        self,
        session_id: str,
        logical_cue_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}/sound-plan/cues/{quote(identifier(logical_cue_id, 'logical_cue_id'), safe='')}",
            method="DELETE",
            idempotency_key=_idempotency_key(idempotency_key),
            if_match_revision=revision(expected_revision, minimum=1),
        )

    def restore_project_sound_plan(
        self,
        session_id: str,
        *,
        expected_revision: int,
        source_plan_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(identifier(session_id, 'session_id'), safe='')}/sound-plan/restore",
            method="POST",
            body={
                "expected_revision": revision(expected_revision),
                "source_plan_id": identifier(source_plan_id, "source_plan_id"),
            },
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=8 * 1024,
        )

    @staticmethod
    def _validate_anchor(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("anchor must be an object.")
        kind = value.get("kind")
        if not isinstance(kind, str):
            raise ValueError("Unsupported sound anchor kind.")
        required_by_kind = {
            "before_segment": {"kind", "segment_id"},
            "after_segment": {"kind", "segment_id"},
            "source_time": {"kind", "time_seconds"},
            "after_word": {"kind", "segment_id", "quote", "occurrence"},
            "next_pause": {"kind", "segment_id", "quote", "occurrence"},
        }
        required = required_by_kind.get(kind)
        if required is None:
            raise ValueError("Unsupported sound anchor kind.")
        allowed = set(required)
        if kind in {"after_word", "next_pause"}:
            allowed.update({"start", "end"})
        if set(value) - allowed or not required <= set(value):
            raise ValueError("anchor contains unsupported or missing fields for its kind.")
        if kind in {"before_segment", "after_segment", "after_word", "next_pause"}:
            segment_id = value.get("segment_id")
            if not isinstance(segment_id, str):
                raise ValueError("segment_id must be a non-empty string.")
            identifier(segment_id, "segment_id")
        if kind == "source_time":
            time_seconds = value.get("time_seconds")
            if (
                isinstance(time_seconds, bool)
                or not isinstance(time_seconds, (int, float))
                or not math.isfinite(time_seconds)
                or time_seconds < 0
            ):
                raise ValueError("time_seconds must be a finite non-negative number.")
        if kind in {"after_word", "next_pause"}:
            quote_text = value.get("quote")
            occurrence = value.get("occurrence")
            if not isinstance(quote_text, str) or not quote_text.strip() or len(quote_text) > 500:
                raise ValueError("quote must contain 1-500 non-whitespace characters.")
            if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1:
                raise ValueError("occurrence must be a positive integer.")
            has_start, has_end = "start" in value, "end" in value
            if has_start != has_end:
                raise ValueError("start and end codepoints must be supplied together.")
            if has_start:
                start, end = value["start"], value["end"]
                if (
                    isinstance(start, bool)
                    or not isinstance(start, int)
                    or isinstance(end, bool)
                    or not isinstance(end, int)
                    or start < 0
                    or end <= start
                ):
                    raise ValueError("start and end must be non-negative ordered codepoints.")
        return dict(value)

    @staticmethod
    def _validate_playback(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("playback must be an object.")
        allowed = {
            "mode",
            "offset_ms",
            "gain_db",
            "trim_start_seconds",
            "trim_end_seconds",
            "fade_in_ms",
            "fade_out_ms",
        }
        if set(value) - allowed:
            raise ValueError("playback contains unsupported fields.")
        mode = value.get("mode")
        if mode is not None and mode not in {"insert", "overlay"}:
            raise ValueError("playback mode must be insert or overlay.")
        offset = value.get("offset_ms", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or not -10000 <= offset <= 10000:
            raise ValueError("offset_ms must be an integer from -10000 to 10000.")
        if mode == "insert" and offset < 0:
            raise ValueError("insert offset_ms must be nonnegative.")
        gain = value.get("gain_db", 0.0)
        if (
            isinstance(gain, bool)
            or not isinstance(gain, (int, float))
            or not math.isfinite(gain)
            or not -60 <= gain <= 12
        ):
            raise ValueError("gain_db must be a finite number from -60 to 12.")
        trim_start = value.get("trim_start_seconds", 0.0)
        if (
            isinstance(trim_start, bool)
            or not isinstance(trim_start, (int, float))
            or not math.isfinite(trim_start)
            or trim_start < 0
        ):
            raise ValueError("trim_start_seconds must be a finite non-negative number.")
        trim_end = value.get("trim_end_seconds")
        if trim_end is not None and (
            isinstance(trim_end, bool)
            or not isinstance(trim_end, (int, float))
            or not math.isfinite(trim_end)
            or trim_end <= trim_start
        ):
            raise ValueError("trim_end_seconds must be finite and greater than trim_start_seconds.")
        for field in ("fade_in_ms", "fade_out_ms"):
            fade = value.get(field, 5)
            if isinstance(fade, bool) or not isinstance(fade, int) or not 0 <= fade <= 1000:
                raise ValueError(f"{field} must be an integer from 0 to 1000.")
        return dict(value)


__all__ = ["ApplicationProjectSoundMethods"]
