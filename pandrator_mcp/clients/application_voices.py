"""Application voices domain request builders."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote

from ..errors import PandratorMcpError
from .application_requests import ApplicationRequests


def _workspace_parameters(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, str):
        raise ValueError("workspace_id must be a nonblank string of at most 80 characters.")
    workspace_id = value.strip()
    if not workspace_id or len(workspace_id) > 80:
        raise ValueError("workspace_id must be a nonblank string of at most 80 characters.")
    return {"workspace_id": workspace_id}


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
        workspace_parameters = _workspace_parameters(values.pop("workspace_id", None))
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
        request_kwargs: dict[str, Any] = {
            "method": "PATCH",
            "body": dict(changes),
            "if_match_revision": expected_revision,
            "maximum_body_bytes": maximum_body_bytes,
        }
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}",
            **request_kwargs,
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
            "workspace_id",
            "session_id",
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
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        parameters = {
            key: value
            for key, value in {
                "service_id": service_id,
                "model": model,
                "workspace_id": workspace_id,
                "session_id": session_id,
            }.items()
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
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_parameters = _workspace_parameters(workspace_id)
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
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": body,
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 64 * 1024,
        }
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json("/api/v1/voices", **request_kwargs)

    def design_google_voice(
        self,
        *,
        service_id: str,
        model: str,
        name: str,
        prompt: str,
        language: str,
        voice_category: str,
        idempotency_key: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_parameters = _workspace_parameters(workspace_id)
        body = {
            "service_id": service_id,
            "model": model,
            "name": name,
            "prompt": prompt,
            "language": language,
            "voice_category": voice_category,
        }
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": body,
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 16 * 1024,
        }
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            "/api/v1/voices/design/google",
            **request_kwargs,
        )

    def get_voice_samples(
        self,
        voice_id: str,
        *,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        path = f"/api/v1/voices/{quote(voice_id, safe='')}/samples"
        workspace_parameters = _workspace_parameters(workspace_id)
        if not workspace_parameters:
            return self._request_json(path)
        return self._request_json(path, parameters=workspace_parameters)

    def promote_voice_design(
        self,
        voice_id: str,
        *,
        artifact_id: str,
        transcript: str,
        language: str | None,
        expected_voice_revision: int,
        idempotency_key: str,
        recipe_signature: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "artifact_id": artifact_id,
            "transcript": transcript,
            "expected_voice_revision": expected_voice_revision,
        }
        if language is not None:
            body["language"] = language
        if recipe_signature is not None:
            body["recipe_signature"] = recipe_signature
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": body,
            "idempotency_key": idempotency_key,
        }
        workspace_parameters = _workspace_parameters(workspace_id)
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/from-preview",
            **request_kwargs,
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
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "artifact_id": artifact_id,
            "transcript_reviewed": bool(transcript_reviewed),
            "expected_voice_revision": expected_voice_revision,
        }
        for key, value in {"transcript": transcript, "language": language}.items():
            if value is not None:
                body[key] = value
        crop_start = 0.0 if start_seconds is None else start_seconds
        if start_seconds is not None and (
            isinstance(start_seconds, bool)
            or not isinstance(start_seconds, (int, float))
            or not math.isfinite(start_seconds)
            or start_seconds < 0
        ):
            raise ValueError("start_seconds must be a finite non-negative number.")
        if end_seconds is not None and (
            isinstance(end_seconds, bool)
            or not isinstance(end_seconds, (int, float))
            or not math.isfinite(end_seconds)
            or end_seconds <= crop_start
        ):
            raise ValueError("end_seconds must be finite and greater than start_seconds.")
        if start_seconds is not None:
            body["start_seconds"] = start_seconds
        if end_seconds is not None:
            body["end_seconds"] = end_seconds
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": body,
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 64 * 1024,
        }
        workspace_parameters = _workspace_parameters(workspace_id)
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/from-artifact",
            **request_kwargs,
        )

    def transcribe_voice_sample(
        self,
        voice_id: str,
        sample_id: str,
        *,
        settings: dict[str, Any],
        idempotency_key: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": settings,
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 32 * 1024,
        }
        workspace_parameters = _workspace_parameters(workspace_id)
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/"
            f"{quote(sample_id, safe='')}/transcribe",
            **request_kwargs,
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
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "transcript": transcript,
            "expected_voice_revision": expected_voice_revision,
        }
        if language is not None:
            body["language"] = language
        request_kwargs: dict[str, Any] = {
            "method": "PATCH",
            "body": body,
            "idempotency_key": idempotency_key,
            "if_match_revision": expected_voice_revision,
            "maximum_body_bytes": 32 * 1024,
        }
        workspace_parameters = _workspace_parameters(workspace_id)
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/samples/"
            f"{quote(sample_id, safe='')}/transcript",
            **request_kwargs,
        )

    def publish_voice(
        self,
        voice_id: str,
        service_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        sample_id: str | None = None,
        sample_sha256: str | None = None,
        recipe_signature: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_parameters = _workspace_parameters(workspace_id)
        body = {}
        if sample_id is not None:
            body["sample_id"] = sample_id
        if sample_sha256 is not None:
            body["sample_sha256"] = sample_sha256
        if recipe_signature is not None:
            body["recipe_signature"] = recipe_signature
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": body,
            "idempotency_key": idempotency_key,
            "if_match_revision": expected_revision,
        }
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/providers/{quote(service_id, safe='')}",
            **request_kwargs,
        )

    def unpublish_voice(
        self,
        voice_id: str,
        service_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_parameters = _workspace_parameters(workspace_id)
        request_kwargs: dict[str, Any] = {
            "method": "DELETE",
            "idempotency_key": idempotency_key,
            "if_match_revision": expected_revision,
        }
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/voices/{quote(voice_id, safe='')}/providers/{quote(service_id, safe='')}",
            **request_kwargs,
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
        workspace_id: str | None = None,
        voice_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_parameters = _workspace_parameters(workspace_id)
        body: dict[str, Any] = {
            "text": text,
            "model": model,
            "voice": voice,
            "language": language,
        }
        if voice_id is not None:
            body["voice_id"] = voice_id
        if generation_prompt is not None:
            body["generation_prompt"] = generation_prompt
        if seed is not None:
            body["seed"] = seed
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": body,
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 64 * 1024,
        }
        if workspace_parameters:
            request_kwargs["parameters"] = workspace_parameters
        return self._request_json(
            f"/api/v1/services/tts/{quote(service_id, safe='')}/preview",
            **request_kwargs,
        )

    def list_voice_collections(
        self,
        *,
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        parameters = {
            key: value
            for key, value in {
                "workspace_id": workspace_id,
                "session_id": session_id,
            }.items()
            if value is not None
        }
        if not parameters:
            return self._request_json("/api/v1/voice-collections")
        return self._request_json(
            "/api/v1/voice-collections", parameters=parameters
        )

    def create_voice_collection(
        self,
        *,
        name: str,
        description: str | None,
        idempotency_key: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        request_kwargs: dict[str, Any] = {
            "method": "POST",
            "body": body,
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 32 * 1024,
        }
        if workspace_id is not None:
            request_kwargs["parameters"] = {"workspace_id": workspace_id}
        return self._request_json("/api/v1/voice-collections", **request_kwargs)

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
        workspace_id: str | None = None,
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
        request_kwargs: dict[str, Any] = {
            "method": "PATCH",
            "body": body,
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 64 * 1024,
        }
        if workspace_id is not None:
            request_kwargs["parameters"] = {"workspace_id": workspace_id}
        return self._request_json(
            f"/api/v1/voice-collections/{quote(collection_id, safe='')}",
            **request_kwargs,
        )

    def update_catalog_voice_metadata(
        self,
        *,
        reference: dict[str, Any],
        expected_revision: int,
        changes: dict[str, Any],
        idempotency_key: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "method": "PATCH",
            "body": {
                "reference": reference,
                "expected_revision": expected_revision,
                "changes": changes,
            },
            "idempotency_key": idempotency_key,
            "maximum_body_bytes": 64 * 1024,
        }
        if workspace_id is not None:
            request_kwargs["parameters"] = {"workspace_id": workspace_id}
        return self._request_json("/api/v1/voice-catalog/metadata", **request_kwargs)

    def tts_catalog(
        self,
        *,
        refresh: bool = False,
        service_id: str | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        view: str = "summary",
    ) -> dict[str, Any]:
        if refresh:
            raise PandratorMcpError(
                "validation_error",
                "The TTS catalogue read is passive; use "
                "pandrator_refresh_tts_catalog to refresh TTS connections.",
            )
        parameters: dict[str, Any] = {}
        if service_id:
            parameters["service_id"] = service_id
        if workspace_id is not None:
            parameters["workspace_id"] = workspace_id
        if session_id is not None:
            parameters["session_id"] = session_id
        if model is not None and service_id:
            parameters.pop("service_id", None)
            parameters["model"] = model
            payload = self._request_json(
                f"/api/v1/services/tts/{quote(service_id, safe='')}",
                parameters=parameters,
                maximum_body_bytes=8 * 1024 * 1024,
            )
            return {
                **{key: value for key, value in payload.items() if key != "service"},
                "services": [payload["service"]],
            }
        parameters["view"] = view
        if view == "full":
            return self._request_json(
                "/api/v1/services/tts", parameters=parameters,
                maximum_body_bytes=8 * 1024 * 1024,
            )
        return self._request_json("/api/v1/services/tts", parameters=parameters)

    def refresh_tts_catalog(
        self,
        *,
        workspace_id: str | None = None,
        service_id: str | None = None,
    ) -> dict[str, Any]:
        parameters = {
            key: value
            for key, value in {
                "workspace_id": workspace_id,
                "service_id": service_id,
            }.items()
            if value is not None
        }
        parameters["view"] = "summary"
        return self._request_json(
            "/api/v1/services/tts/refresh",
            method="POST",
            parameters=parameters or None,
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

    def list_providers(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        if workspace_id is None:
            return self._request_json("/api/v1/providers")
        return self._request_json(
            "/api/v1/providers", parameters={"workspace_id": workspace_id}
        )

    def list_voices(
        self,
        *,
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        parameters = {
            key: value
            for key, value in {
                "workspace_id": workspace_id,
                "session_id": session_id,
            }.items()
            if value is not None
        }
        if not parameters:
            return self._request_json("/api/v1/voices")
        return self._request_json("/api/v1/voices", parameters=parameters)
