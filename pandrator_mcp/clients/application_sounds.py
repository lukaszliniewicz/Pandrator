"""Application HTTP bindings for the reusable-sound domain."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import quote

from .application_project_sounds import ApplicationProjectSoundMethods
from .application_sound_validation import (
    _AUDIO_EXTENSIONS,
    MAX_AUDIO_IMPORT_BYTES,
)
from .application_sound_validation import (
    bounded_page as _bounded_page,
)
from .application_sound_validation import (
    idempotency_key as _idempotency_key,
)
from .application_sound_validation import (
    identifier as _identifier,
)
from .application_sound_validation import (
    revision as _revision,
)

_SOUND_UPDATE_FIELDS = frozenset({"name", "tags", "archived", "preferred_variant_id"})


class ApplicationSoundMethods(ApplicationProjectSoundMethods):
    """Bounded fixed-route client methods for sounds and project sound plans."""

    def sound_catalogue(self) -> dict[str, Any]:
        return self._request_json("/api/v1/sounds/catalogue")

    def generate_sound(
        self,
        *,
        prompt: str,
        idempotency_key: str,
        model_id: str = "stable_audio_3_small_sfx_q8_0",
        candidate_count: int = 1,
        duration_seconds: float | None = None,
        kind: Literal["sound", "music"] | None = None,
        backend: Literal["cpu", "vulkan", "cuda"] | None = None,
        lyrics: str | None = None,
        parameters: dict[str, Any] | None = None,
        seed: int | None = None,
        threads: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or len(prompt) > 8000:
            raise ValueError("Sound prompts must be strings of at most 8000 characters.")
        if not isinstance(model_id, str) or not model_id or len(model_id) > 255:
            raise ValueError("model_id must be a non-empty string of at most 255 characters.")
        if type(candidate_count) is not int or not 1 <= candidate_count <= 8:
            raise ValueError("candidate_count must be an integer from 1 to 8.")
        if duration_seconds is not None:
            if (
                isinstance(duration_seconds, bool)
                or not isinstance(duration_seconds, (int, float))
                or not 1 <= duration_seconds <= 600
                or not math.isfinite(float(duration_seconds))
            ):
                raise ValueError("duration_seconds must be a finite number from 1 to 600.")
        if kind is not None and (not isinstance(kind, str) or kind not in {"sound", "music"}):
            raise ValueError("kind must be sound or music.")
        if backend is not None and (
            not isinstance(backend, str) or backend not in {"cpu", "vulkan", "cuda"}
        ):
            raise ValueError("backend must be cpu, vulkan, or cuda.")
        if lyrics is not None and (not isinstance(lyrics, str) or len(lyrics) > 16000):
            raise ValueError("lyrics must be a string of at most 16000 characters.")
        if parameters is not None:
            if type(parameters) is not dict or len(parameters) > 32:
                raise ValueError("parameters must be a dictionary of at most 32 entries.")
            for key, value in parameters.items():
                if not isinstance(key, str) or len(key) > 64:
                    raise ValueError("parameter names must be strings of at most 64 characters.")
                if isinstance(value, str):
                    if len(value) > 2000:
                        raise ValueError("parameter strings may contain at most 2000 characters.")
                elif type(value) is float:
                    if not math.isfinite(value):
                        raise ValueError("parameter numbers must be finite.")
                elif type(value) not in (bool, int):
                    raise ValueError(
                        "parameter values must be strings, booleans, integers, or numbers."
                    )
        if seed is not None and (
            isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1
        ):
            raise ValueError("seed must be an unsigned 32-bit integer.")
        if threads is not None and (
            isinstance(threads, bool) or not isinstance(threads, int) or threads < 1
        ):
            raise ValueError("threads must be a positive integer.")
        body: dict[str, Any] = {
            "prompt": prompt,
            "model_id": model_id,
        }
        if candidate_count != 1:
            body["candidate_count"] = candidate_count
        if duration_seconds is not None:
            body["duration_seconds"] = duration_seconds
        if kind is not None:
            body["kind"] = kind
        if backend is not None:
            body["backend"] = backend
        if lyrics is not None:
            body["lyrics"] = lyrics
        if parameters is not None:
            body["parameters"] = dict(parameters)
        if seed is not None:
            body["seed"] = seed
        if threads is not None:
            body["threads"] = threads
        return self._request_json(
            "/api/v1/sounds/generate",
            method="POST",
            body=body,
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=512 * 1024,
        )

    def list_sounds(
        self,
        *,
        query: str = "",
        tag: str | None = None,
        cursor: str | None = None,
        limit: int = 40,
        archived: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be a string of at most 200 characters.")
        if tag is not None and (not isinstance(tag, str) or len(tag) > 64):
            raise ValueError("tag must be a string of at most 64 characters.")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
            raise ValueError("cursor must be a string of at most 1024 characters.")
        if type(archived) is not bool:
            raise ValueError("archived must be a boolean.")
        parameters: dict[str, Any] = {
            "query": query,
            "limit": _bounded_page(limit),
            "archived": "true" if archived else "false",
        }
        if tag is not None:
            parameters["tag"] = tag
        if cursor is not None:
            parameters["cursor"] = cursor
        return self._request_json("/api/v1/sounds", parameters=parameters)

    def get_sound(
        self,
        item_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"limit": _bounded_page(limit)}
        if cursor is not None:
            parameters["cursor"] = cursor
        return self._request_json(
            f"/api/v1/sounds/{quote(_identifier(item_id, 'item_id'), safe='')}",
            parameters=parameters,
        )

    def list_sound_candidates(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"limit": _bounded_page(limit)}
        if cursor is not None:
            parameters["cursor"] = cursor
        return self._request_json("/api/v1/sounds/candidates", parameters=parameters)

    def get_sound_candidate(self, candidate_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sounds/candidates/{quote(_identifier(candidate_id, 'candidate_id'), safe='')}"
        )

    def save_sound(
        self,
        *,
        candidate_id: str,
        idempotency_key: str,
        name: str | None = None,
        tags: list[str] | None = None,
        item_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"candidate_id": _identifier(candidate_id, "candidate_id")}
        if item_id is not None:
            if name is not None or tags is not None:
                raise ValueError("Attaching a candidate cannot change an item's name or tags.")
            if expected_revision is None:
                raise ValueError("expected_revision is required when attaching to an item.")
            body["item_id"] = _identifier(item_id, "item_id")
            body["expected_revision"] = _revision(expected_revision, minimum=1)
        else:
            if expected_revision is not None:
                raise ValueError("expected_revision is used only when attaching to an item.")
            if not isinstance(name, str) or not name.strip() or len(name) > 255:
                raise ValueError(
                    "Saving a new sound requires a non-blank name of at most 255 characters."
                )
            body["name"] = name
            if tags is not None:
                body["tags"] = self._validate_tags(tags)
        return self._request_json(
            "/api/v1/sounds/save",
            method="POST",
            body=body,
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=16 * 1024,
        )

    def update_sound(
        self,
        item_id: str,
        *,
        expected_revision: int,
        changes: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(changes, Mapping) or not changes:
            raise ValueError("Sound updates require at least one change.")
        if set(changes) - _SOUND_UPDATE_FIELDS:
            raise ValueError("Sound updates contain unsupported fields.")
        body: dict[str, Any] = {"expected_revision": _revision(expected_revision, minimum=1)}
        for field, value in changes.items():
            if value is None:
                raise ValueError(f"{field} must be non-null when provided.")
            if field == "name" and (
                not isinstance(value, str) or not value.strip() or len(value) > 255
            ):
                raise ValueError("Sound names must be non-blank strings of at most 255 characters.")
            if field == "tags":
                value = self._validate_tags(value)
            if field == "archived" and type(value) is not bool:
                raise ValueError("archived must be a boolean.")
            if field == "preferred_variant_id":
                if not isinstance(value, str):
                    raise ValueError("preferred_variant_id must be a string.")
                value = _identifier(value, field)
            body[field] = value
        return self._request_json(
            f"/api/v1/sounds/{quote(_identifier(item_id, 'item_id'), safe='')}",
            method="PATCH",
            body=body,
            idempotency_key=_idempotency_key(idempotency_key),
            maximum_body_bytes=16 * 1024,
        )

    def discard_sound_candidate(
        self,
        candidate_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sounds/candidates/{quote(_identifier(candidate_id, 'candidate_id'), safe='')}",
            method="DELETE",
            idempotency_key=_idempotency_key(idempotency_key),
        )

    def import_audio_reference(
        self,
        *,
        target: str,
        filename: str,
        body: bytes,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if target == "sound":
            path = "/api/v1/sounds/import"
        elif target == "voice_reference":
            path = "/api/v1/voices/reference-audio/import"
        else:
            raise ValueError("Audio import target must be sound or voice_reference.")
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 255
            or any(character in filename for character in "/\\\r\n\x00")
        ):
            raise ValueError(
                "filename must be an audio basename without paths or control characters."
            )
        extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
        suffix = f".{extension}" if extension else ""
        if suffix not in _AUDIO_EXTENSIONS:
            raise ValueError("Audio imports support WAV, MP3, FLAC, OGG, and M4A files.")
        if not isinstance(body, bytes) or not body:
            raise ValueError("Audio imports require a non-empty binary body.")
        if len(body) > MAX_AUDIO_IMPORT_BYTES:
            raise ValueError("MCP audio imports may not exceed 16 MiB.")
        return self._request_binary_json(
            path,
            method="PUT",
            body=body,
            content_type="application/octet-stream",
            extra_headers={
                "X-Audio-Filename": quote(filename, safe=""),
                "Idempotency-Key": _idempotency_key(idempotency_key),
            },
        )

    @staticmethod
    def _validate_tags(value: Any) -> list[str]:
        if not isinstance(value, list) or len(value) > 20:
            raise ValueError("tags must be a list of at most 20 strings.")
        if any(not isinstance(tag, str) or len(tag) > 64 for tag in value):
            raise ValueError("Each tag must be a string of at most 64 characters.")
        return value


__all__ = ["MAX_AUDIO_IMPORT_BYTES", "ApplicationSoundMethods"]
