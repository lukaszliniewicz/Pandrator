"""Application generation domain request builders."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationGenerationMethods(ApplicationRequests):
    def generation_recast_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call only the four revision-fenced recast and undo endpoints."""
        if action not in {"preview", "recast", "undo_preview", "undo"}:
            raise ValueError("Unknown generation-recast action.")
        values = dict(arguments)
        key = values.pop("idempotency_key", None)
        if action in {"preview", "recast"}:
            identifier = values.pop("session_id")
            path = f"/api/v1/sessions/{quote(str(identifier), safe='')}/generation-recast"
            if action == "preview":
                path += "/preview"
        else:
            identifier = values.pop("generation_run_id")
            path = f"/api/v1/generation-runs/{quote(str(identifier), safe='')}/audio-version"
            path += "/undo-preview" if action == "undo_preview" else "/undo"
        # Null selectors mean changed voices only; [] means an explicit empty selection.
        return self._request_json(
            path, method="POST", body=values, idempotency_key=key,
            read_only_operation=action in {"preview", "undo_preview"},
            maximum_body_bytes=512 * 1024,
        )

    def performance_plan_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Bounded, allowlisted performance API; no arbitrary paths or endpoints."""
        methods = {
            "list": "GET",
            "get": "GET",
            "create": "POST",
            "edit": "PATCH",
            "adopt": "POST",
            "preview": "POST",
            "analyse": "POST",
            "claim": "POST",
            "submit": "POST",
            "renew": "POST",
            "release": "POST",
        }
        if action not in methods:
            raise ValueError("Unknown performance-plan action.")
        values = dict(arguments)
        session_id = str(values.pop("session_id"))
        plan_id = values.pop("plan_id", None)
        batch_id = values.pop("batch_id", None)
        key = values.pop("idempotency_key", None)
        path = f"/api/v1/sessions/{quote(session_id, safe='')}/performance-plans"
        if action not in {"list", "create"}:
            if not plan_id:
                raise ValueError("This action requires a performance plan ID.")
            path += f"/{quote(str(plan_id), safe='')}"
        if action in {"submit", "renew", "release"}:
            if not batch_id:
                raise ValueError("This action requires a performance batch ID.")
            path += f"/batches/{quote(str(batch_id), safe='')}/{action}"
        elif action not in {"list", "get", "create", "edit"}:
            path += f"/{action}"
        if methods[action] == "GET":
            if isinstance(values.get("segment_ids"), list):
                values["segment_ids"] = ",".join(values["segment_ids"])
            if "include_units" in values:
                values["include_units"] = "true" if values["include_units"] else "false"
            return self._request_json(
                path,
                parameters={name: value for name, value in values.items() if value is not None},
            )
        parameters = None
        if action == "create":
            parameters = {name: values.pop(name) for name in ("view", "include_units") if name in values}
            if "include_units" in parameters:
                parameters["include_units"] = "true" if parameters["include_units"] else "false"
        return self._request_json(
            path,
            method=methods[action],
            read_only_operation=action == "preview",
            parameters=parameters,
            body=values,
            idempotency_key=key,
            maximum_body_bytes=512 * 1024,
        )

    def generation_controls_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Bounded character/cast API; no arbitrary paths or endpoints."""

        methods = {"get": "GET", "update": "PUT"}
        if action not in methods:
            raise ValueError("Unknown generation-controls action.")

        values = dict(arguments)
        session_id = values.pop("session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Generation-controls requests require a session ID.")
        path = f"/api/v1/sessions/{quote(session_id, safe='')}/generation-controls"

        if action == "get":
            if values:
                raise ValueError("Getting generation controls accepts only a session ID.")
            return self._request_json(path, method=methods[action])

        key = values.pop("idempotency_key", None)
        # Only strip optional top-level values. Nested nulls, especially
        # cast.narrator=None, are meaningful clear operations for the API.
        body = {name: value for name, value in values.items() if value is not None}
        return self._request_json(
            path,
            method=methods[action],
            body=body,
            idempotency_key=key,
            maximum_body_bytes=512 * 1024,
        )

    def preview_speech_segment(
        self,
        session_id: str,
        *,
        revision_id: str,
        segment_id: str,
        generation_run_id: str | None = None,
        include_request: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "revision_id": revision_id,
            "segment_id": segment_id,
            "include_request": include_request,
        }
        if generation_run_id:
            body["generation_run_id"] = generation_run_id
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-plan/preview",
            method="POST",
            body=body,
            maximum_body_bytes=4096,
        )

    def preview_speech_selection(self, session_id: str, *, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-plan/selection-preview",
            method="POST",
            body=body,
            maximum_body_bytes=512 * 1024,
        )

    def get_speech_selection_context(
        self, session_id: str, *, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-plan/selection-context",
            method="POST",
            body=body,
            maximum_body_bytes=4096,
        )

    def apply_speech_selection(
        self, session_id: str, *, body: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-plan/selection",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=512 * 1024,
        )

    def list_generation_runs(
        self, session_id: str, *, limit: int | None = None, include_repairs: bool | None = None,
        view: str = "full",
        generation_run_id: str | None = None,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {}
        if generation_run_id is not None:
            parameters["generation_run_id"] = generation_run_id
        if view != "full":
            parameters["view"] = view
        if limit is not None:
            parameters["limit"] = limit
        if include_repairs is not None:
            parameters["include_repairs"] = "true" if include_repairs else "false"
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-runs",
            parameters=parameters or None,
        )

    def list_generation_segments(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 50,
        generation_run_id: str | None = None,
        plan_revision_id: str | None = None,
        view: str = "full",
        fields: list[str] | None = None,
        segment_ids: list[str] | None = None,
        end_ordinal: int | None = None,
        around_ordinal: int | None = None,
        source_cue_id: str | None = None,
        radius: int = 2,
        audio_version_side: str = "after",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cursor": max(0, int(cursor)),
            "limit": max(1, min(int(limit), 100)),
        }
        params["audio_version_side"] = audio_version_side
        optional = {
            "generation_run_id": generation_run_id,
            "plan_revision_id": plan_revision_id,
            "end_ordinal": end_ordinal,
            "around_ordinal": around_ordinal,
            "source_cue_id": source_cue_id,
        }
        params.update({key: value for key, value in optional.items() if value is not None})
        if view != "full":
            params["view"] = view
        if fields is not None:
            params["fields"] = ",".join(fields)
        if segment_ids is not None:
            params["segment_ids"] = ",".join(segment_ids)
        if view != "full":
            params["take_history"] = "selected"
        if around_ordinal is not None or source_cue_id is not None:
            params["radius"] = radius
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-segments",
            parameters=params,
        )

    def get_speech_plan_status(self, session_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-plan/status"
        )

    def prepare_speech_plan(
        self,
        session_id: str,
        *,
        expected_revision: int,
        expected_plan_revision_id: str | None,
        source_artifact_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-plan/prepare",
            method="POST",
            body={
                "expected_revision": expected_revision,
                "expected_plan_revision_id": expected_plan_revision_id,
                "source_artifact_id": source_artifact_id,
            },
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def review_speech_plan(
        self,
        session_id: str,
        *,
        revision_id: str,
        content_signature: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-plan/review",
            method="POST",
            body={"revision_id": revision_id, "content_signature": content_signature},
            idempotency_key=idempotency_key,
        )

    def list_speech_plan_revisions(
        self, session_id: str, *, limit: int = 50, before_revision_number: int | None = None
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"limit": limit}
        if before_revision_number is not None:
            parameters["before_revision_number"] = before_revision_number
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-plan/revisions",
            parameters=parameters,
        )

    def revise_generation_plan_topology_batch(
        self,
        session_id: str,
        *,
        expected_revision_id: str,
        operations: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-plan/topology/batch",
            method="POST",
            body={"expected_revision_id": expected_revision_id, "operations": operations},
            if_match_revision=expected_revision_id,
            idempotency_key=idempotency_key,
            request_timeout_seconds=max(self.timeout_seconds, 120.0),
        )

    def adopt_subtitle_source(
        self,
        session_id: str,
        *,
        source_asset_id: str,
        expected_revision: int | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/sources/adopt-subtitles",
            method="POST",
            body={"source_asset_id": source_asset_id},
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def revise_generation_plan_topology(
        self,
        session_id: str,
        *,
        expected_revision_id: str,
        action: str,
        segment_id: str | None = None,
        cursor: int | None = None,
        text_layer: str | None = None,
        left_segment_id: str | None = None,
        right_segment_id: str | None = None,
        target_revision_id: str | None = None,
        segment_ids: list[str] | None = None,
        boundaries: list[int] | None = None,
        max_chars: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "expected_revision_id": expected_revision_id,
            "action": action,
        }
        optional = {
            "segment_id": segment_id,
            "cursor": cursor,
            "text_layer": text_layer,
            "left_segment_id": left_segment_id,
            "right_segment_id": right_segment_id,
            "target_revision_id": target_revision_id,
            "segment_ids": segment_ids,
            "boundaries": boundaries,
            "max_chars": max_chars,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-plan/topology",
            method="POST",
            body=body,
            if_match_revision=expected_revision_id,
            idempotency_key=idempotency_key,
            request_timeout_seconds=max(self.timeout_seconds, 120.0),
        )

    def update_generation_segment(
        self,
        segment_id: str,
        *,
        changes: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/generation-segments/{quote(segment_id, safe='')}",
            method="PATCH",
            body=changes,
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
            request_timeout_seconds=max(self.timeout_seconds, 120.0),
        )

    def update_generation_segments(
        self,
        session_id: str,
        *,
        updates: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-segments",
            method="PATCH",
            body={"updates": updates},
            idempotency_key=idempotency_key,
            request_timeout_seconds=max(self.timeout_seconds, 120.0),
        )

    def select_generation_take(
        self,
        segment_id: str,
        take_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/generation-segments/{quote(segment_id, safe='')}/takes/{quote(take_id, safe='')}/select",
            method="POST",
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def start_generation_run(
        self,
        session_id: str,
        *,
        segment_ids: list[str] | tuple[str, ...] | None = None,
        generation_run_id: str | None = None,
        settings_source: str | None = None,
        settings_source_run_id: str | None = None,
        operation: str = "generate",
        idempotency_key: str,
        speech_plan_revision_id: str | None = None,
        stale_only: bool = False,
        missing_only: bool = False,
        view: str = "full",
        admission: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"operation": operation}
        if segment_ids:
            body["segment_ids"] = list(segment_ids)
        if generation_run_id is not None:
            body["generation_run_id"] = generation_run_id
        if settings_source is not None:
            body["settings_source"] = settings_source
        if settings_source_run_id is not None:
            body["settings_source_run_id"] = settings_source_run_id
        if speech_plan_revision_id is not None:
            body["speech_plan_revision_id"] = speech_plan_revision_id
        if stale_only:
            body["stale_only"] = True
        if missing_only:
            body["missing_only"] = True
        if admission:
            return self._request_json(
                f"/api/v1/sessions/{quote(session_id, safe='')}/generation-admissions",
                method="POST",
                body=body,
                idempotency_key=idempotency_key,
            )
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/generation-runs",
            method="POST",
            body=body,
            parameters={"view": view} if view != "full" else None,
            idempotency_key=idempotency_key,
            request_timeout_seconds=max(self.timeout_seconds, 120.0),
        )

    def create_output_assembly(
        self,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        idempotency_key: str,
        audio_version_side: str = "after",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"audio_version_side": audio_version_side}
        if generation_run_id:
            body["generation_run_id"] = generation_run_id
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/output-assemblies",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )
