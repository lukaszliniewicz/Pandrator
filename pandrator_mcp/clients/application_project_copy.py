"""Validated fixed-route requests for current-project copy operations."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..project_copy_actions import PROJECT_COPY_OPERATION_BY_ACTION
from ..schemas.project_copy import PROJECT_COPY_INPUT_MODELS
from .application_requests import ApplicationRequests

_INPUT_MODELS_BY_NAME = {model.__name__: model for model in PROJECT_COPY_INPUT_MODELS}


class ApplicationProjectCopyMethods(ApplicationRequests):
    def project_copy_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        operation = PROJECT_COPY_OPERATION_BY_ACTION.get(action) if type(action) is str else None
        if operation is None:
            raise ValueError("Unknown project copy action.")

        model = _INPUT_MODELS_BY_NAME.get(operation.model_name)
        if model is None:
            raise RuntimeError("Project copy operation has no registered strict input model.")
        values = model.model_validate(arguments).model_dump(mode="json")
        route = operation.path
        for field in re.findall(r"\{([^}]+)\}", route):
            route = route.replace("{" + field + "}", quote(str(values.pop(field)), safe=""))

        body: dict[str, Any] | None = None
        idempotency_key = None
        if operation.action == "preview_project_copy":
            body = {"destination_workspace_id": values["destination_workspace_id"]}
        elif operation.action == "copy_current_project":
            body = {
                "destination_workspace_id": values["destination_workspace_id"],
                "expected_fingerprint": values["expected_fingerprint"],
                "name": values["name"],
            }
            idempotency_key = values["idempotency_key"]
        if body is not None and values.get("destination_connections"):
            body["destination_connections"] = values["destination_connections"]

        return self._request_json(
            route,
            method=operation.method,
            read_only_operation=operation.risk == "read",
            parameters=None,
            body=body,
            idempotency_key=idempotency_key,
            maximum_body_bytes=128 * 1024 if body is not None else 8 * 1024,
        )


__all__ = ["ApplicationProjectCopyMethods"]
