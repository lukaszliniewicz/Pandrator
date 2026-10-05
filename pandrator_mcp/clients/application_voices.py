"""Application voices domain request builders."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationVoiceMethods(ApplicationRequests):
    def get_voice_setup(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/voice-setup")

    def configure_voice_setup(
        self,
        session_id: str,
        *,
        expected_revision: str,
        mode: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/voice-setup",
            method="PATCH",
            body={"expected_revision": expected_revision, "mode": mode},
            idempotency_key=idempotency_key,
            maximum_body_bytes=4096,
        )

    def voice_metadata_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Bounded managed-voice metadata API; no arbitrary paths or endpoints."""

        if action != "update":
            raise ValueError("Unknown voice-metadata action.")

        values = dict(arguments)
        voice_id = values.pop("voice_id", None)
        expected_revision = values.pop("expected_revision", None)
        changes = values.pop("changes", None)
        if values:
            raise ValueError("Voice metadata requests contain unsupported fields.")
        if not isinstance(voice_id, str) or not voice_id:
            raise ValueError("Voice metadata requests require a voice ID.")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("Voice metadata requests require a positive revision.")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Voice metadata requests require at least one change.")
        allowed_fields = {"name", "language", "description", "voice_category", "profile"}
        if set(changes) - allowed_fields:
            raise ValueError("Voice metadata requests contain unsupported changes.")
        try:
            encoded_changes = json.dumps(
                changes,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("Voice metadata changes must be finite JSON.") from error
        maximum_body_bytes = 64 * 1024 if "profile" in changes else 32 * 1024
        if len(encoded_changes) > maximum_body_bytes:
            raise ValueError(
                f"Voice metadata requests may not exceed {maximum_body_bytes // 1024} KiB."
            )
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}",
            method="PATCH",
            body=dict(changes),
            if_match_revision=expected_revision,
            maximum_body_bytes=maximum_body_bytes,
        )

    def voice_catalog(self, **filters: Any) -> dict[str, Any]:
        """Read the normalized voice catalog through its fixed query route."""

        allowed = {
            "query",
            "language",
            "accent",
            "voice_category",
            "pitch",
            "perceived_age",
            "texture",
            "delivery_preset",
            "tag",
            "use_case",
            "collection_id",
            "kind",
            "origin",
            "service_id",
            "model",
            "ready_only",
            "reviewed_only",
            "sort",
            "limit",
            "cursor",
        }
        if set(filters) - allowed:
            raise ValueError("Voice catalog filters contain unsupported fields.")
        parameters: dict[str, Any] = {
            key: value for key, value in filters.items() if value is not None and value != ""
        }
        if "limit" not in parameters:
            parameters["limit"] = 30
        for key in ("ready_only", "reviewed_only"):
            if key in parameters:
                parameters[key] = "true" if bool(parameters[key]) else "false"
        return self._request_json("/api/v1/voice-catalog", parameters=parameters)

    def voice_catalog_capabilities(
        self,
        *,
        service_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        parameters = {
            key: value
            for key, value in {"service_id": service_id, "model": model}.items()
            if value is not None and value != ""
        }
        return self._request_json(
            "/api/v1/voice-catalog/capabilities",
            parameters=parameters or None,
        )

    def create_voice(
        self,
        *,
        name: str,
        language: str | None,
        description: str | None,
        voice_category: str,
        profile: dict[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "voice_category": voice_category,
        }
        for key, value in {
            "language": language,
            "description": description,
            "profile": profile,
        }.items():
            if value is not None:
                body[key] = value
        return self._request_json(
            "/api/v1/voices",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=64 * 1024,
        )

    def get_voice_samples(self, voice_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/voices/{quote(voice_id, safe='')}/samples")

    def promote_voice_design(
        self,
        voice_id: str,
        *,
        artifact_id: str,
        transcript: str,
        language: str | None,
        expected_voice_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "artifact_id": artifact_id,
            "transcript": transcript,
            "expected_voice_revision": expected_voice_revision,
        }
        if language is not None:
            body["language"] = language
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/from-preview",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def import_voice_reference(
        self,
        voice_id: str,
        *,
        artifact_id: str,
        transcript: str | None,
        language: str | None,
        transcript_reviewed: bool,
        expected_voice_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "artifact_id": artifact_id,
            "transcript_reviewed": bool(transcript_reviewed),
            "expected_voice_revision": expected_voice_revision,
        }
        for key, value in {"transcript": transcript, "language": language}.items():
            if value is not None:
                body[key] = value
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/from-artifact",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=64 * 1024,
        )

    def transcribe_voice_sample(
        self,
        voice_id: str,
        sample_id: str,
        *,
        settings: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/"
            f"{quote(sample_id, safe='')}/transcribe",
            method="POST",
            body=settings,
            idempotency_key=idempotency_key,
            maximum_body_bytes=32 * 1024,
        )

    def review_voice_transcript(
        self,
        voice_id: str,
        sample_id: str,
        *,
        transcript: str,
        language: str | None,
        expected_voice_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "transcript": transcript,
            "expected_voice_revision": expected_voice_revision,
        }
        if language is not None:
            body["language"] = language
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/"
            f"{quote(sample_id, safe='')}/transcript",
            method="PATCH",
            body=body,
            idempotency_key=idempotency_key,
            if_match_revision=expected_voice_revision,
            maximum_body_bytes=32 * 1024,
        )

    def publish_voice(
        self,
        voice_id: str,
        service_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/providers/{quote(service_id, safe='')}",
            method="POST",
            body={},
            idempotency_key=idempotency_key,
            if_match_revision=expected_revision,
        )

    def audition_voice(
        self,
        *,
        service_id: str,
        text: str,
        model: str,
        voice: str,
        language: str,
        generation_prompt: str | None,
        seed: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "text": text,
            "model": model,
            "voice": voice,
            "language": language,
        }
        if generation_prompt is not None:
            body["generation_prompt"] = generation_prompt
        if seed is not None:
            body["seed"] = seed
        return self._request_json(
            f"/api/v1/services/tts/{quote(service_id, safe='')}/preview",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=64 * 1024,
        )

    def list_voice_collections(self) -> dict[str, Any]:
        return self._request_json("/api/v1/voice-collections")

    def create_voice_collection(
        self,
        *,
        name: str,
        description: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        return self._request_json(
            "/api/v1/voice-collections",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=32 * 1024,
        )

    def update_voice_collection(
        self,
        collection_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
        include_description: bool = False,
        add_members: list[dict[str, Any]] | None = None,
        remove_members: list[dict[str, Any]] | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"expected_revision": expected_revision}
        if name is not None:
            body["name"] = name
        if include_description:
            body["description"] = description
        if add_members:
            body["add_members"] = list(add_members)
        if remove_members:
            body["remove_members"] = list(remove_members)
        return self._request_json(
            f"/api/v1/voice-collections/{quote(collection_id, safe='')}",
            method="PATCH",
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=64 * 1024,
        )

    def update_catalog_voice_metadata(
        self,
        *,
        reference: dict[str, Any],
        expected_revision: int,
        changes: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/voice-catalog/metadata",
            method="PATCH",
            body={
                "reference": reference,
                "expected_revision": expected_revision,
                "changes": changes,
            },
            idempotency_key=idempotency_key,
            maximum_body_bytes=64 * 1024,
        )

    def tts_catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/services/tts",
            parameters={"refresh": "true"} if refresh else None,
        )

    def audio_cpp_catalogue(
        self,
        *,
        category: str = "",
        family: str = "",
        query: str = "",
        language: str = "",
        capability: str = "",
        commercial_use: str = "",
        recommended_only: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "recommended_only": "true" if recommended_only else "false",
        }
        parameters.update(
            {
                key: value
                for key, value in {
                    "category": category,
                    "family": family,
                    "query": query,
                    "language": language,
                    "capability": capability,
                    "commercial_use": commercial_use,
                }.items()
                if value != ""
            }
        )
        return self._request_json(
            "/api/v1/services/audio-cpp/catalogue",
            parameters=parameters,
        )

    def list_providers(self) -> dict[str, Any]:
        return self._request_json("/api/v1/providers")

    def list_voices(self) -> dict[str, Any]:
        return self._request_json("/api/v1/voices")
