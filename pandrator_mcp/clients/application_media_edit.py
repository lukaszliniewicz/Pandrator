"""Application media edit domain request builders."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationMediaEditMethods(ApplicationRequests):
    def get_media_edit(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit")

    def list_media_edit_cuts(
        self,
        session_id: str,
        *,
        revision: int | None = None,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {}
        if revision is not None:
            parameters["revision"] = int(revision)
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit/cuts",
            parameters=parameters,
        )

    def inspect_media_edit_boundary(
        self,
        session_id: str,
        *,
        cut_index: int,
        edge: str,
        revision: int | None = None,
        context_ms: int = 5_000,
        cue_limit: int = 40,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "cut_index": int(cut_index),
            "edge": edge,
            "context_ms": int(context_ms),
            "cue_limit": int(cue_limit),
        }
        if revision is not None:
            parameters["revision"] = int(revision)
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit/cuts",
            parameters=parameters,
        )

    def refine_media_edit_boundary(
        self,
        session_id: str,
        *,
        expected_revision: int,
        cut_index: int,
        edge: str,
        idempotency_key: str,
        position_ms: int | None = None,
        delta_ms: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "cut_index": int(cut_index),
            "edge": edge,
        }
        if position_ms is not None:
            body["position_ms"] = int(position_ms)
        if delta_ms is not None:
            body["delta_ms"] = int(delta_ms)
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit/boundary",
            method="PATCH",
            body=body,
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def prepare_media_edit(
        self,
        session_id: str,
        *,
        force: bool = False,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit/prepare",
            method="POST",
            body={"force": bool(force)},
            idempotency_key=idempotency_key,
        )

    def update_media_edit(
        self,
        session_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        keep_ranges: list[dict[str, Any]],
        instructions: str | None = None,
        reviewed: bool | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"keep_ranges": list(keep_ranges)}
        if instructions is not None:
            body["instructions"] = instructions
        if reviewed is not None:
            body["reviewed"] = reviewed
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit",
            method="PUT",
            body=body,
            if_match_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def propose_media_edit(
        self,
        session_id: str,
        *,
        revision: int,
        instructions: str,
        model: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "revision": int(revision),
            "instructions": instructions,
        }
        if model is not None:
            body["model"] = model
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit/propose",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def render_media_edit(
        self,
        session_id: str,
        *,
        revision: int,
        idempotency_key: str,
        subtitles_only: bool = False,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit/render",
            method="POST",
            body={
                "revision": int(revision),
                **({"subtitles_only": True} if subtitles_only else {}),
            },
            idempotency_key=idempotency_key,
        )
