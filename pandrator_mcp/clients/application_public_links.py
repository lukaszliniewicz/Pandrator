"""Validated fixed-route requests for owner-managed public project links."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from ..schemas.public_links import PUBLIC_LINK_ACTION_INPUT_MODELS
from .application_requests import ApplicationRequests

_MAX_REQUEST_BYTES = 8 * 1024
_ROUTES = {
    "get_public_sharing_settings": ("GET", "/api/v1/admin/public-sharing"),
    "configure_public_sharing": ("PUT", "/api/v1/admin/public-sharing"),
    "list_project_links": ("GET", "/api/v1/projects/{project_id}/links"),
    "create_project_link": ("POST", "/api/v1/projects/{project_id}/links"),
    "revoke_project_link": ("POST", "/api/v1/project-links/{link_id}/revoke"),
    "list_link_submissions": (
        "GET",
        "/api/v1/projects/{project_id}/link-submissions",
    ),
    "get_link_submission": ("GET", "/api/v1/link-submissions/{submission_id}"),
    "review_link_submission": (
        "POST",
        "/api/v1/link-submissions/{submission_id}/{action}",
    ),
}


def _request_body(body: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Public-link request values must be finite JSON.") from error
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise ValueError("Public-link request bodies may not exceed 8 KiB.")
    return body


class ApplicationPublicLinkMethods(ApplicationRequests):
    """Bounded request adapter for fixed public-link administration routes."""

    def public_link_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if type(action) is not str or action not in _ROUTES:
            raise ValueError("Unknown public-link action.")
        if not isinstance(arguments, dict):
            raise ValueError("Public-link request arguments must be an object.")

        model = PUBLIC_LINK_ACTION_INPUT_MODELS[action]
        parsed = model.model_validate(arguments)
        values = parsed.model_dump(mode="json")
        method, route = _ROUTES[action]
        for field in re.findall(r"\{([^}]+)\}", route):
            route = route.replace("{" + field + "}", quote(str(values.pop(field)), safe=""))

        is_read = method == "GET"
        parameters: dict[str, Any] | None = None
        body: dict[str, Any] | None = None
        idempotency_key: str | None = None

        if action == "list_link_submissions":
            parameters = {name: values[name] for name in ("status", "offset", "limit")}
        elif action == "get_link_submission":
            parameters = {name: values[name] for name in ("offset", "limit")}
        elif action == "configure_public_sharing":
            body = values
        elif action == "create_project_link":
            body = values
        elif action == "revoke_project_link":
            body = {"expected_revision": values["expected_revision"]}
        elif action == "review_link_submission":
            idempotency_key = values.pop("idempotency_key")
            body = {"expected_revision": values["expected_revision"]}
            for name in (
                "expected_source_hash",
                "expected_document_revision",
                "expected_selected_artifact_id",
            ):
                if values[name] is not None:
                    body[name] = values[name]

        if body is not None:
            body = _request_body(body)

        return self._request_json(
            route,
            method=method,
            read_only_operation=is_read,
            parameters=parameters,
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=_MAX_REQUEST_BYTES,
        )


__all__ = ["ApplicationPublicLinkMethods"]
