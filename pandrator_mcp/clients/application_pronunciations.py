"""Fixed-route application requests for pronunciation dictionary entries."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests

_LIST_FIELDS = frozenset({"query", "language", "status", "scope", "session_id", "limit"})
_CREATE_FIELDS = frozenset(
    {
        "source_form",
        "phonetic",
        "language",
        "backend",
        "scope",
        "session_id",
        "status",
        "alphabet",
        "notes",
        "metadata",
    }
)
_UPDATE_FIELDS = _CREATE_FIELDS
_NULLABLE_UPDATE_FIELDS = frozenset({"session_id", "notes", "metadata"})


def _json_value(value: Any) -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Pronunciation metadata must contain finite JSON values.")
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("Pronunciation metadata object keys must be strings.")
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("Pronunciation metadata must contain finite JSON values.")


def _metadata(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("Pronunciation metadata must be a JSON object.")
    _json_value(value)
    try:
        size = len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Pronunciation metadata must contain finite JSON values.") from error
    if size > 16 * 1024:
        raise ValueError("Pronunciation metadata may not exceed 16 KiB.")


def _encode_body(body: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Pronunciation request values must be finite JSON.") from error
    if len(encoded) > 32 * 1024:
        raise ValueError("Pronunciation request bodies may not exceed 32 KiB.")
    return encoded


def _arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("Pronunciation request arguments must be an object.")
    values = dict(arguments)
    fields = {
        "list": _LIST_FIELDS,
        "create": _CREATE_FIELDS,
        "update": frozenset({"entry_id", "expected_revision", "changes"}),
        "delete": frozenset({"entry_id", "expected_revision"}),
    }.get(action)
    if fields is None:
        raise ValueError("Unknown pronunciation action.")
    if set(values) - fields:
        raise ValueError("Pronunciation requests contain unsupported fields.")
    return values


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Pronunciation requests require a positive expected_revision.")
    return value


def _entry_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Pronunciation requests require an entry_id.")
    return value


def _validate_create(values: dict[str, Any]) -> dict[str, Any]:
    if not {"source_form", "phonetic"}.issubset(values):
        raise ValueError("Creating a pronunciation requires source_form and phonetic.")
    scope = values.get("scope", "global")
    session_id = values.get("session_id")
    if scope == "session" and (not isinstance(session_id, str) or not session_id.strip()):
        raise ValueError("Session-scoped pronunciations require a session_id.")
    if scope == "global" and session_id is not None:
        raise ValueError("Global pronunciations cannot include a session_id.")
    if "metadata" in values:
        _metadata(values["metadata"])
    _encode_body(values)
    return values


def _validate_changes(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("Pronunciation updates require at least one change.")
    changes = dict(value)
    if set(changes) - _UPDATE_FIELDS:
        raise ValueError("Pronunciation updates contain unsupported fields.")
    if any(changes[field] is None for field in set(changes) - _NULLABLE_UPDATE_FIELDS):
        raise ValueError("Pronunciation update fields cannot be null.")
    if "scope" in changes:
        if (
            changes["scope"] == "session"
            and "session_id" in changes
            and (not isinstance(changes["session_id"], str) or not changes["session_id"].strip())
        ):
            raise ValueError("Session-scoped pronunciations require a session_id.")
        if changes["scope"] == "global" and changes.get("session_id") is not None:
            raise ValueError("Global pronunciations cannot include a session_id.")
    if changes.get("metadata") is not None:
        _metadata(changes["metadata"])
    _encode_body(changes)
    return changes


class ApplicationPronunciationMethods(ApplicationRequests):
    """Bounded request adapter for the fixed pronunciation dictionary API."""

    def pronunciation_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        values = _arguments(action, arguments)

        if action == "list":
            limit = values.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ValueError("Pronunciation list limit must be between 1 and 100.")
            parameters: dict[str, Any] = {"limit": limit}
            for field in ("query", "language", "status", "scope", "session_id"):
                value = values.get(field)
                if value is None or value == "":
                    continue
                if not isinstance(value, str):
                    raise ValueError(f"Pronunciation filter {field} must be a string.")
                if field == "status" and value not in {"proposed", "reviewed", "disabled"}:
                    raise ValueError("Unknown pronunciation status filter.")
                if field == "scope" and value not in {"global", "session"}:
                    raise ValueError("Unknown pronunciation scope filter.")
                parameters["q" if field == "query" else field] = value
            return self._request_json("/api/v1/pronunciations", parameters=parameters)

        if action == "create":
            body = _validate_create(values)
            return self._request_json(
                "/api/v1/pronunciations",
                method="POST",
                body=body,
                maximum_body_bytes=32 * 1024,
            )

        if action == "update":
            entry_id = quote(_entry_id(values.get("entry_id")), safe="")
            expected_revision = _revision(values.get("expected_revision"))
            changes = _validate_changes(values.get("changes"))
            return self._request_json(
                f"/api/v1/pronunciations/{entry_id}",
                method="PATCH",
                body=changes,
                if_match_revision=expected_revision,
                maximum_body_bytes=32 * 1024,
            )

        entry_id = quote(_entry_id(values.get("entry_id")), safe="")
        expected_revision = _revision(values.get("expected_revision"))
        return self._request_json(
            f"/api/v1/pronunciations/{entry_id}",
            method="DELETE",
            if_match_revision=expected_revision,
        )


__all__ = ["ApplicationPronunciationMethods"]
