"""Application sources domain request builders."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationSourceMethods(ApplicationRequests):
    def delete_output(
        self,
        session_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        """Permanently remove exactly one session output by durable ID."""

        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/outputs/{quote(artifact_id, safe='')}",
            method="DELETE",
        )

    def list_sources(
        self,
        *,
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/sources",
            parameters=({"include_trashed": "true"} if include_trashed else None),
        )

    def attach_existing_source(
        self,
        session_id: str,
        *,
        source_asset_id: str,
        role: str,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/sources",
            method="POST",
            body={
                "source_asset_id": source_asset_id,
                "role": role,
            },
            idempotency_key=idempotency_key,
            if_match_revision=expected_session_revision,
        )

    def list_artifacts(
        self,
        *,
        session_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "limit": max(1, min(int(limit), 100)),
        }
        if session_id:
            parameters["session_id"] = session_id
        return self._request_json("/api/v1/artifacts", parameters=parameters)

    def artifact_context(self, artifact_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/artifacts/{quote(artifact_id, safe='')}/context")

    def initialize_upload(
        self,
        *,
        filename: str,
        size_bytes: int,
        mime_type: str | None,
        sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/uploads/init",
            method="POST",
            body={
                "filename": filename,
                "size_bytes": int(size_bytes),
                "mime_type": mime_type,
                "sha256": sha256,
            },
            idempotency_key=idempotency_key,
        )

    def upload_status(self, upload_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/uploads/{quote(upload_id, safe='')}")

    def upload_chunk(
        self,
        upload_id: str,
        index: int,
        body: bytes,
        *,
        sha256: str,
    ) -> dict[str, Any]:
        return self._request_binary_json(
            f"/api/v1/uploads/{quote(upload_id, safe='')}/chunks/{int(index)}",
            method="PUT",
            body=body,
            content_type="application/octet-stream",
            extra_headers={"X-Chunk-SHA256": sha256},
        )

    def complete_upload(self, upload_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/uploads/{quote(upload_id, safe='')}/complete",
            method="POST",
            body={},
        )

    def initialize_transcription(
        self,
        *,
        filename: str,
        size_bytes: int,
        sha256: str,
        format: str,
        language: str | None,
        engine: str | None,
        model_quantization: str | None,
        compute_backend: str | None,
        idempotency_key: str,
        qwen_asr_model: str | None = None,
        transcription_vocal_isolation: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "filename": filename,
            "size_bytes": int(size_bytes),
            "sha256": sha256,
            "format": format,
        }
        optional = {
            "language": language,
            "engine": engine,
            "model_quantization": model_quantization,
            "compute_backend": compute_backend,
            "qwen_asr_model": qwen_asr_model,
            "transcription_vocal_isolation": transcription_vocal_isolation,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._request_json(
            "/api/v1/transcriptions",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def upload_transcription_chunk(
        self,
        transcription_id: str,
        index: int,
        body: bytes,
    ) -> dict[str, Any]:
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("Transcription chunks may not exceed 8 MiB.")
        return self._request_binary_json(
            f"/api/v1/transcriptions/{quote(transcription_id, safe='')}/chunks/{int(index)}",
            method="PUT",
            body=body,
            content_type="application/octet-stream",
        )

    def start_transcription(
        self,
        transcription_id: str,
        *,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/transcriptions/{quote(transcription_id, safe='')}/start",
            method="POST",
            body={"wait_seconds": max(0, min(int(wait_seconds), 30))},
            request_timeout_seconds=max(self.timeout_seconds, min(int(wait_seconds), 30) + 5.0),
        )

    def get_transcription(
        self,
        transcription_id: str,
        *,
        format: str | None = None,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "wait_seconds": max(0, min(int(wait_seconds), 30)),
        }
        if format is not None:
            parameters["format"] = format
        return self._request_json(
            f"/api/v1/transcriptions/{quote(transcription_id, safe='')}",
            parameters=parameters,
            request_timeout_seconds=max(self.timeout_seconds, min(int(wait_seconds), 30) + 5.0),
        )

    def get_transcription_result(
        self,
        transcription_id: str,
        *,
        format: str = "txt",
        offset: int = 0,
        limit: int = 16_000,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/transcriptions/{quote(transcription_id, safe='')}/result",
            parameters={
                "format": format,
                "offset": max(0, int(offset)),
                "limit": max(1, min(int(limit), 32_768)),
            },
        )

    def cancel_transcription(self, transcription_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/transcriptions/{quote(transcription_id, safe='')}/cancel",
            method="POST",
            body={},
        )

    def delete_transcription(self, transcription_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/transcriptions/{quote(transcription_id, safe='')}",
            method="DELETE",
        )
