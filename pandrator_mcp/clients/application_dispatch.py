"""Application dispatch domain request builders."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationDispatchMethods(ApplicationRequests):
    def create_dispatch_run(
        self,
        session_id: str,
        *,
        kind: str,
        source_artifact_id: str | None,
        source_language: str | None,
        target_language: str | None,
        instructions: str,
        char_limit: int,
        max_segments_per_batch: int,
        no_remove_subtitles: bool,
        correction_style: Literal["publishable", "faithful"] = "publishable",
        context_before: int = 8,
        context_after: int = 2,
        timing_context_mode: str | None = None,
        include_timing_context: bool | None = None,
        substantial_gap_ms: int,
        glossary: dict[str, str],
        execution_mode: str = "serial",
        max_parallel_batches: int = 1,
        context_capsule: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        resolved_timing_context_mode = timing_context_mode
        if resolved_timing_context_mode is None:
            resolved_timing_context_mode = "full" if include_timing_context is not False else "none"
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/dispatch-runs",
            method="POST",
            body={
                "kind": kind,
                "source_artifact_id": source_artifact_id,
                "source_language": source_language,
                "target_language": target_language,
                "instructions": instructions,
                "char_limit": int(char_limit),
                "max_segments_per_batch": int(max_segments_per_batch),
                "no_remove_subtitles": bool(no_remove_subtitles),
                "correction_style": correction_style,
                "context_before": int(context_before),
                "context_after": int(context_after),
                "timing_context_mode": resolved_timing_context_mode,
                "substantial_gap_ms": int(substantial_gap_ms),
                "glossary": glossary,
                "execution_mode": execution_mode,
                "max_parallel_batches": int(max_parallel_batches),
                "context_capsule": dict(context_capsule or {}),
            },
            idempotency_key=idempotency_key,
        )

    def list_dispatch_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/dispatch-runs",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_dispatch_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/dispatch-runs/{quote(run_id, safe='')}")

    def get_dispatch_preview(
        self, run_id: str, *, batch_ordinal: int | None = None, offset: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-runs/{quote(run_id, safe='')}/preview",
            parameters={"batch_ordinal": batch_ordinal, "offset": offset, "limit": limit},
        )

    def terminate_dispatch_run(
        self,
        *,
        run_id: str,
        expected_status: str,
        action: str,
        reason: str,
        idempotency_key: str,
        replacement_run_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-runs/{quote(run_id, safe='')}/terminate",
            method="POST",
            body={
                "expected_status": expected_status,
                "action": action,
                "replacement_run_id": replacement_run_id,
                "reason": reason,
            },
            idempotency_key=idempotency_key,
        )

    def get_workflow_inputs(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/workflow-inputs")

    def select_workflow_input(
        self,
        *,
        session_id: str,
        consumer: str,
        role: str,
        artifact_id: str,
        expected_outcome_revision: int,
        expected_selection_revision: int,
        idempotency_key: str,
        expected_translation_settings_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/workflow-inputs",
            method="PUT",
            body={
                "consumer": consumer,
                "role": role,
                "artifact_id": artifact_id,
                "expected_outcome_revision": expected_outcome_revision,
                "expected_selection_revision": expected_selection_revision,
                "expected_translation_settings_revision": expected_translation_settings_revision,
            },
            idempotency_key=idempotency_key,
        )

    def request_subtitle_evidence(
        self,
        session_id: str,
        *,
        source_artifact_id: str,
        cue_id: int,
        reason: str,
        routes: list[str],
        audio_model_ids: list[str],
        padding_before_ms: int,
        padding_after_ms: int,
        idempotency_key: str,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/subtitle-evidence",
            method="POST",
            body={
                "source_artifact_id": source_artifact_id,
                "cue_id": int(cue_id),
                "reason": reason,
                "routes": list(routes),
                "audio_model_ids": list(audio_model_ids),
                "padding_before_ms": int(padding_before_ms),
                "padding_after_ms": int(padding_after_ms),
                **({"force_refresh": True} if force_refresh else {}),
            },
            idempotency_key=idempotency_key,
        )

    def get_subtitle_evidence(self, evidence_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/subtitle-evidence/{quote(evidence_id, safe='')}")

    def get_subtitle_evidence_routes(
        self, *, language: str | None = None, include_languages: bool = False
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/subtitle-evidence/routes",
            parameters={
                **({"language": language} if language else {}),
                "include_languages": "true" if include_languages else "false",
            },
        )

    def resolve_subtitle_evidence(
        self,
        session_id: str,
        evidence_id: str,
        *,
        action: str,
        candidate_id: str | None,
        text: str | None,
        note: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"action": action, "note": note}
        if candidate_id is not None:
            body["candidate_id"] = candidate_id
        if text is not None:
            body["text"] = text
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/subtitle-evidence/"
            f"{quote(evidence_id, safe='')}/resolve",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def claim_dispatch_batch(
        self,
        run_id: str,
        *,
        lease_seconds: int = 900,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-runs/{quote(run_id, safe='')}/claim",
            method="POST",
            body={"lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def inspect_dispatch_split_boundaries(
        self, batch_id: str, *, lease_token: str, cue_id: int, offset: int = 0, limit: int = 30
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-batches/{quote(batch_id, safe='')}/split-boundaries",
            method="POST",
            body={"lease_token": lease_token, "cue_id": cue_id, "offset": offset, "limit": limit},
        )

    def renew_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-batches/{quote(batch_id, safe='')}/renew",
            method="POST",
            body={
                "lease_token": lease_token,
                "lease_seconds": int(lease_seconds),
            },
            idempotency_key=idempotency_key,
        )

    def release_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/dispatch-batches/{quote(batch_id, safe='')}/release",
            method="POST",
            body={"lease_token": lease_token},
            idempotency_key=idempotency_key,
        )

    def submit_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        result: dict[str, Any] | None = None,
        response_text: str | None = None,
        context_delta: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"lease_token": lease_token}
        if result is not None:
            body["result"] = result
        if response_text is not None:
            body["response_text"] = response_text
        body["context_delta"] = dict(context_delta or {})
        return self._request_json(
            f"/api/v1/dispatch-batches/{quote(batch_id, safe='')}/submit",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            # JSON string escaping can make a valid 512 KiB UTF-8 response
            # larger on the wire while the backend still enforces the exact
            # decoded response limit.
            maximum_body_bytes=4 * 1024 * 1024,
        )

    def create_source_cleaning_dispatch_run(
        self,
        session_id: str,
        *,
        source_artifact_id: str | None,
        instructions: str,
        evidence_limit: int,
        remove_footnotes: bool | None,
        filter_citations: bool | None,
        pdf_ocr_mode: str | None,
        pdf_ocr_language: str | None,
        pdf_ocr_dpi: int | None,
        pdf_remove_toc: bool | None,
        pdf_remove_repeated_marginals: bool | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "source_artifact_id": source_artifact_id,
            "instructions": instructions,
            "evidence_limit": int(evidence_limit),
        }
        optional = {
            "remove_footnotes": remove_footnotes,
            "filter_citations": filter_citations,
            "pdf_ocr_mode": pdf_ocr_mode,
            "pdf_ocr_language": pdf_ocr_language,
            "pdf_ocr_dpi": pdf_ocr_dpi,
            "pdf_remove_toc": pdf_remove_toc,
            "pdf_remove_repeated_marginals": pdf_remove_repeated_marginals,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/source-cleaning-dispatch-runs",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def list_source_cleaning_dispatch_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/source-cleaning-dispatch-runs",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_source_cleaning_dispatch_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/source-cleaning-dispatch-runs/{quote(run_id, safe='')}")

    def claim_source_cleaning_dispatch_batch(
        self,
        run_id: str,
        *,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-runs/{quote(run_id, safe='')}/claim",
            method="POST",
            body={"lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def renew_source_cleaning_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/renew",
            method="POST",
            body={
                "lease_token": lease_token,
                "lease_seconds": int(lease_seconds),
            },
            idempotency_key=idempotency_key,
        )

    def release_source_cleaning_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/release",
            method="POST",
            body={"lease_token": lease_token},
            idempotency_key=idempotency_key,
        )

    def inspect_source_cleaning_dispatch_extraction(
        self,
        batch_id: str,
        *,
        lease_token: str,
        action: str,
        arguments: dict[str, Any],
        view: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/inspect",
            method="POST",
            body={
                "lease_token": lease_token,
                "action": action,
                "arguments": arguments,
                "view": view,
            },
            idempotency_key=idempotency_key,
            maximum_body_bytes=16 * 1024 * 1024,
        )

    def submit_source_cleaning_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        result: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/source-cleaning-dispatch-batches/{quote(batch_id, safe='')}/submit",
            method="POST",
            body={"lease_token": lease_token, "result": result},
            idempotency_key=idempotency_key,
            maximum_body_bytes=16 * 1024 * 1024,
        )

    def create_speech_optimization_dispatch_run(
        self,
        session_id: str,
        *,
        source_artifact_id: str | None,
        language: str | None,
        voice_language: str | None,
        tts_service: str | None,
        instructions: str,
        char_limit: int,
        max_units_per_batch: int,
        context_before: int,
        context_after: int,
        include_timing: bool,
        annotation_mode: str = "off",
        annotation_only: bool = False,
        execution_mode: str = "serial",
        max_parallel_batches: int = 1,
        context_capsule: dict[str, Any] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instructions": instructions,
            "char_limit": int(char_limit),
            "max_units_per_batch": int(max_units_per_batch),
            "context_before": int(context_before),
            "context_after": int(context_after),
            "include_timing": bool(include_timing),
            "execution_mode": execution_mode,
            "max_parallel_batches": int(max_parallel_batches),
            "context_capsule": dict(context_capsule or {}),
        }
        if annotation_mode != "off":
            body["annotation_mode"] = annotation_mode
        if annotation_only:
            body["annotation_only"] = True
        optional = {
            "source_artifact_id": source_artifact_id,
            "language": language,
            "voice_language": voice_language,
            "tts_service": tts_service,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-optimization-dispatch-runs",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def list_speech_optimization_dispatch_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/speech-optimization-dispatch-runs",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_speech_optimization_dispatch_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-runs/{quote(run_id, safe='')}"
        )

    def claim_speech_optimization_dispatch_batch(
        self,
        run_id: str,
        *,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-runs/{quote(run_id, safe='')}/claim",
            method="POST",
            body={"lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def renew_speech_optimization_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-batches/{quote(batch_id, safe='')}/renew",
            method="POST",
            body={
                "lease_token": lease_token,
                "lease_seconds": int(lease_seconds),
            },
            idempotency_key=idempotency_key,
        )

    def release_speech_optimization_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-batches/{quote(batch_id, safe='')}/release",
            method="POST",
            body={"lease_token": lease_token},
            idempotency_key=idempotency_key,
        )

    def submit_speech_optimization_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        result: dict[str, Any],
        context_delta: dict[str, Any] | None = None,
        character_proposals: list[dict[str, Any]] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = {
            "lease_token": lease_token,
            "result": result,
            "context_delta": dict(context_delta or {}),
        }
        if character_proposals:
            body["character_proposals"] = list(character_proposals)
        return self._request_json(
            f"/api/v1/speech-optimization-dispatch-batches/{quote(batch_id, safe='')}/submit",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=4 * 1024 * 1024,
        )

    def create_media_edit_dispatch_run(
        self,
        session_id: str,
        *,
        revision: int,
        instructions: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit-dispatch-runs",
            method="POST",
            body={"revision": int(revision), "instructions": instructions},
            idempotency_key=idempotency_key,
        )

    def list_media_edit_dispatch_runs(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/media-edit-dispatch-runs",
            parameters={"limit": max(1, min(int(limit), 100))},
        )

    def get_media_edit_dispatch_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/media-edit-dispatch-runs/{quote(run_id, safe='')}")

    def claim_media_edit_dispatch_batch(
        self,
        run_id: str,
        *,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/media-edit-dispatch-runs/{quote(run_id, safe='')}/claim",
            method="POST",
            body={"lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def renew_media_edit_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/media-edit-dispatch-batches/{quote(batch_id, safe='')}/renew",
            method="POST",
            body={"lease_token": lease_token, "lease_seconds": int(lease_seconds)},
            idempotency_key=idempotency_key,
        )

    def release_media_edit_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/media-edit-dispatch-batches/{quote(batch_id, safe='')}/release",
            method="POST",
            body={"lease_token": lease_token},
            idempotency_key=idempotency_key,
        )

    def submit_media_edit_dispatch_batch(
        self,
        batch_id: str,
        *,
        lease_token: str,
        result: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/media-edit-dispatch-batches/{quote(batch_id, safe='')}/submit",
            method="POST",
            body={"lease_token": lease_token, "result": result},
            idempotency_key=idempotency_key,
            maximum_body_bytes=4 * 1024 * 1024,
        )
