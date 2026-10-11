"""Application speech-editor and comparison-export request builders."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationEditorMethods(ApplicationRequests):
    def preview_speech_selection_batch(
        self, session_id: str, *, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-plan/selection-batch-preview",
            method="POST",
            read_only_operation=True,
            body=body,
            maximum_body_bytes=512 * 1024,
        )

    def apply_speech_selection_batch(
        self,
        session_id: str,
        *,
        body: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-plan/selection-batch",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=512 * 1024,
        )

    def create_comparison_export(
        self,
        session_id: str,
        *,
        body: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/comparison-exports",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=64 * 1024,
        )


__all__ = ["ApplicationEditorMethods"]
